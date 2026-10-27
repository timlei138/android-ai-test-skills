# -*- coding: utf-8 -*-
"""bootstrap：把工具层拷到当前工作目录，之后一切走工作目录。

## 用户的要求（原话）

「安装完 SKILLS 后和 preset 都分享给其他用户后第一次运行检查运行环境
  把 skills 里面的内容拷贝一份到工作目录是不是可行呢？然后后期走工作目录。
  但是要求主和子 Agent 不能修改代码。这样是不是可以。
  以后都走工作目录下的代码和位置。」

## 为什么需要它

三个位置互相分离，是这半个月所有"路径问题"的根源：

    安装位置  C:\\Users\\<user>\\.agents\\skills\\android-gui-test\\  （DSH 装的）
    工具层    D:\\Project\\deepseek\\android-ai-test-skills\\        （可能不在沙箱内）
    产物      <cwd>\\storage\\                                     （沙箱内）

DSH 的沙箱边界是 workspace（比如 D:\\dsh），而工具层在 D:\\Project，
所以每次调用都要 ANDROID_TEST_STORAGE 桥接 —— 传漏一次就 WinError 5，
表现还特别像"工具坏了"。

拷到工作目录后：

    <workspace>\\android-ai-test\\        <- 一个自包含目录
        tools\\  assets\\  storage\\

    · 全在 workspace 内，沙箱天然允许
    · 不需要任何环境变量
    · 分享 = 拷这个目录

## 用法

    python bootstrap.py              # 检查，没有就拷
    python bootstrap.py --check      # 只检查，不拷
    python bootstrap.py --force      # 覆盖已存在的

## 幂等

已经有 <cwd>\\android-ai-test\\tools\\env.py 就跳过。
除非 --force。

## 设计取舍：为什么拷"代码 + 资产"但不拷 storage

    tools\\    拷  —— 执行要用
    assets\\   拷  —— 用例和知识卡是**资产**，跟着走
    storage\\  不拷 —— 那是**产物**，新环境应该从空开始

## 为什么拷完要把 tools\\ 设只读

用户要求「主和子 Agent 不能修改代码」。

`tool-fs` 的 `write:false` 只挡了文件工具，**挡不住 shell**：

    Set-Content tools\\act.py "..."      # 能写
    Remove-Item tools\\ -Recurse         # 能删

给 .py 加只读属性后，shell 里直接写会被系统拒绝（要显式去掉属性才能改，
那一步就是明显的、可被察觉的动作）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── UTF-8 兜底（Windows 控制台默认 GBK，print emoji 会崩）────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# 工作目录下用这个目录名
WORKSPACE_DIRNAME = "android-ai-test"

# 要拷的东西（相对安装根）
COPY_DIRS = ("tools", "assets", "docs", "scripts", "webui")
COPY_FILES = ("webui.py", "SKILL.md", "README.md", "requirements.txt",
              "requirements-dev.txt")

# 绝对不拷
SKIP_NAMES = {
    ".venv", "__pycache__", ".git", "storage", "_archived-storage",
    "_archived-skills", ".pytest_cache", ".mypy_cache",
}


def _is_user_code(path: Path) -> bool:
    """这个文件是要保护的"代码"吗？

    只保护会执行的东西：.py / .ps1 / .cmd / .bat。
    用例和知识卡（assets/）**不保护** —— AI 跑完要回写经验到那里。
    """
    return path.suffix.lower() in (".py", ".ps1", ".cmd", ".bat")


def find_install_root() -> Path | None:
    """安装位置在哪？

    优先级：
      ① $ANDROID_TEST_HOME（显式指定）
      ② 本文件所在目录的父目录（bootstrap.py 就在安装根的 tools/ 下）
      ③ ~/.agents/skills/android-gui-test（skill 的真实位置）

    ⚠️ ② 优先于 ③ 是有意的：从安装位置跑 bootstrap 时（正常情况）
       ② 就是答案；从别处跑（比如旧副本）时才靠 ③ 找回去。
       反过来的话，在旧副本里跑 bootstrap 会"回到"安装位置，
       那用户拿到的还是旧代码。
    """
    env = (os.environ.get("ANDROID_TEST_HOME") or "").strip()
    if env:
        p = Path(env).resolve()
        if (p / "tools" / "env.py").is_file():
            return p

    here = Path(__file__).resolve().parent.parent
    if (here / "tools" / "env.py").is_file():
        return here

    skill = Path.home() / ".agents" / "skills" / "android-gui-test"
    if (skill / "tools" / "env.py").is_file():
        return skill.resolve()

    return None


def target_root(cwd: Path | None = None) -> Path:
    """工具层副本该建在哪？

    ## 优先级

      ① `ANDROID_TEST_WORKSPACE` 环境变量  —— 明确指定，最可靠
      ② 显式传入的 cwd 参数
      ③ 当前工作目录

    ## 为什么需要 ①（踩过）

    实测：在 `D:\\dsh\\langgraph-poc>` 下跑了一次 bootstrap，
    于是副本建到了 `D:\\dsh\\langgraph-poc\\android-ai-test\\` ——
    那是**废弃的旧项目目录**，产物落在那里没有任何意义。

    再在 `D:\\dsh>` 下跑一次，又建了一份。结果两份副本、两个 venv，
    谁也说不清 AI 用的是哪个。

    **所以要让"建在哪"能被钉死**，而不是靠"你碰巧在哪个目录开会话"。
    推荐做法：第一次就设好

        $env:ANDROID_TEST_WORKSPACE = "D:\\dsh"

    之后在任何目录跑 bootstrap，落点都是 `D:\\dsh\\android-ai-test\\`。
    """
    explicit = (os.environ.get("ANDROID_TEST_WORKSPACE") or "").strip()
    if explicit:
        return Path(explicit).resolve() / WORKSPACE_DIRNAME
    return (cwd or Path.cwd()).resolve() / WORKSPACE_DIRNAME


def is_installed(root: Path | None = None) -> bool:
    r = root or target_root()
    return (r / "tools" / "env.py").is_file()


def _copytree(src: Path, dst: Path) -> int:
    """拷一个目录，跳过 SKIP_NAMES 里的东西。返回拷了多少文件。"""
    n = 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        # 路径里任何一段命中 SKIP 就跳过
        if any(part in SKIP_NAMES for part in rel.parts):
            continue
        if item.is_dir():
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, out)
        n += 1
    return n


def copy_layer(install: Path, dest: Path) -> tuple[int, int]:
    """把 install 拷到 dest。返回 (文件数, 字节数)。"""
    files = 0
    total = 0

    for d in COPY_DIRS:
        src = install / d
        if src.is_dir():
            files += _copytree(src, dest / d)

    for f in COPY_FILES:
        src = install / f
        if src.is_file():
            out = dest / f
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            files += 1

    # bunny: 统计总字节
    for p in dest.rglob("*"):
        if p.is_file():
            total += p.stat().st_size

    return files, total


def lock_code(dest: Path) -> tuple[int, int]:
    """把代码设只读。返回 (成功数, 失败数)。

    只锁 .py/.ps1/.cmd/.bat —— 用例和知识卡要能被回写。
    """
    good = 0
    bad = 0
    for p in (dest / "tools").rglob("*"):
        if not p.is_file() or not _is_user_code(p):
            continue
        try:
            p.chmod(0o444)
            good += 1
        except OSError:
            bad += 1
    # 顶层脚本也锁
    for name in COPY_FILES:
        if not name.endswith(".py"):
            continue
        p = dest / name
        if p.is_file():
            try:
                p.chmod(0o444)
                good += 1
            except OSError:
                bad += 1
    return good, bad


def unlock_code(dest: Path) -> int:
    """取掉只读（--force 覆盖前要用）。"""
    n = 0
    for p in list(dest.rglob("*.py")) + list(dest.rglob("*.ps1")):
        try:
            p.chmod(0o644)
            n += 1
        except OSError:
            pass
    return n


# ══════════════════════════════════════════════════════════════════════
# venv：在工作目录里重建 python 环境
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ **venv 不能拷贝**。
#
#   第一条实现我试过"把安装位置的 .venv 一起拷过去" —— 不行：
#   venv 里的 python.exe 是个壳，pyvenv.cfg 里写的是**绝对路径**
#   （home = ...\uv\python\cpython-3.12.14-windows-x86_64-none）。
#   拷到别的目录/别的机器，那个路径不存在，整个环境就废了。
#
#   所以必须在目标机用 `python -m venv` 重建，再 pip install。
#
# 另一个坑：`python` 命令可能指向很旧的版本。
#   实测这台机器：C:\Program Files\python\3.11\python.exe 里其实是 3.13.3
#   （目录名骗人），而且没装任何依赖 —— 子 Agent 敲 `python tools/act.py`
#   就会 ImportError。

MIN_PY = (3, 10)


def find_base_python() -> tuple[str | None, str]:
    """找一个 >= 3.10 的 python 来建 venv。返回 (命令, 版本说明)。"""
    explicit = (os.environ.get("ANDROID_TEST_PYTHON") or "").strip()
    candidates: list[list[str]] = []
    if explicit:
        candidates.append([explicit])

    # Windows launcher：能精确挑版本
    if os.name == "nt":
        for v in ("3.12", "3.13", "3.11", "3.10"):
            candidates.append(["py", f"-{v}"])

    candidates += [["python"], ["python3"]]

    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd + ["-c",
                       "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        ver = (r.stdout or "").strip()
        parts = ver.split(".")
        if len(parts) < 2:
            continue
        try:
            major, minor = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if (major, minor) >= MIN_PY:
            return " ".join(cmd), ver

    return None, ""


def venv_python(dest: Path) -> Path:
    """这个目录下 venv 的解释器路径（不管存不存在）。"""
    venv_dir = dest / ".venv"
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


DEPS = ("uiautomator2", "adbutils", "yaml", "PIL")


def check_venv(dest: Path) -> dict:
    """**只检查**，不安装。返回状态 + 一条安装命令。

    为什么单独一个函数：pip install 要拉 200MB+、几分钟起步。
    放进 bootstrap 会让"第一次跑"变成"等十分钟不知道在干嘛"。
    用户原话：「告诉用户如何安装吧…太慢了。自己装也没这么慢」
    """
    py = venv_python(dest)
    install_cmd = (
        f'cd "{dest}"'
        + ("\n" if os.name == "nt" else "\n")
        + ('python -m venv .venv\n'
           '.venv\\Scripts\\pip install -r requirements.txt'
           if os.name == "nt" else
           'python3 -m venv .venv\n'
           '.venv/bin/pip install -r requirements.txt'))

    if not py.is_file():
        return {
            "status": "missing",
            "message": "工作目录下还没有 python 环境（.venv）",
            "install_cmd": install_cmd,
            "note": "要装 uiautomator2 / rapidocr / onnxruntime / opencv，"
                    "约 200MB，第一次几分钟。也可以让 AI 带 --with-venv 装。",
        }

    # 有 venv，查依赖
    code = (
        "import importlib,sys\n"
        "miss=[m for m in %r if not __import__('importlib').util.find_spec(m)]\n"
        "print('MISSING:'+','.join(miss))\n" % (list(DEPS),)
    )
    try:
        r = subprocess.run([str(py), "-c", code], capture_output=True,
                           text=True, timeout=60, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "broken", "python": str(py),
                "message": str(exc)[:200], "install_cmd": install_cmd}

    miss = ""
    for ln in out.splitlines():
        if ln.startswith("MISSING:"):
            miss = ln[8:].strip()

    if not miss:
        return {"status": "ok", "python": str(py)}
    return {
        "status": "incomplete",
        "python": str(py),
        "missing": miss,
        "message": f"venv 在，但缺依赖：{miss}",
        "install_cmd": (f'"{py}" -m pip install -r "{dest / "requirements.txt"}"'),
    }


def ensure_venv(dest: Path, *, quiet: bool = False) -> dict:
    """在 dest 下建 .venv 并装 requirements.txt。**慢**（几分钟），只在明确要求时调。"""
    venv_dir = dest / ".venv"
    venv_py = venv_python(dest)

    # 已经有了就跳过（幂等）
    if venv_py.is_file():
        return {"status": "exists", "python": str(venv_py)}

    base, ver = find_base_python()
    if base is None:
        return {
            "status": "no-base-python",
            "message": f"找不到 >= {MIN_PY[0]}.{MIN_PY[1]} 的 python。"
                       f" 装一个，或设 ANDROID_TEST_PYTHON 指向它",
        }

    # 建 venv
    try:
        r = subprocess.run(
            base.split() + ["-m", "venv", str(venv_dir)],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "venv-failed", "message": str(exc)}

    if r.returncode != 0 or not venv_py.is_file():
        return {
            "status": "venv-failed",
            "message": ((r.stderr or "") + (r.stdout or ""))[:400],
        }

    # 装依赖
    req = dest / "requirements.txt"
    if not req.is_file():
        return {"status": "ok-no-req", "python": str(venv_py),
                "base": base, "base_version": ver}

    try:
        r2 = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "-r", str(req)],
            capture_output=True, text=True, timeout=1800,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "pip-failed", "python": str(venv_py),
                "message": str(exc)[:400]}

    if r2.returncode != 0:
        return {
            "status": "pip-failed",
            "python": str(venv_py),
            "message": ((r2.stderr or "") + (r2.stdout or ""))[-600:],
        }

    # 自检：真能 import 吗
    check = ("import uiautomator2, adbutils, yaml, PIL; print('ok')")
    r3 = subprocess.run([str(venv_py), "-c", check],
                        capture_output=True, text=True, timeout=120,
                        encoding="utf-8", errors="replace")
    ok = r3.returncode == 0 and "ok" in (r3.stdout or "")

    return {
        "status": "ok" if ok else "import-failed",
        "python": str(venv_py),
        "base": base,
        "base_version": ver,
        "message": "" if ok else ((r3.stderr or "") + (r3.stdout or ""))[:400],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 Android 测试工具层拷到当前工作目录")
    ap.add_argument("--check", action="store_true",
                    help="只检查，不拷贝")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的（会先解锁再删）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--with-venv", action="store_true",
                    help="同时创建 .venv 并装依赖（慢，几分钟；"
                         "默认只检查并给出安装命令）")
    args = ap.parse_args(argv)

    cwd = Path.cwd().resolve()
    dest = target_root(cwd)

    def out(payload: dict) -> None:
        if args.json:
            import json as _json
            print(_json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for k in ("status", "message", "install_root", "target",
                      "files", "size_kb", "locked",
                      "venv", "venv_base", "venv_missing", "venv_message",
                      "venv_install_cmd", "next"):
                v = payload.get(k)
                if v not in (None, ""):
                    print(f"{k:14} {v}")

    def venv_payload(payload: dict) -> dict:
        """把 venv 状态并进 payload（默认只检查，--with-venv 才装）。"""
        if args.with_venv:
            v = ensure_venv(dest)
            payload["venv"] = v.get("status")
            if v.get("base"):
                payload["venv_base"] = f"{v.get('base')} ({v.get('base_version')})"
            if v.get("message"):
                payload["venv_message"] = v.get("message")
            py = v.get("python")
        else:
            v = check_venv(dest)
            payload["venv"] = v.get("status")
            if v.get("missing"):
                payload["venv_missing"] = v["missing"]
            if v.get("message"):
                payload["venv_message"] = v.get("message")
            if v.get("install_cmd"):
                payload["venv_install_cmd"] = v["install_cmd"]
            py = v.get("python")

        payload["next"] = (
            f'"{py}" "{dest / "tools" / "env.py"}"'
            if py else
            f'（先建环境）然后 "{dest / "tools" / "env.py"}"')
        return payload

    # 已安装？
    if is_installed(dest) and not args.force:
        out(venv_payload({
            "status": "already-installed",
            "message": "工作目录下已有工具层，直接用",
            "target": str(dest),
        }))
        return 0

    # 找安装位置
    install = find_install_root()
    if install is None:
        out({
            "status": "no-install-root",
            "message": "找不到安装位置。请设 ANDROID_TEST_HOME 指向"
                       " 含 tools/env.py 的目录",
        })
        return 2

    if install == dest:
        out({
            "status": "same-dir",
            "message": "安装位置就是工作目录，不用拷",
            "target": str(dest),
        })
        return 0

    if args.check:
        out({
            "status": "would-copy",
            "message": "还没拷。去掉 --check 就会拷贝",
            "install_root": str(install),
            "target": str(dest),
        })
        return 0

    # --force：先解锁再删
    if dest.is_dir():
        unlock_code(dest)
        shutil.rmtree(dest, ignore_errors=True)

    files, size = copy_layer(install, dest)
    locked, lock_fail = lock_code(dest)

    payload = {
        "status": "installed",
        "message": "已拷到工作目录，之后都用这个",
        "install_root": str(install),
        "target": str(dest),
        "files": files,
        "size_kb": f"{size / 1024:.0f}",
        "locked": f"{locked} 个代码文件已设只读"
                  + (f"（{lock_fail} 个失败）" if lock_fail else ""),
    }
    payload = venv_payload(payload)

    # 只有 python 环境也好了，才算完全就绪
    ready = payload.get("venv") in ("ok", "exists", "ok-no-req")
    if not ready:
        payload["status"] = "installed-need-venv"
        payload["message"] = (
            "代码拷好了。**还差 python 环境** —— 见 venv_install_cmd，"
            "或者让 AI 跑 bootstrap --with-venv（要几分钟）")
    elif ready and not args.with_venv:
        payload["message"] = "已拷到工作目录，python 环境也已就绪"

    out(payload)
    return 0 if ready or not args.with_venv else 3


if __name__ == "__main__":
    sys.exit(main())
