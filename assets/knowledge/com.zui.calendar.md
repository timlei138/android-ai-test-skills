# ZUI 日历（联想平板）

- **app**: `com.zui.calendar`
- **name**: ZUI 日历
- **验证版本**: 9.0.0.110-2026.08.25-release（versionCode 900110，TB522FU / Android 17）
- 无课程表前提：`state.py clear --package com.zui.calendar`
- 图库/拍照导入的解析需联网（约 20s，等待窗口给到 60s）

> 本卡只写这个 App 的**独特经验**：它的坑、它的行为规律、它的控件怎么认。
> **不写**坐标/像素/会话号/某次执行过程/该怎么判 PASS。
> 定位靠 `observe` 现场取 bounds；判定由执行者拿事实对比用例自己下。

## 首次启动三连

`pm clear` 后首次启动会**按固定顺序连弹三个**（不知道这条会以为"主页打不开 / 更多找不到"）：

1. **App 权限说明框**「日历需要使用以下权限」（必要/可选分组 + 退出/同意）
   - 标题 rid `alertTitle`，同意 = `android:id/button1`
   - ⚠️ 一命中就点会**点空**（入场动画未结束）：等约 1.5s 再点，点完核对框已消失
2. **系统权限弹窗**（默认同意会自动点掉，不用管有几个）
3. **课程表功能引导框**「课程表上新了！」—— 触发时机是 **`pm clear` 后首次启动、在【日历主页】上弹**，**不是**"进入课程表功能时"
   - 文案「课程表上新了！」/「点击快速导入课表，自动同步所有上课时间」/「我知道了」
   - rid `curriculum_guide_title` / `curriculum_guide_message` / `timetable_guide_button`

> ⚠️ **这三个只在清数据后出现，冷启动不会再有**（本地已记"看过"）。
> 反过来：普通用例不要清数据，否则每条都要重走这三连。

## 页面拓扑

| 页面 | activity | 怎么到 |
|---|---|---|
| 日历主页 | `.AllInOneActivity` | 启动即此页 |
| 周视图 | `.timetable.display.TimetableActivity` | 主页 `iv_more` → 菜单「课程表」；或 App 打开默认课表 |
| 全部课程表列表页 | `.timetable.management.TimetableListActivity` | 周视图 → 顶栏 `action_curriculum_table_settings`（文案「课程表设置」）。**进课程表不会直接到列表页** |
| 课程表基本信息编辑页 | `.timetable.management.EditTimetableActivity` | 空状态 `btnCreateManually`；或列表页 `action_add_schedule` |
| 课程时间设置页 | `.timetable.management.TimeSlotSettingsActivity` | 编辑页「课程时间设置」行 `layout_time_settings`（**不是** `layout_time_slot_settings`） |
| 添加/编辑课程弹窗 | `.timetable.course.EditCourseActivity` | 周视图点格子 → `iv_add_hint`；**视觉是浮层弹窗**（圆角卡片压在周视图上，背景压暗） |
| 图库导入确认页 | `.timetable.temp.TempConfirmTimetableActivity` | 图库导入流程末段 |
| 识别结果页 | `.timetable.temp.TempTimetableActivity` | 图库导入解析完 |
| 备份与恢复 | `.settings.LenovoSettingsBackupActivity` | 主页 `iv_more` →「设置」→ 滚动到底 |
| 自定义恢复 | `.common.backup.SelectRestoreActivity` | 备份页「自定义恢复」 |
| 设置页 | `.settings.GeneralSettingsActivity` | 主页 `iv_more` → 菜单「设置」 |
| 新建事件/提醒 | `.event.NewBuildActivity` | 主页右上角 `action_add_all_event`（ImageView，无 text/desc） |
| 重复结束页 | `.event.options.EventLastDateActivity` | 新建事件页的「重复」→ 自定义 |
| 倒数日列表页 | `.widget.countdown.CountdownListActivity` | 点桌面 widget；长按 widget →「编辑小部件」 |
| 新建倒数日页 | `.reminder.EditCountDownActivity` | **只能**从列表页点 `countdown_action_add` |
| 倒数日详情页 | `.reminder.CountDownInfoActivity` | **只能**从日历首页 agenda 条目进去 |

**最便宜的边**：列表页 BACK 一次 = 周视图，再 BACK = 主页。别回主页重走「更多 → 课程表」。

## 新建事件页：左侧分类切换，右侧 rid 整套换

左侧 `categoryList` 有 6 个常驻分类（提醒/日程/课程/倒数日/生日/纪念日）。
**切换分类不换 Activity**，但右侧表单节点的 rid 整套替换 —— 这是最容易做错的地方：

| 分类 | 标题 rid | 全天开关 | 起止字段 | 其他特征 |
|---|---|---|---|---|
| 提醒（默认） | `reminder_title` | **无** | `eventTimeLayout`（起+止一个弹框） | `reminderTimeLayout`/`reminderRepeatLayout`/`reminderDescription` |
| 日程 | `event_title` | **`is_all_day`** | `start_date`+`end_date`（全天）/ `start_layout`+`end_layout`（非全天） | `rrule`/`busy_text`/`tv_type` |
| 课程 | `mCurriculumTvTitle` | 无 | `mCurriculumLlStartLayout`/`mCurriculumLlEndLayout` | `mCurriculumEtLocation`/`mCurriculumEtRemark`/`mCurriculumTvTimeFrame`/`mCurriculumTvRepeatRule` |

