"""session：测试会话生命周期。开会话后，observe/act/read 自动把每次调用
作为事件写入当前会话；AI 只需在断言时记 finding、结束时 finish。

用法：
  python session.py start --title "日历新建课程表" --input "用户口述的用例原文..."
  python session.py finding --status PASS --event-id <ID> --expect "出现新建成功toast" --actual "OCR读到'创建成功'"
  python session.py finish --status PASS --summary "3/4 PASS，1 BLOCKED(无图库权限)"
  python session.py list
  python session.py current
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from common import (emit, find_case_file_by_ref, knowledge_hint,
                    resolve_case_stem, steps_hint)
import sqlite3
from db import (  # noqa: I001
    INTERIM, STATUSES, SessionDB, clear_current, current_session, get_db,
    set_current,
)
from runtime import (
    CASES_DIR, STORAGE_DIR, current_record_dir,
    detect_shell, last_record_dir, new_record_dir, pin_record,
    require_writable_storage, unpin_record,
)

# 只有这几条不写盘。其余一律进门先实测 storage 可写性 ——
# 否则报错点会漂到后面某个不相干的调用上，根本看不出是 storage 的问题。
_READ_ONLY_CMDS = frozenset({"list", "current"})

# Windows 控制台默认 GBK，中文/特殊符号会炸 → 全部 CLI 输出强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def maybe_file(v: str) -> str:
    """`@路径` → 读 UTF-8 文件内容作为值。

    中文长文本直接放命令行参数，在部分终端/管道里会被 GBK 双重解码
    （旧 skill 的 GBK 事故同族）；用例口述这种长文本一律建议走 @文件。
    """
    if v and v.startswith("@"):
        return Path(v[1:]).read_text(encoding="utf-8")
    return v


# 「弹窗标题」类 rid：出现即说明某个弹层打开了，是最可靠的页内落点。
#
# 2026-10-28 实测为什么需要它：
#   开「上课时间」弹窗后，sig 新增里有 content/parentPanel/customPanel/...
#   还有 button1（确定）。按字母序取第一个 rid 会拿到 `rid=button1` ——
#   而**几乎每个弹窗都有 button1**。下次执行时只要屏幕上还有任意 button1，
#   校验就误判成"弹窗开了"，然后点下一步全错。
#
#   同一轮里三个弹窗的独特标题 rid 分别是不 npTitle / dialog_title / alertTitle，
#   所以按优先级挑，挑到哪个都能唯一指认"哪个弹窗开了"。
_MODAL_TITLE_RIDS = (
    "npTitle", "dialog_title", "alertTitle", "title_template",
    "curriculum_guide_title",
)

# 「通用节点」rid：几个弹窗/页面都有的容器和按钮。
#
# 它们**不能当锚点**（点了不知道点的是哪个控件），
# 也**不能当落点**（屏幕上有它不代表到了目标页面）。
#
# 2026-10-29 实测教训：这轮导出的 17 步里有
#   via: rid=android:id/button1   （确定）
#   via: rid=android:id/button2   （取消）
#   expect: rid=contentPanel / rid=action_bar_root
# 全是通用节点 —— 下次执行时第一步就会误判。
_GENERIC_RIDS = {
    "content", "parentPanel", "customPanel", "custom", "divider",
    "buttonPanel", "button_divider", "button1", "button2", "button3",
    "empty", "topPanel", "action_bar_root", "contentPanel", "custom",
    "alertTitle", "title_template", "android:id/button1",
    "android:id/button2", "android:id/button3", "android:id/content",
}


_OPEN_CLOSE = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】"}
# 括号里出现这些 = 坐标/位置描述，整条不可执行。
# `\d,\d` 是裸坐标：AI 常直接写 `text=关闭(193,718)` / `rid=xx (952,732)`，
# 前面不带 x=/y= —— 这种换个分辨率就废，必须拒。
_BAD_IN_BRACKET = re.compile(
    r"[xXyY]\s*=\s*-?\d|第\s*\d+\s*[节格行列]|[×x]\s*\d+|\d\s*,\s*\d")


def _strip_brackets(via: str) -> str | None:
    """去掉 via 里的括号注释；括号里是坐标/位置描述则返回 None（整条不可执行）。

    ★ 为什么是「先看括号里是什么」而不是「有括号就拒」（2026-11-17 改）：

    AI 很爱在锚点后面加一句人类注释 —— `rid=themeLayout(text=主题)`、
    `text=设置(hotseat)`。括号里是**正常的文字补充**，锚点本身完全可执行。
    原来一刀切全拒，实测全库 5940 次 tap 里有 1591 次（26%）因此进不了 yaml，
    是所有过滤器里最大的一块。后果很具体：`长按图标_117` 的 start_from 从
    「桌面」退到了「主题预览页」——「桌面 → 设置 → 主题」三步全被丢掉，
    剩下的两步都是页内动作，下次照它走第一步就得自己先摸到主题预览页。

    真正该拒的只有括号里写坐标/位置的那种：
        `弹框外空白（x=300,y=1700）`、`日列选中行上一格 (-1)×22`
    这类换个机型就废，且多半要 AI 现场判断。
    """
    out: list[str] = []
    i = 0
    while i < len(via):
        close = _OPEN_CLOSE.get(via[i])
        if close is None:
            out.append(via[i])
            i += 1
            continue
        j = via.find(close, i + 1)
        if j < 0:
            return None                  # 括号不配对，说不清是什么
        if _BAD_IN_BRACKET.search(via[i + 1:j]):
            return None
        i = j + 1
    return "".join(out)


def _anchors_of(via: str) -> list[str] | None:
    """把 via 拆成可执行的锚点列表；不可执行返回 None。

    可执行 = 纯 `key=value`，多个用逗号/空格分隔，每个值都非空。

    ⚠️ 三类必须拒掉（都是实测踩过的）：
      ① **坐标/位置混入**：`rid=cv_empty_content 第1节 周一格`
         —— rid 有 14 个同名的格子，光凭 rid 不知道点哪个。
         括号里的坐标（`弹框外空白（x=300,y=1700）`）同样拒，见 `_strip_brackets`。
      ② **通用 rid**：`rid=android:id/button1`
         —— 每个弹窗都有，点它等于瞎点。
      ③ **空值 / 非 kv**：`rid=`、`返回`
    """
    if not via:
        return None
    via = _strip_brackets(via)
    if not via:
        return None
    parts = [p for p in re.split(r"[,\s]+", via.strip()) if p]
    if not parts:
        return None
    for p in parts:
        if "=" not in p:
            return None                      # 有人话
        key, _, val = p.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if not key or not val:
            return None                      # `x=` 空值
        if key in ("t", "c"):
            return None                      # 文本/坐标锚点，不稳
        # 值里还有非 ASCII 且不是 rid/desc 常见字符 → 多半混了中文描述
        if key in ("rid", "desc", "id") and any(
                "\u4e00" <= ch <= "\u9fff" for ch in val):
            return None
        # ③ 通用 rid
        if key == "rid" and val in _GENERIC_RIDS:
            return None
        if key == "rid" and val.split("/")[-1] in _GENERIC_RIDS:
            return None
    return parts


def _pick_landing(new_nodes: set[str]) -> str | None:
    """从「点击后新增的节点」里挑一个最可靠的当落点。

    优先级：
      ① 弹窗标题类 rid（npTitle / dialog_title / alertTitle ...）—— 唯一指认某个弹层
      ② 普通 rid（排除 _GENERIC_RIDS 那批通用容器）
      ③ desc
      （text 一律不选：改个文案就废）

    返回形如 `rid=npTitle`，挑不出就 None。
    """
    rids = [str(n) for n in new_nodes if str(n).startswith("rid=")]
    # ① 弹窗标题
    for want in _MODAL_TITLE_RIDS:
        for r in rids:
            if r[4:] == want:
                return r
    # ② 普通 rid：排除"几乎每个页面都有"的通用容器
    specific = sorted(
        r for r in rids
        if r[4:] not in _GENERIC_RIDS
        and r[4:].split("/")[-1] not in _GENERIC_RIDS)
    if specific:
        return specific[0]
    # ③ desc
    descs = sorted(str(n) for n in new_nodes if str(n).startswith("desc="))
    return descs[0] if descs else None


def _case_stem_of(row: dict) -> str | None:
    """一条会话记录对应的用例 md 文件名；对不上返回 None。"""
    return resolve_case_stem(row.get("package") or "", row.get("title"),
                             row.get("user_input"))


def export_steps(db, session_id: int) -> str | None:
    """把本轮「能复用的跳转步骤」导成 `<md文件名>_steps.yaml`，返回写到的路径。

    ## 存什么

    只存**用例步骤**里那些「锚点明确 + 落点可验」的动作：

        steps:
          - n: 1
            via: rid=btnCreateManually
            why: 用例步骤2：点击手动创建课程表
            expect: activity=.timetable.management.EditTimetableActivity

    ## 落点怎么来

    优先用 AI 写的 `expect`；**没有就退而用点击后的前台 activity**。

    实测（2026-10-28）：AI 大多数点击都不写 `--expect`，
    176 那轮 25 个 act 里 expect 全空。但每个 act 事件都带 `activity`
    （点击后的前台页面）—— 用它当落点，可用率从 0% 涨到 60%+。

    **只收「activity 变了」的**：没变说明是本页内动作（开/关弹窗），
    不是跳转，自动点的意义不大。

    ## 不存什么

    1. **前置准备** —— `why` 里含「前置 / 准备」字样的。
       同一份用例每次前置可能完全不同（这轮要删课表、下轮不用）。
    2. **带坐标的 via** —— 换机型就废，且这种多半要 AI 现场判断。
    3. **非 tap**（input / swipe 需要额外参数）。
    4. **没有可用落点的**（既没 expect 也没 activity 变化）。

    ## 为什么只存「跳转」不存判定

    跳转是纯机械的（点 A 到 B），代码能做；判定要理解用例，必须 AI 做。
    """
    row = db.get_session(session_id) or {}
    pkg = (row.get("package") or "").strip()
    if not pkg:
        return None

    rows = db.list_events(session_id) or []
    steps: list[dict] = []

    # 起始页面：取第一条带 activity 的事件（通常是开头那次 observe）。
    # 不用 None 起手 —— 否则第一个 act 的 after 和 None 一比必定"变了"，
    # 会把「本页内动作」误判成跳转（实测 rid=iv_more 就这么被误收过）。
    prev_activity: str | None = None
    prev_sig: set[str] = set()
    for ev in rows:
        try:
            d0 = json.loads(ev.get("data_json") or "{}")
        except ValueError:
            continue
        a0 = str(d0.get("activity") or "").strip()
        if a0:
            prev_activity = a0
            prev_sig = set(d0.get("sig") or [])
            break

    for ev in rows:
        if ev.get("kind") != "act":
            # 非 act（observe/read）也要更新基线，否则差分跨了中间那步会失真
            try:
                dn = json.loads(ev.get("data_json") or "{}")
            except ValueError:
                continue
            an = str(dn.get("activity") or "").strip()
            if an:
                prev_activity = an
            sn = set(dn.get("sig") or [])
            if sn:
                prev_sig = sn
            continue
        try:
            data = json.loads(ev.get("data_json") or "{}")
        except ValueError:
            continue
        act = data.get("action") or {}
        via = str(act.get("via") or "").strip()
        expect = str(act.get("expect") or "").strip()
        why = str(act.get("why") or "").strip()
        after = str(data.get("activity") or "").strip()
        after_sig = set(data.get("sig") or [])

        # ① 前置不要（用例原文里没写、AI 自己补的准备工作）
        if any(k in why for k in ("前置", "准备", "prep")):
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        # ② 只收 tap
        if act.get("type") != "tap":
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        # ③ 锚点必须可执行（纯 key=value、无坐标/人话、非通用 rid）
        anchor_parts = _anchors_of(via)
        if not anchor_parts:
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        clean_via = ",".join(anchor_parts)

        # ⑤ 落点，三级优先：
        #    ① AI 写的 expect
        #    ② activity 变了 → activity=xxx（真跳转）
        #    ③ activity 没变但 sig 有新增 → 用新增节点当落点（页内动作）
        #
        #    第 ③ 条是 2026-10-28 补的。实测缺失它会导致漏掉必需的一步：
        #      rid=iv_more（打开「更多」菜单）不跳页，activity 不变，
        #      于是被当成"页内无效动作"丢掉 —— 而下一步「选课程表」
        #      恰恰依赖菜单已经打开。丢了它，下次执行第一步就点不到。
        #
        #    sig 是「点击后屏幕上可见的节点集合」（已去重）。
        #    和上一事件的 sig 做差，就知道这一步让什么新东西出现了。
        landing = expect
        in_page = False
        if not landing:
            if after and after != prev_activity:
                landing = f"activity={after}"
            else:
                new_nodes = after_sig - prev_sig
                landing = _pick_landing(new_nodes)
                if landing:
                    in_page = True

        # ★ from_activity 必须是「动作之前」的页面，所以在更新 prev_* 之前取。
        #
        #   2026-10-29 实测踩到：更新语句写在 item 构造之前，
        #   于是 from_activity 记成了「点完之后在哪」。导出结果自相矛盾：
        #
        #     start_from: activity=.timetable.display.TimetableActivity
        #     - n: 1  via: text=课程表
        #       expect: activity=.timetable.display.TimetableActivity
        #     （从课程表页出发，点「课程表」，又到课程表页）
        #
        #   之前没暴露是因为第一步往往是不跳页的页内动作（如 iv_more
        #   开菜单），前后 activity 相同，取错也看不出来。
        from_activity = prev_activity
        prev_activity = after or prev_activity
        prev_sig = after_sig or prev_sig

        if not landing:
            continue

        item = {"n": 0, "why": why[:60], "via": clean_via, "expect": landing,
                "from_activity": from_activity}
        if in_page:
            item["in_page"] = True
        steps.append(item)

    if not steps:
        return None

    # ★ 去重：同一对 (via, expect) 只留**最后**一次。
    #
    #   为什么会有重复：AI 可能为了做前置先走一遍、正式执行再走一遍。
    #   实测（2026-10-28 20:44 那轮）：
    #     seq3  why=步骤1：菜单里选择课程表        ← 其实是为了删课表
    #     seq12 why=步骤1：选择课程表，预期1 ...   ← 这才是正式的
    #   两步的 (via, expect) 完全一样。
    #
    #   保留最后一次：后面的那次是"正式执行"，前面的多半是踩点/前置。
    seen: dict[tuple[str, str], int] = {}
    for i, s in enumerate(steps):
        seen[(s["via"], s["expect"])] = i
    steps = [s for i, s in enumerate(steps)
             if seen[(s["via"], s["expect"])] == i]
    for i, s in enumerate(steps, 1):
        s["n"] = i

    # 起点校验：第一步点击前在哪。
    #
    # ⚠️ 为什么必须记：steps 第 1 步（如 rid=iv_more）只在**日历首页**存在。
    #   如果下次起点在别的页面，这一步要么找不到（好，会停），
    #   要么**别的页面恰好也有同名 rid** → 点错地方 → 后 8 步全错。
    #   所以记下起点 activity，执行前先验。
    start_from = None
    for s in steps:
        fa = (s.get("from_activity") or "").strip()
        if fa:
            start_from = fa
            break

    # 导出文件名 = 用例 md 的文件名 + `_steps.yaml`（与 md 同名，天然唯一）。
    title = str(row.get("title") or "")
    stem = resolve_case_stem(pkg, title, row.get("user_input"))
    if not stem:
        # 解析不出对应用例 md —— 不硬造一个孤儿产物（WebUI 里只会是空白条目）
        return None
    out_dir = CASES_DIR / pkg
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}_steps.yaml"

    # 步数变少时**照样覆盖**，只在返回里告警。
    #
    #   「旧步数多就不覆盖」看着稳妥，实际分不清「旧的坏」和「旧的好」，
    #   一旦写坏就永久锁死：过滤 bug 时期导出过 17 步的脏文件（混了坐标、
    #   通用 rid、验证绕路），后来正确只剩 5 步，5 < 17 被拒，坏文件一直留着，
    #   每轮 start 都把它当上次路径喂给 AI（含改设备数据的步骤）。
    #
    #   坏文件的害处（AI 照错路径走、改坏设备数据）远大于一次覆盖错；
    #   而且文件里带 source_run，随时能追回是哪一轮写的。
    drop_note = ""
    if out.is_file():
        try:
            old = out.read_text(encoding="utf-8")
            om = re.search(r"^step_count:\s*(\d+)", old, re.M)
            old_n = int(om.group(1)) if om else 0
        except (OSError, ValueError):
            old_n = 0
        if old_n > len(steps):
            drop_note = (f"（已覆盖：步数 {old_n} → {len(steps)}，比上次少 "
                         f"{old_n - len(steps)} 步。如果不是路径变短，"
                         f"回看这轮是否没走全）")

    lines = [
        f"# 自动导出：{stem} 的可复用跳转步骤",
        "#",
        "# 只含「锚点明确 + 落点可验 + 非前置」的 tap。",
        "# 前置条件、造数据、判定都不在这里 —— 那些每次都不一样。",
        "#",
        "# 用法：照着点即可。每步点之前验 expect，不命中就停、交回 AI。",
        "# 注意：这是**上一次**的实测路径，不是唯一正确路径。",
        f"case: {stem}",
        f"source_run: {Path(str(db.path)).parent.name}",
        f"last_run: {row.get('started_at') or ''}",
        f"verdict: {row.get('status') or ''}",
        f"step_count: {len(steps)}",
    ]
    lines.append("steps:")
    # 起点：第 1 步之前得在哪个页面。执行前先验它，不然可能点错地方。
    if start_from:
        lines[-1:-1] = [f"start_from: activity={start_from}"]
    for s in steps:
        lines += [
            f"  - n: {s['n']}",
            f"    why: {s['why']}",
            f"    via: {s['via']}",
            f"    expect: {s['expect']}",
        ]
        if s.get("in_page"):
            # 不跳页的动作（开/关弹层、切 tab）。expect 验「新节点出现了没」，
            # 不用等 activity 变化。
            lines.append("    in_page: true")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"assets/cases/{pkg}/{out.name}{drop_note}"




def main() -> None:
    p = argparse.ArgumentParser(description="测试会话生命周期")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--title", default=None)
    s.add_argument("--input", default=None, help="用户口述的用例原文（追溯锚点）")
    s.add_argument("--case", type=str, default=None, dest="case_id",
                   help="引用 assets/cases 下的用例文件，自动带出标题与正文。"
                        "传文件名末尾的编号（如 176）；该编号在本应用内重号时"
                        "传 模块_编号（如 图库导入入口_168）。不要传文件名或路径")
    s.add_argument("--device", default=None)
    s.add_argument("--force", action="store_true",
                   help="即使已有运行中的会话也强行新建（默认拒绝，防孤儿会话）")
    s.add_argument("--diag", action="store_true",
                   help="标记为验证/调试会话：不进「测试记录」库、不计入仪表盘成功率。"
                        "用于工具自检、探针验证等非正式测试的跑动")
    s.add_argument("--package", default=None,
                   help="被测包名（强烈建议显式声明）：崩溃分级的 target 依据。"
                        "不声明则靠首个 observe 回填前台包——但被测 App 崩溃后"
                        "前台会回落到桌面，会把被测包崩溃误判成 other")
    s.add_argument("--related", default="",
                   help="关联包（逗号分隔），其崩溃同样按 BLOCKED 处理；"
                        "权限框宿主默认已含")
    s.add_argument("--no-restart", action="store_true", dest="no_restart",
                   help="跳过冷启动复位。**默认会 force-stop 并冷启动被测包**，"
                        "把起点钉死在 App 首页——这样用例 YAML 的步骤顺序可直接执行，"
                        "不必迁就「上次停在哪」。仅在用例前提要求「App 已处于某状态」"
                        "（如接着上一步继续）时才跳过。注意：只关进程、不清数据。")

    s = sub.add_parser("finding")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--expect", default="")
    s.add_argument("--actual", default="")
    s.add_argument("--note", default="")
    s.add_argument("--event-id", action="append", type=int, default=[],
                   help="关联本会话证据事件；可重复传入多个事件 ID")
    s.add_argument("--id", type=int, default=None,
                   help="记录到指定会话（不给就记到当前会话）")

    # ★ 批量记断言：一次调用写多条
    #
    # 2026-10-28 实测：176 那轮记了 9 次 finding，每次间隔约 6 秒
    # （AI 思考 + 进程冷启动），共 53 秒。合并成一次就没这个开销。
    #
    # 用法：给一个 JSON 文件，内容是数组：
    #   [
    #     {"status": "PASS", "expect": "预期1 ...", "actual": "...",
    #      "event_id": [13]},
    #     {"status": "FAIL", "expect": "预期2 ...", "actual": "...",
    #      "event_id": [15, 16]}
    #   ]
    s = sub.add_parser("findings-batch",
                       help="一次提交多条断言（给 JSON 文件）")
    s.add_argument("--file", required=True,
                   help="JSON 文件：数组，每项 {status, expect, actual, "
                        "event_id:[..], note}")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    s = sub.add_parser("finish")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--summary", default="")
    s.add_argument("--package", default=None)
    # ⚠️ --device 收下但**不参与逻辑**（2026-11-02）。
    #
    #    真实踩过：跑 168 时 AI 按 start 的参数习惯写了
    #    `finish --status PASS --device HA2DMRBK`，argparse 直接报
    #    「unrecognized arguments: --device」，AI 只好去掉重试 ——
    #    多花一次往返。finish 本来就不需要设备（连库都不连设备），
    #    但为了"同一个会话的 start/finish 参数风格一致"，收下忽略更省事。
    s.add_argument("--device", default=None,
                   help="（兼容用，收下忽略）设备 serial；finish 不连设备")
    s.add_argument("--id", type=int, default=None,
                   help="收尾指定会话（不给就收尾当前会话）")

    s = sub.add_parser("resume", help="崩溃终止后人工确认继续（清除终态）")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--note", default="", help="恢复原因，随事件入库")

    s = sub.add_parser("perm-intent",
                       help="声明/查看/清除权限响应意图（act/observe 遇到权限弹窗时按此自动响应）")
    s.add_argument("--action", default=None, choices=["grant", "deny"],
                   help="遇到权限弹窗点【同意】(grant) 还是【拒绝】(deny)")
    s.add_argument("--perm", default="",
                   help="权限类型标记（如 media_images/camera/location），仅用于记录")
    s.add_argument("--clear", action="store_true", help="清除意图（分支测完就清）")
    s.add_argument("--show", action="store_true", help="只查看当前意图")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    sub.add_parser("list")
    sub.add_parser("current")
    # abandon 以前只解绑指针，**不动 sessions.status** —— 于是被放弃的会话
    # 永远停在 'running'：列表里看着还在跑、仪表盘 running_n 一直不归零
    # （2026-11-09 本机踩过：自测误开的 #51 就是这么变成僵尸的）。
    # 现在它写一条终态收尾。为什么用 ERROR 不用 BLOCKED：BLOCKED 在产品口径里
    # 是「用例的前置不成立、没机会验证」，算缺陷线索；而作废会话是「这条根本没跑」，
    # 进 ERROR 桶（db.session_stats 单列 error_n，日报趋势里落 other），
    # 不会污染 PASS/FAIL/BLOCKED 三条统计。summary 前缀「作废：」让记录自解释。
    a = sub.add_parser("abandon",
                       help="作废一个会话：写终态收尾 + 解绑指针（不产出用例结论）")
    a.add_argument("--id", type=int, default=None,
                   help="会话 id；缺省 = 当前指针指向的会话")
    a.add_argument("--reason", default="",
                   help='作废原因（必填，写进 summary，如 --reason "title 写错重开"）')
    a.add_argument("--device", default=None, help="（兼容用，收下忽略）")

    args = p.parse_args()

    if args.cmd not in _READ_ONLY_CMDS:
        require_writable_storage(args.cmd)

    # ══════════════════════════════════════════════════════════════
    # ★ 先做参数校验，**再**建运行目录
    #
    #   顺序反过来会：`session.py start --title X`（漏了 --input）先
    #   建出一个空的 run_history/<uuid>/ 目录、再报错退出，工作目录里
    #   堆一堆空 uuid 目录，看起来像"跑过了"。
    #
    #   现在：不合法就直接退，一个字节都不写。
    # ══════════════════════════════════════════════════════════════
    # start 时先建运行目录，再开库（get_db 读 ANDROID_TEST_RECORD 定库位置）
    if args.cmd == "start" and not current_record_dir():
        _rec = new_record_dir()
        # pin_record 同时做两件事：设环境变量（本进程内）+ 写磁盘指针
        # （供后续 observe/act 这些独立进程读）
        pin_record(_rec)
        print(json.dumps({"record_dir": str(_rec)}, ensure_ascii=False))

    db = get_db()

    if args.cmd == "start":
        # ⚠️ 防重复 start（2026-11-02 新增）。
        #
        #    真实事故（10-31 跑 46）：同一目录下建了两个会话 ——
        #      session 1  跑 4 个事件后就被抛弃 → 永远停在 running
        #      session 2  235 个事件 / 32 分钟
        #    旧 session 成了孤儿，既没结论也没人管，污染统计。
        #
        #    根因：`start` 只在「没有 record 目录」时才新建目录，
        #    但**每次都无条件新建 session** 并覆盖 current 指针。
        #
        #    处置：发现还有 running 会话就停下，并给两条明确出路 ——
        #    不要静默新建，也不要硬拦（AI 可能确实需要重开）。
        if not args.force:
            _cur = current_session()
            if _cur:
                try:
                    _s = db.get_session(_cur)
                except Exception:  # noqa: BLE001
                    _s = None
                if _s and str(_s.get("status")) == "running":
                    _nev = len(_s.get("events") or [])
                    _age = str(_s.get("started_at") or "")[:19]
                    raise SystemExit(
                        f"已有运行中的会话 #{_cur}「{_s.get('title') or ''}」\n"
                        f"  开始 {_age}    已记 {_nev} 个事件\n"
                        f"\n"
                        f"  继续跑 → 直接用现有会话，不要再 start\n"
                        f"\n"
                        f"  确实要重来（title 写错等），挑一条收尾再 start：\n"
                        f"    session.py abandon --id {_cur} --reason \"重开\"\n"
                        f"        （作废：进 ERROR 桶，不算用例结论 —— 跑错了用这条）\n"
                        f"    session.py finish --status BLOCKED --summary \"重开\"\n"
                        f"        （算一条「没机会验证」的结论 —— 用例真跑不动用这条）\n"
                        f"\n"
                        f"  （确定不要这条检查时加 --force）")

        # ── 定位这条用例是哪份 md 文件 ──────────────────────────────
        #
        # ★ 用例的**唯一真相是磁盘上的 md 文件**（2026-11-17 用户裁决：
        #   「case 表整个删掉」）。数据库里不再有用例表，所以这里只做一件事：
        #   把 `--case` 给的标识解成一份 md，读它的标题行和正文。
        #
        # 标识接受两种写法：
        #   · 纯编号      `--case 176`
        #   · 模块_编号   `--case 图库导入入口_168`
        # 编号在本应用内**已有重号**（启动应用_03 vs 重要日倒数_03），
        # 所以重号时必须用后者；一个编号只命中一条时写编号就行。
        _case_file = find_case_file_by_ref(args.case_id, args.package)
        if _case_file is not None:
            # 文件头是 `# com.zui.calendar_176` 这种标题行，正文才是用例
            _raw = _case_file.read_text(encoding="utf-8", errors="replace")
            title = _raw.splitlines()[0].lstrip("# ").strip() if _raw else None
            user_input = _raw
        else:
            title = maybe_file(args.title) if args.title else None
            user_input = maybe_file(args.input) if args.input else None
        if not title or not user_input:
            raise SystemExit("需要 --case <编号>"
                             "（编号在本应用内重复时写 模块_编号）"
                             "或 --title/--input（新用例，中文长文本用 @文件）")
        sid = db.start_session(title, user_input, args.device,
                               kind="diag" if args.diag else "test")
        set_current(sid)
        if args.package:
            # 显式声明优先：首个 observe 的 backfill 不会覆盖已有值
            db.set_package(sid, args.package)
        related = [x.strip() for x in args.related.split(",") if x.strip()]
        if related:
            db.set_related(sid, related)
        # 扩 logcat 缓冲区防冲（-G 5M）+ 清 crash 缓冲区归因；设备不在也不影响建会话
        from common import try_connect
        d = try_connect(args.device)
        if d is not None:
            notes = []
            for cmd, okmsg in (("logcat -G 5M", "logcat 缓冲区已扩到 5M"),
                               ("logcat -b crash -c", "crash 缓冲区已清空")):
                try:
                    d.shell(cmd)
                    notes.append(okmsg)
                except Exception:
                    notes.append(f"{cmd} 失败（可手动 logcat.py setup / clear）")

            # ── 屏幕常亮：测试过程中不许灭屏 ────────────────────────────
            # 灭屏的代价不是「白等」那么简单：屏幕一黑，UI 树和截图都拿不到，
            # AI 会判成「控件找不到」，然后按 2 次上限记 BLOCKED —— 一条用例
            # 就这么废了，而真实原因只是设备省电。
            #
            # `svc power stayon true` 对应「充电时不锁屏」，插着 USB 就一直亮。
            # **开了不还原**（用户 2026-11-06 的裁决：「只开不管，一直保持常亮」）：
            # 还原反而会在收尾和下一轮开工之间留一个灭屏窗口。
            # 失败不阻断开工 —— 屏幕设置不成功也该把用例跑完，只在 note 里写明。
            try:
                d.shell("svc power stayon true")
                notes.append("屏幕常亮已开启（stayon true）")
            except Exception as e:  # noqa: BLE001
                notes.append(f"屏幕常亮设置失败：{e}（跑的过程中可能灭屏）")

            # ── u2 输入法：换成 AdbKeyboard 并设为默认 ──────────────────
            # 为什么在开场做（2026-11-05）：换新设备、做过 OTA、或用户手动切回
            # 搜狗之后，`send_keys(clear=True)` 会直接抛
            #   AdbBroadcastError: ADB_KEYBOARD_CLEAR_TEXT failed: Attempt to read
            #   from field 'ExtractedText.text' on a null object reference
            # 这个报错出现在 act 的输出里，AI 很容易误判成「控件坏了」，
            # 然后按「同一目标最多试 2 次」记 BLOCKED —— 实际是环境没准备。
            #
            # 用 u2 自带能力（_input.py:49 set_input_ime）：没装就先装
            # （内置 assets/app-uiautomator.apk），装了就 ime enable/set +
            # settings put secure default_input_method。已就绪时只花 ~145ms。
            # 不自己在 connect() 里做 —— 那是每次工具调用都付的代价。
            try:
                d.set_input_ime(True)
                notes.append(f"输入法已就绪（{d.current_ime()}）")
            except Exception as e:  # noqa: BLE001
                notes.append(f"输入法设置失败：{e}（会打不了字，先手动确认设备输入法）")
            device_note = "；".join(notes)
        else:
            device_note = "设备未连接（连上后手动跑 logcat.py setup + logcat.py clear）"

        # ── 冷启动复位：把起点钉死在「App 冷启动后的首页」────────────────
        # 为什么默认做（2026-10-15 定）：起点如果取决于「上一次停在哪个界面」，
        # 那么用例 YAML 的步骤顺序就没法直接用 —— 复跑时设备可能停在链路中间，
        # 判据对不上，只能靠人去「迁就设备现在在哪」（这正是「融合」要解决的问题）。
        # 冷启动把这个问题从根上消掉：**不管上次停在哪，冷启动后一定在首页**，
        # 于是「从首页按固定路径走到 s1」成为可能，YAML 顺序即可直接执行。
        #
        # ⚠️ 只 force-stop（关进程），**不清数据**。
        # 「清数据」（pm clear / pm revoke）是**用例明确要求**时才做的事
        # （如「首次使用」「清除日历的数据」「测首次授权」）——见 SKILL「清理的范围跟着用例走」。
        # 拿 pm clear 当默认复位＝拿大锤砸一切：会清掉本用例要用的前置数据。
        restart_note = None
        pkg = args.package
        if d is not None and pkg and not args.no_restart:
            try:
                d.app_stop(pkg)
                d.app_start(pkg)
                restart_note = f"已冷启动复位 {pkg}（起点固定为 App 首页）"
            except Exception as e:  # noqa: BLE001
                restart_note = f"冷启动复位失败：{e}（起点未固定，需自行确认当前界面）"
        elif args.no_restart:
            restart_note = "已跳过冷启动复位（--no-restart），**起点不固定**，需自行确认当前界面"
        if restart_note:
            device_note = f"{device_note}；{restart_note}"

        (STORAGE_DIR / f".crash_seen_{sid}").unlink(missing_ok=True)
        out = {"ok": True, "session_id": sid, "related": related,
               "note": f"后续 observe/act/read 自动记入本会话；{device_note}"}

        # ★ 环境探测：告诉 AI 这台机器上该用什么 shell 语法。
        #
        #   用户要求：「skills 应该能够根据当前用户的设备情况。
        #             自动调整命令的格式和写法。而不是我们强制限定死。」
        #
        #   所以不是让 AI 自己猜、也不是在 SKILL 里写死"Windows 用 PS"，
        #   而是在开场把探测结果直接给它。
        out["env"] = detect_shell()
        # 知识卡浮现：有卡就提示路径 + 小节索引（只提示一次，见 common.knowledge_hint）
        hint = knowledge_hint(args.package)
        if hint:
            out["knowledge_hint"] = hint
            # 只要发出了 hint 就落标记（无卡包也有 system_card 提示，同样别重复发）
            db.mark_knowledge_shown(sid)
        # 上次走过的跳转步骤：纯参考，不自动执行。
        # 每次跑同一条用例，AI 都要重新摸「更多按钮在哪、菜单里哪个是课程表」——
        # 这些是机械跳转，finish 时已经导出到 steps.yaml，开场给它看。
        try:
            steps = steps_hint(args.package, title, user_input)
            if steps:
                out["last_steps"] = steps
        except Exception as e:  # noqa: BLE001
            out.setdefault("warnings", []).append(f"步骤提示读取失败：{e}")
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finding":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        fid = db.add_finding(sid, args.status, maybe_file(args.expect),
                             maybe_file(args.actual), maybe_file(args.note),
                             event_ids=args.event_id)
        emit(True, {"finding_id": fid, "evidence_event_ids": args.event_id})
    elif args.cmd == "findings-batch":
        # 一次写多条断言。任何一条不合法就整批不写（避免写一半）。
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        try:
            # ★ utf-8-sig：PowerShell 的 `Set-Content -Encoding utf8` 会写
            #   BOM，直接 json.loads 会报 "Unexpected UTF-8 BOM"。
            #   实测（2026-10-28）：AI 写断言文件必然用 Set-Content，必踩。
            raw = Path(maybe_file(args.file)).read_text(encoding="utf-8-sig")
            items = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"读不了断言文件：{exc}")
        if not isinstance(items, list) or not items:
            raise SystemExit("文件内容要是非空数组")
        # 先校验
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise SystemExit(f"第 {i} 项不是对象")
            st = it.get("status")
            if st not in STATUSES:
                raise SystemExit(
                    f"第 {i} 项 status 不合法：{st!r}（可用 {'/'.join(STATUSES)}）")
            if not it.get("expect"):
                raise SystemExit(f"第 {i} 项缺 expect")
        ids = []
        for it in items:
            ev = it.get("event_id") or it.get("event_ids") or []
            if isinstance(ev, int):
                ev = [ev]
            ids.append([int(x) for x in ev])
            fid = db.add_finding(sid, it["status"], it.get("expect", ""),
                                 it.get("actual", ""), it.get("note", ""),
                                 event_ids=ids[-1])
            ids[-1] = fid
        emit(True, {"count": len(items), "finding_ids": ids,
                    "note": "批量写入完成"})
    elif args.cmd == "resume":
        # 用途只有一个：**崩溃终止后人工确认继续**（清除终态，否则工具仍拒绝执行）。
        #
        # ⚠️ 顺序要紧：先校验理由，**通过后才清终态**。
        # 反过来会导致"无理由被拒"的那次调用已把终态抹掉，
        # 第二次不带理由的 resume 反而放行（真实 bug）。
        was_terminated = db.get_termination(args.id)
        note = maybe_file(args.note) if args.note else ""
        if was_terminated and not note:
            raise SystemExit(
                f"会话 #{args.id} 因崩溃被终止，继续执行必须给出理由："
                f"resume --id {args.id} --note \"为什么可以继续（如已确认偶发、已恢复前置）\"")
        db.clear_termination(args.id)
        db.set_session_status(args.id, "running")
        set_current(args.id)
        if note:
            db.log_event(args.id, "note", "session resume", note)
        out = {"ok": True, "session_id": args.id, "status": "running"}
        if was_terminated:
            out["note"] = "已清除崩溃终态，会话恢复运行"
            out["was_terminated"] = True
            out["terminate_reason"] = was_terminated
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finish":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（要收尾别的会话请加 --id）")
        summary = maybe_file(args.summary)
        ok = db.finish_session(sid, args.status, summary, args.package)
        cleanup_warning = None
        try:
            clear_current(sid)
        except (ValueError, OSError) as error:
            cleanup_warning = f"Conclusion committed, but current pointer cleanup failed: {error}"
        crash_n = db.count_crash_events(sid)
        stats = db.finding_stats(sid)
        self_check = []
        if cleanup_warning:
            self_check.append(cleanup_warning)
        # 操作效率自查（SKILL.md「执行节奏」）：
        # 只提示不阻断 —— 效率差不等于用例结论错，但**必须让人看见**，
        # 否则"跑得很脏"会悄悄过去（实测有的会话 44% 的 act 是无效试探）。
        eff = db.efficiency_stats(sid)
        if eff["act"]:
            if eff["observe"] >= eff["act"]:
                self_check.append(
                    f"⚠️ observe({eff['observe']}) 不少于 act({eff['act']}) —— "
                    f"SKILL 要求 observe 明显少于 act（第一反应该是读上一步的 next）")
            if eff["swipe"]:
                self_check.append(
                    f"⚠️ 用了 {eff['swipe']} 次 swipe —— 拨滚轮/重复手势请改用 "
                    f"act tap --repeat N（swipe 有惯性会过冲）")
            if eff["cp_miss"]:
                self_check.append(
                    f"⚠️ 有 {eff['cp_miss']} 次检查点未命中 —— 确认是否为无效试探步")
        # 采集路径边（不再导出步骤序列）。
        # 为什么换掉：步骤序列是最易腐的知识（换个状态/版本就废），
        # 导出「可复用的跳转步骤」（只含锚点明确+落点可验+非前置的 tap）。
        # 下次跑同一条用例时可以直接照着点这几步，不用 AI 每步重新找控件。
        #
        # ⚠️ diag（验证/调试）会话不导出。
        #   自检会话的标题常常也带用例编号（如"联想日历_176 步骤提示验证"），
        #   导出会覆盖掉真实运行的步骤文件 —— 实测踩过一次。
        steps_note = None
        try:
            _kind = str((db.get_session(sid) or {}).get("kind") or "test")
            if _kind == "diag":
                steps_note = "（diag 会话，跳过步骤导出）"
            else:
                steps_note = export_steps(db, sid)
        except Exception as e:  # noqa: BLE001
            self_check.append(f"⚠️ 步骤导出失败：{e}")

        out = {"ok": ok, "session_id": sid, "crash_events": crash_n,
               "findings": stats, "self_check": self_check,
               "hint": (f"本会话有 {crash_n} 个崩溃事件——若涉及被测/关联包，"
                        "结论应为 BLOCKED 且报告需附 crash.log 证据路径；"
                        "无关包崩溃仅需在报告中说明")
               if crash_n else "无崩溃事件"}
        if steps_note:
            out["steps_exported"] = steps_note
            out["steps_note"] = (
                "已把本轮「锚点明确+落点可验」的跳转步骤导出。"
                "下次跑同一条用例可以直接照着点这几步，"
                "前置条件/造数据/判定不在里面（那些每次不一样）。")
        emit(True, out)
        # ★ 会话收尾 -> 清掉"当前 record"指针。
        #
        #   不清的后果：下一次不属于任何会话的 observe/act
        #   会误接到上一次的目录里，把这次的证据写进历史运行。
        unpin_record()
    elif args.cmd == "abandon":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit(
                "没有进行中的会话可放弃。\n"
                "  想作废历史记录里某条：session.py abandon --id <会话id> --reason \"...\"\n"
                "  看有哪些：session.py list")
        cur = current_session()
        row = db.get_session(sid) or {}
        if not row:
            raise SystemExit(f"会话 #{sid} 不存在")
        status = str(row.get("status"))
        if status not in INTERIM:
            # 已经有结论了：绝不覆盖人写过的判定，只把指针解绑。
            if cur == sid:
                clear_current(sid)
            unpin_record()
            emit(True, {"session_id": sid, "status": status,
                        "note": f"会话 #{sid} 已是结论态（{status}），"
                                f"abandon 不覆盖已写的结论；本次只解绑了指针"})
            raise SystemExit(0)
        reason = (maybe_file(args.reason) or "").strip()
        if not reason:
            raise SystemExit(
                f'abandon 需要原因：session.py abandon --id {sid} --reason "为什么作废"\n'
                "  原因会写进 summary —— 不然半年后没人知道这条为什么没结论。")
        # 崩溃终止过的会话只能落 BLOCKED（db.finish_session 里写死的规矩），
        # 这里跟着让步，别让人卡在一个想不通的 ValueError 上。
        final = "BLOCKED" if row.get("terminated") else "ERROR"
        db.finish_session(sid, final, f"作废：{reason}")
        if cur == sid:
            clear_current(sid)
        unpin_record()
        emit(True, {"session_id": sid, "status": final, "reason": reason,
                    "note": ("已作废收尾，不计入用例通过率；"
                             + ("该会话曾被崩溃终止，按规矩落 BLOCKED；"
                                if final == "BLOCKED" else "进 ERROR 桶；")
                             + "证据目录保留不动")})
    elif args.cmd == "perm-intent":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（或用 --id 指定）")
        if args.clear:
            db.clear_perm_intent(sid)
            print(json.dumps({"ok": True, "session_id": sid, "intent": None,
                              "note": "权限意图已清除"}, ensure_ascii=False))
        elif args.show or not args.action:
            cur = db.get_perm_intent(sid)
            print(json.dumps({"ok": True, "session_id": sid, "intent": cur,
                              "hint": "声明：perm-intent --action grant|deny [--perm 类型]"
                              } if cur else {
                                  "ok": True, "session_id": sid, "intent": None,
                                  "hint": "当前无意图：遇到权限弹窗只会记录、不会点击"},
                             ensure_ascii=False))
        else:
            ok = db.set_perm_intent(sid, args.action, args.perm)
            db.log_event(sid, "perm", "perm-intent",
                         f"权限意图: {args.action}"
                         + (f"（{args.perm}）" if args.perm else ""))
            print(json.dumps({"ok": ok, "session_id": sid,
                              "intent": {"action": args.action, "permission": args.perm},
                              "hint": (f"后续 act/observe 遇到权限弹窗将自动点"
                                       f"【{'同意' if args.action == 'grant' else '拒绝'}】"
                                       f"；分支测完请 perm-intent --clear")},
                             ensure_ascii=False))
    elif args.cmd == "list":
        print(json.dumps(db.list_sessions(20), ensure_ascii=False, indent=2))
    elif args.cmd == "current":
        print(json.dumps({"session_id": current_session()}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, ValueError, OSError) as error:
        emit(False, {"error": str(error), "stage": "session",
                     "rejected": isinstance(error, ValueError),
                     "hint": "Correct the input or storage issue; rejected finish does not commit."})
        raise SystemExit(1)
