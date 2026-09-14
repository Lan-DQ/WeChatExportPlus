# WeChatExportPlus 3.0 交接文档

> 这份文档给**新会话**用的。开局第一句话建议说：
> 「先读 `C:\My_AI_Gongju\ds\projectOne\WeChatExportPlus-3.0交接文档.md`，然后读仓库代码。」
>
> 目的：让新会话不用重新踩一遍已经踩过的坑。**尤其是「坑清单」那一节，先看完再动手。**

---

## 1. 项目是什么

把微信 PC 端的聊天记录批量导出成**给 AI 用的语料**。

- 原理：读本地数据库 → 用密钥解密 → 导出成文本。**纯本地，不联网，不碰账号**
- 目标用户：需要把聊天记录喂给 DeepSeek 等 AI 的人
- 形态：Windows 单文件 exe（PyInstaller 打包）+ 一堆运行时依赖

**最关键的设计约束**：输出的东西要能**直接拖进 DeepSeek 网页版**（它不支持文件夹），
所以 Markdown 格式的目标文件必须直接落在导出目录最外层。

---

## 2. 仓库与本地路径

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/Lan-DQ/WeChatExportPlus`（public） |
| 本地仓库 | `C:\My_GongJu\grab\WeChatExportPlus` |
| 发布目录 | `C:\My_GongJu\grab\WeChatExportPlus_发布版` |
| 打包出的 zip | `C:\My_GongJu\grab\WeChatExportPlus_v2.2.0_win64.zip` |
| 临时调试脚本 | `C:\My_GongJu\grab\_probe\`（**不在仓库里**，可随时删） |
| 版本 | 当前 v2.2.0，目标做 v3.0 |

⚠️ **2026-09-13 这个仓库目录曾被误删过一次**，当时从 GitHub 重新 clone 恢复。
如果发现目录不见了：先查回收站，或直接重新 clone（代码全在 GitHub，不会丢）。
但 `ME.token`（GitHub PAT）不在仓库里，删了就没了。

---

## 3. 怎么跑起来

```bash
cd C:\My_GongJu\grab\WeChatExportPlus

# 1. 连开发环境（runtime/dll/electron/resources 是 junction，不在仓库里）
python dev_setup.py --kernel "C:\My_GongJu\grab\WeChatExportPlus_发布版"
#     ↑ 原始内核目录已被删，现在用「发布版」当内核来源，实测可行

# 2. 跑测试（126 个，约 4 秒）
python -m pytest tests/ -q

# 3. 打包
python build_dist_plus.py --kernel "C:\My_GongJu\grab\WeChatExportPlus_发布版"
```

**打包有两个坑**：

1. 必须传 `--kernel`，否则它去找已经不存在的原始内核目录，直接报错退出
2. 构建成功后产物在 `dist\WeChatExportPlus\`，**需要手动移到发布目录**：
   ```powershell
   Move-Item "C:\My_GongJu\grab\WeChatExportPlus\dist\WeChatExportPlus" `
             "C:\My_GongJu\grab\WeChatExportPlus_发布版"
   ```
   移动前记得备份 `.ui_settings`（用户设置）、`创建桌面快捷方式.bat`、`刷新图标缓存.bat`

**发布到 GitHub Releases**：
```bash
python publish_release.py --repo Lan-DQ/WeChatExportPlus --tag v3.0.0 \
  --name "WeChatExportPlus v3.0.0" \
  --release-dir "C:\My_GongJu\grab\WeChatExportPlus_发布版" \
  --zip "C:\My_GongJu\grab\WeChatExportPlus_v3.0.0_win64.zip" \
  --notes RELEASE_NOTES_v3.0.0.md
```
Token 从环境变量 `GH_TOKEN` 或仓库根的 `*.token` 文件读。
**如果 `ME.token` 不在了**，可以从 Windows 凭据管理器取（`git credential fill`，
里面有 `Lan-DQ` 的凭据，scope 含 `repo`）。

---

## 4. 代码地图