- **「全天事件」开关 `is_all_day` 只在「日程」分类页存在**。在默认「提醒」页找它找不到
- 开全天 → 时间栏消失、出现 `start_date`/`end_date`；关全天 → 变回带时分的那对
- 课程分类的「重复」进 `.curriculum.CurriculumRepeatActivity`（**不是** `.reminder.RepeatSelectedActivity`），
  只有三项：不重复(默认)/每天/每周；选「每周」再进 `.curriculum.CurriculumCustomRepeatActivity` 选周一~周日
- ⚠️ 左侧菜单的「课程表」(`.timetable.display.TimetableActivity`) 是**另一套数据** ——
  在这页新建的「课程」分类事件**不会**出现在课程表里，反之亦然

## 日期/时间选择器（新建事件页通用）

调起后是**居中真弹框**：`date_time_picker`（时+分）或 `datePicker`（仅日期，全天用），
`left_button`=取消、`right_button`=确定。**activity 不变**，故判「是否真弹框」看 activity 有没有变。

- 三列滚轮左→右 = **日期 / 小时 / 分钟**，横向居中均分
- **滚轮是 Canvas 自绘，dump 读不到当前值** —— 要读值用截图 + OCR（`android_read`），
  或问视觉模型「读图中日期选择弹框当前选中的日期」
- **改值用点按 ±1，不要用 swipe**：点**上方**一格 = **−1**，点**下方**一格 = **+1**（与直觉相反，别记反）
- **一次点按就是一格**，不需要点一次观察一次。要走几格就点几次，用 `--repeat N` 一次点完
- ❌ **不要用外层 shell 循环包 N 次调用**：每次冷启动 3-6 秒，极易撞超时；
  超时=动作执行了却没记账 → 会话判 `recording incomplete` 直接作废
- 全天开关打开时，日期弹框只有 `datePicker`（单列），**没有农历开关**
- 农历开关 `lunarToggle`（文案「显示农历选择」）**只在部分形态的弹框里出现**，找不到就是没有，别硬找

### ⚠️ 滚轮要点准：先量「可点带」

**同款弹框在不同页面会上下平移，可点带跟着平移。** 照搬旧坐标会「点了没反应」，
很容易误以为是「滚轮不响应合成点击」而去改用 `swipe`（→ 过冲 → 更难收敛）。

**先量后点：**

1. dump 一次，取三件套：
   - `datePicker` 的 bounds —— 整个弹框
   - `parent` 的 bounds —— **滚轮真正的可点区**，比 datePicker 矮一截（去掉了底部按钮）
   - `selector_top_divider` 的 bounds —— **选中行上沿**，行高的定位锚点
2. `parent` 高度 ÷ 可见行数(约5) ≈ 行高；以 divider 为基准，算出「选中行 / 上一行 / 下一行」的 y
3. 点完用 `read --region`（框住弹框 title）**回读当前值**再决定下一步

规则（跨弹框通用）：

- 点 `parent` 里偏上的位置 → **−1**
- 点选中行 → 无变化
- 点偏下的位置 → **+1**
- 太靠近弹框上下边缘是**死带**，点了完全不变
- ⚠️ **方向可能因弹框而异**：月视图的日期弹框与新建事件页的弹框方向相反，**第一次用先探一下**

### ⚠️ 坑：`--repeat` 连续点「月」轮会把改动吃掉

`--repeat N` 对**日**轮可靠（一次调用点到位，值是准的）。但对**月/年**轮要当心：
第一次点按改变月份后，**滚轮内容会重排**，第二次点按落到的可能已是另一格，
于是「+2」实际变成「+1 再 −1」= **回到原值**，看起来像「改了月却提交不上」。

- ✅ **改月轮：一次调用只点 1 下**，回读 title 确认后再点下一下
- ✅ **改日轮：可以放心用 `--repeat N`**
- ⚠️ **月用 `--repeat 2` 再改日** → 「确定」后**月改动丢失**、只剩日改动。这是工具用法问题，不是产品缺陷

### 月视图「票」标记的读法

抢火车票设置生效后，月视图日期格**右上角**会出现一个小「票」字。整屏 OCR 能读出 `text == "票"`。

⚠️ **「票」画在格子右上角，日期数字在格子中偏左** —— 两者横向不对齐，直接用「票」的 x 认日期会错位。
**必须用点击回验**：点日期格后读 `top_button_day`，确认列归属正确，再据此换算日期。

## 结束进程：只能用 `am kill`，不能用 `am force-stop`

验证「结束后台运行后提醒仍能到达」这类用例时，两种方式语义完全不同：

- `am force-stop <pkg>`：结束进程且置 `stopped=true`（禁用应用），**连同已注册的闹钟一并取消**
  → ❌ 不能用于这类用例（太严厉）
- `am kill <pkg>`：只结束后台进程，`stopped=false`，**闹钟保留**
  → ✅ 等价于用例说的「双击 HOME / 从最近任务划掉，结束后台运行」

用 `force-stop` 后查 `dumpsys alarm` 会看到闹钟被移除，并在 alarm history 留下
`Reason=pi_cancelled rtc=<force-stop 时刻>` —— **这才是「提醒没来」的真正原因**，不是应用缺陷。
且 `force-stop` 后应用处于 `stopped=true`，**下次手动启动前闹钟/广播都不会送达**；
要恢复得先启动一次（`monkey -p <pkg> -c android.intent.category.LAUNCHER 1`）。

**正确姿势**：先 `act home`，再 `am kill <pkg>`，然后核对三件事再等提醒：

