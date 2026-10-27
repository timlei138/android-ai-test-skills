"""Measured stages and invocation identity; no device or model policy."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import time
import uuid
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.environ.get("ANDROID_TEST_STORAGE", str(ROOT / "storage"))).resolve()

# ══════════════════════════════════════════════════════════════════════
# 目录约定（2026-10-27 重构）
# ══════════════════════════════════════════════════════════════════════
#
#   assets/                      ← **进 git**，是资产不是产物
#     cases/<应用>/              ← 按应用分目录
#       <标题>.md                ← 原始用例（人写的，或从需求文档粘的）
#       <标题>.yaml              ← 跑过之后生成的结构化用例（AI 读这个）
#     knowledge/<包名>.md        ← 知识卡 + paths/<包名>.md 路径图
#
#   storage/                     ← **不进 git**，纯本地运行产物
#     record/<uuid>/             ← 每次运行一个目录，互不干扰
#       db.sqlite3               ← 该次的会话库
#       evidence/<步号>/         ← 截图 + dump + meta
#       calls/                   ← 调用账本
#       timing/
#       recording_errors/
#     tmp/                       ← Agent 的临时文件（探针、一次性脚本、草稿）
#
# 为什么 record/ 下面按 uuid 分：
#   原来所有运行共用一个 `storage/sessions.db`（7.6MB）+ 全局 evidence/（8513 文件）。
#   跑多了会互相污染：清理一次影响全部历史，查"这次到底点了什么"要按 session_id 过滤。
#   改成一次运行一个目录后，删一次 = 删一个 uuid 目录，不影响别的。
#
# 为什么 assets 和 storage 分开：
#   用户：「storage 的数据不应该提交。只是本地运行的一些东西。」
#   而用例和知识卡是**要提交的资产** —— 它们决定"下次怎么跑得更快"。

ASSETS_DIR = Path(os.environ.get("ANDROID_TEST_ASSETS", str(ROOT / "assets"))).resolve()
CASES_DIR = ASSETS_DIR / "cases"
# ★ 2026-10-27：知识卡也搬到 assets/ 下（用户要求"把 knowledge 也放到 assets 下面"）。
#
#   为什么它该在 assets 而不在根目录：
#     知识卡是**资产**（进 git、跨设备跨会话复用、决定下次跑得更快），
#     和用例是同一类东西。放根目录会让人以为是代码的一部分。
#
#   新位置：assets/knowledge/<包名>.md + assets/knowledge/paths/<包名>.md
KNOWLEDGE_DIR = Path(os.environ.get(
    "ANDROID_TEST_KNOWLEDGE", str(ASSETS_DIR / "knowledge"))).resolve()
PATHS_DIR = KNOWLEDGE_DIR / "paths"
RECORD_DIR = STORAGE_DIR / "record"
TMP_DIR = STORAGE_DIR / "tmp"

CALL_ID = os.environ.get("ANDROID_TEST_CALL_ID") or uuid.uuid4().hex
PROCESS_STARTED = time.perf_counter()
STAGES: dict[str, float] = {}
EVENT_IDS: list[int] = []
EXECUTION_STATE = "unknown"
P = ParamSpec("P")
T = TypeVar("T")


def current_record_dir() -> Path | None:
    """本次运行属于哪个 record 目录？

    两个来源，环境变量优先：

      ① `ANDROID_TEST_RECORD` —— 同一个进程内（session.py 内部）传播快
      ② `storage/record/.current` 磁盘指针 —— 跨进程用这个

    ⚠️ 为什么必须有两个：observe/act 是独立进程，
       环境变量传不过去。只靠 ① 的话，一次运行的产物会散在
       record/<uuid>/ 和老的 storage/evidence/ 两处。
    """
    raw = (os.environ.get("ANDROID_TEST_RECORD") or "").strip()
    if raw:
        p = Path(raw).resolve()
        if p.is_dir():
            return p
    return _pinned_record()


def new_record_dir() -> Path:
    """建一个新的运行目录（uuid），返回路径。"""
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    d = RECORD_DIR / uuid.uuid4().hex[:12]
    for sub in ("evidence", "calls", "timing", "recording_errors"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def ensure_tmp() -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR


# ══════════════════════════════════════════════════════════════════════
# 环境探测：告诉 AI 这台机器上该用什么 shell 语法
# ══════════════════════════════════════════════════════════════════════
#
# 用户的要求（原话）：
#   「skills 应该能够根据当前用户的设备情况。自动调整命令的格式和写法。
#     而不是我们强制限定死。」
#
# 所以不是把 SKILL 写成"Windows 用 PowerShell"（那在 Linux 上就错了），
# 而是**在会话开始时探测一次，把结论告诉 AI**。
#
# 实测数据：72 条 bash 命令里有 8 条 Linux 写法（11%）——
# 那些是在 Windows 上跑的，其中 `ls -la` / `2>/dev/null` / `||` 会失败。
# 但同样的写法在 Linux 上是对的，所以不能一刀切。


def detect_shell() -> dict:
    """探测当前环境，返回写命令时该用的语法。

    返回：
        {
          "platform": "win32" | "linux" | "darwin" | ...,
          "shell": "powershell" | "cmd" | "bash" | "zsh" | "unknown",
          "hint": "给 AI 看的一句话说明",
          "path_sep": "\\\\" | "/",
          "tmp_dir": "..."        ← 临时目录的真实路径
        }

    ★ 判定顺序很重要：
      ① 有 SHELL 环境变量且含 bash/zsh  -> 那才是真 shell（Git Bash/WSL）
      ② 否则看 sys.platform
         win32 -> powershell（现代 Windows 的默认）
      ③ 拿不准就返回 unknown，让 AI 自己试
    """
    import os as _os
    import sys as _sys
    import tempfile

    plat = _sys.platform
    shell_env = (_os.environ.get("SHELL") or "").strip()
    comspec = (_os.environ.get("ComSpec") or "").strip()

    shell = "unknown"
    hint = ""

    if "bash" in shell_env:
        shell = "bash"
        hint = ("当前是 bash：`ls -la` / `head -20` / `&&` / `2>/dev/null` / "
                "`/tmp` 都能用。")
    elif "zsh" in shell_env:
        shell = "zsh"
        hint = "当前是 zsh：bash 的写法基本都能用。"
    elif plat == "win32":
        # Windows 上还要看有没有 Git Bash / WSL 的 bash
        shell = "powershell" if comspec or True else "cmd"
        hint = ("当前是 Windows（PowerShell）。`ls -la` / `head` / `&&` / "
                "`2>/dev/null` 会失败 —— 用 `dir` / `Get-Content -TotalCount` / "
                "`;` / `2>$null`。**但 `adb shell \"...\"` 里的内容仍是 "
                "Android 的 Linux，照常用 grep 等**。")
    elif plat in ("linux", "darwin"):
        shell = "bash"
        hint = "当前是 Linux/macOS：bash 写法都能用。"

    return {
        "platform": plat,
        "shell": shell,
        "path_sep": "\\" if plat == "win32" else "/",
        "tmp_dir": tempfile.gettempdir(),
        "hint": hint,
    }


# ══════════════════════════════════════════════════════════════════════
# "当前 record 目录" 的磁盘指针
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ 环境变量不够用。observe.py / act.py 是**独立进程**，
#    session.py 在自己进程里设的 ANDROID_TEST_RECORD 传不过去。
#
#    实测后果（第一版）：session start 建出 record/<uuid>/ 目录，
#    但紧接着的 observe 拿不到环境变量 -> 又落回 storage/evidence/，
#    一次运行的东西散在两个地方。
#
#    所以除了环境变量，还要写一个磁盘指针：storage/record/.current
#    后续进程启动时读它。

_CURRENT_POINTER = STORAGE_DIR / "record" / ".current"


def pin_record(d: Path) -> None:
    """把"当前运行目录"写进磁盘，供后续独立进程读取。"""
    d = Path(d).resolve()
    d.mkdir(parents=True, exist_ok=True)
    _CURRENT_POINTER.parent.mkdir(parents=True, exist_ok=True)
    _CURRENT_POINTER.write_text(str(d), encoding="utf-8")
    os.environ["ANDROID_TEST_RECORD"] = str(d)


def unpin_record() -> None:
    """会话结束后清掉指针（不然下一次会误接到旧目录）。"""
    try:
        _CURRENT_POINTER.unlink(missing_ok=True)
    except OSError:
        pass
    os.environ.pop("ANDROID_TEST_RECORD", None)


def _pinned_record() -> Path | None:
    try:
        txt = _CURRENT_POINTER.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not txt:
        return None
    p = Path(txt).resolve()
    return p if p.is_dir() else None


@contextmanager
def stage(name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        STAGES[name] = STAGES.get(name, 0.0) + (time.perf_counter() - started) * 1000


def timing_snapshot() -> dict:
    return {
        "call_id": CALL_ID,
        "measured_at": datetime.now().isoformat(timespec="milliseconds"),
        "process_observed_ms": round((time.perf_counter() - PROCESS_STARTED) * 1000, 3),
        "stages_ms": {name: round(value, 3) for name, value in STAGES.items()},
        "scope": "since runtime import; startup and earlier imports require launcher timing",
    }


def timed(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with stage(name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


def timed_call(name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    with stage(name):
        return function(*args, **kwargs)


def execution(state: str) -> None:
    global EXECUTION_STATE
    EXECUTION_STATE = state


def call_path(storage: Path, session_id: int | None, call_id: str) -> Path:
    return storage / "calls" / (str(session_id) if session_id is not None else "unbound") / f"{call_id}.json"


def invocation_paths(storage: Path, session_id: int | None = None):
    root = storage / "calls"
    root.mkdir(parents=True, exist_ok=True)
    migrated = root / ".partitioned"
    if not migrated.exists():
        for path in root.glob("*.json"):
            try:
                call = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(call, dict):
                    raise ValueError(f"Invalid invocation record: {path}")
                destination = call_path(storage, call.get("session_id"), path.stem)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f"Duplicate invocation record: {path}")
                path.rename(destination)
            except FileNotFoundError:
                if path.exists():
                    raise
        migrated.touch()
    if session_id is not None:
        return (root / str(session_id)).glob("*.json")
    return root.glob("*/*.json")


def recording_issues(storage: Path, session_id: int) -> list[str]:
    """本会话是否留下了需要人工裁决的"悬空动作"。

    ⚠️ 只锁定"可能已执行"的调用。用法错误（参数不对、工具入口即拒绝）
    确定没碰设备，不构成悬空动作 —— 否则一次手误就要作废整个会话。
    区分见 run.py:is_usage_error 与 audit 记录里的 executed 字段。
    """
    issues = []
    errors = storage / "recording_errors" / str(session_id)
    if errors.exists() and any(errors.iterdir()):
        issues.append("recording failure marker exists")
    for path in invocation_paths(storage, session_id):
        call = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(call, dict):
            raise ValueError(f"Invalid invocation record: {path}")
        audit = call.get("audit", {})
        if not isinstance(audit, dict):
            raise ValueError(f"Invalid invocation audit: {path}")
        if call.get("session_id") != session_id or not call.get("expects_event"):
            continue
        if call.get("call_id") == CALL_ID:
            continue
        # 明确未执行的调用放行（其动作没有发生，无需裁决）
        if audit.get("executed") is False:
            continue
        if call.get("status") == "started" or not audit.get("complete", False):
            issues.append(f"incomplete invocation: {call.get('call_id')}")
    return issues