```
gui/            界面层（自绘，不用 ttk）
  app_plus.py       主程序：业务编排 + 页面构建。约 1250 行，最大也最杂
  ui_widgets.py     自绘控件：Surface / Button / CheckList / Checkbox / Dropdown
  ui_theme.py       配色、背景生成、动画驱动、抗锯齿圆角渲染
  icon.ico          图标（用户立绘裁剪，16~256px）

exporters/      导出层
  batch_export.py   批量导出主流程；unique_base/safe_name 等命名工具也在这
  message_content.py 消息解析成可读文本（最关键、最难的一块）
  md_exporter.py    Markdown 单文件格式（默认格式，图片编号 0001.jpg）
  ai_exporter.py    JSONL/TXT（给 RAG 入库用）
  html/pdf/csv/excel_exporter.py  其它格式
  ai_prompt.py      「给 AI 的指令」的读取与默认文本
  index_exporter.py 总索引页 导出清单.html

scripts/        内核桥接（改得很少）
  wcdb_server.py    拉起 node 子进程 + HTTP 通信
  wcdb_server.js    koffi FFI 调 dll/WCDB.dll
  decrypt_image.js  图片解密

tests/          126 个测试
  test_ui_invariants.py  ★ 界面回归安全网（见第 6 节）
  test_message_content.py / test_batch_export.py / test_md_exporter.py
  test_ai_prompt.py / test_clear_before.py / test_name_collision.py
```

**架构分工**：业务逻辑在 `app_plus.py` 和 `exporters/`，界面细节全在 `gui/ui_*.py`。
改界面时尽量别动 `exporters/`，反之亦然。

---

## 5. ★ 坑清单（最值钱的一节，动手前必读）

这些全是真实踩过的，每一个都花了很久才定位。**别重犯。**

### 5.1 tkinter 的 `event.type` 是枚举，不是 int

```python
e.type = <EventType.ButtonPress: '4'>
e.type == 4          # False ！枚举成员不等于同值的 int
{4: 'x'}.get(e.type) # None  —— 查不到
{4: 'x'}.get(int(e.type))  # 'x'  —— 必须转 int
```
**后果**：所有鼠标事件被静默丢弃 → **整个界面点不动**。
修法在 `Surface.dispatch_input`，用 `int()` 归一化。

**同类陷阱**：`canvas.itemcget(i, 'image')` 返回的是 **Tcl 对象**，
和 Python 字符串用 `==` 比较不可靠，两边都要 `str()`。

### 5.2 Tk 的 tag 匹配不支持通配符

```python
cv.find_withtag('cl123_row*')   # 返回 0 个！不是 glob
```
**后果**：想用模式删除行图元，实际一个都没删 → 图元无限堆积。
修法：`CheckList._delete_all_rows()` 按行 tag 逐个删。

### 5.3 重绘必须清理「上一轮画的图元」

`CheckList.draw()` 早期只 `cv.delete('listbody')`，而行图元带的是 `_row_tag`，
于是**每次重绘旧行都留在画布上、新的叠上去**。

**症状**：「窗口一闪就多一个控件」「切主题后勾选显示错乱」「某一行钩子凭空消失」。
实测一次多余 `draw()` 就让行图元翻倍。

**通用教训**：Canvas 自绘一定要想清楚「这次重绘要删掉哪些命名空间」，漏一个就堆积。

### 5.4 `return 'break'` 会中止后续所有处理器

Tk 里一个事件可以绑多个处理器，**任何一个返回 `'break'` 就全停**。

`CheckList._on_click` 早期无条件 `return 'break'`（连没命中行时也返回），
于是绑在它后面的下拉框永远收不到点击。

**规矩**：只在**真正消费了事件**时才返回 `'break'`，否则返回 `None`。

### 5.5 别用「动画进度」判断点击

```python
was = self._pressed_inside and self._press_t > 0.15   # ❌
```
`_press_t` 是异步动画值。快速点击时 Press/Release 之间一帧都没跑，
`_press_t` 还是 0 → 判成"没按过" → **按钮点了没反应**，而且**点击快慢会改变行为**。