1. `pidof <pkg>` 为空（进程确已结束）
2. `dumpsys package <pkg>` 里 `User 0: ceDataInode=…` 那行 `stopped=false`
3. `dumpsys alarm` 里目标闹钟仍在

## 通知栏会挡住后续操作，且 `act back` 关不掉

上一条用例结束在**通知栏展开**状态时，下一条的第一步会打在通知栏上而**完全无效**，
表现为 `activity` 仍是 `NotificationShade`、checkpoint 不命中。

- 判据：`dumpsys window | grep -m1 mCurrentFocus` 返回 `Window{… u0 NotificationShade}`
- **`act back` 与 `KEYCODE_BACK` 都关不掉**（反复无效）
- ✅ 有效手段：`adb shell input swipe 1520 1400 1520 200 300`（从下往上滑），或 `KEYCODE_HOME`

## 设置页很长，「备份与恢复」在最底部

`.settings.GeneralSettingsActivity` 是 `recycler_view` 长列表：

| 屏幕 | 内容 |
|---|---|
| 第 1 屏 | 每周起始日 / 抢火车票设置 |
| 中部 | 时区 / 使用自定义时区 / 自定义时区 |
| **底部**「其它」分组 | **备份与恢复**（第 3 屏，需 2 次上滑） |

上滑后**重读 UI 树**再找，不要按固定次数滑。

备份与恢复页 `LenovoSettingsBackupActivity` 控件（**都在首屏**）：

| 元素 | rid |
|---|---|
| 自定义恢复 | `selecte_restore_layout`（整行可点） |
| 一键备份 | `backup_button` |
| 一键恢复 | `restore_button` |

- 「自定义恢复」：**整行可点的是 `selecte_restore_` 容器**，`title` 只是行内文字（点了不跳转）
- 时区选择页 `.settings.LenovoSettingsActivity`（标题「时区」，条目 rid=`text1`，
  首项「(GMT+8:00) 北京」），选中后**自动返回**并回显到「自定义时区」行的 `summary`

### 坑：清数据会连备份一起删掉

备份产物在 `/sdcard/Android/data/com.zui.calendar/cache/LenovoCalendar/BackUp/<时间戳>/`
（含 `Events.vcs` + `114_lecalendar.db`）。

**虽然该路径在外部存储，但 `pm clear` 在 Android 11+ 会连 App 的外部
`Android/data/<pkg>/` 一起清掉。**

所以「已备份过 + 清除日历的数据」这组前置**自相矛盾**：清完就没备份可恢复，
点「一键恢复」只提示 **「未找到可用的备份记录」**（既不弹 dialog 也不出 toast）。

要验"清数据后恢复"，必须先把备份目录拷出来、clear 后再拷回去。

另外：即使事件为空时点「一键备份」，也会提示「备份成功」——
**这个 toast 在有内容 / 没内容两种情况下文案一样。**

### 自定义恢复页

- 列表项文本形如 `日历_2026年9月17日_10时37分02秒`
- **右上角「编辑」是纯图标**（无 text/desc）→ rid `edit_menu`，不能按文字定位
- 编辑模式：标题 `listviewx_edit_title`「选取项目」；`listviewx_select_all`「全选」；
  `listviewx_cancel` 取消；`restoreBtnDelete`「删除」
  - **置灰看 `enabled`**：未勾选 `enabled=false`，勾选 `slideview_check` 后变 `true`
- 点删除 → 二次弹窗「**删除所选的记录？**」；点某条 → 「**恢复到所选的备份记录？**」

## 两层数据：UI 与 `com.android.calendar/events` 不是同一份

做「清除日历数据」「以 DB 计数判断事件数量」这类用例前必须知道：

- **事件不在 `com.zui.calendar` 的数据目录里**。`content://com.android.calendar/events`
  由系统组件 **`com.android.providers.calendar`** 承载
- 因此 `pm clear com.zui.calendar` **清不掉任何事件**，它只重置应用自身设置
  （会出现首次运行三连，「抢火车票设置」回到「暂不显示」，「每周起始日」回到「星期一」）
- 真正让 provider 归零要用 `pm clear com.android.providers.calendar`
- **但日历 UI 读的不是这一层** —— 出现过「`content query` 查得 0 条，而 UI 仍显示事件」，
  即 UI 走自带同步缓存（联想账号 `com.lenovo.lsf.account` / `com.zui.sync.calendar` 体系）

**启动日历就会触发云同步回灌**：只要启动应用，就会跑一次联想账号同步并把事件插回来。
`sync_events=0` 与 `am force-stop` 都拦不住这次拉取；不启动应用时，
`pm clear com.android.providers.calendar` 的结果能稳定保持。

关联账户：`com.lenovo.lsf.account`；日历账户名 `PC Sync`（`account_type=LOCAL`）。

## `content://com.android.calendar/events` 只存「日程」类

**新建事件页左侧的分类不是同一套存储**，这个 provider **只暴露「日程」一类**：

| 分类 | provider 可见？ |
|---|---|
| 日程 | ✅ |
| 提醒 / 课程 / 倒数日 / 生日 / 纪念日 | ❌ **不可见**（存应用内部库） |

`content://com.zui.calendar/events` 直接抛 `Could not find provider`；
`content://com.android.calendar/reminders` 无 `title` 列。

**后果**：拿它当「保存是否成功」的判据，会把提醒/课程/生日/倒数日/纪念日的正常保存
全部判成「保存不落库」。这是**方法错误**，不是产品缺陷。

