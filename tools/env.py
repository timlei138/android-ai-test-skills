# -*- coding: utf-8 -*-
"""env：一条命令告诉你"在这台机器上命令该怎么写"。

## 为什么需要独立的 env.py

`session.py start` 的返回里已经带 `env` 了，但那是**执行子 Agent** 的入口。
**主 Agent 从不调 session start**（它只派发、查用例、汇总），
所以拿不到那个字段 —— 于是它继续按 Linux 习惯敲 `ls -1`。

实测：主 Agent 问"有多少 case"，第一条命令就是
    ls -1 "D:/Project/deepseek/android-ai-test-skills/assets/cases"
在 Windows PowerShell 上 `ls` 不认 `-1`，直接 exit 1。

## 用法

```bash
python tools/env.py           # 人读的提示
python tools/env.py --json    # 给程序读
```

## 设计取舍：为什么不让 AI 自己探测

AI 自己写 `uname -s` 或 `python -c "import sys..."` 也能探，
但那要**多一次工具往返**，而且它经常懒得探就直接敲命令。

一条 `python tools/env.py` 比"先想想要不要探测"更省。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ensure_utf8_stdio  # noqa: E402
from runtime import (  # noqa: E402
    KNOWLEDGE_DIR, RECORD_DIR, STORAGE_DIR, TMP_DIR, detect_shell,
)

# ★ 必须第一件事就切 UTF-8。
#
#   踩过（2026-10-27，代价很大）：
#     Windows 控制台默认 GBK，`print("⚠️ ...")` 直接抛
#         UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'
#     整个脚本崩掉 -> AI 拿不到 platform / shell / storage 提示 ->
#     它只能自己瞎试（敲 Linux 命令、猜路径、反复重启 webui），
#     一条用例还没开始就烧掉 37 次工具调用。
#
#   `common.ensure_utf8_stdio()` 就是干这个的（session.py 等早就调了），
#   env.py 是新加的，当时忘了 —— 所以它自己反而是最不可靠的那个。
ensure_utf8_stdio()


def _deps_ok() -> str:
    """关键依赖在不在？返回 "ok" 或缺失列表。

    为什么 env.py 要报这个：AI 拿到 `python` 路径后直接用它跑命令，
    但那个解释器可能一个依赖都没有（比如刚建的空 venv）。
    提前说清，比跑到 act.py 才 ImportError 好。
    """
    import importlib.util
    need = ("uiautomator2", "adbutils", "yaml", "PIL")
    miss = [m for m in need if not importlib.util.find_spec(m)]
    return "ok" if not miss else "缺: " + ",".join(miss)


def main() -> None:
    env = detect_shell()
    if "--json" in sys.argv:
        payload = dict(env)
        payload.update({
            # ★ 这条最关键：**用哪个解释器跑后面的命令**
            #
            #   不能写死在 skill 里，因为"哪个 venv 装了依赖"是运行时状态：
            #     本机：安装位置的 venv 有依赖，target 的是空的
            #     别人：可能反过来（自己装过依赖之后）
            #
            #   所以由这里报出来，AI 拿到什么就用什么 —— 换机器自动跟着变。
            "python": sys.executable,
            "tools_dir": str(Path(__file__).resolve().parent),
            "storage_dir": str(STORAGE_DIR),
            "record_dir": str(RECORD_DIR),
            "tmp_output": str(TMP_DIR),
            "knowledge_dir": str(KNOWLEDGE_DIR),
            "deps_ok": _deps_ok(),
            "storage_source": (
                "env:ANDROID_TEST_STORAGE"
                if os.environ.get("ANDROID_TEST_STORAGE")
                else "默认 = cwd/storage"),
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"platform : {env['platform']}")
    print(f"shell    : {env['shell']}")
    print(f"tmp_dir  : {env['tmp_dir']}")
    print()
    print(env["hint"])
    print()

    # ── 产物目录：这个必须打出来，AI 才能确认它对不对 ────────────
    src = ("环境变量 ANDROID_TEST_STORAGE"
           if os.environ.get("ANDROID_TEST_STORAGE")
           else "默认 = 当前工作目录下的 storage/")
    print("★ 用这个解释器跑后面的所有命令：")
    print(f"  {sys.executable}")
    deps = _deps_ok()
    if deps != "ok":
        print(f"  ⚠️ 但它{deps} —— 先跑 pwsh -File scripts/setup.ps1 装依赖")
    print()
    print("运行产物目录（来源：%s）：" % src)
    print(f"  storage  = {STORAGE_DIR}")
    print(f"  record   = {RECORD_DIR}")
    print(f"  tmp      = {TMP_DIR}")
    print(f"  knowledge= {KNOWLEDGE_DIR}")
    print()

    # ── 工具目录 + 开会话模板 ────────────────────────────────────
    #
    # ★ 2026-10-28 加的：实测 176 那轮为了"工具在哪、怎么开会话"花了
    #   30~46 秒（3 次 Get-ChildItem + 1 次 pwd + 试 session.py --help）。
    #   这些都写在这里，省掉那几步探路。
    #
    # ⚠️ 不能用 Path(__file__).resolve() —— 它会**解开 junction**：
    #   skill 装在 ~/.agents/skills/android-gui-test，
    #   是个指向源码 D:\Project\...\android-ai-test-skills 的链接。
    #   resolve() 之后报的是源码路径，而 AI 该用的是工作区那份
    #   （有 .venv、有 storage）。所以优先用工作区根目录。
    here = Path(__file__).parent          # 不 resolve，保留 junction 路径
    ws_root = STORAGE_DIR.parent          # <target>（storage 的上一级）
    tools_dir = ws_root / "tools"
    if not (tools_dir / "session.py").is_file():
        tools_dir = here                  # 回退：用脚本所在目录
    py = sys.executable
    print("★ 工具目录（不用再找）：")
    print(f"  tools    = {tools_dir}")
    print(f"  target   = {ws_root}")
    print()
    print("★ 开会话（唯一必须用 bash 的命令）：")
    print(f'  & "{py}" "{tools_dir}\\session.py" start '
          f'--title "<标题>" --input "@<用例文件>" --package <包名>')
    print("  中文长文本先写 UTF-8 文件再传 @文件；别直接放命令行")
    print()
    print("⚠️ 如果你在**受限沙箱**里跑（只能写某个 workspace），")
    print("   而这个 storage 不在可写范围内，先设环境变量再调工具：")
    print()
    print('   $env:ANDROID_TEST_STORAGE = "<你的可写目录>\\storage"')
    print()
    print("   设完重跑这条命令，确认上面的 storage 变了。")
    print()

    if env["shell"] == "powershell":
        print("本机常用对照：")
        print("  ls -la dir        ->  dir dir   /  Get-ChildItem dir")
        print("  ls -1 dir         ->  dir dir   （PowerShell 的 ls 不认 -1）")
        print("  head -20 f        ->  Get-Content f -TotalCount 20")
        print("  cmd1 && cmd2      ->  cmd1; cmd2")
        print("  cmd 2>/dev/null   ->  cmd 2>$null")
        print("  cat f             ->  Get-Content f")
        print("  which python      ->  Get-Command python")
        print()
        print("★ 但 `adb shell \"...\"` 里的内容仍是 Android 的 Linux，照常写 grep 等。")


if __name__ == "__main__":
    main()
