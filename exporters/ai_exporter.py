# -*- coding: utf-8 -*-
"""AI 语料导出器（JSONL + 带发言人的纯文本）。

为什么单独做这个格式
--------------------
其余 6 种格式是给人看的（气泡、表格、PDF 排版），喂给大模型时
HTML 的 CSS/JS 会白吃 token，Excel/PDF 根本没法直接读。
这里专门产出「信息量最大、对模型最友好」的两份文件：

1. `对话.jsonl` —— 每行一条消息的独立 JSON 对象。
   - 一行一条 = 可以流式读取、可以按行切片入库、不会被多行内容破坏结构；
   - 保留全部结构化字段（原始 wxid、消息类型、时间戳、图片路径）；
   - `render` 字段是已经拼好的「可直接喂模型的文本」，不想自己拼就用它；
   - 转发聊天记录 / 小程序 / 引用 / 文件 等富信息全部保留在 `extra` 与正文里。

2. `对话.txt` —— `[时间] 发言人: 内容` 的纯文本。
   - 给「直接整篇丢给模型」或做 RAG 切块的场景用；
   - 发言人标记统一格式，模型能稳定区分谁在说话；
   - 图片写成 `[图片: 图片/xxx.jpg]`，多模态模型/检索链路能按相对路径取图。

发言人是重点：`sender`（显示名）、`sender_id`（原始 wxid）、
`role`（self/other/system）三个字段都给出，避免模型猜谁是谁。
"""
import json
import os

# 富信息类型：正文本身就带结构，文本渲染时原样保留多行
_RICH_KINDS = {'link', 'file', 'quote', 'chatrecord', 'miniprogram',
               'solitaire', 'groupannounce', 'location', 'card', 'call', 'app'}


def _role(m):
    if m.get('kind') == 'system':
        return 'system'
    return 'self' if m.get('is_mine') else 'other'


def image_rel_path(m):
    """这条消息对应的图片相对路径（相对会话文件夹），没有则返回 ''。

    约定图片存放在 `<会话文件夹>/图片/<md5>.<ext>`。
    """
    if m.get('kind') != 'image':
        return ''
    name = m.get('image_file')
    if not name:
        return ''
    return f'图片/{name}'


def render_text(m, with_image_path=True):
    """把一条消息渲染成「[时间] 发言人: 内容」形式的一行/多行文本。"""
    ts = m.get('time_str', '')
    sender = m.get('sender_display') or m.get('sender_raw') or ''
    text = (m.get('text') or '').strip()
    kind = m.get('kind', 'unknown')

    if kind == 'system':
        head = f'[{ts}] (系统)'
    else:
        head = f'[{ts}] {sender}:'

    body = text
    if kind == 'image' and with_image_path:
        rel = image_rel_path(m)
        body = f'[图片: {rel}]' if rel else '[图片: 未导出]'
    if not body:
        return ''

    # 富信息类型：正文含换行时缩进后续行，保证第一列永远是「时间 发言人」
    if kind in _RICH_KINDS and '\n' in body:
        lines = body.split('\n')
        body = lines[0] + ''.join('\n    ' + l for l in lines[1:])
    return f'{head} {body}'


def build_records(rows, chat, with_image_path=True):
    """把 prepare() 出来的行列表转成 JSONL 记录列表。"""
    records = []
    for m in rows:
        if not (m.get('text') or m.get('kind') == 'image'):
            continue
        ts = str(m.get('create_time') or '')
        rec = {
            'id': f"{chat.get('wxid', '')}:{ts}:{m.get('local_id', '')}",
            'ts': int(ts) if ts.isdigit() else None,
            'time': m.get('time_str', ''),
            'sender': m.get('sender_display') or m.get('sender_raw') or '',
            'sender_id': m.get('sender_raw', ''),
            'role': _role(m),
            'type': m.get('kind', 'unknown'),
            'text': m.get('text', ''),
            'chat': chat.get('title', ''),
            'chat_id': chat.get('wxid', ''),
            'is_group': bool(chat.get('is_group')),
        }
        extra = m.get('extra') or {}
        if extra:
            rec['extra'] = extra
        if m.get('kind') == 'image':
            rel = image_rel_path(m) if with_image_path else ''
            if rel:
                rec['image'] = rel
        rec['render'] = render_text(m, with_image_path)
        records.append(rec)
    return records


def export(rows, out_dir, chat, fmt='ai', with_image_path=True, prompt=None):
    """写出 `对话.jsonl` 与 `对话.txt`，返回 {'jsonl': path, 'txt': path}。

    prompt 非空时写进 jsonl 的 _meta.ai_instruction 和 txt 的文件头。
    """
    os.makedirs(out_dir, exist_ok=True)
    records = build_records(rows, chat, with_image_path)

    jsonl_path = os.path.join(out_dir, '对话.jsonl')
    # 首行写一条 meta，方便入库时知道这份语料的来源与范围
    days = [r['time'][:10] for r in records if r.get('time')]
    meta = {
        '_meta': {
            'chat': chat.get('title', ''),
            'chat_id': chat.get('wxid', ''),
            'is_group': bool(chat.get('is_group')),
            'message_count': len(records),
            'date_from': min(days) if days else '',
            'date_to': max(days) if days else '',
            'note': ('每行一条消息；render 字段是可直接喂模型的文本；'
                     '图片按 image 字段的相对路径存放于同目录下。'),
            'ai_instruction': (prompt or '').strip(),
        }
    }
    with open(jsonl_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(meta, ensure_ascii=False) + '\n')
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    txt_path = os.path.join(out_dir, '对话.txt')
    with open(txt_path, 'w', encoding='utf-8', newline='\n') as f:
        if prompt and prompt.strip():
            f.write('===== 给 AI 的指令（以下不是聊天记录，请先读完再回答）=====\n')
            for line in prompt.strip().split('\n'):
                f.write(f'# {line}\n' if line.strip() else '#\n')
            f.write('===== 以下是聊天记录正文 =====\n\n')
        f.write(f"# 聊天记录：{chat.get('title', '')}\n")
        f.write(f"# 会话ID：{chat.get('wxid', '')}\n")
        f.write(f"# 类型：{'群聊' if chat.get('is_group') else '单聊'}\n")
        f.write(f"# 消息数：{len(records)}\n")
        if days:
            f.write(f"# 时间范围：{min(days)} ~ {max(days)}\n")
        f.write('# 格式：[时间] 发言人: 内容   （(系统) 表示系统消息，'
                'self 表示本人）\n')
        f.write('\n')
        for r in records:
            line = r.get('render') or ''
            if line:
                f.write(line + '\n')

    return {'jsonl': jsonl_path, 'txt': txt_path}