**要看这些分类存没存上，走纵览表**：主页 → `iv_more` →「纵览」，
左侧分类点 `*_layoutx`（`all_layoutx`/`reminder_layoutx`/`event_layoutx`/
`curriculum_layoutx`/`countdown_layoutx`/`birthday_layoutx`/`creditcard_layoutx`/`trip_layoutx`），
右侧列表项 rid 以 `title` 结尾即为该分类下的条目。

> ⚠️ 纵览表默认停在**搜索态**时，左侧分类点了不生效（右侧仍显示上次搜索结果）。
> 点分类前先确认不在搜索态（有搜索词时点 `cancel` 退出）。

## 负一屏（桌面左侧资讯页）

**1. 桌面设置入口是「长按桌面空白处」**

- 桌面空白处长按 → 编辑菜单：`btn_wallpaper`「壁纸和主题风格」、
  `btn_widgets`「添加小部件」、`btn_settings`「桌面设置」
- 点「桌面设置」跳 `com.zui.launcher/.settings.ZuiLauncherSettings`
  （也可直接 `am start -n com.zui.launcher/.settings.ZuiLauncherSettings`）
- ❌ 若长按落在负一屏页面上，只会唤出负一屏自身内容，**不会**出现编辑菜单

**2. 负一屏开关位置**

`ZuiLauncherSettings` 首行即「负一屏」：`title`「负一屏」+ `summary`「在桌面负一屏开启天气、资讯等服务。」，
右侧是 `android:id/switch_widget`。

⚠️ **别点错行**：该页所有行的开关 **rid 完全相同**，只能靠 **bounds 的 y** 区分。
定位方式：先取「负一屏」`title` 节点的 y，再找 y 最接近它的 `switch_widget` —— 不要按"第几个 switch"数。

**3. 开关状态怎么判**

- 用 `checked` 属性判：`"true"` 为开，`"false"` 为关
- ⚠️ `settings get secure settings_contextual_home` 在本机**返回 `null`**，且与开关状态不对应
  —— 不要用它判断负一屏开没开
- 点完开关**必须回读一次**：动作返回 ok 不代表开关变了，可能因页面未加载完而点空

**4. 「预授权弹窗 / 日历权限弹窗」在本机无法重现**

- 该弹窗**只在「首次授权」时出现一次**，之后开开关都是静默生效
- `pm clear com.zui.launcher` 清不掉这个授权态（复位桌面数据后再开开关，依然不弹）
  —— 授权记录保存在 launcher 应用数据之外（随账号/服务端持久化）
- 因此涉及「负一屏首次授权的预授权弹窗 → 日历权限弹窗」的用例，在本机**只能记为无法验证**

**5. 本机负一屏不含「今日日程」卡片**

当前卡片组合：服务与反馈、天气 2x2、文件夹、应用商店、安全中心、设置、天禧智能体、超级文件。
`com.zui.launcher` 的 `READ_CALENDAR` 为 `granted=false`，但卡片仍正常显示。

## 倒数日小部件（widget 2x1）

### 三个页面要分清

| 页面 | activity | 怎么进 | 关键 rid |
|---|---|---|---|
| 倒数日列表页 | `.widget.countdown.CountdownListActivity` | 点桌面 widget 占位区 / 长按 widget→「编辑小部件」 | `toolbar_title`/`countDownTip`/`date_left`/`todaynull_tv`/`bottom_tv`/`countdown_action_add` |
| 新建倒数日页 | `.reminder.EditCountDownActivity` | **只能**从列表页点右上 `countdown_action_add` | `countDownTvTitle`/`countDownLlDate`/`countdownLlReminder`/`countDownTvDescription`/`save_view` |
| 倒数日详情页 | `.reminder.CountDownInfoActivity` | **只能**从日历首页 agenda 条目 `agenda_item_text_container` 点 | `countTvTitle`/`countTvDate`/`countTvReminderLabel`/`countTvOtherDate`/`bottom_bar` |

⚠️ **最易踩的坑**：**点 widget 永远只到 `CountdownListActivity`**（无论有无数据），
永远不会进「新建页」也永远不会进「详情页」。

### widget 的 rid 与占位态

- widget 本体在 UI 树里以 `content-desc="倒数日 2x1"` 暴露（无 rid）；
  内部节点是日历进程的：容器 `countdownLlNo`（`clickable=true`）、
  占位文案 `countdownTvNo`（`clickable=false`）
- **无数据占位文案是「暂无倒数日」**（不是"无倒数日"/"无事件"）
- 列表页无数据时是 `todaynull_tv` = **「无事件」**（与此不同，别混）
- ⚠️ **点 widget 请点 `countdownLlNo` 的中心**；`countdownTvNo` 自身 `clickable=false`

### 长按 widget 本体 → 编辑菜单

长按**桌面 widget 本体**（不是长按桌面空白）弹出两项：`编辑小部件` / `移除`。
点「编辑小部件」进 `CountdownListActivity` —— **这就是用例说的"编辑菜单页"**。
（长按桌面空白出的是 launcher 菜单，不是这个。）

### 已知缺陷

1. **有数据时 widget 不展示最近一条**：建好倒数日后回桌面，widget **仍显示「暂无倒数日」**。
   已排除刷新延迟与取错页，**两次独立复现**，非偶发
2. **列表页点选倒数日卡片不返回桌面**：条目容器 `click_layout` 虽 `clickable=true`，
   点其中心/左/右三种位置**均无任何反应**

