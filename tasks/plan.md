# Implementation Plan: 微信聊天记录批量导出工具

## Overview

以 `Ray0612/WeChat-Export-Tool` v1.2.0 为内核（保留 WCDB 解密、密钥提取、图片解密、6 种导出格式），替换其 GUI 交互模型：把「双击单会话逐个导出」改为「勾选多会话 + 统一格式 + 一次批量导出到结构化目录」。同时修复内核的一个真实缺陷：判定 appmsg 只看 `local_type` 而不看内容，导致 670 条纯文字消息被静默丢弃。

## Architecture Decisions

1. **源码重建而非反编译**：GitHub 上 `main` 分支源码版本 = v1.2.0（已核对），直接使用，避免反编译 `.pyc` 的风险与许可问题（GPL-3.0 要求保留许可）。
2. **不动内核**：`scripts/wcdb_server.js|py`、`get_key.js`、`decrypt_image.js`、`exporters/media_resolver.py|image_decoder.py|packed_info_parser.py` 全部保持原样（唯一例外见第 3 条）。这保证「密钥已配置即可用」。
3. **唯一的内核例外**：`wcdb_server.js` 解压 ZSTD 时有一处 `t.length < 10000` 上限，超长消息会把裸 hex 交给 Python，而 Python 侧没有 zstandard，结果是该条消息被导出器 `continue` 跳过 → **静默丢消息**。修法：提升上限到 1_000_000 并保留原有行为（仍取 `<title>` 作为桥梁回退值）。这是「防丢数据」的必要最小改动。
4. **消息解析集中在一处**：新增 `exporters/message_content.py`，全部 6 个导出器都改为读它的输出，避免在 6 个文件里各写一套类型判断（现状就是各写各的，且互相不一致）。
5. **导出编排与 GUI 解耦**：新增 `exporters/batch_export.py`，纯函数式，接收 `wcdb` 客户端、会话列表、格式、目标目录、`progress` 回调、`should_cancel` 回调。GUI 只负责收集参数与显示进度。这样批量逻辑可单测，不需要开窗口。
6. **大文件夹布局**：`导出_YYYYMMDD_HHMM\<会话名>\`，平铺、不分类、同名覆盖（用户明确选择）。
7. **发布形态**：独立完整文件夹 `dist\WeChatExportPlus\`（用户明确选择），内含自带 node runtime / dll / electron / resources，可整目录拷走。
8. **构建用 Python 3.12**：本机仅有 3.12.10；上游用 3.13 编译无强制要求（未用 3.13 特性），3.12 完全可行。

## Task List

### Phase 1: 基础（先修数据正确性，因为它是后续一切的地基）
- [ ] Task 1: `message_content.py` — 统一消息解析器 + 从真实数据提取的 fixtures + 单测
- [ ] Task 2: 修 `wcdb_server.js` 的 ZSTD 长度上限（防丢消息）

### Checkpoint: Foundation
- [ ] `pytest tests/test_message_content.py` 全绿
- [ ] 用真实数据跑一遍解析，确认 `[类型…]` 占位符比例大幅下降

### Phase 2: 导出器改造
- [ ] Task 3: 改造 `csv_exporter.py` + `excel_exporter.py`（表格类，最简单，先验证解析器接入正确）
- [ ] Task 4: 改造 `html_exporter.py`（重点：图片 + 富文本 + 类型渲染）
- [ ] Task 5: 改造 `pdf_exporter.py`

### Checkpoint: Exporters
- [ ] 单独导出 1 个会话的 6 种格式，人工确认内容可读、图片正常

### Phase 3: 批量编排
- [ ] Task 6: `batch_export.py` — 目录布局、覆盖语义、进度回调、取消 + 单测
- [ ] Task 7: `index_exporter.py` — `导出清单.html`

### Checkpoint: Batch
- [ ] `pytest tests/test_batch_export.py` 全绿
- [ ] 命令行直接调用 `batch_export` 导出 3 个真实会话，检查目录树

### Phase 4: GUI
- [ ] Task 8: `gui/app_plus.py` — 首页（与截图一致）+ 会话页（勾选 + 全选/反选 + 统一格式 + 导出）
- [ ] Task 9: 进度窗口（第 N/M 个 + 取消）+ 保留双击预览

### Checkpoint: GUI
- [ ] 开发模式手动跑通完整流程

### Phase 5: 打包交付
- [ ] Task 10: `build_dist_plus.py` + PyInstaller 打包 + 组装发布文件夹
- [ ] Task 11: 用打包产物做端到端验证（不是开发模式）

### Checkpoint: Complete
- [ ] spec.md 第 10 节验收清单逐条通过
- [ ] 输出 `使用说明.md`

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 打包后 `sys.executable` 路径变化导致找不到 `scripts/`、`runtime/` | 高 | 新 GUI 显式用 `os.path.dirname(sys.executable)` 作 BASE；打包后专门验证 |
| 图片解密依赖 `shutil.which('node')`，打包后可能找不到 node | 中 | 发布包自带 `runtime\node.exe`；在验证清单里专门测「带图片的会话」 |
| 批量导出耗时（图片解密），用户以为卡死 | 中 | 进度窗口显示「第 N/M 个：名称」，可取消；导出前后写日志 |
| 同名会话覆盖导致用户丢数据 | 中 | 用户已明确选择覆盖；在 `导出清单.html` 与完成提示里显示导出路径，便于找回 |
| `local_type` 高位的含义是推断的 | 中 | 不依赖高位语义：判定只看「内容里有没有 `<appmsg`」+ base32 值，已用真实数据验证 |
| Python 3.12 打包产物与上游 3.13 行为差异 | 低 | 用真实数据端到端验证，不只看单测 |

## Open Questions

无。
