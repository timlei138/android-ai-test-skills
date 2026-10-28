#!/usr/bin/env python3
"""本地 Web 测试台（v2）：测试会话时间线 + 知识库编辑。
零依赖：Python 标准库 http.server。

启动:  python webui.py [--port 8900]
访问:  http://127.0.0.1:8900
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
from db import SessionDB, get_db  # noqa: E402
from runtime import KNOWLEDGE_DIR, RECORD_DIR, STORAGE_DIR  # noqa: E402

# ★ UTF-8 兜底：Windows 控制台默认 GBK，启动日志里 print 一个 ⚠️ 就崩。
#   实测踩过：webui 起不来，AI 以为是端口问题，换了 5 种启动方式都没想明白 ——
#   实际是一个 print 里的 emoji 编码失败。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001
    pass

WEBUI_DIR = ROOT / "webui"
# ★ 2026-10-27：不再自己算 `ROOT / "storage"`。
#
#   上一版这里写死了 ROOT（skill 项目目录），而 runtime.py 已经改成
#   "storage 落在当前工作目录"。两边不一致 -> webui 显示的是旧目录里的数据，
#   而 AI 在新目录里跑 —— 用户看到的就是"有的用新的有的用旧的"。
#
#   现在统一从 runtime 导入，一个来源。
#   KNOWLEDGE_DIR 同理。
MD_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\-]+\.md$")

# 用例程序化描述：YAML 是唯一执行真相，同名 .md 是渲染产物（由 tools/case_md.py 生成）。
#
# ★ 2026-10-27 重构：用例从 `storage/cases/` 搬到 `assets/cases/<应用>/`。
#   · assets 是**资产**（进 git），storage 是**运行产物**（不进 git）
#   · 按应用分子目录，所以下面查文件一律 rglob / 用 _find_spec 定位
CASES_DIR = ROOT / "assets" / "cases"
SPEC_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\-（）()]+$")


# ══════════════════════════════════════════════════════════════════════
# 运行记录：跨多个 uuid 库聚合
# ══════════════════════════════════════════════════════════════════════
#
# ## 这一层干什么
#
# 一次运行 = 一个独立目录：
#
#     storage/record/
#     ├── 6f11becbfd41/
#     │   └── db.sqlite3        <- 这次运行的库（1 session / 6 events / 3 findings）
#     └── a2a5fce9c8f4/
#         └── db.sqlite3        <- 另一次
#
# 本模块负责**扫全部 uuid 库并聚合**，页面才看得到跨运行的历史和统计。
# 只读某一个固定库的话，页面上一条记录都不会有 —— 数据在，但没接上。
#
# ## 为什么不合并成一个库（方案 B）
#
# 用户选了 C："最干净"。一次运行一个库，天然隔离：
#   · 删一次运行 = 删一个目录，不影响别人
#   · 两个会话同时跑不会互相锁库
#   合并回去就把这些好处丢了。
#
# ## 所以：每个请求临时扫一遍 uuid 库，拼成列表
#
# 代价是每次请求多开几个 sqlite 连接。运行记录数量在几十个量级，
# 每个库只有一张小表，实测可以接受。
#
# ## 会话 id 怎么做到全局唯一
#
# 各个库的 `sessions.id` 都从 1 开始，直接合并会撞。
# 用 `<uuid>:<local_id>` 做复合 id，前端当字符串用，不用改。
#
# 例：
#     "6f11becbfd41:1"    第 6f11becbfd41 次运行的第 1 个会话
#
# 纯数字 id（"12"）也接受：解析不出 uuid，属于不存在的入口。

RUN_DB_GLOB = "*/db.sqlite3"


def _record_dbs() -> list[tuple[str, Path]]:
    """所有 uuid 运行库：[(uuid, db 路径)]，按目录修改时间从新到旧。"""
    out: list[tuple[str, Path]] = []
    if not RECORD_DIR.is_dir():
        return out

    for d in RECORD_DIR.iterdir():
        if not d.is_dir():
            continue
        db = d / "db.sqlite3"
        if db.is_file():
            out.append((d.name, db))

    def _mtime(item: tuple[str, Path]) -> float:
        try:
            return item[1].stat().st_mtime
        except OSError:
            return 0.0

    return sorted(out, key=_mtime, reverse=True)


def _split_sid(sid: str) -> tuple[str, int | None]:
    """把 `<uuid>:<local_id>` 拆开。纯数字 -> (uuid 为空, id)。

    返回 (uuid, local_id)：
      · "6f11:1"  -> ("6f11", 1)
      · "12"      -> ("", 12)         解析不出 uuid（不存在的入口）
      · 解析不了 -> ("", None)
    """
    sid = str(sid or "").strip()
    if ":" in sid:
        head, _, tail = sid.rpartition(":")
        if tail.isdigit():
            return head, int(tail)
        return "", None
    return ("", int(sid)) if sid.isdigit() else ("", None)


def _uuid_of(sid: str) -> str:
    """从复合 id 里取 uuid 部分。纯数字时按旧库处理。"""
    head, _ = _split_sid(sid)
    return head or "webui"


def _open_db(uuid: str) -> SessionDB | None:
    """按 uuid 打开对应的运行库。打不开返回 None（不该让整页崩）。

    **不再支持 "webui" 这个旧聚合库**（2026-10-28 删）——
    它从来没被写入过。传 "webui" 进来只会拿到 None。
    """
    if not uuid or uuid == "webui":
        return None
    path = RECORD_DIR / uuid / "db.sqlite3"
    if not path.is_file():
        return None
    try:
        return SessionDB(path)
    except Exception:  # noqa: BLE001
        return None


def all_sessions(include: tuple[str, ...] | None = None,
                 kind: str | None = "test") -> list[dict]:
    """跨所有运行库聚合会话列表。

    每个 session 的 `id` 改写成 `<uuid>:<local_id>`，并带上 `run_id`
    方便前端显示"哪一次运行"。
    """
    out: list[dict] = []
    for uuid, _path in _record_dbs():
        db = _open_db(uuid)
        if db is None:
            continue
        try:
            rows = db.list_sessions(include=include, kind=kind, limit=500)
        except Exception:  # noqa: BLE001
            continue
        finally:
            pass
        for r in rows:
            r = dict(r)
            r["local_id"] = r.get("id")
            r["id"] = f"{uuid}:{r.get('id')}"
            r["run_id"] = uuid
            out.append(r)

    # 按开始时间倒序（新在前）；没有时间的排后面
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out


def session_detail(sid: str, include_events: bool = True) -> dict | None:
    """取单个会话详情（含 events / findings）。sid 可以是复合 id 或旧数字。"""
    uuid = _uuid_of(sid)
    _h, local_id = _split_sid(sid)
    if local_id is None:
        return None
    db = _open_db(uuid)
    if db is None:
        return None
    try:
        s = db.get_session(local_id)
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    s = dict(s)
    s["local_id"] = s.get("id")
    s["id"] = f"{uuid}:{s.get('id')}"
    s["run_id"] = uuid
    return s


def session_stats_all() -> dict:
    """全部运行的结论统计（测试记录页顶部那几个数字）。

    ⚠️ `db.session_stats()` 的字段名是 `pass_n` / `fail_n` / `blocked_n` /
       `running_n` / `paused_n`（带 `_n` 后缀），不是 PASS/FAIL。
       第一版按 PASS/FAIL 取，结果 total 对、各状态全是 0 —— 页面看着像
       "有记录但统计坏了"。这里把两种写法都认一遍。
    """
    # WARN 只是**历史数据的展示位**（2026-10-28 起不再产生新 WARN）。
    # 新会话只有 PASS / FAIL / BLOCKED / ERROR 四种结论。
    agg = {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "PAUSED": 0,
           "WARN": 0, "ERROR": 0, "RUNNING": 0, "total": 0}
    # db 字段 -> 我们的键
    KEYMAP = {
        "pass_n": "PASS", "fail_n": "FAIL", "blocked_n": "BLOCKED",
        "running_n": "RUNNING", "paused_n": "PAUSED",
        # 万一哪天 db 改了名，这样也不用改这里
        "pass": "PASS", "fail": "FAIL", "blocked": "BLOCKED",
        "running": "RUNNING", "paused": "PAUSED",
        "warn": "WARN", "error": "ERROR",
    }
    for uuid, _p in _record_dbs():
        db = _open_db(uuid)
        if db is None:
            continue
        try:
            st = db.session_stats() or {}
        except Exception:  # noqa: BLE001
            continue
        agg["total"] += int(st.get("total") or 0)
        for src, dst in KEYMAP.items():
            agg[dst] += int(st.get(src) or 0)
    return agg


def session_kind_counts_all() -> dict:
    out: dict[str, int] = {}
    for uuid, _p in _record_dbs():
        db = _open_db(uuid)
        if db is None:
            continue
        try:
            k = db.session_kind_counts()
        except Exception:  # noqa: BLE001
            continue
        for key, n in (k or {}).items():
            out[key] = out.get(key, 0) + int(n or 0)
    return out


def run_stats_all(days: int = 3) -> list[dict]:
    """最近 N 天每天的执行情况（仪表盘柱状图），跨所有运行库合并同一天。"""
    merged: dict[str, dict] = {}
    for uuid, _p in _record_dbs():
        db = _open_db(uuid)
        if db is None:
            continue
        try:
            rows = db.run_stats(days)
        except Exception:  # noqa: BLE001
            continue
        for r in rows or []:
            day = str(r.get("day") or "")
            if not day:
                continue
            m = merged.setdefault(day, {"day": day, "total": 0, "pass": 0,
                                        "fail": 0, "blocked": 0})
            for k in ("total", "pass", "fail", "blocked"):
                m[k] += int(r.get(k) or 0)
    return sorted(merged.values(), key=lambda x: x["day"])


def _find_spec(title: str, suffix: str) -> Path | None:
    """按名字在 assets/cases/**/ 下找文件。

    为什么不能直接 `CASES_DIR / f"{title}{suffix}"`：
    分子目录后，文件在 `cases/<应用>/<title>.yaml`，
    直接拼根目录会永远找不到（而且报"还没渲染"这种误导性错误）。
    """
    if not CASES_DIR.is_dir():
        return None
    for fp in CASES_DIR.rglob(f"{title}{suffix}"):
        if fp.is_file():
            return fp
    return None


# 用例编号：`com.zui.calendar_168` -> "168"，`联想日历_168 课程表` -> "168"
#
# 前后各加一个约束，两个方向都有实际反例：
#   前面 `[_\-]`   防止把 `com.zui.calendar` 里没有的数字算进来；
#                  也防止 `abc1234` 这种没有分隔符的串被误当编号
#   后面 `(?!\d)`  防止 `1680` 被切出 `168`（那样 168 和 1680 会串）
#
# ⚠️ 这个正则和 tools/session.py 里"提示用 --case 复用"的那个必须一致。
CASE_NUM_RE = re.compile(r"[_\-](\d{2,4})(?!\d)")


def _case_number(text: str) -> str | None:
    """从用例文件名或会话标题里抽用例编号。

    用例在文件里叫 `com.zui.calendar_168`，会话标题叫
    `联想日历_168 课程表-从图库导入` —— 两边唯一稳定的共同点就是 168。
    dashboard 要靠它把"跑过的记录"对到"用例"上。
    """
    m = CASE_NUM_RE.search(str(text or ""))
    return m.group(1) if m else None


def _scan_case_files() -> list[dict]:
    """把 `assets/cases/<应用>/` 下的用例文件列成「用例库」的条目。

    ## 为什么不再从数据库读（2026-10-27 用户反馈）

    「webui里面case的用例库是空的。应该拿去assets里面的case目录下面找。」

    用例的**唯一真相是文件**，数据库只是"跑过的痕迹"：
      · 用户/AI 加一条用例 —— 就是放一个 .md 或 .yaml 文件
      · 数据库的 cases 表只有"跑过且回写过"的才有记录

    旧实现 `/api/cases` 走 `db.list_cases()`，表空 -> 用例库空，
    但 assets/cases 下明明有 15 个文件。**asset 在、交付面没有。**

    ## 同一条用例的 .md 和 .yaml 怎么合并

    文件名相同、扩展名不同 = 同一条用例：
      · `.md`   人写的原文（一定有）
      · `.yaml` 跑过之后沉淀的结构化描述（可能有）
    一条用例只出一行，`has_yaml` 标记有没有结构化描述。

    ## 运行统计从哪来：按**用例编号**关联

    用例在文件里（`com.zui.calendar_168`），会话在数据库里
    （`联想日历_168 课程表-从图库导入`），两边名字对不上。
    唯一的共同点是**编号 168**。

    所以要分别从两边抽编号再配对：

        文件名  com.zui.calendar_168            -> 168
        会话标题 联想日历_168 课程表-从图库导入   -> 168      ✅ 配上

    编号从哪来：`[_\\-](\\d{2,4})(?!\\d)`。
      · 前面的 `[_\\-]` 防止把 "calendar" 里的数字（没有）或
        长数字串的后四位误当成编号
      · 后面的 `(?!\\d)` 防止 1680 被切出 168

    ⚠️ 这个匹配规则和 `tools/session.py start` 里"提示用 --case 复用"
       用的是同一个正则 —— 两处必须一致，否则统计和复用判断会打架。
    """
    if not CASES_DIR.is_dir():
        return []

    # (应用目录, 标题) -> 该标题下的文件
    found: dict[tuple[str, str], dict[str, Path]] = {}
    for fp in sorted(CASES_DIR.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in (".md", ".yaml"):
            continue
        # 应用目录名 = 相对 CASES_DIR 的第一段
        try:
            rel = fp.relative_to(CASES_DIR)
        except ValueError:
            continue
        app = rel.parts[0] if len(rel.parts) > 1 else ""
        stem = fp.stem
        found.setdefault((app, stem), {})[fp.suffix.lower()] = fp

    # ── 扫所有运行库，按用例编号汇总 ──────────────────────────────
    #
    # 一次遍历拿全：编号 -> {run_count, last_status, last_run_at,
    #                        last_session_id, last_run_id}
    # 不逐条用例查库，避免 15 条用例开 15 × N 个连接。
    by_num: dict[str, dict] = {}
    for uuid, _path in _record_dbs():
        db = _open_db(uuid)
        if db is None:
            continue
        try:
            rows = db._conn().execute(
                "SELECT id, title, status, started_at, finished_at, case_id"
                " FROM sessions WHERE title IS NOT NULL"
                " ORDER BY started_at").fetchall()
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            num = _case_number(str(r["title"] or ""))
            if not num:
                continue
            slot = by_num.setdefault(num, {"run_count": 0})
            slot["run_count"] += 1
            # rows 按 started_at 升序遍历，最后写入的就是最近一次
            slot["last_status"] = r["status"]
            slot["last_run_at"] = r["started_at"]
            slot["last_session_id"] = f"{uuid}:{r['id']}"
            slot["last_run_id"] = uuid

    items: list[dict] = []
    for (app, stem), files in sorted(
            found.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        num = _case_number(stem)
        st = by_num.get(num or "", {})
        # id 用标题（前端只用它做 key / 路由），不用数据库自增 id
        items.append({
            "id": stem,
            "title": stem,
            "package": app,
            "case_number": num,
            "has_md": ".md" in files,
            "has_yaml": ".yaml" in files,
            "run_count": st.get("run_count") or 0,
            "last_status": st.get("last_status"),
            "last_run_at": st.get("last_run_at"),
            "last_session_id": st.get("last_session_id"),
        })
    return items


def _case_detail(item: dict) -> dict:
    """单条用例详情：列表字段 + 用例原文。

    原文优先读 `.md`（人写的），没有 `.md` 就渲 `.yaml` 的 text。
    前端「用例原文」那个页签直接显示 `input` 字段。
    """
    out = dict(item)
    text = ""
    md = _find_spec(item["title"], ".md")
    if md is not None:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        yml = _find_spec(item["title"], ".yaml")
        if yml is not None:
            try:
                text = yml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
    out["input"] = text
    return out


def _module_stats() -> list[dict]:
    """按应用（包名）聚合用例数与结论分布 —— 供 `/api/modules` 和仪表盘用。

    和 `_scan_case_files()` 同源：从 assets/cases 的文件算，不从 cases 表。
    """
    items = _scan_case_files()
    agg: dict[str, dict] = {}
    for it in items:
        pkg = it.get("package") or "未识别"
        a = agg.setdefault(pkg, {
            "package": pkg, "total": 0, "pass": 0, "fail": 0,
            "blocked": 0, "warn": 0, "error": 0, "never_run": 0,
        })
        a["total"] += 1
        st = it.get("last_status")
        if not st:
            a["never_run"] += 1
        elif st in ("PASS", "FAIL", "BLOCKED", "WARN", "ERROR"):
            a[st.lower()] += 1
        else:
            a["never_run"] += 1
    return sorted(agg.values(), key=lambda x: x["package"])


def _case_overview() -> dict:
    """仪表盘顶部的用例总数 + 结论分布 —— 也从文件算。

    用户反馈：「仪表盘用例显示 0」。

    原因和用例库一样：`/api/dashboard` 用的是 `db.case_overview()`，
    它 `COUNT(*) FROM cases` —— 表空就是 0，而 assets/cases 下有 15 个文件。
    """
    items = _scan_case_files()
    ov = {
        "total": len(items),
        "pass": 0, "fail": 0, "blocked": 0, "warn": 0, "error": 0,
        "never_run": 0,
    }
    for it in items:
        st = it.get("last_status")
        key = (st or "").lower()
        if key in ("pass", "fail", "blocked", "warn", "error"):
            ov[key] += 1
        else:
            ov["never_run"] += 1
    return ov

# ── 视觉模型配置（OpenAI 兼容多模态通道，vision.py 消费）────────────
VISION_KEYS = ("base_url", "model", "api_key")


def _load_vision() -> dict:
    conf = {k: "" for k in VISION_KEYS}
    fp = STORAGE_DIR / "vision.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in VISION_KEYS:
                conf[k] = str(data.get(k) or "")
    except (OSError, ValueError):
        pass
    return conf


def _save_vision(conf: dict) -> None:
    STORAGE_DIR.mkdir(exist_ok=True)
    (STORAGE_DIR / "vision.json").write_text(
        json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(s: str) -> str:
    """api_key 脱敏回显：只留首 4 与末 4 位。GET 绝不能返回明文。"""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


# 1x1 透明 PNG：视觉连通性测试用最小图，不依赖本地证据文件
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _test_vision() -> dict:
    """用已保存的配置做一次真实连通性验证（不发真图，只验鉴权+模型可用）。"""
    import base64
    import time
    import urllib.error
    import urllib.request

    conf = _load_vision()
    base = (conf.get("base_url") or "").rstrip("/")
    model = conf.get("model") or ""
    key = conf.get("api_key") or ""
    if not (base and model and key):
        return {"ok": False, "error": "配置不完整（base_url / model / api_key 均必填）"}

    b64 = base64.b64encode(_TINY_PNG).decode()
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "回复两个字：可用"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 20,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        ms = round((time.time() - t0) * 1000)
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "latency_ms": ms, "model": model,
                "reply": str(text)[:200],
                "usage": data.get("usage"),
                "hint": "连通成功：鉴权与模型均可用"}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"HTTP {e.code} {e.reason}", "detail": detail,
                "hint": "401/403 多为 api_key 错误；404 多为 base_url 少了或多了 /v1"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "网络不可达或 base_url 写错（需含 http(s):// 与 /v1 前缀，视服务而定）"}

MIME = {".html": "text/html", ".js": "application/javascript",
        ".css": "text/css", ".png": "image/png", ".xml": "text/xml",
        ".json": "application/json", ".md": "text/markdown", ".log": "text/plain"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _static(self, relpath: str):
        fp = (WEBUI_DIR / relpath).resolve()
        # is_relative_to 而非 startswith：字符串前缀会放过同前缀兄弟目录
        if not fp.is_relative_to(WEBUI_DIR) or not fp.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, fp.read_bytes(), MIME.get(fp.suffix, "application/octet-stream"))

    def _replay(self, sid):
        """回放数据：把会话里有截图的事件串成"帧"。

        每帧 = {evidence(图片), x/y(点击坐标，仅动作有), size(原图尺寸),
                kind, action, via, why, detail, ts}

        ⚠️ **坐标换算交给前端按百分比做**：这里给的是**原图坐标系**的 x/y 和
        size，前端用 `left = x/size_w*100%` 定位 —— 这样图片无论被 CSS
        缩放成多大，圆点都不会偏。
        """
        s = session_detail(sid)
        if not s:
            return self._json({"error": "not found"}, 404)

        frames = []
        for e in s.get("events") or []:
            ev = e.get("evidence")
            if not ev:
                continue
            try:
                j = json.loads(e.get("data_json") or "{}")
            except ValueError:
                j = {}
            act = j.get("action") or {}
            size = j.get("size") or []
            # action 字段可能没存（历史数据），从 detail 首段回退（如"点击 (x,y) via ..."）
            action = act.get("action") or ""
            if not action and e.get("detail"):
                action = str(e["detail"]).split(" ")[0]
            frames.append({
                "seq": e.get("seq"),
                "kind": e.get("kind"),
                "evidence": ev,
                "x": act.get("x"),
                "y": act.get("y"),
                "size": size if len(size) == 2 else None,
                "action": action,
                "via": act.get("via") or "",
                "why": act.get("why") or "",
                "detail": e.get("detail") or "",
                # 图还在不在（被 cleanup 轮转/误删后为 False）
                "ok": (ROOT / ev).is_file(),
            })

        missing = sum(1 for f in frames if not f["ok"])
        return self._json({
            "session_id": sid,
            "title": s.get("title") or f"会话 #{sid}",
            "status": s.get("status"),
            "frames": frames,
            "total": len(frames),
            "missing": missing,
            # 一张可用的图都没有 → 前端把回放入口置灰
            "playable": any(f["ok"] for f in frames),
        })

    def _storage_file(self, relpath: str):
        """伺服 storage 下的证据文件。

        ⚠️ 事件流里的 evidence 字段来自 common.rel()，是**项目根相对**路径
        （形如 `storage/evidence/.../shot.png`），而本函数历史上按
        **storage 相对**解析 → 拼成 storage/storage/... 恒 404（真实事故：
        记录里所有截图全空白）。故先按 storage 相对试，未命中再按项目根相对试。
        """
        candidates = [STORAGE_DIR / relpath]
        p = Path(relpath)
        if p.parts and p.parts[0] == STORAGE_DIR.name:
            candidates.append(ROOT / p)
        for fp in candidates:
            fp = fp.resolve()
            # is_relative_to 而非 startswith：字符串前缀会放过同前缀兄弟目录
            if fp.is_relative_to(STORAGE_DIR) and fp.is_file():
                return self._send(200, fp.read_bytes(),
                                  MIME.get(fp.suffix, "application/octet-stream"))
        return self._json({"error": "not found"}, 404)

    # ── GET ─────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/files/"):
            return self._storage_file(urllib.parse.unquote(path[len("/files/"):]))

        if path == "/api/sessions":
            # 默认只看正式测试记录；?all=1 追加**验证/调试**会话。
            # ⚠️ mock（占位/构造数据）**永不出现**——与 session_kind_counts()
            # 口径一致，否则"显示全部"会漏出 mock 而计数里又没有它。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            show_all = qs.get("all", ["0"])[0] in ("1", "true")
            # ★ 跨所有 uuid 运行库聚合（一次运行一个库，见文件头注释）
            sessions = all_sessions(
                include=("test", "diag") if show_all else None,
                kind=None if show_all else "test")
            return self._json({"sessions": sessions,
                               "stats": session_stats_all(),
                               "kinds": session_kind_counts_all()})

        if path == "/api/vision":
            conf = _load_vision()
            conf["api_key_masked"] = _mask(conf.pop("api_key"))
            return self._json(conf)

        if path == "/api/modules":
            # ★ 模块分组也从文件算（和用例库同源）
            #
            #   原来走 `db.module_stats()`，它只看 cases 表 —— 表空就没有分组，
            #   前端顶部的"全部 / com.zui.calendar 15"标签栏整条消失。
            return self._json({"modules": _module_stats()})

        if path == "/api/dashboard":
            # ★ overview/modules 从文件算；recent/sessions 从所有 uuid 库聚合
            return self._json({"overview": _case_overview(),
                               "modules": _module_stats(),
                               "recent": run_stats_all(3),
                               "sessions": session_stats_all()})

        if path == "/api/cases":
            # ★ 用例库 = `assets/cases/` 下的文件，不是数据库
            #
            #   用户反馈：「webui里面case的用例库是空的。
            #              应该拿去assets里面的case目录下面找。」
            #   数据库只是"跑过的痕迹"，文件才是用例本身。
            return self._json({"items": _scan_case_files()})

        m = re.match(r"^/api/cases/([^/]+)$", path)
        if m:
            # id 现在是标题（文件名 stem），不再是数据库自增 id。
            # 先在文件里找；找不到再退回数据库（兼容旧的数字 id 链接）。
            key = urllib.parse.unquote(m.group(1))
            for item in _scan_case_files():
                if str(item["id"]) == key or item["title"] == key:
                    return self._json(_case_detail(item))
            if key.isdigit():
                c = get_db().get_case(int(key))
                if c:
                    return self._json(c)
            return self._json({"error": "not found"}, 404)

        # session id 现在是 `<uuid>:<local_id>`（也接受旧的纯数字）
        m = re.match(r"^/api/sessions/([^/]+)/replay$", path)
        if m:
            return self._replay(urllib.parse.unquote(m.group(1)))

        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m:
            s = session_detail(urllib.parse.unquote(m.group(1)))
            return self._json(s if s else {"error": "not found"},
                              200 if s else 404)

        if path == "/api/knowledge":
            items = []
            if KNOWLEDGE_DIR.is_dir():
                for fp in sorted(KNOWLEDGE_DIR.glob("*.md")):
                    st = fp.stat()
                    items.append({"name": fp.name, "size": st.st_size,
                                  "mtime": datetime.fromtimestamp(st.st_mtime)
                                  .isoformat(timespec="seconds")})
            return self._json({"items": items})

        m = re.match(r"^/api/knowledge/(.+\.md)$", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            if not MD_NAME_RE.match(name):
                return self._json({"error": "bad name"}, 400)
            fp = KNOWLEDGE_DIR / name
            if not fp.is_file():
                return self._json({"error": "not found"}, 404)
            return self._json({"name": name, "content": fp.read_text(encoding="utf-8")})

        # ── 用例程序化描述（storage/cases/*.yaml，MD 为渲染产物）──
        if path == "/api/case-specs":
            items = []
            for fp in sorted(CASES_DIR.rglob("*.yaml")):
                st = fp.stat()
                title = fp.stem
                cid = None
                try:
                    import yaml as _y
                    cid = ((_y.safe_load(fp.read_text(encoding="utf-8")) or {})
                           .get("meta") or {}).get("case_id")
                except Exception:  # noqa: BLE001
                    pass
                md = fp.with_suffix(".md")
                items.append({
                    "name": fp.name, "title": title, "case_id": cid,
                    "size": st.st_size,
                    "has_md": md.is_file(),
                    "mtime": datetime.fromtimestamp(st.st_mtime)
                    .isoformat(timespec="seconds"),
                })
            return self._json({"items": items})

        # 渲染后的 MD（只读视图）
        m = re.match(r"^/api/case-specs/(.+)/rendered$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            md = _find_spec(title, ".md") or (CASES_DIR / f"{title}.md")
            if not md.is_file():
                return self._json({"error": "not rendered yet"}, 404)
            return self._json({"name": md.name, "content": md.read_text(encoding="utf-8")})

        # YAML 源（可编辑）
        m = re.match(r"^/api/case-specs/(.+)$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            fp = _find_spec(title, ".yaml") or (CASES_DIR / f"{title}.yaml")
            if not fp.is_file():
                return self._json({"error": "not found"}, 404)
            return self._json({"name": fp.name,
                               "content": fp.read_text(encoding="utf-8")})

        return self._json({"error": "not found"}, 404)

    # ── POST ────────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/vision":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            conf = _load_vision()
            for k in VISION_KEYS:
                v = str(data.get(k) or "").strip()
                # api_key 留空 = 保留原值（避免只想改 model 却清掉密钥）
                if v or not (k == "api_key" and conf["api_key"]):
                    conf[k] = v
            _save_vision(conf)
            return self._json({"ok": True})

        if path == "/api/vision/test":
            return self._json(_test_vision())

        m = re.match(r"^/api/cases/(\d+)/kind$", path)
        if m:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            ok = get_db().set_case_kind(int(m.group(1)), str(data.get("kind") or ""))
            return self._json({"ok": ok}, 200 if ok else 400)

        # ⚠️ session id 是 `<uuid>:<local_id>` 复合串（含冒号），
        #    原来写成 (\d+) 只匹配纯数字，导致带 uuid 的 id 全部 404
        #    —— 这就是「删除功能失效」的原因（2026-10-28 修）。
        m = re.match(r"^/api/sessions/([^/]+)/kind$", path)
        if m:
            _uuid, sid = _split_sid(urllib.parse.unquote(m.group(1)))
            _db = _open_db(_uuid) or get_db()
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            ok = _db.set_session_kind(sid, str(data.get("kind") or ""))
            return self._json({"ok": ok}, 200 if ok else 400)

        if path == "/api/cases":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            title = str(data.get("title") or "").strip()
            inp = str(data.get("input") or "").strip()
            if not (title and inp):
                return self._json({"ok": False,
                                   "error": "标题与用例原文均必填"}, 400)
            cid = get_db().add_case(title, inp,
                                    str(data.get("package") or "").strip() or None)
            return self._json({"ok": True, "case_id": cid})

        m = re.match(r"^/api/cases/(\d+)$", path)
        if m:
            # 用例**只读**：case.input 与同名 YAML 是同一份用例的两种形态，
            # 只改一个会让两者静默不一致（"合法但对不上"）。
            # 正解是 改原文 → 重跑 → 重新沉淀 YAML，该流程在 WebUI 之外完成。
            return self._json({
                "error": "用例为只读，不支持通过 WebUI 修改。"
                         "如需调整请改用例文件后重跑，再重新沉淀程序化描述。",
                "readonly": True,
            }, 403)

        m = re.match(r"^/api/case-specs/(.+)$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            # 用例程序化描述在本阶段**只读**：YAML 由"已验证成功的会话"沉淀生成，
            # 手工改动会破坏它与证据/知识卡的对应关系。
            # 前端不提供编辑入口；服务端这里也一并拒绝，避免绕过前端直接改。
            return self._json({
                "error": "用例程序化描述为只读，不支持通过 WebUI 修改。"
                         "如需调整请直接编辑文件并重新渲染。",
                "readonly": True,
            }, 403)

        m = re.match(r"^/api/knowledge/(.+\.md)$", path)
        if not m:
            return self._json({"error": "not found"}, 404)
        name = urllib.parse.unquote(m.group(1))
        if not MD_NAME_RE.match(name):
            return self._json({"error": "bad name"}, 400)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            content = data["content"]
            if not isinstance(content, str):
                raise ValueError
        except (ValueError, KeyError):
            return self._json({"error": "bad body"}, 400)
        KNOWLEDGE_DIR.mkdir(exist_ok=True)
        (KNOWLEDGE_DIR / name).write_text(content, encoding="utf-8")
        return self._json({"ok": True})

    def do_DELETE(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/cases/(\d+)$", path)
        if m:
            # 级联删除：用例 + 全部历史测试记录（数据库与磁盘证据）+ 程序化描述文件。
            # 不可恢复，前端已做二次确认。
            return self._json(get_db().delete_case_cascade(int(m.group(1))))
        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m:
            # 默认连带删除磁盘证据；?evidence=0 只删库记录
            keep_ev = qs.get("evidence", ["1"])[0] in ("0", "false")
            # ⚠️ id 是 `<uuid>:<local_id>`，要拆开去对应的运行库里删
            _uuid, sid = _split_sid(urllib.parse.unquote(m.group(1)))
            _db = _open_db(_uuid) or get_db()
            r = _db.delete_session(sid, remove_evidence=not keep_ev)
            return self._json(r, 200 if r.get("ok") else 404)
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):  # 静默访问日志
        pass


def main():
    ap = argparse.ArgumentParser(description="Android AI 测试台")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠️ 警告：绑定非回环地址——Web UI 无鉴权，知识库 POST 接口可被"
              "同网段任意改写，仅限可信内网短时使用！")
    print(f"测试台: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