> 倒数日数据不通过 provider 暴露（`content query` / `sqlite3` 查不到），
> 数据在不在只能看 UI 回显。

## 全部课程表列表页

- 标题「全部课程表」；列表项 `tv_schedule_name`（名称）/ `tv_default_badge`（「当前」角标）/
  `tv_schedule_info`（「设置」）
- 顶栏两图标：`action_schedule`（文案「编辑」）、`action_add_schedule`（文案「添加课程表」）
- **列表页点课名 = 编辑课程表**（`EditTimetableActivity`），**不是查看**

### 设为当前课程表

- 确认框「**确定设为当前课程表吗？**」+ 取消(`android:id/button2`) / 确定(`android:id/button1`)
- 确定 → 「当前」角标移到选中行；取消 → 仅关框不改状态
- 删除弹框文案**随勾选数量变化**：「确定要删除选中的 **N** 个课程表吗？」

### 删除全部课程表后的两种「空」

| 时机 | 落点 | 三按钮 |
|---|---|---|
| 删除全部**当场** | 列表空态「没有课程表」+「点击 + 创建新课程表」 | ❌ 无 |
| **重新进入**课程表 | 引导式空状态页「还未添加课程表」 | ✅ 三个都在 |

删光后列表页**不会自动跳转**，要**离开再回来**才回到引导式空状态页。

### 多选态与置灰规则

点 `action_schedule` → 多选态：出现 `checkbox_select` + `bottom_bar` + `btn_set_default` / `btn_delete`。
**置灰看 `enabled` 字段**：

| 勾选情况 | btn_set_default | btn_delete |
|---|---|---|
| 仅勾选**当前**课程表 | `false`（置灰） | `true` |
| 勾选**多个**（含当前） | `false`（置灰） | `true` |
| 仅勾选**非当前**课程表 | `true`（可用） | `true` |

即：**勾选集合含"当前课程表" → 设为当前置灰**。

⚠️ 删除弹框文案「确定删除**此**课程表吗？」区别于课程卡片的「确定删除**此课程**吗？」。

- 新建的课表**不自动抢占"当前"**

## 课程表空状态

- 三个入口按钮**直接铺在屏幕上**，**此时没有工具栏** —— 不要再去找「更多」菜单
- 容器 `emptyView`；文案「还未添加课程表」

## 导入弹窗顺序（拍照/图库通用）

点「拍照导入」或「从图库导入」后是**两段式、有先后**：

1. **App 内提示弹窗**「请确保图片清晰、完整」→ 点「知道了」
   - 日历自己的框，**与系统权限弹窗无关，无论权限是否已授予都会弹**
   - **必须关掉，否则后面什么都不发生**：不点掉，相机 25s 都不拉起、PhotoPicker 也不出现
2. **系统权限弹窗**（相机/照片）→ **仅当对应权限未授予时才弹**；已授予则跳过

测权限行为前若上一轮已允许过，需 `revoke` 重置前提，否则第 2 段根本不出现。

## 图库导入解析

- 解析需联网（约 20s），**等待窗口给到 60s**；低于 20s 会在解析中途超时
- **选图别盲点第一张**：照片 tab 按媒体库时间倒序、所有缩略图**共用 `icon_thumbnail` 一个 rid**
  → 第一张取决于媒体库最近有什么（其他用例拍照/截图会插队）。要先看缩略图内容再选
- **解析失败弹窗（模态，非 toast）**：标题「图片内容不是课程表」+「知道了」
  - 这是**通用解析失败框**：选错图会弹、**真课程表云端解析偶发失败也弹**
    → 不能据此断定"图选错了"
  - 预检通过的候选被弹此框 = 偶发失败 → 重试同一张；总次数上限 3 次，用尽 BLOCKED

## 必填校验（与规格不符）

规格说"名称为空时完成按钮**置灰**"，实测**不置灰**：

- 新建课程表页 `save_view`、图库导入确认页 `btn_finish`：`enabled=true`、`clickable=true`
- 点击后**被校验拦住**（页面停在原页、无 toast）

**置灰断言**：日历的置灰是「整体变淡」，UI 树 `enabled/clickable` 可能不变；
以截图判读（问视觉模型「这个按钮是不是灰色不可点」）。

## 拍照导入返回

从相机返回：返回键可能被取景器吃掉，`am force-stop com.zui.camera` 更快更稳；
且退回的是**日历主界面**（不是课程表页），需重新走「更多 → 课程表」。

## 课程表基本信息编辑页 `EditTimetableActivity`

- 名称默认空且**必填**（图库导入确认页才有预填名）
- 保存 = toolbar 右侧 `save_view`（文本「完成」，clickable），**不是** `btn_finish`；
  `action_save` 是其不可点父容器
- **不要按 back 收键盘** —— 见 `_system.md`「BACK 键-两段式」
- 两个开关**默认均关闭**（`switch_weekend_classes`/`switch_show_non_current_week`）；
  图库导入确认页的「显示非本周课程」可能默认开启，两者默认态不同
- ⚠️ **本页就是「课程表基本信息编辑页」** —— 用例前提写"进入基本信息编辑页"时停在本页即可
- ⚠️ **点「完成」保存后落点是周视图**（不是列表页），且新建的课表名不会作为可点列表项出现
- 课程提醒时间入口 `layout_default_reminder`（可点行）/ `tv_default_reminder`（行内值），
  默认 `5分钟前`，**在编辑页首屏，不在课程时间设置页**

### 课程提醒时间弹层

