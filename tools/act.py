"""act：动一下。执行单个动作后自动回传动作后画面（= act + observe 一次完成）。

用法：
  python act.py tap --x 540 --y 1200 --via rid=com.app:id/btn_ok --why "提交表单" --name case172/step02
  python act.py longclick --x 540 --y 1200 --name ...
  python act.py input --x 540 --y 1200 --text "你好" --via text="确定" --name ...
  python act.py input --x 540 --y 1200 --text "追加内容" --no-click --append --name ...
  python act.py swipe --from 540,2000 --to 540,800 --dur 300 --watch text="允许" --name ...
  python act.py back --why "关闭二级页" --name ...
  python act.py home --name ...
  python act.py recents --name ...             # 打开多任务（最近任务）
  python act.py key --code 4 --name ...        # 4=BACK 3=HOME（一般用 back/home 即可）

语义说明：
- input 默认会先 click 目标坐标聚焦输入框（AI 已点过输入框时加 --no-click 避免
  双击副作用）；默认 clear=True 覆盖已有内容，追加用 --append。
- --watch：动作后监听瞬态弹窗并立即点击（tap/longclick/input/swipe 均支持），
  多候选用 | 分隔；决策在挂载时完成，弹窗出现亚秒级响应，不经 AI。
- --via 定位依据溯源（链路卡提炼靠它）；--why 本步理由（时间轴展示）。
- --settle 动作后稳定窗口毫秒数（默认 500）。异步渲染页（联网解析等）AI 应
  按知识卡经验加大，而不是靠反复 observe 轮询堆证据。
"""
from __future__ import annotations
from runtime import execution

import argparse
import json
import sys
import time
from datetime import datetime
from runtime import timed_call

from common import (add_common_args, add_common_args_all_subcommands, apply_stdin_args, capture,
                    next_targets, labeled_nodes, npm_identity,
                    connect, emit, evidence_paths, fail, guard_terminated,
                    log_auto, rel, summarize_xml, texts_payload)
import checkpoint



# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

def xy(v: str) -> tuple[int, int]:
    a, b = v.split(",")
    return int(a), int(b)


def parse_selector(spec: str) -> dict:
    """watch/选择器规格 → u2 查询参数。支持 rid=/text=/desc=。"""
    for prefix, key in (("rid=", "resourceId"), ("text=", "text"), ("desc=", "description")):
        if spec.startswith(prefix):
            return {key: spec[len(prefix):]}
    raise SystemExit(f"选择器规格不支持: {spec}（支持 rid=/text=/desc=）")


def check_crash(d, package: str | None, evidence_dir) -> dict | None:
    """崩溃检测（分级 + 20s 上下文取证）委托给 logcat.detect。

    ⚠️ evidence_dir 必须传当前步骤证据目录，否则 crash.log 不会落盘、
    崩溃取证就是空承诺（本注释为修过的真实事故留）。

    ⚠️ target 不能只用"当前前台包"：**被测 App 崩溃后前台会回落到桌面**，
    此时 foreground=com.zui.launcher、崩溃 process=com.zui.calendar，
    按前台包分类必然判成 other（"不影响判定"）→ 崩溃被放过。
    故优先用会话声明的被测包（首个 observe 回填的 session.package），
    前台包仅在会话未记录时兜底。
    """
    related: list[str] = []
    target = package
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            related = get_db().get_related(sid)
            sess_pkg = get_db().get_session_package(sid)
            if sess_pkg:
                target = sess_pkg
    except Exception:  # noqa: BLE001
        pass
    from logcat import detect as detect_crash
    return timed_call("crash_detection", detect_crash, d, target, related, evidence_dir)