**修法**：用独立的布尔量 `_pressed_inside`。

### 5.6 图片/图元缓存不能用「单槽」

```python
self._ck_photo = ImageTk.PhotoImage(...)   # ❌ 整个列表只有一张
```
已画出的 Canvas 图元**持有的是对象引用**，一旦把属性指向新图，
之前画好的所有行会跟着"变脸"。

**症状**：悬停切行时，**整个列表的复选框一起变成最后那张**（但计数是对的）。
**修法**：按状态缓存成字典（`_ck_photos[True/False]`）。

### 5.7 `unique_base` 必须避开 `<名字>_图片`

```python
if not exists(name + '.md') and not exists(name):   # ❌ 漏了 name + '_图片'
```
md 格式的图片放在 `<名字>_图片/`。若上一轮残留了图片目录、而 md 已被移走，
会误判"名字没占用" → `md_exporter` 里 `rmtree(<名字>_图片)` **把旧图删了**。

**症状**：「导出很多会话时，有时候图片文件夹没了」。
**修法**：命名时同时避开 `.md`、同名目录、`_图片` 目录三者。

### 5.8 中文路径进三引号字符串要用 raw

```python
DEFAULT_PROMPT = """...「群聊A_图片\0007.jpg」..."""   # ❌ \000 被当八进制转义
DEFAULT_PROMPT = r"""...「群聊A_图片\0007.jpg」..."""  # ✅
```
**后果**：提示词凭空少 3 个字符，肉眼看不出来（用 sha256 校验才发现）。

### 5.9 关窗口别被清理阻塞

DB 清理里有 `terminate → wait(5s) → taskkill(/T, 10s)`，
放在 `root.destroy()` **之前**会让关窗卡最多十几秒（实测 1500ms+）。
**修法**：先 `destroy()`，清理丢到 daemon 线程（实测降到 5ms）。

### 5.10 Windows 图标缓存

换了 exe 图标后资源管理器仍显示旧的（按**文件路径**缓存）。
需要刷新图标缓存，或者把文件夹改个名。发布包里已放 `刷新图标缓存.bat`。

### 5.11 PowerShell 的引号地狱

在这个环境里写 `python -c "..."` 带中文/复杂引号**极容易坏**。
**建议**：需要写脚本就直接用 write 工具落成 `.py` 文件再运行，
不要用 `python -c` 或 PowerShell here-string。

另外：`Set-Content -Encoding UTF8` 会加 BOM，Python 读到会语法报错（`\ufeff`）。
`.ps1` 脚本如果无 BOM，Windows PowerShell 5.1 按 ANSI 读，中文乱码。

---

## 6. ★ 测试是安全网，别改坏

**126 个测试，约 4 秒跑完。改任何东西之前先跑一遍。**

其中 `tests/test_ui_invariants.py` 是**专门防界面回归**的，把上面那些坑全固化成了断言：

| 测试 | 盯的是什么坑 |
|---|---|
| `test_home_has_background_item` | 背景必须真画在画布上 |
| `test_redraw_does_not_accumulate_items` | 重绘不能堆积图元（坑 5.3） |
| `test_button_click_fires_command` | 按钮必须能点动 |
| `test_button_click_works_without_animation_frame` | 快速点击不能丢（坑 5.5） |
| `test_click_outside_list_does_not_consume` | 不能乱 `break`（坑 5.4） |
| `test_checkbox_photos_differ_by_state` | 复选框图要按状态区分（坑 5.6） |
| `test_mouse_wheel_scrolls` | 滚轮必须能滚 |
| `test_scrollbar_drag_changes_scroll` | 拖滚动条要有效 |
| `test_input_registration_no_leak` | 输入消费者不能泄漏 |
| `test_unique_base_avoids_existing_image_folder` | 图片文件夹不能被误删（坑 5.7） |

**这些测试有一个重要特性：** 写测试时必须用**真实的 `tkinter.EventType`**，
不能用裸 int。早期测试用 int 传事件，结果**测试全绿而真机全坏**（坑 5.1）。
测试辅助构造器 `_mk()` / `_wheel()` 已经处理好了，新增测试照抄即可。