- 点开是 RadioGroup（非 Canvas，文本可读）：`不提醒 / 任务发生时 / 5分钟前 / 15分钟前 / 30分钟前`，底部「取消」
- ⚠️ RadioButton 的 `checked`/`selected` **本 ROM 不暴露在 dump 里**
  → 别用 `enabled/checked` 判定；改读**行内显示值**（选中即时更新，弹框里没有"确定"）
- 点其它项即时换行内值；点「取消」/点外面关弹框都**保留新值**

### ⭐ 提醒触发时间 = 事件开始时间 − 提醒偏移

**这条是造「等提醒」类数据的前提，算错就白造。**

```
触发时刻 = 事件开始时间 − 提醒偏移
  提醒偏移取：任务发生时(0) / 5分钟前 / 15分钟前 / 30分钟前
  日程分类的默认值是「15分钟前」

例：事件设 10:19，提醒「15分钟前」→ 触发时刻 = 10:04
```

**⚠️ 触发时刻已经过去 → 保存后立即弹通知**（不是不生效！）

```
当前 10:16，事件 10:19 + 「15分钟前」→ 触发点 10:04 已过
→ 保存的瞬间就弹提醒，alarm 列表里也查不到（因为已经触发掉了）

❌ 别把这个误判成"提醒没注册"——它注册了，只是立刻触发了。
```

**要「等一会儿再响」，反推开始时间：**

```
1. adb shell date 拿设备当前时间（别用本机时间，可能校时跳变）
2. 开始时间 = 当前时间 + 提醒偏移 + 操作余量（至少 5 分钟）
3. 建事件时就把开始时间设成这个值
4. 执行"结束进程"等操作，然后等到点
```

**⭐ 「提醒生效了没有」的判据：看通知栏**

**最简单的方法 —— 直接把通知栏拉下来看：**

```powershell
# 1. 拉下通知栏
python tools/act.py swipe --from <屏幕上方> --to <屏幕下方>
# 2. observe 看屏幕上有没有那条日程通知（截图/OCR 都能看到标题）
```

**这也是用户实际做的动作** —— 提醒来了，就是通知栏里多一条。

**要看结构化数据时（不用拉通知栏）：**

```powershell
adb shell "dumpsys notification --noredact" | Select-String "android.title"
# 只看日历的
adb shell "dumpsys notification --noredact" | Select-String "android.title=.*日程"
```

```
例：android.title=String ([日程] 测试83日程E)     ← 提醒真的来了
```

**这是用户实际看到的东西。** `dumpsys alarm` 是系统内部调度表 ——
alarm 存在≠用户收到通知，alarm 不在≠通知没来（触发后就从表里消失了）。

> ❌ **不要用 `dumpsys alarm` 判断"提醒有没有生效"** ——
> 触发过的 alarm 会从表里移除，查不到不代表没生效。
> 实测中 AI 正是这样误判了 5 次，重造了一堆事件。
>
> alarm 列表只在**造数据阶段**有用：确认"闹钟排在未来某个时刻"，
> 判断的是「我设的时间对不对」，不是「用户有没有收到」。

**三种会白做的情形（实测都踩过）：**

```
① 开始时间设成「现在或过去」→ 触发点已过 → 保存瞬间就弹
② 用了默认「15分钟前」但开始时间只比现在晚 10 分钟 → 同样是过去
③ 触发点已过时去查 alarm 查不到，就断定"没注册" → 白重建
```

> 实测代价：一条用例为此连建 5 个事件（A~F），40 分钟才试对。

**⚠️ 但「到点了通知没来」不一定是造数据错 —— 可能是被测行为本身：**

```
如果用例要求「先结束进程，再等提醒」，那么：
  进程被杀 → 闹钟可能随之取消 → 通知不来

这是「提醒在进程结束后能否到达」的**被测结果**，
不是造数据失败。判 FAIL 或 PASS 要看用例怎么写的，别急着重造。
（真实机制见上面「结束后台运行」那一节：force-stop 会 pi_cancelled）
```

## 图库导入确认页 `TempConfirmTimetableActivity`

- 断言用控件：名称 `et_schedule_name`（**预填**「学生课程表」/ 学期名）、完成 `btn_finish`、
  学期开始 `layout_semester_start_date`/`tv_semester_start_date`、
  当前周数 `layout_current_week`/`tv_current_week`、总周数 `layout_total_weeks`/`tv_total_weeks`、
  周末有课 `switch_weekend_classes`、显示非本周 `switch_show_non_current_week`
- **学期总周数弹框 = 滚轮选择器**：当前值居中高亮，数字为 Canvas 绘制 **dump 读不到**
  → 用 OCR 定位数字坐标后点选
- 学期开始点开为日期选择框：`rid=datePicker`，标题形如「2026年9月1日 周二」，
  按钮是 `left_button`=取消 / `right_button`=确定（⚠️ 不是 `android.widget.DatePicker`）
- ⚠️ **总周数弹框的按钮 rid 与日期弹框不同**：总周数是系统对话框 `button1`=确定 / `button2`=取消；
  日期框是 `left_button`/`right_button`。别混用

## 确认页导航关系

- 「确认课程表基本信息」页点左上角返回 → 「确认识别结果」页（`TempTimetableActivity`），
  **不是**课程表页也不是主页；要重进就再点「下一步」
- 识别结果页顶栏右侧**没有图标**
- 确认页点「完成」→ 创建课程表 → 课程表列表（含「全部课程表 / <课表名> / 当前 / 设置」）
- 「当前」标签跟在**原有**课表后面 —— 新导入/新建的课表**不会自动抢占当前**

