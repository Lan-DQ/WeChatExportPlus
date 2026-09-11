# Task List: 微信聊天记录批量导出工具

图例：`[ ]` 未开始 · `[~]` 进行中 · `[x]` 已完成

> **最终状态：全部完成。** 60 个单测通过；真实数据端到端验证通过
> （3 会话 / 5978 条消息 / 508 张图片，0 缺失）；发布包 516MB 已产出并验证。

---

### Phase 1: 基础

- [x] **Task 1: `exporters/message_content.py` 统一消息解析器**
  - 覆盖 spec.md 第 9 节全部类型；判定 appmsg 看内容不看 local_type
  - 真实数据实测：12,858 条消息，`[类型N]` 占位符 **0 个**，空文本 **0 条**
  - 34 个单测
- [x] **Task 2: 修 `scripts/wcdb_server.js` ZSTD 长度上限（10000 → 1000000）**
  - 实测你的库里有 2 条消息因该上限被静默丢弃，修复后已恢复

### Checkpoint: Foundation — 通过

---

### Phase 2: 导出器改造

- [x] **Task 3: CSV + Excel** —— 改读统一解析结果，「类型」列输出可读中文
- [x] **Task 4: HTML** —— 图片走 `图片/` 相对路径、引用两段式、聊天记录折叠、链接可点击
- [x] **Task 5: PDF** —— 图片内嵌；不再多余落一份 `图片/`

### Checkpoint: Exporters — 通过（6 种格式真实数据导出均正常）

---

### Phase 3: 批量编排

- [x] **Task 6: `exporters/batch_export.py`**
  - `导出_YYYYMMDD_HHMM\<会话名>\`；同名会话自动加 `_2`；同名分钟级导出不覆盖
  - `progress(i,total,name)` / `should_cancel()` 回调；单会话失败不影响其余
  - 10 个单测
- [x] **Task 7: `exporters/index_exporter.py`** —— `导出清单.html`，可点击跳转

### Checkpoint: Batch — 通过（含 508 张图片落盘、0 缺失）

---

### Phase 4: AI 语料格式（用户追加需求）

- [x] **Task 3b: `exporters/ai_exporter.py`** —— `对话.jsonl` + 带发言人的 `对话.txt`
  - 发言人：`sender`(显示名) + `sender_id`(wxid) + `role`(self/other/system)
  - 实测群聊 90% / 单聊 99% 消息有发言人名（其余是系统消息，本就无发言人）
  - 图片给相对路径 `图片/xxx.jpg`；实测 325 个引用全部有效
  - 用 使用说明.md 4.1 的示例代码验证：20/20 图片 data URL 合法，请求体 15.23 MiB（< 48 MiB 上限）

### Checkpoint: AI 语料 — 通过

---

### Phase 5: GUI

- [x] **Task 8: `gui/app_plus.py`** —— 首页（与截图一致）+ 勾选式会话列表 + 统一导出栏
  - 无头验证：勾选/取消、全选、全不选、反选、全选搜索结果、搜索后保留勾选 —— 全部正确
- [x] **Task 9: 进度窗口 + 取消 + 保留双击预览**

### Checkpoint: GUI — 通过（控件构建与选择逻辑无头验证；exe 启动 25 秒无崩溃）

---

### Phase 6: 打包交付

- [x] **Task 10: `build_dist_plus.py`** —— 发布包 516MB，20 项必需文件全部存在
- [x] **Task 11: 端到端验证**
  - 用发布包自带 `runtime\node.exe` 跑通完整链路（含图片解密 325/325）
  - 证明目标机器**无需安装 Node.js**

### Checkpoint: Complete — 通过

