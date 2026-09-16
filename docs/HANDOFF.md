# WeChatExportPlus 接管文档（截至 v3.0.1 · 2026-09-16）

> ✅✅ **2026-09-16 第四轮（最新）—— WebView2 已放弃，回到 v3.0.0 的稳定基线。**
> 用户拍板："参考一下 3.0.0 版本，那个我感觉挺稳定的，功能都是正常的，就是输入有些 bug。"
> 所以默认后端改回 **`embed`（Electron 真嵌入）**，本轮只干一件事：把输入那条路查清。
>
> **本轮实测出来的四条硬结论（取代下面 2026-09-16 那段旧警示的第 1 条）：**
> 1. **本机的 `SendInput` 输入管线是通的** —— 用 Tk Entry 对照实测 `entry='abc123'` 成功。
>    上一轮之所以认定"合成输入不可信"，是因为用的是 `PostMessage`/`WM_CHAR` **投递**
>    （那条路确实不通，连独立顶层窗口都写不进去）。**别再拿"投递"的结论否定整类输入实验。**
> 2. **跨进程子窗口真的收不到真实键盘** —— 这次是**可信**证明（同一个进程、同一个窗口、
>    同一串按键，唯一变量是有没有 `SetParent`）：
>
>    | 形态 | `GetParent` | 真实按键 |
>    |---|---|---|
>    | 独立顶层窗口 | 0 | ✅ 收得到 |
>    | **`SetParent` 子窗口** | =宿主画布 | ❌ **收不到（空）** |
>    | **owner 窗口** | 顶层窗口语义 | ✅ **收得到** |
>
>    脚本：`C:\My_AI_Gongju\ds\projectOne\test_crossproc_kbd.py`（用另一个进程的
>    WinForms TextBox 当靶子，文本落盘做地面真值）。
>    → 所以 v3.0.0 用 `SetParent` 是**根上的错**，也是它"只有输入有 bug"的原因。
> 3. **"退回页签再进来卡死"的真因已找到并修掉**：**owner 设好之后，对"隐藏中"的窗口
>    调 `show()` 会让 Electron 主进程卡死**（`/show` 10 秒超时）。实测 10.02s → 修后 0.01s。
>    修法：`show()` 先摘 owner → `/show` → 再挂回 owner（见 `host.show()`）。
> 4. **不要再用 `AttachThreadInput` 永久连接**（v3.0.0 的 `_attach_input`）：那是 IME
>    跨进程死锁的成因（界面变白、CPU 却不高 —— 正是用户报的"卡死"）。
>
> ⚠️ **仍未闭环的一条**：把**模拟**按键送进 **Chromium** 这件事我做不到（独立 Electron
> 也收不到，Tk Entry 却收得到），所以我无法用脚本证明"手按键盘能打字"。
> **这一跳必须由用户手按验收** —— 但注意它测的是我做不到的模拟投递，不是 app 的缺陷。
>
> 下面这段是 2026-09-16 早些时候（WebView2 那轮）的旧警示，**第 1 条已被上面取代**，
> 第 2 条（别批量杀进程）仍然有效：

> ⛔⛔ **（旧）新会话先看这条（2026-09-16）**：WebView2「真嵌入」那一轮**没做成**，
> 用户明确反馈"一个都没有修复"（开全屏卡死/比例错乱、打几个字符卡死、
> 退回导出页再进来不重新嵌入）。**详细交接见**
> `C:\My_AI_Gongju\ds\projectOne\接档-WebView2真嵌入未完成-20260916.md`。
>
> 三条硬结论，先记住再动手：
> 1. ~~**这台机器上"模拟输入类"的实验结论全部不可信**~~ —— **已推翻，见上面第 1 条**：
>    不可信的是 `PostMessage` 投递，`SendInput` 是可信的（且必须先把窗口弄到前台，
>    用 `AttachThreadInput` 临时连接可以绕过前台锁）。
> 2. **别用 PowerShell 批量杀进程**（`Get-CimInstance | Stop-Process`、`taskkill /T`）：
>    已经把执行工具的宿主进程误杀过好几次（整会话被干掉）。
>    **只按明确 PID 操作。**
> 3. 官网页签的后端可以用设置 `ds_backend` 在 `embed`（默认）/ `webview2` /
>    `electron` 之间切；`webview2` 那套三个现象都没解决，只作为历史保留。

---

## 0. 一分钟速览

| 项 | 状态 |
|---|---|
| 项目 | 把微信 PC 端聊天记录批量导出成「给 AI 用的语料」，并**内嵌 DeepSeek 官网按批自动投喂** |
| 当前版本 | **v3.0.1**（`gui/app_plus.py` 里 `APP_VERSION`） |
| 已发布 | v3.0.1 / v3.0.0（都是正式版，latest = v3.0.1） |
| 仓库 | https://github.com/Lan-DQ/WeChatExportPlus （public，main 分支，工作区干净） |
| HEAD | `8c66dbf Update WeChatExportPlus` |
| 测试 | `pytest tests/ -q` → **219 passed**；假官网页自检 **27/27**；窗口形态验收 **12/12** |
| 下一步建议 | ① 更新 `README.md` / `使用说明.md`（还写着 v2.2.0）② 回复 issue #1（文案已备好） |

**当前最大待验证项**：跨进程子窗口的键盘输入修复（`AttachThreadInput`）只在代码层完成，
**没能在本机自动化验证**（会话里无法把窗口置为前台），需要用户手动确认。

### ⚠️ 2026-09-15 未提交的改动（13 个文件 + 3 个新文件）
官网页签的**窗口方案被整体重做**（放弃 `SetParent` 嵌入 → 独立顶层窗口），
另有 4 个真 bug 修复。**细节见第 5.6 / 5.14 / 5.16 / 5.18 / 5.19 / 5.20 节**，摘要：
1. `ds_bridge/host.py`：加 `import ctypes.wintypes`（缺了它，3.0.1 的键盘修复在发布版里
   每次调用都抛异常被吞掉）；删掉整段 `AttachThreadInput`；新增 `place/summon/set_topmost/
   focused_info/type_text/set_background`，其中 `place()` 必须做 **DPI ÷1.25** 换算（见 5.18）。
2. `dsview/main.js`：新增 `/bounds` `/summon` `/topmost` `/focused` `/bg`；`/focus` 里删掉
   `showInactive()`（它把刚设好的焦点又撤掉，见 5.19）；`/type` 按目标类型选注入手段。
3. `dsview/renderer.js`：`findComposer` 不再无条件跳过 `<input>`（见 5.20）；
   新增 `focusedTarget/focusTarget/focusedInfo/composerHasText/setComposerValue`。
4. `gui/app_plus.py`：官网页签改独立窗口摆位/跟随/置顶；**布局表 `ds_page_layout()`**
   （见 5.25）；最小化跟着藏（见 5.26）。
5. `dsview/check_renderer.py`（**新文件**）：机械检查 renderer.js 的模板完整性，
   已接进 pytest（`test_renderer_js_template_intact`）—— 因为那天我踩了 3 次
   "注释里写反引号截断模板"的坑。
6. `build_dist_plus.py`：`HIDDEN_IMPORTS` 加 `ctypes.wintypes`。
7. 测试：`test_ds_plan.py` / `test_ds_sender.py` 里写死的 "50 个一批" 改成 30；
   `test_ui_invariants.py` 换掉已删除的「发给 AI」输入框测试、加 renderer 检查、
   加 6 条官网页签布局/焦点/底色的不变式（见 5.25~5.27）。