## 课程时间设置页 `TimeSlotSettingsActivity`

入口：编辑页的「课程时间设置」行 `layout_time_settings`。

默认值都有独立 rid，**可直接断言，不用 OCR**：

| 字段 | rid | 图库导入路径 | 手动创建路径 |
|---|---|---|---|
| 每节课上课时长 | `tv_lesson_duration` | 30分钟 | 50分钟 |
| 课间休息时长 | `tv_break_duration` | 10分钟 | 10分钟 |
| 上午课程节数 | `tv_morning_slot_count` | 4节 | 4节 |
| 下午课程节数 | `tv_afternoon_slot_count` | 5节 | 4节 |
| 晚上课程节数 | `tv_evening_slot_count` | 0节 | 4节 |

**默认值因创建路径不同**（上表是「图库导入」和「手动创建」两条路径的值）。

- 时长两行的**可点容器**是 `layout_lesson_duration` / `layout_break_duration`；
  `tv_lesson_duration` / `tv_break_duration` 只是值节点（`clickable=false`），点值节点无效
- 节次明细「第1节」与「08:00-08:30」是**两个独立文本节点** → 要分别匹配，
  不能要求同一节点同时含「第」和「-」（那样永远匹配不到）
- 两个时长弹框的内容区是 **Canvas 自绘**（`customPanel` 下无 dump 子节点），当前值读不到；
  数值用 OCR 补读 —— 弹框半透明，OCR 会混入背景文字
- 「上午课程 / 下午课程 / 晚上课程」是**分组标题**（`clickable=false`），点了页面不动是正常
- 返回确认页用 BACK 键；**别点本页「完成」**（会直接创建课程表）

### 坑：必须先读值再操作

**元素不在当前视口时按 rid 读返回 `''`** —— 直接判会连报失败。顺序：

1. 进页面立刻读首屏值（上课时长 / 休息时长 / 上午 / 下午节数）
2. 再上滑读 `tv_evening_slot_count`（懒加载）
3. 要断言的「默认时间显示」必须**在改时长之前**采集快照

第 3 点尤其关键：**节行时间随「上课时长/课间休息」实时重算** ——
把时长拨到 30 分钟后所有节行变成 30 分钟制，此时再读原始时间已不是原值。

### 节行定位

所有小节共用 `item_container` / `tv_slot_number`（第N节）/ `tv_time_range`（时间）/
`iv_arrow`（可点，desc=编辑）。**没有 per-slot rid** —— 定位第 N 节 = 取 `iv_arrow`
**按 bounds 的 y 排序后取下标**。

跨屏读全部节行要逐屏下滚 + 并集累加（UI 树只返回当前视口，一屏装不下 12 节）。

### 节数 ＋/－ 按钮是 ImageView

「+」「－」是图形（OCR 会读成粘连文本）。固定 rid：
减号（desc=删除）`btn_remove_<morning|afternoon|evening>_slot`；
加号（desc=新建）`btn_add_<morning|afternoon|evening>_slot`。

### 弹框滚轮：上课时长与课间休息都是双向循环

- **每节课上课时长**（`{30,35,…,120}`，每档 5）：**循环** —— 120 再拨 → 30分钟
- **课间休息**（`{5,10,15,20,25,30}`，每档 5）：**循环** —— 5 再上拨 → 30分钟、30 再上拨 → 25分钟

方向：**上=减小、下=增大**（两个轮一致）。

⚠️ **拨档 `--gap` 不能太小**：`--gap 350` 时点 14 次只生效 5 格（点击被丢弃）；
`--gap 800` 时点 9 次全部生效。**别把"点了几次"当拨了几档，以 OCR 回读为准。**

### 坑：OCR 偶发漏读时别让 OCR 进判定循环

目标值 5 但 OCR 在弹框内返回 None → 以为"没动"继续点 → 在循环上绕圈 → 走满上限、原地打转。

**正确做法**：按**档位次数**点按（不读 OCR 反馈），点确定，**读 UI 树文本**
（`tv_lesson_duration`/`tv_break_duration`）作判据。

## 节行编辑弹窗（双滚轮）

- 设置页 desc=编辑 的 arrow，**按 y 排序后前 2 个是「上课时长/课间休息」行，第 3 个起才是节行**
  —— 按序号定位前先排序
- 节行弹窗 = 同款 Canvas 双滚轮（左=开始时+分，右=结束时+分），无 UI 节点，读值用 OCR
- **改值用点按 ±1，用 `--repeat` 一次点完**：点选中行**下方一格=+1、上方一格=−1**
- 修改任一小节点「确定」后会弹**「是否根据课程时长和休息时长自动调整其他课程？」**
  → 点「确定」（**真实产品流程，不是干扰弹窗**）
- **同步规则**：第1节结束改 :53 后，上午后续节 = 前节结束 + 课间休息；
  **下午/晚上不受影响**；「每节课上课时长」不变
- **冲突表现（上午末节结束 > 下午开始）**：冲突两节的时间文字**变红**（UI 树无色值，看截图）；
  点右上角「完成」→ toast **「课程时间有冲突，无法设置」**（窗口约 2-3s）
  - 抓这个 toast：点击后**立即** OCR 整屏匹配「冲突」；中间多一次 observe 截图就会占满窗口
- ⚠️ 本页右上角「完成」是 `save_view`，**bounds 中心点击可能无反应**，必要时带偏移

## 重复结束页的『自定义结束时间』点了没反应（已知缺陷）