def watch_and_click(d, stages: list[list[str]], timeout_ms: int,
                    per_stage_ms: int = 2500) -> dict:
    """瞬态弹窗确定性响应：在 UiAutomator 层轮询匹配即点（亚秒级）。

    用于权限框等**出现几秒就消失**的弹窗——决策在挂载时已完成，工具只负责快。

    ⚠️ 系统权限框约 6s 未点击会自动消失（Android 通用机制）。
    所以绝不能用"点一下 → observe 看看 → 再决定点哪"的节奏：
    一次 observe（截图 2.4MB + dump）就足以吃掉整个窗口（真实事故：
    168 用例的图库权限框就是这样丢的，最后权限没授上、选择器打不开）。

    支持**多阶段**：`stages=[stage1_specs, stage2_specs, ...]`
    典型场景是"App 自己的提示框 → 系统权限框"两段式：
    stage1 命中并点掉后，继续等 stage2，直到所有阶段处理完或超时。

    参数 `stages` 为二维列表（每阶段一组候选，阶段内用 | 分隔）。
    返回 {"clicked": [...每次点中的 spec...], "waited_ms":..., "missed_stage": idx}
    """
    t_start = time.time()
    all_deadline = t_start + timeout_ms / 1000
    clicked: list[dict] = []
    for idx, specs in enumerate(stages):
        selectors = [(s, parse_selector(s)) for s in specs]
        # 每阶段给一个较短的窗口；但总时长不超过 all_deadline
        stage_deadline = min(all_deadline, time.time() + per_stage_ms / 1000)
        hit = False
        while time.time() < stage_deadline:
            for spec, sel in selectors:
                try:
                    obj = d(**sel)
                    if obj.exists:
                        obj.click()
                        clicked.append({"stage": idx, "spec": spec,
                                        "waited_ms": round((time.time() - t_start) * 1000)})
                        hit = True
                        break
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                break
            time.sleep(0.15)
        if not hit:
            # 该阶段没出现：可能本来就没有（如权限已授予），交给 AI 判断，不算致命
            return {"clicked": clicked, "missed_stage": idx,
                    "missed_specs": specs,
                    "waited_ms": round((time.time() - t_start) * 1000)}
        time.sleep(0.25)   # 让下一个弹窗有时间渲染
    return {"clicked": clicked, "missed_stage": None,
            "waited_ms": round((time.time() - t_start) * 1000)}


SELECTOR_FLAGS = ("--text", "--id", "--rid", "--desc", "--xpath", "--uiautomator")