**验证测试真的有效**：改坏产品代码，看测试是否变红。我做过这个验证 ——
去掉 `int()` 后 4 个测试立刻失败。

---

## 7. 导出流程与格式

### 默认格式（md）

```
导出_20260213_1430\
├── 群聊A.md              ← 目标文件直接在最外层（能直接拖进网页版）
├── 群聊A_图片\           ← 图片按 0001.jpg 编号，与正文 [图片0001] 对应
│   ├── 0001.jpg
│   └── ...
├── 私聊B.md
├── 私聊B_图片\
├── 给AI的指令.txt         ← 整个导出只此一份（v2.2.0 起不再塞进每个 md）
└── 导出清单.html          ← 总索引
```

**「给 AI 的指令」的位置（重要变更）**：
v2.2.0 起，**Markdown 正文里不再夹带指令**，改为每个导出目录只放一份
`给AI的指令.txt`。原因：会话多时每个 md 都塞一份纯属浪费。
其它格式（txt/json/jsonl/html/pdf/csv/excel）仍然各自带上。

### 图片编号

用 **4 位**（`0001.jpg`）不用 2 位 —— 超过 99 张时 `100.jpg` 会排到 `01.jpg` 前面。
同一 `local_id` 可能对应多张图，`build_image_map` 会去重。

---

## 8. 发布相关

- ZIP 用 `Compress-Archive`（**不要用 `tar`**，中文文件名会乱码）
- 每次发布前检查包内**不能有** `.ui_settings`（含用户私人路径）
- 隐私自查：exe 里搜 `LanDeQuan` / `My_GongJu` / 64 位 hex 串，都应为 0 次
- 密钥存在**发布目录之外**（`桌面\wx_export\key.txt`），不会进包
- 发布包应包含：`启动工具.bat`、`创建桌面快捷方式.bat`、`刷新图标缓存.bat`

### 3.0 发布前要修的东西

- **Release 说明里的下载链接**：v2.2.0 的资产被用户改名为
  `WeChatExportPPPlus_v2.2.0_win64.zip`（**多了个 P**），
  而说明里写的是 `WeChatExportPlus_v2.2.0_win64.zip` → **链接会 404**。
  3.0 发布时顺便把这条修正。
- 提到「电脑端关了『不保留聊天记录』就抓不到历史消息」这个限制（用户问过）

---

## 9. 已知限制（写进 README 的那部分要准确）

- 只能抓**这台电脑上存着的**消息；PC 端"不保留聊天记录"关掉后抓不到历史
- 只支持 Windows + 微信 4.x 数据目录
- 图片要手动一起拖给 AI（网页版不支持文件夹）
- 单会话过大时 md 会很长
- 打包成 PyInstaller 单文件，**杀毒软件可能误报**

---

## 10. 3.0 可以做什么（未定，等你定方向）

几个候选（都还没做，别当成已定需求）：

- 导出格式：新增 Markdown 长文合并、按时间分段
- 会话筛选：按时间范围、关键词、图片数量筛选
- AI 直连：直接调 API 而不是手动拖文件（注意泄露风险）
- 图片处理：可选内嵌 base64 / 压缩 / 只导出有图的会话
- 界面：`app_plus.py` 已经 1250 行，**建议先拆分**（业务编排 vs 页面构建分开）

**如果 3.0 要动界面，先读 `tests/test_ui_invariants.py`，
并保持那 10 条不变式全部通过。**

---

## 11. 新会话开局建议做的三件事

1. 读这份文档（尤其**第 5 节坑清单**）
2. `python -m pytest tests/ -q` 确认基线是 126 passed
3. 读 `gui/ui_widgets.py` 的 `Surface.dispatch_input` 和 `CheckList`，
   理解输入分发和行重绘机制 —— 3.0 改界面一定会碰这两处

---

*文档生成于 v2.2.0 完成时。如果 3.0 改动很大，记得更新这份文档。*