`.event.options.EventLastDateActivity`：

- 结构：`toolbar_title`=重复结束｜『永不结束』复选框 + `lastSwOver`(Switch)
  ｜`lastTvOverText`=『自定义结束时间』｜`lastTvOvertime`=日期文字
- **『永不结束』默认是勾选的**；此时自定义结束时间的日期为**灰色**不可用
- 点 `lastSwOver` 或复选框可取消勾选 → 该行文字与日期**变黑**（界面认为可交互了）
- ⚠️ **但那一行点了没有任何反应**：容器 `lastLlDate` 在 dump 里 `clickable=true`，
  多点位置都试过，Activity 不变、无弹框、节点无变化；logcat 显示触摸**已投递到应用**且无异常
  ⇒ 是点击被吃掉，不是工具没点到
- 附带：`lastTvOvertime` 默认值为 **2024年1月1日**，早于当前日期，初始值本身不合理

## 权限弹窗（本 App 文案）

**通用机制（默认同意、先拒后允、rid 匹配）见 `_system.md`「权限-响应机制」，本卡不复制。**

本 App 实测（Android 17）：

- **首次** = 仅在使用时允许 / 仅本次使用时允许 / **拒绝**
- **拒绝后再请求** = 同上，末项变 **拒绝并不再询问**
- MEDIA 首次 = 选择照片 / 全部允许 / **拒绝**
- 拒绝后 App 自弹提示框（**不是系统弹窗**）：
  文案「需要权限」+「拍照和导入课程表需要相机权限，请前往设置中开启」/
  「访问相册和导入课程表需要存储权限，请前往设置中开启」，按钮 取消 / 前往设置

## 周视图

### 空格子与加号

- 空格子 = `cv_empty_content`（clickable）
- **进入空课表时：全部格子只有 `cv_empty_content`，没有 `iv_add_hint`**
- **点某个格子后：该格出现 `iv_add_hint`（加号浮标）**
- 再点 `iv_add_hint`：进入新建课程页
- 「+」是图，无 text / desc，只能靠 rid 判（OCR 认不了图标）

### 结构

- toolbar 标题 = 课表名，副行 `tv_date_range` = 当前周数（第N周）
- 结构 = viewPager + 表头周几（`tv_monday…tv_friday` + `_date`）+
  recyclerView 网格（`tv_section` 节号 + `tv_time`）

## 添加/编辑课程 **弹窗**（`EditCourseActivity`，浮层形态）

⚠️ **形态要点（别判错）：** 实现是 `EditCourseActivity`，但**视觉上是浮层弹窗** ——
圆角卡片压在周视图上、背景压暗、下面的课表还在、关闭后回到原处。
dump 里**没有** `Dialog` 字样，activity 名也不带 Dialog，**但这就是弹窗**。
用例说「弹出编辑弹窗」时按浮层判，不要按 activity 名判成"整页"。

「新建课程」与「编辑课程」是**同一个弹窗**：

- 字段 rid：课程名 `etCourseName`（必填）/ 教室 `etClassroom` / 备注(老师) `etTeacher`；
  课程时间 `llCourseTime` / `tvCourseTime`（默认"第1节"）；上课周数 `llCourseWeeks` /
  `tvCourseWeeks`（默认"第1-20周"）；背景色 `llCourseColor`（行显示 `viewColorIndicator`）
- **底部「删除课程」= `btnDelete`**（仅编辑态有）；右上角「完成」= 保存退出；左上角 desc=关闭
- **「课程背景色」点行弹独立 AlertDialog**（`alertTitle`/`customPanel`/`buttonPanel`），
  `customPanel` 内 **10 个纯色块**（clickable 无 text/desc）
  - ⚠️ **数色块要看图**：UI 树数 clickable 会被嵌套/装饰节点干扰（实测 25≠10），
    看当步 `shot.png` 数，或问视觉模型
- **布局**：`curriculum_scroll_view` 内容区在本机型一屏内 —— **所有字段都在首屏**。
  踩过的坑：误以为"在首屏之外"就无脑上滑，把本来可见的元素滚出屏幕上方、越滚越找不到
  → **读不到元素时先滚回顶部再逐屏向下找（双向）**

## 课程卡片 → 编辑页是两段，不是一段

点周视图课程卡片 `curriculum_card_view` → **先进「课程列表」弹框** `CourseListActivity`
（`clCourseDetail` / `tvFirstLine` 课程名 / `tvSecondLine`「第1节 | 第1-20周」）→
再点 **`ivEdit`**（desc=编辑）才进编辑页。

直接按"点卡片=进编辑"写会挂在找不到 `etCourseName`。

**删除确认框**：标题「确定删除此课程吗？」，取消 `android:id/button2`，删除 `android:id/button1`。

- 同名课程可以有多门（按名称数个数不准）
- ⚠️ **基准必须在周视图上取**：编辑页里读不到课程列表，会拿到空；
  而 `空集 ⊆ 任意集合` 恒真 → 「取消后课程仍在」会变成**无论是否真删除都通过**。
  基准务必在打开编辑页**之前**、回到周视图后取
- ⚠️ **网格异步渲染**：进周视图立刻 dump 可能拿到空 recyclerView →
  按 rid 轮询等待，别一次性读取就下结论

## 后续补充

- 进入空课表时全部格子只有 cv_empty_content，没有 iv_add_hint
  （证据事件 16, 17）

- 进入空课表时全部格子只有 cv_empty_content，没有 iv_add_hint
  （证据事件 16, 17）
