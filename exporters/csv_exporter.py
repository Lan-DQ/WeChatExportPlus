# -*- coding: utf-8 -*-
"""CSV 导出器。

[WeChatExportPlus 改动] 改用 message_content.prepare() 的统一解析结果：
  - 不再只导出 local_type in (1, 244813135921) 的文字，图片/语音/链接/文件/系统消息都会导出；
  - 「类型」列由 kind_label() 生成可读中文，不再是 '类型49' 这种占位。
列格式保持不变（时间/发送者/消息内容/类型），保证与旧版产物兼容。
"""
import csv

import message_content

# CSV 没法优雅承载长段说明，所以指令追加在文件末尾（用 Excel 打开就是最后几行），
# 中间用一行标记隔开，AI 读到最后也能看到。
INSTRUCTION_MARK = '# --- 给 AI 的指令（以下不是聊天记录）---'


def export(msgs, path, my_name='我', title=None, session_title=None, prompt=None):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['时间', '发送者', '消息内容', '类型'])
        for m in msgs:
            text = (m.get('text') or '').strip()
            if not text:
                continue
            w.writerow([
                m.get('time_str', ''),
                m.get('sender_display', ''),
                text,
                message_content.kind_label(m.get('kind', 'unknown')),
            ])
        if prompt and prompt.strip():
            w.writerow([])
            w.writerow([INSTRUCTION_MARK])
            for line in prompt.strip().split('\n'):
                w.writerow([line])
    return path
