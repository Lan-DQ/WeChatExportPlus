# WeChatExportPlus 接管文档（截至 v3.0.1 · 2026-09-14）

> **给新会话的开场白**：先读本文档，再读仓库代码。
> 目的：不用重新踩一遍已经踩过的坑。**第 5 节「坑清单」动手前必看。**
>
> 本文档覆盖到 v3.0.1；`docs/HANDOFF-3.0.md` 是 3.0 开发期的旧版（架构图仍可参考），
> **结论以本文为准**。

---

## 0. 一分钟速览

| 项 | 状态 |
|---|---|
| 项目 | 把微信 PC 端聊天记录批量导出成「给 AI 用的语料」，并**内嵌 DeepSeek 官网按批自动投喂** |
| 当前版本 | **v3.0.1**（`gui/app_plus.py` 里 `APP_VERSION`） |
| 已发布 | v3.0.1 / v3.0.0（都是正式版，latest = v3.0.1） |
| 仓库 | https://github.com/Lan-DQ/WeChatExportPlus （public，main 分支，工作区干净） |
| HEAD | `8c66dbf Update WeChatExportPlus` |
| 测试 | `pytest tests/ -q` → **190 passed**；假官网页自检 26/26 |
| 下一步建议 | ① 更新 `README.md` / `使用说明.md`（还写着 v2.2.0）② 回复 issue #1（文案已备好） |

**当前最大待验证项**：跨进程子窗口的键盘输入修复（`AttachThreadInput`）只在代码层完成，
**没能在本机自动化验证**（会话里无法把窗口置为前台），需要用户手动确认。

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
  verify_mock.py            假官网页端到端自检（26 条断言）
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

## 6. 构建 / 发布（逐条命令）

```powershell
cd C:\My_GongJu\grab\WeChatExportPlus
$env:PYTHONIOENCODING='utf-8'         # 中文输出必须设，否则控制台乱码

# 连开发环境（junction；新机器/目录被删后才需要）
python dev_setup.py --kernel "C:\My_GongJu\grab\WeChatExportPlus_发布版"

# 回归
python -m pytest tests/ -q                       # 期望 190 passed
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
