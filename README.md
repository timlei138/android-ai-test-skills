# android-ai-test-skills

Android 设备黑盒 GUI 测试 v2：**AI 即执行者**。

对话里的 AI（CodeBuddy / Claude Code）直接驱动真机执行测试用例：每步调用薄工具层
"看（observe）/ 动（act）/ 读（read）"，自行分析结果、规划下一步；知识卡承载 App
操作经验；每步证据自动落盘，最终输出带证据的测试报告。

与 v1（AI 写 Python 用例脚本 → 确定性回放）的区别：**不再写 case**。
用例就是用户口述的步骤+预期，执行即探索，成功路径的知识沉淀进**知识卡**
（坑与行为规律）与**路径图**（在哪找什么）而不是代码。

---

# 安装（手动，一次性）

> **这一节请自己复制到终端里执行，别让 AI 代跑。**
>
> 原因：pip 要拉约 200MB，2-5 分钟。AI 在会话里跑的时候，一旦会话中断
> （切窗口、超时、你按了停止），pip 进程会被一起杀掉 —— 看起来像"跑了
> 半天什么都没装"。**自己开个终端跑最稳。**

## 你会装到哪

工作目录下多一个 `android-ai-test\`：

```
<你的工作目录>\
└── android-ai-test\          <- 工具层（代码 + 用例 + 知识卡 + 产物）
    ├── .venv\                <- 你要建的 python 环境（约 500MB）
    ├── tools\                代码（装完会设只读）
    ├── assets\cases\         用例
    ├── assets\knowledge\     知识卡 + 路径图
    └── storage\              运行产物（record\ + tmp\）
```

**为什么装在工作目录而不是系统目录**：这样 AI 读写全在它的 workspace 内，
不受沙箱限制；分享给别人 = 整个目录拷过去，各人一份互不干扰。

## 三步

### 第 1 步：拿工具层

在你想放它的目录下（比如 `D:\dsh`）：

```powershell
cd /d D:\dsh
python D:\<工具层安装路径>\tools\bootstrap.py
```

它会拷一份到 `D:\dsh\android-ai-test\`，然后打印：

```
status         installed-need-venv
target         D:\dsh\android-ai-test        <- 记住这个
venv_install_cmd  （下面那三条命令）
```

### 第 2 步：建环境装依赖（**这一步慢，2-5 分钟**）

```powershell
cd /d D:\dsh\android-ai-test
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.ctyun.cn/pypi/simple --trusted-host mirrors.ctyun.cn
```

**`-i` 那一行不能省。​** 不带的话直连 `pypi.org`，国内经常十几分钟
甚至超时（实测：天翼云源 48ms / pypi 306ms，差 6 倍）。

装什么：`uiautomator2` + `adbutils`（设备通信）、`rapidocr` + `onnxruntime`
\+ `opencv-python`（OCR 读 toast/Canvas）、`PyYAML`、`pillow`。

**Python 版本**：3.10 以上都行（**3.13 也可以** —— OCR 库已改用新包名
`rapidocr`，不再限制 `<3.13`）。

### 第 3 步：确认装好了

```powershell
.venv\Scripts\python.exe tools\env.py
```

看到这样就算成了：

```
platform : win32
shell    : powershell

★ 用这个解释器跑后面的所有命令：
  D:\dsh\android-ai-test\.venv\Scripts\python.exe

运行产物目录：
  storage  = D:\dsh\android-ai-test\storage
  record   = D:\dsh\android-ai-test\storage\record
  tmp      = D:\dsh\android-ai-test\storage\tmp
```

如果它报「缺依赖」，说明第 2 步没装完 —— 回去重跑那条 `pip install`。

## 可选：设备端初始化（每台设备一次）

```powershell
adb devices -l                                   # 确认设备在线且已授权
.venv\Scripts\python.exe -m uiautomator2 init    # 装 u2 的 agent
```

## 可选：看一眼设备当前画面

```powershell
.venv\Scripts\python.exe tools\observe.py --name smoke/first
```

---

# 起测试台（WebUI）

```powershell
cd /d D:\dsh\android-ai-test
python webui.py --port 9810
```

打开 http://127.0.0.1:9810 。**Ctrl+C 停止。**

**不需要 `.venv`** —— `webui.py` 只用标准库（`http.server` / `sqlite3` /
`json` / `pathlib`），任意 Python 都能跑。实测系统自带的 3.13.3 直接跑通。

## 它显示什么数据

读**这个工具层自己**的：

```
storage\record\<uuid>\     每次运行的记录（会话、事件、断言、证据）
assets\cases\<应用>\       用例列表
assets\knowledge\          知识卡
```

在哪个目录起，就看哪份数据。

## 端口被占

```powershell
Get-NetTCPConnection -LocalPort 9810 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

或者换端口：`python webui.py --port 9811`

---

# 装完之后

把用例口述给 AI 就行，它按 `SKILL.md` 的工作流执行。

```
「跑 168。步骤 1... 2... 预期 1... 2...」
```

**注意 PowerShell 活动代码页是 936 时**：用例原文里的中文长文本**别直接
放在命令行参数里** —— argparse 会把它拆开。先写进 UTF-8 文件，再用
`--input @文件` 传入：

