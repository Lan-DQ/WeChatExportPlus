# Spec: 微信聊天记录批量导出工具（以 WeChatExport 为内核）

> 版本: v2.0.0
> 状态: 待用户确认
> 上游内核: [Ray0612/WeChat-Export-Tool](https://github.com/Ray0612/WeChat-Export-Tool) v1.2.0 (GPL-3.0)

---

## 1. Objective

### 要解决的问题

原工具（v1.2.0）的流程是：**浏览会话 → 双击某个会话 → 弹出独立窗口 → 单个会话、单个格式导出**。
要导出 12 个群，就得重复 12 次「双击 → 选格式 → 导出」。且导出格式在选择会话之后才能设置，无法统一。

### 目标用户

个人用户，需要一次性把多个微信群/私聊的完整聊天记录归档成可读文件。

### 成功标准（可验证）

设置好密钥并连接数据库后，用户能够：

1. 在**一个界面内勾选多个会话**（支持全选/反选/搜索后勾选）。
2. 在**同一个界面内统一设置导出格式**（HTML / PDF / TXT / JSON / CSV / Excel），不需要逐个会话设置。
3. 点击「导出」，弹窗选择**总目录**，程序自动创建带时间戳的大文件夹。
4. 大文件夹内**平铺每个会话一个子文件夹**，子文件夹内部自包含（HTML 含 `index.html` + `图片/`）。
5. 导出过程中显示**「第 N/M 个：会话名」进度**，且可**取消**。
6. 生成 **`导出清单.html`** 总索引，可点击跳转到各会话。
7. **非文字消息（转发/链接/小程序/引用/语音/视频/文件/位置/名片/系统消息）导出为可读文本**，不再是 `[类型49]` 占位符。

### 非目标（明确不做）

- 不下载/解密图片以外的媒体（不做视频、语音、文件的原始文件下载）。
- 不改动密钥提取、WCDB 解密、图片解密等内核能力。
- 不做增量导出、去重、合并多会话为一个文件。
- 不修改上游 `wcdb_server.js` / `get_key.js` / `image_decoder.py` 的解密逻辑。

---

## 2. Tech Stack

| 组件 | 版本 | 用途 | 是否改动 |
|---|---|---|---|
| Python | 3.12.10（本机） | 构建与运行 | 使用 |
| tkinter / ttk | 标准库 | GUI | **重写** |
| PyInstaller | 6.22.2 | 打包 onefile exe | 使用 |
| Node.js | runtime/node.exe（上游自带） | WCDB 桥接 | 复用 |
| koffi / fzstd | 上游 node_modules | FFI + ZSTD | 复用 |
| WCDB.dll / wcdb_api.dll / SDL2.dll | 上游 dll/ | 数据库解密 | 复用 |
| wx_key.dll | 上游 dll/ | 密钥提取 | 复用 |
| pycryptodome / fpdf2 / openpyxl / Pillow | 已装 | 导出依赖 | 复用 |
| Electron | 上游 electron/ | WCDB 运行时备选 | 复用 |

**新增 Python 依赖：无。** 消息解析用标准库 `re` / `xml.etree.ElementTree` / `html`。

---

## 3. Commands

```bash
# 环境准备（已执行）
python -m pip install --user pycryptodome fpdf2 openpyxl Pillow pyinstaller

# 单元测试
python -m pytest tests/ -v

# 开发模式运行（需在上游发布目录旁，或用 --root 指定）
python gui/app_plus.py

# 构建发布包（产物：dist/WeChatExportPlus/）
python build_dist_plus.py

# 验证产物
dist/WeChatExportPlus/WeChatExportPlus.exe
```

---

## 4. Project Structure

```
C:\My_GongJu\grab\WeChatExportPlus\
├── gui\
│   ├── app_plus.py           ← 新 GUI 入口（重写自 app_v3.py）
│   └── icon.ico              ← 复用
├── exporters\
│   ├── message_content.py    ← 【新】统一消息 → 可读文本/结构化
│   ├── batch_export.py       ← 【新】批量导出编排 + 目录布局 + 进度 + 取消
│   ├── index_exporter.py     ← 【新】导出清单.html
│   ├── html_exporter.py      ← 改：使用 message_content
│   ├── pdf_exporter.py       ← 改：同上
│   ├── csv_exporter.py       ← 改：同上
│   ├── excel_exporter.py     ← 改：同上
│   ├── image_decoder.py      ← 不动
│   ├── media_resolver.py     ← 不动
│   ├── packed_info_parser.py ← 不动
│   └── logger.py             ← 不动
├── scripts\                  ← 内核，全部不动
│   ├── wcdb_server.js / .py
│   ├── get_key.js
│   ├── decrypt_image.js
│   └── node_modules\
├── resources\                ← 原生模块 + ffmpeg，不动
├── tests\
│   ├── test_message_content.py
│   ├── test_batch_export.py
│   └── fixtures\             ← 从真实数据提取的 XML 样本（脱敏）
├── tasks\
│   ├── spec.md / plan.md / todo.md
├── build_dist_plus.py        ← 【新】打包脚本
├── requirements.txt
└── LICENSE-upstream-GPLv3
```

---

## 5. Code Style

```python
# -*- coding: utf-8 -*-
"""模块职责一句话说明。"""
import os, re

def extract_body_content(msg: dict) -> tuple:
    """把一个微信消息转成 (kind, text, extra)。

    kind ∈ {'text','image','voice','video','file','link','miniprogram',
            'quote','chatrecord','location','card','call','sticker',
            'system','transfer','redpacket','unknown'}
    text 是可直接写入任何导出格式的**可读文本**（已含换行，未做 HTML 转义）。
    extra 是可选结构化信息 dict，供需要富文本的导出器使用。
    """
    ...
```

约定：
- 文件名 `snake_case.py`；函数 `snake_case`；常量 `UPPER_SNAKE`。
- 所有面向用户的文本用中文。
- 新模块**不打印**，通过传入 `log_func` 或 `logger` 输出。
- 优先纯函数 + 显式入参，避免隐式全局状态（上游 `OUT` 全局变量是已知痛点，新代码不复用该模式）。

---

## 6. Testing Strategy

- 框架：`pytest`（本机未装，打包脚本不依赖它；仅开发期使用）。
- 位置：`tests/`。
- 单元测试覆盖：
  1. **消息解析**（`message_content.py`）——用 `tests/fixtures/` 里从真实数据库提取的 XML 样本，断言每种类型的输出文本。
  2. **目录布局**（`batch_export.py`）——用假 WCDB 客户端 + 假导出器，断言生成的目录树与文件名，断言同名覆盖、非法字符清洗。
  3. **取消语义**——断言 cancel 后不再处理后续会话。
- 不做 GUI 自动化测试（tkinter GUI 测试成本高于收益）；GUI 用手动验收清单。
- 真实端到端验证：用用户已配置的密钥连接真实数据库，批量导出 3 个会话，人工打开产物确认。

---

## 7. Boundaries

**Always**
- 改动前先跑 `pytest`；交付前跑一次真实数据端到端导出。
- 新代码默认安全：格式默认 HTML，输出目录必须用户显式选择。
- 保留上游文件头注释与 GPL-3.0 许可声明。

**Ask first**
- 修改 `scripts/` 下任何内核文件。
- 修改 `exporters/media_resolver.py`、`image_decoder.py`、`packed_info_parser.py`。
- 引入新的 Python 第三方依赖。

**Never**
- 把密钥写入源码或提交到仓库。
- 删除/覆盖用户已有的 `C:\My_GongJu\grab\grab\WeChatExport` 目录。
- 在导出目录之外写入文件。

---

## 8. 输出目录布局（核心契约）

用户点击「导出」→ 选择总目录（如 `C:\Users\LanDeQuan\Desktop`）→ 程序生成：

```
<用户选择的总目录>\
└── 导出_20260213_1430\              ← 带时间戳的大文件夹
    ├── 导出清单.html                 ← 总索引，列出所有会话，可点击跳转
    ├── 群聊A\                        ← 会话名，同名直接覆盖
    │   ├── index.html                ← HTML 格式
    │   └── 图片\img_1.jpg ...
    ├── 私聊B\                        ← 会话名
    │   ├── index.html
    │   └── 图片\...
    └── 群聊C\
        └── 群聊C.xlsx                ← 非 HTML 格式：内部一个以会话名命名的文件
```

规则：
1. 大文件夹名 `导出_YYYYMMDD_HHMM`。
2. 子文件夹名 = 会话显示名，清洗掉 Windows 非法字符 `\/:*?"<>|`，去除首尾空格与点。
3. 子文件夹名重复（不同 wxid 同名）→ 自动追加 `_2`、`_3`。
4. 会话名清洗后为空 → 回退用 wxid。
5. 已存在同名子文件夹 → **直接覆盖**（符合用户选择）。
6. 图片仅 HTML/PDF 需要；HTML 走 `图片/` 子目录，PDF 内嵌。

---

## 9. 消息类型映射（基于真实数据探测）

探测来源：用户真实数据库，10 个高频会话共约 11000 条消息。

| local_type (base32) | 含义 | 原工具行为 | 新行为 |
|---|---|---|---|
| 1 | 文字 | 导出 | 导出 |
| 1（带高位标志，如 `244813135921`） | 文字/长文本 | 导出 | 导出（**不再当 appmsg**） |
| 3 | 图片 | `[图片]` | `[图片]`（HTML/PDF 内嵌） |
| 34 | 语音 | `[类型34]` | `[语音 4.6秒]`（解析 `voicelength`） |
| 42 | 名片 | `[类型42]` | `[名片] 昵称` |
| 43 | 视频 | `[类型43]` | `[视频 9秒]` + 缩略图尺寸 |
| 47 | 表情 | `[表情]` | `[表情]` |
| 48 | 位置 | `[类型48]` | `[位置] 北京首都国际机场T3航站楼` |
| 49 / type=5 | 链接/分享 | `[类型49]` | `[链接] 标题\nURL` |
| 49 / type=57 + refermsg | 引用回复 | `[类型49]` | `↩ 引用 <被引者>：<被引内容>\n<本条内容>` |
| 49 / type=19 | 合并转发聊天记录 | `[类型49]` | `[聊天记录] 标题\n  - 发送者: 内容`（展开 dataitem） |
| 49 / type=33/36/44 | 小程序 | `[类型49]` | `[小程序] 标题` |
| 49 / type=6 | 文件 | `[类型49]` | `[文件] 文件名 (大小)` |
| 49 / type=53 | 接龙 | `[类型49]` | `[接龙] <title>` |
| 49 / type=87 | 群公告 | `[类型49]` | `[群公告] <文本 dataitem>` |
| 49 / type=8 | 系统 appmsg | `[类型49]` | `[系统消息]` 或跳过 |
| 49 **无 appmsg XML、纯文字**（探测 670 条，最大量！） | 拍一拍等 | **整条丢弃** | **按文字导出** |
| 50 | 语音/视频通话 | `[通话]` | `[通话] 通话时长 26:21` |
| 10000 | 系统通知（撤回等） | `[系统通知]` | `[系统通知] "某某" 撤回了一条消息` |
| 8589934592049 / 8594229559345 | 转账 / 红包 | `[类型…]` | `[微信转账]` / `[微信红包]` |
| 其他未知 | — | `[类型N]` | `[未知消息类型 N]`（保留原文前 100 字便于排查） |

**关键设计**：判定「是不是 appmsg」不能只看 `local_type`，必须看 `message_content` 是否真的包含 `<appmsg`。
这是原工具的根因 bug（`(lt & 0xFFFFFFFF) == 49` 就当作 appmsg，导致 670 条纯文字被丢弃）。

**另一关键点**：`message_content` 有三种形态，解析前必须统一：
1. 纯文本（`local_type` base 为 1 或 49 但无 XML）
2. 明文 XML（多数）
3. 十六进制 ZSTD 压缩（`28b52ffd`，需 `zstandard` 或复用 Node 侧解压结果）

群聊消息可能带 `wxid_xxx:` 发送者前缀，需剥离。

---

## 10. Success Criteria（验收清单）

- [ ] 勾选 3 个会话 + 选 HTML + 选桌面 → 生成 `导出_<时间戳>\`，内含 3 个会话文件夹 + `导出清单.html`。
- [ ] 每个会话文件夹内含 `index.html` 与 `图片\`，双击 `index.html` 能看到消息、图片正常显示。
- [ ] 换成 Excel → 每个会话文件夹内是 `<会话名>.xlsx`，打开后消息类型列显示「语音」「链接」等可读值而非「类型49」。
- [ ] 导出进度显示「第 2/3 个：群聊A」，点取消后立即停止且不产生半个文件。
- [ ] `导出清单.html` 里点击会话名能跳转到对应 `index.html`。
- [ ] 全选 128 个会话不崩溃（允许耗时长，可取消）。
- [ ] 双击会话仍能打开预览窗口查看消息（原功能保留）。
- [ ] 密钥、连接数据库、浏览会话三步流程与截图一致。

---

## 11. Open Questions

无（已与用户确认：目录平铺、仅会话名、同名覆盖、独立发布文件夹、保留预览、解析消息类型）。
