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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime import detect_shell  # noqa: E402


def main() -> None:
    env = detect_shell()
    if "--json" in sys.argv:
        print(json.dumps(env, ensure_ascii=False, indent=2))
        return

    print(f"platform : {env['platform']}")
    print(f"shell    : {env['shell']}")
    print(f"tmp_dir  : {env['tmp_dir']}")
    print()
    print(env["hint"])
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