**已构建好的测试包**：`<构建输出目录>\WeChatExportPlus_v3.0.2_fix\`
（含上述修复；已铺好 `.ui_settings` 与登录态副本；**未发布**，仅供真机验证。）

**打包版已验证到什么程度**（2026-09-15，四个脚本，都可复跑）：
| 脚本 | 断言 | 验的是什么 |
|---|---|---|
| `ds_window_accept.py` | **12/12** | 仓库源码的窗口形态：顶层性/摆位 DPI 换算/summon 焦点/topmost/`/type` 定向注入/`/bg` 底色 |
| `ds_exe_e2e.py` | **5/5** | exe 能启动、无 `启动错误.log`/`.ui_errors.log`、**`.boot.log` 被自动清理**、零残留 |
| `ds_pkg_dsview_check.py` | **8/8** | **包内那份 dsview**：新路由、DPI 摆位、顶层性、`/summon` 焦点，以及在真官网登录页写进手机号 |
| `ds_writein_flow.py` | **3/3** | 兜底路能不能登录：定位手机号框 → 写入 → 没写错框 |


---

## 1. 路径地图

| 用途 | 路径 |
|---|---|
| 本地仓库 | `C:\My_GongJu\grab\WeChatExportPlus` |
| 运行时依赖来源（内核） | `C:\My_GongJu\grab\WeChatExportPlus_发布版`（`runtime/dll/electron/resources` 是指向它的 junction） |
| 打包产物 | `C:\My_GongJu\grab\WeChatExportPlus\dist\WeChatExportPlus\`（**唯一目录**，登录态/标签/设置都在里面） |
| 发布 zip | `C:\My_GongJu\grab\WeChatExportPlus_v3.0.1_win64.zip`（225 MB，已剔除私人数据） |
| 私人数据备份 | `C:\My_GongJu\grab\_probe\private_backup\`（`.ui_settings` / `会话标签.json` / `ds_profile`） |
| 探针脚本（不在仓库） | `C:\My_GongJu\grab\_probe\` |
| 用户工作区（放截图/给新会话读的文档） | `C:\My_AI_Gongju\ds\projectOne\` |
| 用户导出数据（测试用真实文件） | `C:\My_GongJu\grab\数据1\导出_20260914_1856\` |

⚠️ 该仓库目录 **2026-09-13 被误删过一次**，从 GitHub 重新 clone 恢复。代码不会丢，但本机 token 不在仓库里。

---

## 2. 铁律（动手前先读）

1. **绝不杀用户的进程**。要清理只按**可执行文件路径**匹配（例：`Where ExecutablePath -like '*dist\WeChatExportPlus*'`）。用户自己开着软件时打包会 `WinError 32/5`，那就换 `--out dist\WeChatExportPlus_vXXX` 打，或先问用户。
2. **私人数据三样，打包前必须删**：`.ui_settings`、`会话标签.json`、`ds_profile\`（含登录 cookie）。用 `_probe\pack_release.py`（它会先备份→删除→复查→校验必需文件→打包→抽查 zip）。
3. **改完必须验证，并说清验证边界**。能自动化的就自动化（pytest / 假官网页 / 探针）；不能验证的**明说"没验证"**，别含糊过去。
4. **别往用户的 DeepSeek 会话里发测试消息**。已经发过若干条（`WeChatExportPlus_测试*.txt`、`验收*`、`合成文档*`、`TextSend 自检` 等），再测优先用**假官网页**（`dsview/verify_mock.py`）。
5. **PowerShell 的坑**：不支持 `<<'PY'` heredoc；`Set-Content -Encoding UTF8` 会把中文写坏。要跑多行 Python 就**写成 `.py` 文件再执行**，写文件用 write 工具。
6. **tkinter 只能有一个 root**：GUI 测试统一放 `tests/test_ui_invariants.py`（module 级共享 root），别另开测试文件建 Tk。
7. 回复用户：**中文、先结论后细节**，改完给一句"你现在可以做什么"。
8. 发布前**必须问或至少确认**：发布是外部可见动作。用户明确说"你帮我发布"时才发。

---

## 3. 架构地图（3.0 之后）

```
gui/                        界面（全自绘 Canvas，不用 ttk）
  app_plus.py               ★ 主程序：业务编排 + 三个页面（首页/会话/官网页签）约 2500 行
  session_tags.py           会话标签存储（原子写 会话标签.json）
  ui_widgets.py             自绘控件：Surface/Button/Entry/CheckList/Checkbox/Dropdown/ProgressBar
  ui_theme.py               配色/背景/动画；rgb2hex 接受 '#rrggbb' 字符串
exporters/
  batch_export.py           批量导出主流程；unique_base/safe_name
  message_content.py        消息解析（最关键最难的模块）
  md_exporter.py            Markdown 单文件；build_image_map(rows, prefix) → 会话名_0001.jpg
  ai_prompt.py              「给 AI 的指令」模板（外部 AI提示词.txt 优先，内置默认兜底）
  index_exporter.py         导出清单.html
  ai_exporter.py / html_exporter.py / pdf_exporter.py / csv_exporter.py / excel_exporter.py
scripts/                    内核桥接（很少改）
  wcdb_server.py            拉起 node/electron 子进程 + HTTP；decrypt_image.js 解图
ds_bridge/                  ★ 3.0 新增：Python 侧的 DeepSeek 投喂桥
  host.py                   DeepSeekHost：启动/握手/嵌入(SetParent)/显示隐藏/聚焦/attach/send/stop/type/diag
  plan.py                   扫描导出目录 → 展开单元（文档在前、图片在后）→ 分批
  sender.py                 BatchSender：逐批 挂载→发送→等答完/超时截断→间隔；ETA 与失败指引
dsview/                     ★ 3.0 新增：内嵌官网页宿主（Electron，复用软件自带 electron.exe）
  main.js                   窗口(show:false)+HTTP 控制接口(/attach /send /stop /type /focus /state /last /diag /eval…)
  renderer.js               ★ 页面侧 helper（结构性启发式：找输入框/发送键/停止键/附件计数）
  mock/ds_mock.html         假官网（自检用，**故意不给按钮加 id/aria-label**）
  verify_mock.py            假官网页端到端自检（27 条断言）
tests/                      190 个测试
  test_ui_invariants.py     ★ 界面回归安全网（含官网页签各交互）
  test_ds_plan.py / test_ds_sender.py / test_session_tags.py / test_datadir.py
  test_md_exporter.py / test_batch_export.py / test_message_content.py / test_ai_prompt.py …
```

**数据流（批量投喂）**：
```
用户勾选导出目录里的 md/图片
  → plan.expand_units 排序（给AI的指令.txt 永远第一个）→ plan_batches(每批 30)
  → 每批：host.attach(settleMs=文档250/图片150ms) → 等"发送键连续两次可用" → host.send()
        → 等它自然答完（超 25s 才截断）→ ETA 更新 → 下一批
  → 最后一批：host.send(text='我已发送完毕') 附在消息里