def _reject_selector_misuse(p: argparse.ArgumentParser) -> None:
    """拦住最常见的误用：给 tap/longclick 传文本选择器。

    ⚠️ 这两个动作**只收坐标**，没有选择器形式 —— 坐标由本次 observe 的
    next[].c 现场派生（比选择器更稳，可规避"同 rid 多态"）。
    直接抛 argparse 默认错误（"the following arguments are required: --x, --y"）
    只会让人以为"少传了参数"，故在此给出可执行的下一步。
    """
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("tap", "longclick"):
        return
    used = [flag for flag in SELECTOR_FLAGS if flag in argv]
    if not used:
        return
    action = argv[0]
    p.error(
        f"{action} 不支持 {'/'.join(used)} —— 该动作只按坐标点击。\n"
        f"  正确做法：先 `python tools/observe.py` 取 next[].c 坐标，\n"
        f"           再 `python tools/act.py {action} --x <c0> --y <c1> --via \"rid=xxx\"`。\n"
        f"  要按文字定位请用 observe 输出的坐标，不要猜选择器。"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="act：执行动作并回传动作后画面")
    # --settle / --perm-action / --perm 已在 add_common_args 里定义
    add_common_args(p)
    sub = p.add_subparsers(dest="action", required=True)

    for name in ("tap", "longclick"):
        s = sub.add_parser(name)
        s.add_argument("--x", type=int, required=True)
        s.add_argument("--y", type=int, required=True)
        s.add_argument("--via", default=None, help="定位依据，如 rid=... 或 text=...")
        s.add_argument("--why", default=None, help="这一步的理由（入库展示在时间轴）")
        s.add_argument("--watch", default=None,
                       help="动作后监听瞬态弹窗并立即点击，多个候选用 | 分隔。"
                            "如 rid=com.android.permissioncontroller:id/permission_allow_button|text=允许")
        s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout",
                       help="watch 总窗口毫秒数，默认 8000（系统权限框约 6s 自行消失，必须一次处理完）")
        if name == "tap":
            s.add_argument("--repeat", type=int, default=1,
                           help="连点次数，默认 1。用于滚轮「点上方/下方一格=±1」这类"
                                "同一位置重复点按：N 次点按在**一个进程内**完成，"
                                "不要拆成 N 次工具调用（每次冷启动 3-6s，会撞命令超时，"
                                "超时会让动作执行了却没记账 → 会话被判 recording incomplete）。")
            s.add_argument("--gap", type=int, default=120,
                           help="--repeat 时每次点按之间的间隔毫秒，默认 120")

    s = sub.add_parser("input")
    s.add_argument("--x", type=int, required=True)
    s.add_argument("--y", type=int, required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--via", default=None)
    s.add_argument("--why", default=None)
    s.add_argument("--watch", default=None)
    s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout", help="watch 总窗口毫秒（系统权限框约6s自行消失）")
    s.add_argument("--no-click", action="store_true", dest="no_click",
                   help="不先点击聚焦（AI 已点过输入框时用，避免双击副作用）")
    s.add_argument("--append", action="store_true",
                   help="追加到已有内容（默认覆盖 clear=True）")

    s = sub.add_parser("swipe")
    s.add_argument("--from", dest="src", required=True, help="x1,y1")
    s.add_argument("--to", dest="dst", required=True, help="x2,y2")
    s.add_argument("--dur", type=int, default=300, help="毫秒，默认 300")
    s.add_argument("--via", default=None)
    s.add_argument("--why", default=None)
    s.add_argument("--watch", default=None)
    s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout", help="watch 总窗口毫秒（系统权限框约6s自行消失）")

    for name in ("back", "home", "recents"):
        s = sub.add_parser(name)
        s.add_argument("--why", default=None, help="这一步的理由（入库展示在时间轴）")

    s = sub.add_parser("key")
    s.add_argument("--code", type=int, required=True)
    s.add_argument("--why", default=None)

    # 让 --serial/--dir/--name 在子命令前后都能写（两种位置解析后合并）
    add_common_args_all_subcommands(p)
    _reject_selector_misuse(p)
    apply_stdin_args()   # 支持 --stdin-json（参数走 stdin，避开 Windows 引号问题）
    args = p.parse_args()
    guard_terminated(f"act {args.action}")   # 会话已因崩溃终止 → 拒绝执行
    via = getattr(args, "via", None)
    why = getattr(args, "why", None)

    # 顺带声明权限意图（在动作之前生效，这样本次动作触发的弹窗就能被接住）
    #
    # ⚠️ 意图落不了地就必须当场报错，绝不能静默往下走。
    #    旧写法是 `if si:` + `except: pass` —— 没有活动会话（或会话库和
    #    工具层不同树）时意图被丢掉，后面 perm.py 读不到意图就回落
    #    default=grant，于是「测拒绝授权」的用例实际点了「同意」，
    #    还留下一条看起来正常的记录。实测跑 169 时两条拒绝分支全废在这里。
    perm_inline = None
    if getattr(args, "perm_action", None):
        si = None
        why_missing = ""
        try:
            from db import current_session, get_db
            si = current_session()
        except Exception as e:  # noqa: BLE001
            why_missing = f"读会话失败：{e}"
        if si:
            try:
                get_db().set_perm_intent(si, args.perm_action, args.perm or "")
                perm_inline = {"action": args.perm_action, "permission": args.perm or "",
                               "session": si}
            except Exception as e:  # noqa: BLE001
                fail(f"permAction={args.perm_action} 写不进会话 {si}：{e}"
                     "｜先修记录通道再跑，否则这条授权判定不会被留证")
        elif args.perm_action == "deny":
            from runtime import storage_root_source  # 只在报错路径上付代价
            fail("permAction=deny 需要活动会话才能生效，当前取不到会话"
                 f"{('：' + why_missing) if why_missing else ''}。"
                 f"会话指针来源：{storage_root_source()}。"
                 "两种可能：① 没开会话（先 session.py start）；"
                 "② 插件 tool_root 和会话不在同一棵树"
                 "（改 plugins/*/[preset/]cordis.patch.yml 的 target 或重启 harness）。"
                 "不报错继续跑的话会按默认点「同意」，把拒绝分支测成假通过。")
        else:  # grant 本来就是默认值，丢了意图不改变行为，只说明一句
            perm_inline = {"action": "grant", "permission": args.perm or "",
                           "registered": False,
                           "note": "无活动会话，未写意图；grant 与默认行为一致"}

    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    execution("may_have_executed")
    try:
        if args.action in ("tap", "longclick"):
            repeat = getattr(args, "repeat", 1) or 1
            gap = getattr(args, "gap", 120) or 0
            fn = d.long_click if args.action == "longclick" else d.click
            for i in range(repeat):
                if i:
                    time.sleep(gap / 1000)
                timed_call("action", fn, args.x, args.y)
            action = {"type": args.action, "x": args.x, "y": args.y}
            if repeat > 1:
                action["repeat"] = repeat
                action["gap_ms"] = gap
        elif args.action == "input":
            if not args.no_click:
                timed_call("action", d.click, args.x, args.y)
                timed_call("input_focus_wait", time.sleep, 0.3)
            timed_call("action", d.send_keys, args.text, clear=not args.append)
            action = {"type": "input", "x": args.x, "y": args.y, "text": args.text,
                      "mode": "append" if args.append else "overwrite"}
        elif args.action == "swipe":
            x1, y1 = xy(args.src)
            x2, y2 = xy(args.dst)
            timed_call("action", d.swipe, x1, y1, x2, y2, args.dur / 1000)
            action = {"type": "swipe", "from": [x1, y1], "to": [x2, y2], "dur_ms": args.dur}
        elif args.action == "back":
            timed_call("action", d.press, "back")
            action = {"type": "back"}
        elif args.action == "home":
            timed_call("action", d.press, "home")
            action = {"type": "home"}
        elif args.action == "recents":
            timed_call("action", d.press, "recent")
            action = {"type": "recents"}
        else:
            timed_call("action", d.shell, f"input keyevent {args.code}")
            action = {"type": "key", "code": args.code}
    except Exception as e:  # noqa: BLE001
        fail(f"动作执行失败: {e}")

    execution("executed")
    # ⚠️ **所有动作**执行完都在这里等 settle 再 dump+截图，无一例外。
    #    这个等待不是可选的：界面（尤其是弹窗）有滑入动画，动画结束前后
    #    dump 出来的控件坐标是两套 —— 实测权限框刚弹出时「允许」在
    #    (831,376)（动画中间帧），稳住后才到 (2171,1420)。整段动画约 300ms。
    #    漏掉这个等待，后面拿到的是动画中间帧的坐标，点了点空。
    timed_call("settle_wait", time.sleep, args.settle / 1000)
    _is_click = args.action in ("tap", "longclick")

    # ⚠️ evidence_paths() 在下面**只调一次**，别在这里再调一遍。
    #   它内部会 mkdir + auto_name() 自增序号：调两次就会多建一个空目录、
    #   并且把 step 号用掉一个。
    # ⚠️ 必须按 action 分支求值：字典字面量会**同时**求值所有 value，
    # 而 tap 没有 args.text / swipe 没有 args.x / back 没有 args.code
    # → AttributeError 让除 input 外的所有动作在采集阶段直接崩（真实事故）。
    if args.action in ("tap", "longclick"):
        detail = f"{'点击' if args.action == 'tap' else '长按'} ({args.x},{args.y})"
        _rep = getattr(args, "repeat", 1) or 1
        if _rep > 1:
            detail += f" ×{_rep}"
    elif args.action == "input":
        detail = (f"输入 {args.text!r}"
                  + ("（追加）" if getattr(args, "append", False) else ""))
    elif args.action == "swipe":
        detail = f"滑动 {args.src} → {args.dst}"
    elif args.action == "back":
        detail = "返回"
    elif args.action == "home":
        detail = "回到桌面"
    elif args.action == "recents":
        detail = "打开多任务"
    else:
        detail = f"keyevent {args.code}"
    if via:
        detail += f" via {via}"
        action["via"] = via
    if why:
        action["why"] = why

    # watch 在 capture 之前：瞬态弹窗的窗口期不能浪费在一次 dump 上。
    # ⚠️ 系统权限框约 6s 自动消失，必须先挂 watch 再让动作执行完，
    # 绝不能用"点一下 → observe → 再点"的节奏（会丢掉整个窗口）。
    watch_result = None
    watch_spec = getattr(args, "watch", None)
    if watch_spec:
        # 多阶段语法：阶段之间用 ";;" 分隔，阶段内候选用 "|" 分隔。
        # 例：--watch "text=知道了;;rid=...permission_allow_all_button|text=允许"
        #     （先点 App 提示框"知道了"，再点系统权限框"全部允许"）
        stages = [[s for s in stage.split("|") if s.strip()]
                  for stage in watch_spec.split(";;") if stage.strip()]
        watch_result = timed_call("watch", watch_and_click, d, stages,
                                  getattr(args, "watch_timeout", 8000))
        got = watch_result.get("clicked") or []
        if got:
            detail += " · watch命中 " + ", ".join(
                f"[阶段{c['stage'] + 1}]{c['spec']}" for c in got)
        if watch_result.get("missed_stage") is not None:
            detail += (f" · watch阶段{watch_result['missed_stage'] + 1}未出现"
                       f"（候选 {watch_result.get('missed_specs')}）")

    # ── 权限弹窗处理 ─────────────────────────────────────────────────────
    #
    # 只有**点击类动作**（tap / longclick）才查权限：权限申请几乎只会被点击触发
    # （用户 2026-11-06 裁决）。swipe / back / home / input / key 一律不查 ——
    # 为极低概率的场景让每步都付一次检测钱不划算，代码也别复杂化。
    #
    # 流程是一条直线，每一轮**只拍一次**（截图+dump），拍完直接拿那份 dump 判断：
    #   点 → 等 settle → 拍一份 → 有权限框就点 → 等 300ms → 拍一份 → 循环到框消失
    #   → 最后那份就是本次 act 的证据
    #
    # ⚠️ 截图每轮只拍一次（用户 2026-11-06）：「点击后等 500ms 我们 dump 截图，
    #    肯定有这个弹窗的截图…不用存两份吧」。但**点掉的每个弹窗仍记一步**
    #    （「点过的就是当做一个步骤」）：shot() 刚写进 act 目录的那份画面就是
    #    弹窗本体，on_dialog 把它**复制**到独立 step 目录再记事件 —— 零额外拍摄，
    #    下一轮 shot() 自然覆盖 act 目录，最后留下的（无框）当 act 证据。
    perm_result = None
    _perm_steps: list[str] = []
    png, xml, meta = evidence_paths(args.dir, args.name)

    def _snap_perm_dialog(info, btn) -> None:
        """把刚拍的这份弹窗画面转存成一个独立权限步 + 一条事件。不重拍、异常不外抛。"""
        try:
            import shutil
            _ts = datetime.now().isoformat(timespec="seconds")
            _p, _x, _m = evidence_paths(None, None)     # 自动命名，step 号递增
            shutil.copyfile(png, _p)
            shutil.copyfile(xml, _x)
            _prompt = (info or {}).get("prompt")
            _buttons = [b["text"] for b in ((info or {}).get("buttons") or [])]
            try:
                _md = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _md = {}
            _md.update({"kind": "permission", "permission_prompt": _prompt,
                        "permission_buttons": _buttons,
                        "clicked": (btn or {}).get("text")})
            _m.write_text(json.dumps(_md, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            _clk = (btn or {}).get("text") or "?"
            log_auto("perm", "perm-dialog",
                     f"权限弹窗：{_prompt or '?'} · 已点击「{_clk}」",
                     {"kind": "permission", "prompt": _prompt,
                      "buttons": _buttons, "clicked": btn},
                     rel(_p), None, _ts)
            _perm_steps.append(_p.parent.name)
        except Exception:  # noqa: BLE001  证据不该阻断点框主流程
            pass

    def _shot_for_perm():
        """拍一份 dump+截图并**直接落成本次 act 的证据**，返回 (xml_text, payload)。

        权限流程每一轮都调它：既回答「框还在不在」，又产出最终证据。
        多次调用会覆盖同一个 step 目录 —— 留下的永远是最后那份（框消失后的画面）；
        中途点掉过的弹窗由 _snap_perm_dialog 从这些文件里复制留证。
        """
        nonlocal _captured
        _captured = capture(d, png, xml, meta, extra={
            "action": action, **({"watch": watch_result} if watch_result else {})})
        return xml.read_text(encoding="utf-8"), _captured

    _captured = None                    # 已拍好的采集结果，避免拍第二遍
    if _is_click:
        try:
            from db import current_session, get_db
            si = current_session()
            intent = get_db().get_perm_intent(si) if si else None
            import perm as _perm
            perm_result = timed_call(
                "permission", _perm.handle, d,
                intent["action"] if intent else None,
                shot=_shot_for_perm, on_dialog=_snap_perm_dialog,
                settle_s=0.3)
        except Exception as e:  # noqa: BLE001
            perm_result = {"error": str(e)}
    if perm_result and perm_result.get("detected"):
        clk = ", ".join(c["text"] for c in perm_result.get("clicks") or [])
        src = "（默认）" if perm_result.get("action_source") == "default" else ""
        detail += " · 权限弹窗"
        if clk:
            detail += f" 已自动点击: {clk} {src}".rstrip()
        elif perm_result.get("unmatched"):
            detail += f" 未匹配可点按钮，可见: {perm_result.get('buttons')}"
        else:
            detail += " 未点击"
        # 告诉 AI 后台存了哪几个权限步。不说的话它会在 summary 里写
        # 「没有留下独立步骤」——账实不符（实测踩过，跑 168 时）。
        if _perm_steps:
            perm_result["evidence_steps"] = list(_perm_steps)
            detail += f" · 已存权限步 {', '.join(_perm_steps)}"
        if perm_result.get("stuck"):
            detail += " · ⚠️弹窗点不动（内容无变化），可能被遮挡"

    try:
        if _captured is not None:
            # shot() 在权限流程里已经拍好并落盘了，直接用它当证据 —— 不重拍
            info = _captured
        else:
            info = capture(d, png, xml, meta,
                           extra={"action": action, **({"watch": watch_result} if watch_result else {})})
    except Exception as e:  # noqa: BLE001
        fail(f"动作已执行但采集失败: {e} | action={action}")

    # 崩溃自动检测（分级 + 20s 上下文取证，crash.log 落在当前步骤证据目录）
    failure = None
    crash = check_crash(d, info.get("package"), png.parent)
    alert = None
    if crash and crash.get("new"):
        from logcat import verdict as crash_verdict
        v = crash_verdict(crash)
        entry = crash.get("entry") or {}
        scope = crash.get("scope")
        info["crash_detected"] = True
        info["crash"] = {"scope": scope, "type": entry.get("type"),
                         "process": entry.get("process"), "time": entry.get("time"),
                         "log_path": crash.get("log_path")}
        detail += f" · ⚠️{v['message']}"
        if v["blocked"]:
            alert = v["message"]
            # 崩溃一次即终止：写会话终态（工具层硬拦截后续动作），
            # 并置 blocked 标记，AI 必须立即 finish 而不再继续
            info["blocked"] = True
            failure = {"blocked": True, "scope": scope,
                       "type": entry.get("type"), "process": entry.get("process"),
                       "reason": v["message"]}
            try:
                from db import current_session, get_db
                sid = current_session()
                if sid:
                    get_db().terminate_session(sid, v["message"])
            except Exception:  # noqa: BLE001
                pass


    # ⚠️ 事件必须在**补齐 perm/watch 等附加字段之后**再落库：
    # 这些字段是链路卡回放的关键（🎯 权限意图声明、🔐 权限框响应），
    # 若在组装前落库，卡里就只剩裸动作，回放者不知道当时声明了什么意图（真实缺陷）。
    if watch_result:
        info = {**info, "watch": watch_result}
    if perm_inline:
        info = {**info, "perm_intent_set": perm_inline}
    if perm_result and perm_result.get("detected"):
        info = {**info, "permission": perm_result}
    # 落库前先算一份**屏幕身份签名**（`kind=value` 集合），随事件一起存。
    # 用途：给这一步打一份「屏幕身份签名」，事后能认出当时停在哪一页。
    # 只存签名不存整个 nodes —— 签名几十个短串，nodes 可能有上百个对象。
    _sig = npm_identity(xml.read_text(encoding="utf-8"),
                        None if getattr(args, "no_filter", False) else info.get("package"))
    info = {**info, "sig": _sig}
    # 检查点：由工具判"这一步落在预期位置没有"。
    # 让 AI 不必再读 dump 自己认页面（那是会话时长的大头 —— 实测 58~64% 花在
    # 两次调用之间的判断上）。命中与否都返回，但**不阻断**：工具只报事实，
    # 由 AI 决定继续还是修正（测试里"实际发生了什么"比"帮它走通"重要）。
    _ck = checkpoint.evaluate(checkpoint.parse_expect(getattr(args, "expect", None)),
                              activity=info.get("activity"), sig=_sig)
    if _ck["checked"]:
        info = {**info, "checkpoint": _ck}
        detail += (" · ✅检查点命中" if _ck["ok"]
                   else " · ⚠️检查点未命中: " + "; ".join(_ck["missed"]))
    duration_ms = round((time.perf_counter() - t0) * 1000)
    info["duration_scope"] = "core operation through capture/signature/crash check; excludes connection, event write and response"
    log_auto("act", f"act {args.action}", detail, info, rel(png), duration_ms, started_at)

    _texts: list = []
    nodes = summarize_xml(xml.read_text(encoding="utf-8"),
                          mode="full" if getattr(args, "full", False) else "nav",
                          fg_package=None if getattr(args, "no_filter", False)
                          else info.get("package"),
                          texts_out=_texts)
    payload = {
        "action": action,
        **info,
        "evidence": {"png": str(png), "xml": str(xml), "meta": str(meta)},
        # 「下一步能点什么」精简清单放最前 —— 直接从上一层的返回走到下一步，
        # **不需要再单独 observe 拿坐标**（那会多花 2s 工具 + 10~15s 决策等待）。
        "next": next_targets(nodes, labeled=labeled_nodes(
            xml.read_text(encoding="utf-8"),
            None if getattr(args, "no_filter", False) else info.get("package"))),
        "nodes": nodes,
        "nodes_count": len(nodes),
        "nodes_mode": "full" if getattr(args, "full", False) else "nav",
        **texts_payload(_texts),
        "hint": ("下一步点 `next` 里的目标（c=坐标，t=文字，r=rid）；"
                 "`next` 只含可点控件，`texts` 是本页不可点文字（最多 20 条）。"
                 "判断「某字段/某功能在不在」看 texts 或 nodes，不要用 next 有没有；"
                 "texts_more>0 要看全就 full=true。"
                 "toast 等浮层不在 nodes 里，用 android_read OCR"),
    }
    # `sig`（屏幕身份签名）和 `timing`（各阶段耗时）只落盘、不回传给 AI：
    # 它们由工具侧消费（checkpoint 判定、浮层检测、路径采集、离线性能分析），
    # AI 一个都不需要，白占返回体积。
    #
    # ⚠️ 以后加字段注意：**落库用的 info 和返回用的 payload 是两份**，
    #    往 info 里加会同时进这两处；只想落库就加在 info，再从这里弹掉。
    payload.pop("sig", None)
    payload.pop("timing", None)
    if perm_inline:
        payload["perm_intent_set"] = perm_inline
        detail = (f"[意图:{perm_inline['action']}"
                  + (f"/{perm_inline['permission']}" if perm_inline["permission"] else "")
                  + "] " + detail)
    if alert:
        payload["alert"] = alert
    if _ck["checked"]:
        payload["checkpoint"] = _ck
        if not _ck["ok"]:
            payload["checkpoint_hint"] = (
                "⚠️ 落点与预期不符：未命中 " + "; ".join(_ck["missed"]) + "。"
                "先判断是「多余的临时弹窗」（不影响主流程 → 关掉继续）还是"
                "「用例要的东西不存在了」（主流程被改 → 停止并如实记录）。")
    if perm_result and perm_result.get("detected"):
        payload["permission"] = perm_result
        if not (perm_result.get("clicks") or []):
            payload["permission_hint"] = (
                "检测到系统权限弹窗但**没有自动点击**：弹窗里没有可匹配的按钮"
                f"（可见 {perm_result.get('buttons')}）。"
                "若是要测「拒绝」路径，用 perm-intent --action deny 后重触发；"
                "否则看 buttons 自己决定点哪个。")
    if failure:
        # 硬性终止信号：AI 必须立即 finish(BLOCKED)，不要再发下一个 act
        payload["terminate"] = True
        payload["terminate_reason"] = failure["reason"]
        payload["hint"] = ("⛔ 检测到被测/关联包崩溃 → 立即停止执行："
                           "session.py finding --status BLOCKED ... 然后 "
                           "session.py finish --status BLOCKED ...；不要继续下一步")
    emit(True, payload)


if __name__ == "__main__":
    main()
