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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 Android 测试工具层拷到当前工作目录")
    ap.add_argument("--check", action="store_true",
                    help="只检查，不拷贝")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的（会先解锁再删）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    cwd = Path.cwd().resolve()
    dest = target_root(cwd)

    def out(payload: dict) -> None:
        if args.json:
            import json as _json
            print(_json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for k in ("status", "message", "install_root", "target",
                      "files", "size_kb", "locked", "next"):
                v = payload.get(k)
                if v not in (None, ""):
                    print(f"{k:14} {v}")

    # 已安装？
    if is_installed(dest) and not args.force:
        out({
            "status": "already-installed",
            "message": "工作目录下已有工具层，直接用",
            "target": str(dest),
            "next": f'python "{dest / "tools" / "env.py"}"',
        })
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

    out({
        "status": "installed",
        "message": "已拷到工作目录，之后都用这个",
        "install_root": str(install),
        "target": str(dest),
        "files": files,
        "size_kb": f"{size / 1024:.0f}",
        "locked": f"{locked} 个代码文件已设只读"
                  + (f"（{lock_fail} 个失败）" if lock_fail else ""),
        "next": (f'python "{dest / "tools" / "env.py"}"  '
                 f'# 确认 storage 指向 {dest / "storage"}'),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