```

---

## 4. 3.0 / 3.0.1 做了什么

| 功能 | 关键实现 |
|---|---|
| 会话标签 | `gui/session_tags.py`（`会话标签.json`）；`CheckList.set_selected_wxids()` 替换语义；行标题后内联显示标签 |
| 图片文件名带会话前缀 | `md_exporter.build_image_map(rows, prefix)` → `群聊A_0001.jpg`；`batch_export` 传 `unique_base`；正文仍 `[图片0001]` |
| 内嵌 DeepSeek 官网页签 | `dsview`(Electron) + `ds_bridge.host`；`SetParent` 真嵌入；UA 伪装 + 独立 `--user-data-dir`；主题跟随 |
| 按批自动投喂 | `ds_bridge.sender.BatchSender` + 界面进度窗（含**实时剩余时间**、失败时给手动补救指引） |
| 分批接收协议 | `AI提示词.txt` 首段：发完之前只回「我已接收上述信息」，收到「我已发送完毕」再分析；**发送前自动刷新导出目录里的指令文件** |
| 数据目录自动检测 | `app_plus.find_xwechat_dirs()`：优先读微信自己记的位置（4.x `%APPDATA%\Tencent\xwechat\config\*.ini`、3.x 注册表 `FileSavePath`），再「文档」(含 OneDrive 重定向)，再各盘根+下一层 |
| 「发给 AI」通道（3.0.1） | 官网页签顶部输入框 → `host.type_text()` → Electron `insertText`（**不依赖键盘焦点**），可带 submit |
| 键盘焦点修复（3.0.1） | `host._attach_input()`（`AttachThreadInput`）+ `host.focus()` + `/focus` 路由；`show()` 后自动聚焦 |

---

## 5. ★ 坑清单（3.0 新增部分，每条都真踩过）

### 5.1 `dsview/renderer.js` 整个是一个模板字符串
helper 源码被塞在 `String.raw` 模板里当字符串注入。所以：
- **绝不能出现反引号或 `${`**（会截断模板）；
- 正则里的反斜杠要**写双份**（`\\s`），否则被模板吃掉 —— 曾导致 `pathCmds` 恒为空、发送键永远找不到；
- 改完必须 `node --check dsview/renderer.js`。

### 5.2 数附件必须限定在「输入卡片」里
现象：一条 50 个附件的消息发出去后，程序数到"输入区残留 37 个附件"并中止整轮。
根因：已发消息里的附件卡片/`blob:` 缩略图与输入区长得一样，扫整页就会把它们算进来。
修法：`renderer.js` 的 `inputCard()` —— 先找"同时含输入框和发送键"的**输入行**，
再看它父层有没有附件标记（附件条是输入行的兄弟），有就取父层。
实测 DOM：`textarea → div._24fad49 → div._020ab5b(输入行) → div._77cefa5(卡片)`。

### 5.3 发送键会「闪禁用」，且**绝不能轻易退化到回车**
- 挂完附件后发送键在**可用/禁用之间来回跳**（站点还在处理文件）→ `waitSendReady` 要求**连续两次**可用才算就绪，按钮路径要**重试**（最多 60s）。
- 点按钮不成就用回车兜底，会造成：**把还没传完的消息提交上去 → 官网报「请检查网络后重试」**，看起来就是"没发出去"。
  现在只有按钮路径彻底没戏才用回车；且 `clickSend` 遇到禁用态**如实返回 `how:'disabled'`，不假装点过**。
- 曾经的致命 bug：给 `clickSend` 加禁用检查时**误删了 `var p = pickSend();`** → 函数抛 `ReferenceError` → 按钮从没被点过。**改完这类函数一定用 `/last` 看内部记录。**

### 5.4 官网单条消息其实接不下 40 个附件（实测）
| 一批数量 | 结果 |
|---|---|
| 20 / 30 | ✅ 成功 |
| **40** | ❌ 每个附件显示「服务器繁忙」，整条消息报「请删除异常文件再发送」 |
| 50 | ❌ 同样失败 |

所以：默认**每批 30**（可选 10/20/30）、默认**只发文档（建议）**、每个文件挂上后等
**文档 0.25s / 图片 0.15s**（`sender.settle_ms_for`）再点发送。**别再往上调。**

### 5.5 截断要"先等自然答完"
用户反馈：被截断的消息在官网侧是"没说完"的状态，**不利于参与上下文**。
现在：先等 `streaming` 自然变 false（新协议下只回一句，1~3 秒），**超过 `reply_timeout=25s` 才截断**。
另外**纯文字消息**（无附件）发出后输入框立刻只读，靠"输入框清空/附件消失"判不出成功 ——
补了第三条判据：**发送前没生成、发送后开始生成 = 提交成功**（`streaming-started`）。

### 5.6 跨进程子窗口收不到键盘（3.0.1 修）
现象：切到别的页面再回官网页签，**输入框打不了字**（点击后 `document.hasFocus()` 都是 true）。
根因：Electron 窗口被 `SetParent` 挂成宿主的子窗口，但属于**另一个进程**；Windows 只把键盘消息
派发给"前台窗口所在输入队列"里的焦点窗口。**这是 `SetParent` 跨进程的已知限制。**
修法：`host._attach_input()` 用 `AttachThreadInput` 把两个线程的输入队列接起来并**保持连接**，
`SetFocus` 才生效；同时 `/focus` 让 Electron 恢复 WebContents 焦点。`host.show()` 会自动调 `focus()`。
兜底：`/type` + 界面顶部「发给 AI」输入框（走 `insertText`，不吃键盘）。
⚠️ **UU 远程 / 向日葵类工具下仍然打不了字** —— 它们注入合成按键到"当前前台窗口"，
层级对不上就被吞掉。这**不是软件 bug**；本机登录一次即可（登录态存 `ds_profile\`）。

### 5.7 `did-start-loading` 会误清页面就绪状态
子资源加载也会触发它，且没有配对的 `did-finish-load`。就绪判定改用**主框架的 `did-start-navigation`**；
`/state` 同时给出 `readyState`，preflight 两者取一。

### 5.8 Tk 界面：嵌入的浏览器是**独立 Win32 子窗口，永远盖在画布之上**
所以：toast/弹层/下拉浮层**不能压在那块矩形上**（会被盖住，用户点不到）。
- `_toast_rect()` 把提示挪到左栏；
- 「每批 N 个」用**点击循环按钮**而不是 `Dropdown`（下拉浮层会被盖住）；
- 官网页签的选项一律放在 `x < 382` 的左栏里。

### 5.9 工作线程绝不能碰 Tk
发送线程只往 `queue` 丢消息，主线程用 `after` 轮询消费。以前线程里直接 `root.after` 会抛
"main thread is not in main loop"，且**线程崩了没人置回 `_ds_sending`**，界面永久卡在"发送中"。
`_ds_ask` / `_ds_send` 这类入口都要 `try/except` 包住。

### 5.10 UI 异常默认被静默吞掉
`Surface` 现在有 `on_error` 钩子：异常写 `.ui_errors.log` 并弹 toast。
历史上就因为静默吞异常，标签弹窗"空白一闪"查了很久（真因：`rgb2hex('#rrggbb')` 抛 ValueError）。

### 5.11 假官网页（`dsview/mock/ds_mock.html`）是 **contenteditable**
真官网是 **textarea**。我试过把假页面改成 textarea，结果**牵连到停止键的几何判据**
（`pickStop` 找不到停止按钮 → `/state.streaming` 恒 false），已回退。**别随手改这个夹具**；
要改就同时跑 `python dsview\verify_mock.py` 确认 26 条断言全绿。

### 5.12 打包/进程占用
`WinError 32/5`（exe/DLL 被占）几乎都是**用户实例或我自己的孤儿 electron** 还活着。
按路径匹配杀**我自己的**，或 `--out` 换个目录打。生命周期自检：`_probe\lifecycle_test.py`（开官网页→关窗→应 0 残留）。

### 5.13 发布资产的坑
- 资产名**必须和发布页里的链接一字不差**（v2.2.0 曾被改成 `WeChatExportPPPlus_...` 多一个 P → 链接 404）。
- zip 用 **`Compress-Archive`**（tar 会搞坏中文文件名）。
- 发布后用 `_probe\verify_release.py` 复查：draft=false、latest 指向、**下载链接 HTTP 200**、tag 里有新源码。

---

### 5.25 ★★ 官网页签的布局表（2026-09-15 第三轮，文字重叠的根治）
事故：用户截图圈了 6 处文字重叠 —— ①勾选统计那行被「发送清单」标题盖住；
②副标题撞页签按钮行；③警示撞工具栏；④「只发文档」复选框压清单下沿；
⑤底部「导出目录」与提示叠成一团；⑥右下角调试日志压选项行。
**根因不是字体，是那些 Y 坐标各写各的**：副标题 70 / 工具栏 96 / 提示 132 /
警示 148 / 清单 164 / 选项 H-58 / 路径 H-116 / 日志 H-24 —— 改一个就压另一个。

修法：**一张纯函数布局表** `app_plus.ds_page_layout(W, H, hint_lines, warn_lines,
left_buttons, right_buttons)`，自上而下推进，返回所有行/控件的矩形：

| 键 | 含义 |
|---|---|
| `header` | [(key, 中文名, 矩形, 基线, 高)] × 3（品牌标题 / 副标题 / 页签按钮） |
| `tool_rect` / `buttons` | 工具栏整行 + 每个按钮的 (id, 矩形)（按钮也会互相压，一并交给测试） |
| `hint_rect` / `warn_rect` | 提示行（左栏）/ 警示行（右栏，浏览器矩形**之上**） |
| `list_rect` | 发送清单（高度由**下面那几行**决定，不再 `max(140, …)` 硬撑） |
| `path_rect` / `opt_rect` / `log_rect` | 导出目录 / 选项行 / 底部日志（都在 x<382 左栏） |
| `browser_rect` | 浏览器真窗口占的矩形 |

几条**别改坏**的细节：
- 提示行/警示行按**像素宽度折行**（左栏只有 330px 宽，那句话实测 ~560px），
  折行数会反过来决定清单的起点 —— 所以 `_ds_layout()` 先量文字、再算表，
  `build_ds` 里也要**先用真实数据算出提示文字**再布局（否则会出现"按 1 行摆控件、
  最后画出 2 行文字"的错位）。
- 行距必须 ≥ 字体 linespace（9 号 Microsoft YaHei UI = **17px**），
  留 11px 会让第二行和第一行自己压自己（第一版就是这么错的）。
- 工具栏左栏按钮**按列宽自动折行**：x≥382 那一带会被独立浏览器窗口盖住，
  所以「演练分批/诊断」不能横着排过去（老代码在 1060 宽下就已经压在浏览器区里）。
- Canvas 的 `itemcget(tag)` / `itemconfigure(tag, …)` 在多图元同 tag 时作用于
  **显示列表里第一个**（不是最新建的）。所以**别留空的占位图元**：
  `build_ds` 里原来那个空的 `dshint` 占位会让 `itemcget('dshint')` 读回空字符串，
  看起来就是"提示行没画上"。现在每块文字只有一个带 `dshint`/`dswarn` 的图元
  （整块带 `dshintline`/`dswarnline`），测试会断言这一点。
- 最小窗口 940×640 下清单只剩 4 行左右（`minsize(940, 640)`）：所有控件都塞得下
  且不重叠，但再矮就不行了。

**回归测试（`tests/test_ui_invariants.py`）**：
`test_ds_layout_rows_never_overlap`（5 种窗口尺寸，任意两矩形不相交）、
`test_ds_layout_keeps_browser_and_clickable_columns`（浏览器区不许压可点控件）、
`test_ds_layout_matches_folded_text`（画出来的行数 = 算的行数）、
`test_ds_page_text_boxes_never_overlap`（真画一遍后用 tk bbox 再量一次，含清单表头）、
`test_ds_widgets_are_placed_at_layout_coordinates`（控件确实摆在表说的位置）。
截图：`C:\My_AI_Gongju\ds\projectOne\ds_page_shot.png`（脚本 `_probe\ds_shot.py`）。

### 5.26 官网页签"两个软件"的观感（2026-09-15 第三轮，方案 A）
用户："这完全是两个软件，只动一个都不行"。**不要回头去试 SetParent**（5.18 已实测否决：
嵌入后键盘整条通道全废）。方案 A 做了四件事：
1. **跟随更快**：`_on_root_configure` 的防抖 160ms → **60ms**（拖动时的错位感最明显）。
2. **窗口底色同步**：新增 Electron 路由 `/bg`（`host.set_background()`，
   `_ds_apply_theme()` 里跟 `/theme` 一起发）。页面重绘那一瞬间露出来的是窗口
   `backgroundColor`，纯白配深色面板就会闪白角。非法颜色直接拒绝（有断言）。
3. **占位框与浏览器矩形严丝合缝**：原来是 `x-6,y-6,+12` 外扩一圈，
   真窗口盖上去后四周留出一圈异色边 —— 现在同矩形、无外扩。
   另外 `roundedCorners: false`（Win11 会给无边框窗口加圆角，和直边对不上）。
4. **最小化跟着藏**：`<Unmap>/<Map>` → `_on_root_unmap/_on_root_map`。
   浏览器是独立顶层窗口，主窗口最小化时它**不会消失**，桌面上就留着一块官网。
   ⚠️ **别做"主窗口失焦就隐藏"**：用户是要在官网页面里打字的，点页面本身就会让
   主窗口失焦，那样会把正在用的窗口藏掉。

### 5.27 搜索框"光标停留在输入框"（2026-09-15 第三轮）
现象（用户原话）："输入关键词后鼠标光标会停留在输入框"。这是接档文件里那条
"还没查"的遗留项 —— 和"点会话跳出随机会话"（已修，见 `CheckList.item_index`）
是**两个**现象。
根因：搜索框是**叠在画布上的真 tk.Entry**，列表/按钮都走 `Surface.dispatch_input`；
在画布上按下鼠标**不会**自动把 Entry 的键盘焦点拿走，于是搜索完再点列表，
接着敲键盘还是打进搜索框。
修法：`Surface._install_input_pump` 的 handler 里，按下鼠标就 `canvas.focus_set()`。
回归测试 `test_click_on_canvas_takes_focus_away_from_search_box`。

**写这类焦点测试的坑**：必须用 `canvas.event_generate('<Button-1>', x=…, y=…, when='now')`
走**真实 tk 绑定**；直接 `sf.dispatch_input(ev)` 是绕开绑定的，永远测不到它
（第一版这么写，于是"修好了测试还红"）。

### 5.28 本机截 GUI 截图的正确姿势（2026-09-15 踩了四版）
`ImageGrab.grab(bbox=winfo_rootx…)` 在本机（多显示器 + 125% 缩放 + 窗口会被挪）
**裁不准**：tkinter 报逻辑像素、截图是物理像素，四次尝试分别截到了桌面和别的窗口。
最终可用的做法（脚本 `C:\My_GongJu\grab\_probe\ds_shot.py`，产物
`C:\My_AI_Gongju\ds\projectOne\ds_page_shot.png`）：
**Win32 `PrintWindow(hwnd, memDC, 3)`** —— 让窗口自己把内容画进内存 DC，
位置/遮挡/缩放都不影响。`3` = `PW_RENDERFULLCONTENT`（不加会拿到黑图），
配 `GetDIBits` + `Image.frombuffer(..., 'BGRA', 0, 1)`（负 biHeight = 自上而下）。
注意仓库路径是 `My_GongJu`（小写 g），用户工作区是 `My_AI_Gongju`，别写混。

### 5.29 ★ 别再拿 PowerShell 改中文文档（2026-09-15 本轮真踩，代价很大）
我为了把文档里的 "203 passed" 改成最新数字，写了：
```powershell
(Get-Content docs\HANDOFF.md -Raw) -replace '203 passed','219 passed' |
  Set-Content -NoNewline -Encoding UTF8 docs\HANDOFF.md
```
结果**整篇中文变乱码，而且不可逆**（有些字节被替换成 U+FFFD，信息已丢），
只能拿 `git show HEAD:docs/HANDOFF.md` 当底本、用 Python 重建；
工作区里没提交过的那些章节（5.14~5.24）只能按记忆补成摘要（见文末附录）。
铁律第 5 条早就写了 `Set-Content -Encoding UTF8` 会把中文写坏 —— 这次是我自己犯的。
**规矩**：中文文件一律用 write/edit 工具或 Python（`open(..., encoding='utf-8')`）改，
**不要**用 `Get-Content | Set-Content` 这条链路，连 `-Encoding UTF8` 也不行。


## 6. 构建 / 发布（逐条命令）

```powershell
cd C:\My_GongJu\grab\WeChatExportPlus
$env:PYTHONIOENCODING='utf-8'         # 中文输出必须设，否则控制台乱码

# 连开发环境（junction；新机器/目录被删后才需要）
python dev_setup.py --kernel "C:\My_GongJu\grab\WeChatExportPlus_发布版"

# 回归
python -m pytest tests/ -q                       # 期望 219 passed
python dsview\verify_mock.py                     # 期望 26/26
python C:\My_GongJu\grab\_probe\lifecycle_test.py  # 期望"零残留进程"

# 打包（必须先关掉正在运行的实例，或换 --out）
python build_dist_plus.py --kernel "C:\My_GongJu\grab\WeChatExportPlus_发布版"
Remove-Item 'dist\_build_WeChatExportPlus' -Recurse -Force -ErrorAction SilentlyContinue

# 清私人数据 + 校验 + 打 zip（zip 名在脚本里改成当前版本）
python C:\My_GongJu\grab\_probe\pack_release.py  # → C:\My_GongJu\grab\WeChatExportPlus_v3.0.1_win64.zip

# 发布（会把源码提交推送 + 建 Release + 传附件）
$c = "protocol=https`nhost=github.com`n`n" | git credential fill 2>$null
$env:GH_TOKEN = (($c -split "`n" | Where-Object { $_ -match '^password=' }) -replace '^password=','').Trim()
python publish_release.py --repo Lan-DQ/WeChatExportPlus --tag v3.0.1 `
  --name "WeChatExportPlus v3.0.1（小补丁）" `
  --zip "C:\My_GongJu\grab\WeChatExportPlus_v3.0.1_win64.zip" --notes RELEASE_NOTES_v3.0.1.md

# 发布后复查
$env:REL_TAG='v3.0.1'; python C:\My_GongJu\grab\_probe\verify_release.py
```

**Token 来源**：`GH_TOKEN` 环境变量 → 仓库根 `*.token` → **Windows 凭据管理器**（`git credential fill`，
里面是 `gho_` OAuth token，scopes 含 `gist, repo, workflow`，**能建 Release/传附件**）。
⚠️ **DSH 里的 GitHub MCP 工具用的是另一个只读 token**：`add_issue_comment` 会 403，**评论发不出去**。

---

## 7. 验证清单（改完哪些必跑）

| 场景 | 跑什么 |
|---|---|
| 改了 Python 业务/界面 | `pytest tests/ -q`；界面相关再看 `_probe\ui_ds_options.py` / `ui_progress_eta.py` 出图 |
| 改了 `dsview/*.js` | `node --check`；`python dsview\verify_mock.py` |
| 改了发送/挂载逻辑 | `python dsview\verify_mock.py` + 真机 `_probe\real_50_send.py --docs N --imgs M`（会真发消息，提前跟用户说） |
| 改了指令模板 | 确认 `AI提示词.txt` 与 `ai_prompt.DEFAULT_PROMPT` **逐字一致**、UTF-8 无 BOM |
| 改了数据目录检测 | `python -m pytest tests/test_datadir.py -q` + `_probe\check_datadir.py` |
| 打包后 | `_probe\exe_smoke3.py <exe> --match '<路径特征>'`（标题版本号 + 关窗干净退出） |

有用探针（都在 `C:\My_GongJu\grab\_probe\`）：
`real_50_send.py`（挂 N 个文件并发送）、`real_final_check.py`（不截断 + 附带「我已发送完毕」）、
`test_ds_typesend.py`（/type 通道）、`repro_ds_focus.py`（焦点复现）、`real_upload_wait.py`、
`real_card_walk.py`（DOM 层级）、`check_prompts.py`（各副本是否新版）、`refresh_prompts.py`、
`pack_release.py`、`verify_release.py`、`check_sendinput.py`（本会话能否发真按键）。

---

## 8. 未完成 / 待办 / 已知问题

1. **`README.md` / `使用说明.md` 还是 v2.2.0 文案**（发布页与仓库首页第一印象）。用户说"之后再说"。
2. **issue #1（连接数据库卡住）未回复**。回复文案已备好（见 git 历史 / 上轮对话），要点：
   澄清工具只读本机数据不"同步云端"；「浏览」是首页第一张卡片右下角那个（不是中间大的「浏览会话」）；
   要选**含 `wxid_xxx` 子文件夹的那一层**；6 条排查（微信是否退出、目录对不对、残留 node/electron、
   密钥 64 位、微信"不保留聊天记录"、杀软拦截）；v3.0.0 起已能自动读微信记录的位置。
   配图：`C:\My_AI_Gongju\ds\projectOne\issue1-浏览按钮位置.png`。
3. **键盘输入修复未经真机验证**（见 5.6 / 第 0 节的提示）。
4. **假页面夹具是 contenteditable**（见 5.11），与真站 textarea 有差异；`/type` 的 submit 结果在夹具上仅供参考。
5. **v3.0.0 与 v3.0.1 资产并存**。用户若想只留一份，需要把 v3.0.0 标记为旧版或删除资产。
6. 用户提出过的可选项：把「浏览会话」改名「打开会话列表」消除歧义（还没做）。
7. 图片在"只发文档"模式下要用户手动拖给网页版（暂不做自动发图）。

---

## 9. 调试技巧

**内嵌页控制接口**（`ds_bridge.host.DeepSeekHost._post(path, body)`；端口由启动握手返回）：

| 路由 | 用途 |
|---|---|
| `/ping` `/state` | 存活；`ready/readyState/streaming/attachments/fileInput/loggedIn` |
| `/last` | ★ 最近一次 attach/send/stop 的**内部记录**（`tried` 里能看到每次尝试与失败原因）——排查"为什么没发出去"首选 |
| `/diag` | 导出页面结构诊断 JSON |
| `/attach` `{files, settleMs}` | 挂附件（返回 attached/expected/sendReady/diag） |
| `/send` `{method:'auto'|'button'|'enter', text}` | 发送（text 会先填进输入框） |
| `/stop` | 截断生成 |
| `/type` `{text, submit}` | 写文字（可直发），**不依赖键盘焦点** |
| `/focus` `/show` `/hide` `/theme` | 焦点/显示隐藏/主题 |
| `/eval` `{expr}` | 执行 JS（**需要启动参数 `--ds-trace`**） |

**复现"发不出去"的标准动作**：`/attach` → `/state`（看 attachments/streaming）→ `/send` → `/last`。
`tried` 数组会告诉你按钮路径为什么失败（`disabled` / `Script failed to execute` / `site-error`）。

**写探针的三个固定动作**：复制 profile 到临时目录（避免撞 Chromium 单例锁）、
`extra_args=['--ds-trace']`（开 `/eval`）、结束后 `detach()` + `shutdown(wait=True)`。

---

## 10. 和用户沟通的约定

- 中文，**先给结论**，再给证据（命令输出/截图路径），最后给"你现在可以做什么"。
- 用户偏好：**能自动做完就别问**；但涉及发布/删数据/动他本机东西要先说。
- 用户会直接说"简洁回答"——那就只给结论和两条可执行动作。
- 用户会贴截图报 bug。看截图时注意区分**已发出去的消息**和**还在输入区的附件**（5.2 就是被这个误导的）。
- 用户重视"别把好的地方搞坏"：每次改完都跑全量回归，并在回复里点名验证结果。

---

### 5.30 ★★ 悬停高亮"搜索后不亮"的真因（2026-09-15 第三轮，用户实测报的）
现象（用户原话）："搜索词填完后鼠标移动到会话上会话没有变灰"。

**根因和"点会话跳出随机会话"是同一个：两套下标混用。**
`hover_row` 存的是 `_row_at()` 给的**可见行号**，而 `_draw_row` 里
`hovered = (idx == self.hover_row)` 的 `idx` 是 **items 下标**。
不过滤时 `view[row] == row`，所以看不出问题；一搜索就错位 ——
鼠标移到可见第 0 行，点亮的是 `items[0]`（完全另一行），用户看到的就是"没反应"。

修法：`_on_motion` 里把行号换算成 items 下标再存
（`i = self.item_index(self._row_at(e.y))`），和 `_on_click` / `_on_double` 一致。

⚠️ **我第一版没复现出来，因为复现姿势错了**：我构造的搜索词命中 `[0,1,2,…]`，
`view[0] == 0`，恰好把 bug 掩盖了；而且我只查了"给 items[0] 加没加高亮"，
没查"该亮的是不是那一行"。教训：**写复现用例时，过滤后的第一行必须不是 items[0]**。
现在的回归测试 `test_hover_highlight_follows_mouse_after_search` 就是这么构造的
（命中 items[3] / items[7]），并且断言"别的行不许被点亮"。

### 5.31 ★ 真嵌入的实测结论（2026-09-15 第三轮，用户要求"合成一个"）
用户："先不说这个不显示在这个窗口上面了，这完全是两个软件，能不能就是合成一个，
跟真正的嵌入一样"。方案 A（压小观感差距）**做不到**这一点，所以本轮专门做了实验。

**实验一：Electron 自己建子窗口（`new BrowserWindow({parent: tkHWND})`）**
结论：**做不出真子窗口**。实测 `GetParent(electronHwnd) == 0` —— Electron 的
`parent` 在 Windows 上只是 **owner** 关系（置顶跟随、不随父窗口裁剪），
所以它仍旧是顶层窗口，视觉上还是"两个窗口"。
（脚本：`C:\My_AI_Gongju\ds\projectOne\ds_child_embed_poc.py`，脚本里还带了
`/bounds` 的子窗口坐标分支和 `/wininfo` 自检路由。）

**实验二：Python 侧自己 `SetParent` + 四条键盘通道**
结论：**子窗口能做成（`GetParent` 正确），但键盘四条通道全部失败**：
| 通道 | 实测结果 |
|---|---|
| a) 宿主置前台 + `SetFocus(子窗口)` | `GetFocus()` 返回子窗口 ✅，但页面 value 不变 ❌ |
| b) `AttachThreadInput` 后再 `SetFocus` + `PostMessage(WM_CHAR)` | value 不变 ❌ |
| c) `PostMessage(WM_CHAR)` → `Chrome_RenderWidgetHostHWND`（lParam 按规范填了扫描码） | value 不变 ❌ |
| d) `WM_KEYDOWN` + `WM_CHAR` + `WM_KEYUP` 三连投递 | value 不变 ❌ |
这**复现了 5.18 的结论**（跨进程子窗口键盘全废），而且这次是"焦点也拿到了、
消息也按规范投了"仍然不行。

**⚠️ 但这条实验有个必须说清的边界**：同一轮里我做了对照实验
（`ds_embed_ab_test.py`）：**同一个 Electron、同一个页面，保持独立顶层窗口时
投递 WM_CHAR 也写不进去**。也就是说**这个会话里的合成/投递输入通道本身就不通**
（`SetForegroundWindow` 在记事本上也拿不到前台）。所以：
* "子窗口收不到按键"这条**方向上是可信的**（和 5.18 独立复现一致，且焦点是对的）；
* 但**"合法通道到底能不能打字"必须由人手按键验证**，不能用我这套脚本下结论。

**实验三：WebView2（唯一的"进程内子控件"路线）**
* 运行时**装了**：`Microsoft Edge WebView2 Runtime 153.0.4234.32`
  （`C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.32`，
  `msedgewebview2.exe` 在；`WebView2Loader.dll` 不在运行时目录里，
  从 VS 的 `runtimes\win-x64\native\` 拷了一份 x64 的过来，版本查询正常）。
* **纯 ctypes 路线卡住**：环境对象能 `QueryInterface` / `AddRef` / `Release`
  （说明指针和 vtable 布局都对），但 `get_BrowserVersionString`（vtable 槽 3）
  一调就 `access violation`。
* **comtypes 路线卡在回调形状**：把 handler 传 `None` → `E_POINTER (0x80004003)`；
  传任何 COM 对象 → **`0x80070002`（"找不到文件"）**。
  注意这个错误码**很容易误判成"没装运行时"**，实际是 WebView2 认为
  handler 参数不是它要的接口（它 QI 那个 IID 失败后返回的错误码）。
  两个 user data 目录（含纯 ASCII 路径）都一样，排除路径问题。
* **下一步（最省事的顺序）**：
  1. 用 comtypes 把 `ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler`
     的 vtable **严格按 MIDL 顺序**声明（含 `QueryInterface` 的 IID 校验要返回
     `E_NOINTERFACE` 而不是"来者不拒"），handler 的 `Invoke` 参数类型也要完全对齐；
  2. 用现成封装试水：`pip install pywebview`（成熟、自带 WebView2 后端），
     先确认它的窗口能不能被 reparent 成 Tk 的子窗口；
  3. 若 Python 侧始终过不去，就写一个 ~200 行的 C# WinForms WebView2 宿主，
     用 `COM 可见` 的接口暴露 `Navigate/SetBounds/Eval/Upload`，Python 用 ctypes 调
     （本机有 .NET 8.0.21 + .NET Framework 4.8，且打包版本来就要带 exe，多一个 DLL 可接受）。

**结论（给用户的话）**：真正"合成一个窗口"只有 WebView2 这条路；现在卡在
"回调接口声明"这一步，**不是"做不到"，是还没做通**。方案 A 的改动都还在，
可以先顶着用。

### 5.32 ★★★ 真嵌入做通了：WebView2 进程内子控件（2026-09-15 第三轮，实测通过）
用户要求"把两个软件合成一个，跟真正的嵌入一样"。上一节（5.31）已经把 Electron 的
三条路全部否掉，只剩 WebView2。本轮**做通了**，关键结论如下。

**新文件 `ds_bridge/webview2_host.py`**（`WebView2Host`），接口**刻意和
`ds_bridge.host.DeepSeekHost` 同名**：start / place / move / show / hide / summon /
set_topmost / set_theme / set_background / navigate / state / eval_js / last /
focused_info / shutdown，所以上层 `app_plus` / `sender` 不用大改。
另外多了 `reparent(parent, x, y, w, h)` —— 真嵌入那一步（`SetParent` 成宿主子窗口）。

**验证脚本**：`C:\My_AI_Gongju\ds\projectOne\test_webview2_host.py`（假官网 `ds_mock.html`）
```
运行时目录 = C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.32
[PASS] 找到 WebView2 运行时     [PASS] WebView2 启动（0.3s）
[PASS] reparent 后是真子控件（GetParent == Tk canvas）
[PASS] eval_js 可用（result='DeepSeek Mock'）   [PASS] place 可用
断言 5 条，通过 5，失败 0
```

**四个坑（都是本轮踩出来的，改代码前必读）**：
1. **`0x80070002` 不是"没装运行时"**。WebView2 的 loader **不会自己发现**运行时目录，
   必须设 `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER=...\EdgeWebView\Application\<版本>`
   （`webview2_host._find_webview2_runtime()` 会自动找）。这个错误码极具误导性 ——
   我前面在它上面绕了很久，还一度以为是程序集/运行时版本不兼容。
2. **控件必须建在自己的 STA 线程上**。Tk 主线程被 Tk 初始化成 STA，而 pythonnet 的
   CLR 要 MTA → `RPC_E_CHANGED_MODE (0x80010106)`。
   而且**建控件之前要先 `CoInitializeEx(COINIT_APARTMENTTHREADED)`**，否则
   WinForms 第一次碰 `Handle` 时会把线程定成 MTA，之后 `CreateAsync` 直接抛同样的错。
3. **异步任务一律用 WinForms `Timer` 轮询，绝不在 STA 线程上 `.Result`/`.Wait()`/
   自旋等待**。`CreateAsync` / `EnsureCoreWebView2Async` / `ExecuteScriptAsync` 的
   续体都要回到 STA 线程的消息泵上执行，一堵住就是死锁或 `E_NOINTERFACE`
   （"转不成 ICoreWebView2Environment"——**这个报错其实是自己把泵堵死了**，不是版本问题）。
   `webview2_host` 的做法：命令队列 + `_collect_eval()` 在 tick 里收结果。
4. **程序集用 pywebview 自带的那份**（`pip install pywebview` →
   `site-packages\webview\lib\Microsoft.Web.WebView2.Core.dll`，版本 1.0.3856.49，
   配运行时 153 完全正常，它的原生 `evaluate_js` 也验过能用）。
   不需要自己去找 WebView2 SDK，也不用手写 COM 声明（手写 ctypes vtable 那条路我试过，
   环境对象能 QI/AddRef、一调属性就访问违例，别再走）。

**挂附件（替代 CDP）**：WebView2 没有 CDP 端口，程序化挂文件的正确手段是
`CoreWebView2.FilePickerRequested`（运行时 ≥1.0.1185，我们的 SDK/运行时都满足）——
页面弹文件选择框时由宿主接管、直接给出文件路径，等价于"用户选了文件"。
**这条还没接**（下一步），`renderer.js` 里的结构性启发式（找输入框/发送键/停止键/
附件计数）也要照搬成页面侧 helper。

**还没做的（下一步清单，按顺序）**：
1. 把 `dsview/renderer.js` 的启发式搬成 `webview2_host` 的页面侧 JS helper
   （直接 `ExecuteScriptAsync` 注入，不需要再走 HTTP + eval 通道）；
2. `attach(paths)`：用 `FilePickerRequested` + 附件计数；
3. `send/stop/state/last`：照 `BatchSender` 的契约实现（发送键"连续两次可用"、
   等自然答完 25s 再截断这些逻辑都在 Python 侧，不用改）；
4. `app_plus.build_ds` 切到 WebView2 后端（**建议用开关，保留 Electron 后端**，
   出问题能马上切回来）；
5. `.gitignore` 排除 `wv2_*_profile`；打包时确认 pywebview 的 dll 被带进包里
   （打包体积/依赖要重新核）。

### 5.33 ★★★ v3.1.0：官网页签默认走 WebView2 真嵌入（2026-09-15 第三轮完成）
**成品包**：`<构建输出目录>\WeChatExportPlus_v3.1.0\`（540 MB，含登录态与设置）。
exe 里**同时带两套后端**，用设置 `ds_backend` 切换：

| 值 | 行为 |
|---|---|
| `webview2`（**默认**） | WebView2 进程内子控件 = 真嵌入，浏览器长在窗口里 |
| `electron` | 独立顶层窗口 + 跟着摆位（老方案，留作退路） |

**验证（都在源码侧与打包版各跑了一遍）**
| 脚本 | 结果 | 验的是什么 |
|---|---|---|
| `test_webview2_host.py` | 5/5 | 启动 / reparent 成真子控件 / eval_js / place |
| `test_webview2_e2e.py` | **12/12** | 假官网上：helper 注入 → 状态 → 挂附件 → 发送 → 事件回读 → 停止 → /last |
| `test_app_webview2.py` | 6/6 | App 集成：后端=webview2、真嵌入、子控件正好落在布局表浏览器区、截图 |
| `test_pkg_webview2.py` | 5/5 | **打包版**：窗口出现 / 主窗口内有子控件 / 关窗 code=0 / 零残留 |
| `pytest` / `verify_mock` / `ds_window_accept` | 221 / 27 全绿 / 12 全绿 | 原有功能没被破坏 |

**这一轮又踩的四个坑（务必记住）**
1. **`ds_bridge/webview2_host.py` 里的 .NET 相关 import 必须惰性**（`_load_dotnet()`）。
   我一度把它们放在模块顶层，结果**打包版主窗口再也不出现**（进程活着、9MB、
   只有 4 个线程、没日志）—— 冻结环境里在 import 阶段初始化 .NET 会把进程卡住。
   `app_plus` 只在真正要用 WebView2 时才 import 这个模块，所以顶层保持"只有标准库"。
2. **`--collect-all webview / comtypes` 会毁掉打包版**：加上之后主窗口不出现。
   我们本来就不需要它 —— WebView2 程序集由 `copy_ds()` 复制到
   `<包根>/webview/lib`、`pythonnet_runtime/`，运行期由 `_load_dotnet()` 指路。
   `HIDDEN_IMPORTS` 里只要有 `clr`（pythonnet 自己的 hook 会带运行时）。
3. **判断打包版有没有出窗口，不能用 `Get-Process().MainWindowTitle`**（经常是空的，
   老包也一样空，我在这上面白查了很久）。要 `EnumWindows` + 按标题找；
   而且 onefile 是"父进程 + 子进程"，**窗口属于子进程**，不能按 `Popen` 的 pid 匹配。
4. **`host.start()` 不能占着 Tk 主线程等**：新建 user-data 目录时首次要十几到几十秒。
   现在 `_ds_ensure_webview2()` 起后台线等、`after` 轮询回来再 reparent，
   页面先画出来并显示"正在准备内嵌浏览器…（已等 Ns）"。实测 97s → **9s** 且界面不卡。

**挂附件怎么做的**（替代 Electron 的 CDP）
主路是 **DataTransfer 注入**：Python 读文件 → base64 → 页面里造 `File` 塞进
`input[type=file].files` 并派发 `change`（实测假官网上 3/3 成功、附件计数正确）。
`CoreWebView2.FilePickerRequested` 这条**在我们用的 SDK 1.0.3856.49 里没暴露**
（`'CoreWebView2' object has no attribute 'FilePickerRequested'`），所以它只是兜底：
先点页面上的"回形针"让页面弹文件框，宿主再接管给路径。**真官网上要重点验这条**
（官网是 React 上传组件，DataTransfer 注入不一定被它认）。

**回归测试**：`test_ds_follow_is_noop_for_webview2`（webview2 不许有"跟随"动作）、
`test_ds_follow_skips_when_minimized`（electron 后端最小化时不许摆位）。

### 5.34 ★★ 打包版 WebView2 起不来的真凶：pythonnet 的 .NET 运行时（2026-09-15）
现象：源码里跑得好好的 WebView2 真嵌入，**打成 exe 后完全不工作** ——
没有 WebView2 用户数据目录、没有子控件、界面也不报错（错误全被界面 toast 吞了）。

排查手段（很有效，记下来）：**把 `webview2_host` 单独冻成一个小 exe 带 `--console`**，
比"打完 500MB 包再猜"快得多，而且报错直接看得见
（脚本：`C:\My_GongJu\grab\_probe\build_wv2_frozen_check.py`）。
它给出的原始报错是：
```
Failed to create a .NET runtime (C:\Users\...\Temp\_MEI00002d782) using the parameters {}.
```

真凶与修法：
1. **必须显式指定用 .NET Framework**：`os.environ['PYTHONNET_RUNTIME'] = 'netfx'`
   （在 `import clr` **之前**）。默认它会按 CoreCLR 去起运行时 → 打包版直接失败；
   就算起来了也缺 `System.Windows.Forms`（那是 .NET Framework 的程序集，CoreCLR 没有）。
   ⚠️ 实测：设成 netfx 后冻结环境里 clr 正常可用。
2. **别给 `PYTHONNET_RUNTIME` 传目录**！这个变量的语义是"用哪个 .NET 运行时"
   （netfx / coreclr / mono）。我一开始写成 `setdefault('PYTHONNET_RUNTIME', <pythonnet_runtime 目录>)`
   —— 结果它把 netfx 覆盖掉，报出上面那个"Failed to create a .NET runtime"，
   我因此绕了很久。**要加 DLL 搜索路径就用 `os.add_dll_directory()`**，别碰这个变量。
3. **打包版没有控制台，界面 toast 也可能看不见** → 启动过程要**写文件日志**。
   现在 `_ds_ensure_webview2` 会写 `<包根>/.wv2_start.log`，里面能看到
   `控件已建 / 环境就绪 / start 成功 / reparent -> {...}` 每一步。
   打包版实测（`WXEXPORT_START_PAGE=ds` 直接进官网页签）：
```
后端=webview2  runtime=...\EdgeWebView\Application\153.0.4234.32
[t=0.03s] 控件已建：form=1248162 wv=3934886
[t=0.11s] 环境就绪：153.0.4234.32
[t=0.35s] WebView2 就绪，开始导航
start 成功，用时 0.66s
reparent -> {'ok': True, 'parent': 10749546}      ← 父窗口就是 Tk 画布
```
   即 **打包版里真嵌入成立**（`GetParent == 画布 HWND`）。

### 5.35 与 v3.0.0「能嵌入但输入不了」的对照（用户问的）
v3.0.0 的嵌入确实成功了 —— `ds_bridge/host.py:338 embed()`：
`SetParent(child, parent)` + `WS_CHILD`，和本轮 Electron 路线的实验二一样。
**它坏在键盘**：跨进程子窗口的键盘消息进不去（独立窗口 `value='138'` ✅；
嵌入后同样投递 `value=''` ❌）。当时为了绕开这个，才加了界面上那条
「写给 AI 的话」输入框，走后端 `/type`（`insertText` / value-setter）**注入**文字。
⚠️ 注意 `/type` 对 `<input>` 是**静默无效**的（返回成功、value 不变），
直到 SDK 5.20 才补上原型 setter 兜底 —— 所以那条输入框在 v3.0.0 上多半也"看着有、用不了"。

**本轮 WebView2 为什么不会有这个问题（架构层面）**：WebView2 是**同一个进程**的
子窗口，键盘走本进程的消息队列，不存在"跨进程子窗口"那一类限制；
开关页签时焦点在同一个进程内部交接。已验证的前提条件：
* 容器窗口 `style=0x56010000 exstyle=0x00010000`：**没有** `WS_EX_NOACTIVATE`、没被禁用；
* 渲染窗口 `Chrome_RenderWidgetHostHWND` 同样**没有** `WS_EX_NOACTIVATE`。
（脚本：`C:\My_AI_Gongju\ds\projectOne\test_webview2_kbd_conditions.py`）

⚠️ **"真实按键能否打字"我没有验证，也验证不了**：这台机器/这个会话里
`SetForegroundWindow` 被 Windows 前台锁拒绝（`WindowFromPoint` 落在别的程序
`UnityWndClass` 上），所以任何"模拟点击/投递按键"的实验都不可信 ——
前面我自己就被这个坑过一次，写下了错误的"投递失败"结论。**必须由人手按键盘确认。**

## 附：2026-09-15 早期未提交改动（5.14~5.24 摘要）
> ⚠️ 这一节是**事故后补写的摘要**：原文写在 HANDOFF.md 里，被我上面 5.29 那次
> PowerShell 误操作冲掉了（那份工作区副本没有提交过，git 里没有）。
> 结论都在，细节以代码/测试为准。

- **5.14 「发给 AI」输入框已删**：它画在 canvas x 560~934，正好压在右侧浏览器上，
  用户以为"要用这条才能发话"。后端 `/type` 与 `host.type_text()` **保留**（排查用）。
  回归：`test_ds_page_has_no_text_send_box`。
- **5.15 千万别对同一账号起第二个浏览器实例**：DeepSeek 令牌族会整体作废，
  用户被迫重新登录。要读页面状态就调程序自己的 `/state`。
- **5.16 永久 AttachThreadInput 是"卡死"真身**：把宿主线程和 Chromium 线程绑在一条
  输入队列上，IME 跨进程转换直接死锁（窗口 `IsHungAppWindow()==True`，
  但 `.ui_errors.log` 空、stderr 空 —— 是**阻塞**不是崩溃）。现在只在校验焦点那一瞬
  需要它，而且**用完立刻 detach**；不要再改回永久保持。
- **5.17 `/type` 在登录页用不了**（已修）：登录页是 `<input type="tel">`，
  而 `visibleEditables()` 当时无条件跳过所有 `<input>`（见 5.20）。
- **5.18 官网页签改成「独立顶层窗口」**（本轮之前的核心重做）：`SetParent` 跨进程
  子窗口会让**键盘整条通道全废**（连直接 PostMessage `WM_CHAR` 到
  `Chrome_RenderWidgetHostHWND` 都无效：独立窗口 value='138' ✅，嵌入后 value='' ❌）。
  现在 Electron 保持顶层 + `/bounds` 摆位 + `/summon` 激活。
  ⚠️ `place()` 收**物理像素**，内部要 ÷ 缩放比再发给 Electron（`setBounds` 收 DIP）——
  125% 缩放下不换算，窗口会大一圈、位置也偏。这条**一定要留着**。
- **5.19 `/focus` 里的 `showInactive()` 会把焦点撤掉**（已删）：它按定义"显示但不激活"，
  而 Chromium 认为窗口没焦点时**不处理键盘输入**。现在 `show()` + `focus()` +
  `webContents.focus()`，并轮询确认 `isFocused()`。
- **5.20 `findComposer` 曾无条件跳过 `<input>`**（已修）：分三档，最后一档才允许
  `<input>` 并放宽尺寸阈值；`insertText` 对 `<input>` 是静默无效的，所以 `/type`
  加了走原型 `value` setter + 派发 `input/change` 的兜底。
- **5.21 「静默消失」已补退出日志**：`atexit` 钩子 + `sys.excepthook` + `app.run()`
  返回后各记一行到 `.boot.log`；`ds_exe_e2e.py` 把"`.boot.log` 还在"判为启动没跑稳。
- **5.22 官网页签窗口优先级过高 / 关不掉**（已修）：不再置顶；提示时先隐藏浏览器
  （`_ds_toast`/`_ds_after_toast`）；离开页签主动藏；`quit_app()` 在 `root.destroy()`
  **之前**先藏。
- **5.23 残留进程的两个真成因**（已修）：① `WCDBClient.stop()` 顺序错（先 `terminate`
  再 `taskkill /T`，父进程一死进程树就断了）→ 改成"趁主进程还活着按树杀 → terminate
  兜底 → 再补一次树杀 → 校验残留"；② `quit_app()` 清理线程是 `daemon=True`，
  主线程一结束就被掐掉 → 改成 `daemon=False` 且不 join。
  判断进程存活必须 `GetExitCodeProcess` 看 `STILL_ACTIVE`（`OpenProcess` 成功不算）。
- **5.24 图片解密批量 + 按 wxid 匹配账号**（issue #2，已修）：一图一进程 69ms/张 →
  批量 2ms/张（60 张实测 4.1s → 0.1s）；`decrypt_image.js` 加 `--batch`，
  `media_resolver.py` 加 `_native_cache` + `_decrypt_batch_in_dir()`；
  账号匹配从写死 `accounts[0]` 改成"按路径 wxid 精确 → 前缀 → 才退回第一个"。
  回归：`tests/test_image_batch.py`。