```powershell
# 写文件
Set-Content -Path .\case168.txt -Value "步骤1: ... 预期1: ..." -Encoding utf8
# 传文件
.venv\Scripts\python.exe tools\session.py start --title "168" --input "@.\case168.txt"
```

**AI 会先做什么**：跑 `tools/env.py` 拿到「该用哪个解释器」和「产物落哪」，
然后才开始动设备。别让它跳过这一步 —— 它拿到的 `shell` 决定命令怎么写
（Windows 上不能用 `ls -la` / `2>/dev/null`）。

---

> **改过 `scripts/*.ps1` 后跑一次 BOM 校验**：Windows PowerShell 5.1 读无 BOM 的
> `.ps1` 会按 GBK 解析，中文/emoji 会导致语法崩溃。多数编辑工具重写文件时会
> 丢掉 BOM，所以改完必查：
> ```powershell
> pwsh -File scripts/check_bom.ps1        # 只看；-Fix 自动补回
> ```

## 工具层（无流程逻辑，每个都是独立命令）

| 命令 | 作用 |
|---|---|
| `tools/observe.py` | dump UI 树（摘要节点列表）+ 截图 + 前台包名/activity/旋转 |
| `tools/act.py` | tap / longclick / input / swipe / back / home / key；`--via` 溯源、`--why` 理由、`--watch` 瞬态弹窗响应、`--settle` 稳定窗口 |
| `tools/read.py` | OCR 读 toast / Canvas 自绘文字（输出像素 + 归一化坐标） |
| `tools/state.py` | 设备状态原语：权限 grant/revoke、pm clear、应用启停 |
| `tools/session.py` | 会话生命周期：start / pause(--ask 问人) / resume / finding / finish（PASS 时自动采集路径边）/ case |
| `tools/logcat.py` | 崩溃/ANR 取证：分级（被测/关联/无关）、自动抓崩溃前 20s 日志 |
| `tools/vision.py` | 可选视觉通道：OpenAI 兼容多模态模型问图（测试台「视觉模型」页配置） |
| `tools/cleanup.py` | 证据/日志轮转（--days，防 storage 膨胀） |
| `tools/run.py` / `tools/audit.py` | 独立启动调用、事件对账与子进程/阶段耗时报告；正式性能基准使用 |

所有 observe/act/read 调用自动归档证据到 `storage/evidence/`（`--name` 可省略，
自动派生 `<会话id>/step<N>`），并自动写入当前会话事件流。

## 测试会话与测试台

```powershell
.venv\Scripts\python tools\session.py start --title "冒烟" --input "口述的用例原文"
# …… AI 调 observe/act/read 执行（自动记录事件流）……
.venv\Scripts\python tools\session.py finding --status PASS --event-id <证据事件ID> --expect "<预期>" --actual "<实际>"
# 每条预期分别记录 finding，全部实际通过后收尾
.venv\Scripts\python tools\session.py finish --status PASS --summary "全过"
python webui.py                      # http://127.0.0.1:9810 查看时间线/证据/知识库
```

数据库 `storage/sessions.db` 三张表：`sessions`（会话）/ `events`（工具调用事件流，
自动写入）/ `findings`（断言结论），另有用例表和断言证据关联表。界面按"会话 → 时间线 → 截图证据"组织，断言可跳转关联事件。

### 可信记录与性能基准

PASS/FAIL/WARN 断言必须显式绑定本会话证据：`session.py finding --event-id <ID> ...`；
多个证据重复传 `--event-id`，ID 来自工具返回的 `event_ids`。收尾在校验后事务提交，
拒绝时不更新结论与用例统计。记录失败会明确报错，动作可能已经执行，禁止直接重放。

基准用 `tools\run.py act ...` 等方式替代直接调用，再用
`tools\audit.py --session-id <ID>` 对账。详细使用、计时范围与隔离方式见
[环境与工具](docs/环境与工具.md)。这些是可信记录与测量基础设施，
尚未据此完成真机基线或证明提速收益。
调用失败与记录完整性分别报告：`audit.ok` 只表示完整性，拒绝/失败尝试单列；
计时默认后台落盘，正常返回不展开。逐步动作接口保留，finding 证据绑定和 finish 硬校验为有意收紧的约束。

隔离回归：

```powershell
.venv\Scripts\python -m unittest discover -s tests -p test_trusted_execution.py -v
.venv\Scripts\python tests\test_crash_pipeline.py
```

## 目录

```
├── SKILL.md                    # 工具说明书（AI 按需加载）
├── tools/                      # 薄工具层：observe / act / read / session + db
│   ├── env.py                  #   ★ 第一条命令：报解释器 / shell / storage
│   ├── bootstrap.py            #   把工具层拷到工作目录
│   └── ...
├── assets/
│   ├── cases/<应用>/            # 用例：<包名>_<编号>.md（原文）+ .yaml（跑过的）
│   └── knowledge/              # App 知识卡（按包名）+ paths/ 路径图
├── storage/                    # 运行产物（不进 git）
│   ├── record/<uuid>/          #   每次运行一个目录（db + evidence + calls + timing）
│   └── tmp/                    #   Agent 临时文件
├── webui/                      # 测试台前端
└── webui.py                    # 测试台服务
```
