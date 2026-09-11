# 微信聊天记录批量导出工具

把多个微信群 / 私聊的聊天记录**一次性批量导出**成适合喂给 AI 的语料。
默认输出**单文件 Markdown**，可以直接拖进 DeepSeek 等网页版对话框。

> 内核来自 [Ray0612/WeChat-Export-Tool](https://github.com/Ray0612/WeChat-Export-Tool) v1.2.0（GPL-3.0），
> 在其基础上重建了交互层，并补全了消息类型解析、发言人识别、批量导出与 AI 语料输出。

---

## 下载使用（普通用户看这里）

**不需要安装 Python / Node.js**，下载解压双击即可。

1. 到 [**Releases**](../../releases/latest) 页面下载 `WeChatExportPlus_v2.0.0_win64.zip`（约 224 MB）
2. 解压到任意目录（路径别太深，也别放 C 盘根目录）
3. 双击 **`WeChatExportPlus.exe`**（或 `启动工具.bat`）
4. 按界面提示：确认数据目录 → 获取密钥 → 连接数据库 → 浏览会话
5. 勾选要导出的会话，选格式（默认 Markdown 单文件），点「开始导出」

详细图文步骤见包内的 **`使用说明.md`**。

### 系统要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 64 位 |
| 微信 | **微信 4.x** Windows 版（旧版 3.x 不支持） |
| 权限 | 获取密钥时需要管理员权限（要注入微信进程读取密钥） |
| 磁盘 | 约 1.5 GB（解压后 516 MB + 导出产物） |

---

## 功能

- **勾选多个会话**，一次导出（支持全选 / 反选 / 搜索后全选）
- **统一设置导出格式**，不用逐个会话设置
- **进度显示「第 N/M 个：会话名」**，可随时取消
- 输出带时间戳的归档目录 + `导出清单.html` 索引
- **消息类型完整解析**：文字、图片、语音、视频、链接、文件、引用、
  合并转发、位置、名片、通话、转账、红包、接龙、群公告、系统消息
- **发言人识别**：群聊每条消息都带发言人显示名与原始 wxid
- **图片解密导出**并按出现顺序编号（`0001.jpg`），正文用 `[图片0001]` 对应
- **每次导出自动附带「给 AI 的指令」**，可自行编辑（`AI提示词.txt`）

### 导出格式

| 格式 | 用途 |
|---|---|
| **Markdown 单文件**（默认） | 一个会话一个 `.md`，**可直接拖进 DeepSeek 网页版** |
| AI 语料 JSONL | 一行一条消息，带发言人/时间/类型/图片路径，适合入库做 RAG |
| HTML | 气泡页面，支持搜索，图片独立存放 |
| PDF | 图片内嵌，单文件归档 |
| Excel / CSV | 表格，方便筛选统计 |
| TXT / JSON | 纯文本 / 结构化 |

---

## 从源码运行 / 构建

```bash
# 依赖
python -m pip install --user pycryptodome fpdf2 openpyxl Pillow pyinstaller pytest

# 开发期：把原版 WeChatExport 的运行时目录链接进来（dll/runtime/resources/electron）
python dev_setup.py --kernel "路径\到\WeChatExport"

# 运行
python gui/app_plus.py

# 测试
python -m pytest tests/ -v

# 打包发布（产物 dist/WeChatExportPlus/）
python build_dist_plus.py
```

打包需要原版 `WeChatExport` 目录提供运行时文件（`node.exe`、`WCDB.dll`、
`resources/native` 图片解密模块等），它们体积大且属于第三方组件，故不进本仓库。

### 发布到 GitHub Releases

```bash
# 先把 token 存到 .token 文件（已被 .gitignore 排除），或设 GH_TOKEN 环境变量
python publish_release.py --repo 你的用户名/WeChatExportPlus --tag v2.0.0
```

---

## 项目结构

```
gui/app_plus.py             主界面（tkinter）
exporters/
  message_content.py        统一消息解析（所有类型的可读化）
  batch_export.py           批量导出编排（目录布局/进度/取消）
  md_exporter.py            单文件 Markdown 导出
  ai_exporter.py            JSONL + 带发言人 TXT
  html_exporter.py          HTML 气泡页面
  pdf_exporter.py           PDF
  csv_exporter.py           CSV
  excel_exporter.py         Excel
  index_exporter.py         导出清单.html
  ai_prompt.py              「给 AI 的指令」读写
  media_resolver.py         图片查找与解密编排
  image_decoder.py          .dat 图片解密
  packed_info_parser.py     从消息里提取图片 md5/aeskey
  logger.py                 日志
scripts/                    内核（不进仓库的运行时除外）
  wcdb_server.js            Node + koffi 调用 WCDB.dll
  wcdb_server.py            Python 侧 HTTP 客户端
  get_key.js                密钥提取
  decrypt_image.js          图片解密助手
tests/                      92 个单元测试
tasks/                      规格 / 计划 / 任务清单
```

---

## 已知限制

| 项 | 说明 |
|---|---|
| 语音 / 视频 | 只有时长，没有内容，也未转写 |
| 链接 / 小程序 / 公众号 | 只有标题和链接，拿不到正文（需登录态请求） |
| 文件消息 | 只有文件名和大小，不下载文件本体 |
| 表情 | 只有 `[表情]` 占位，不下载表情图 |
| 图片内容 | 图片已解密导出，但**图片里的文字 AI 看不到**，需要把图一并上传 |
| 撤回消息 | 只能看到「谁撤回了一条消息」 |
| 已删除会话 | 微信本地已清理的会话读不到 |
| PDF 中的 emoji | 中文字体不含 emoji 字形，会显示为空白 |

## 隐私提醒

导出的聊天记录包含你与他人的私密对话。程序**不会**上传任何数据，全部在本地处理；
但如果你把这些导出文件交给云端 AI，等于把内容提供给了模型厂商。
介意的话请改用本地模型，导出的 `.md` / `对话.jsonl` 一样能用。

---

## 许可证

本项目同样以 **GPL-3.0** 分发（因为使用了 GPL-3.0 的上游内核）。
许可证原文见 `LICENSE-upstream-GPLv3`。

第三方组件：WCDB.dll (BSD-3)、SDL2 (zlib)、wx_key.dll (MIT)、Electron (MIT)、
koffi (MIT)、fzstd (MIT)、PyInstaller (GPL-2.0)、ffmpeg (GPL)、fpdf2 (LGPL)、openpyxl (MIT)。

**仅供导出本人的聊天记录备份使用。请遵守相关法律法规，不要用于侵犯他人隐私。**
