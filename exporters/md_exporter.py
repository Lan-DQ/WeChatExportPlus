# -*- coding: utf-8 -*-
r"""单文件 Markdown 导出器 —— 专为「直接拖进 DeepSeek 网页版」设计。

为什么需要这个格式
------------------
网页版聊天窗口只能**上传文件**（最多 50 个文件、单文件 100MB），不认文件夹，
所以「一个会话一个 jsonl + 一个图片文件夹」在网页版里不好用。

本格式的产出（都直接放在导出根目录下，同级）：

    导出_20260213_1430\
    ├── 群聊A.md              ← 整个会话的文本，双击打开就是一篇完整对话
    ├── 群聊A_图片\           ← 图片，按 0001.jpg 0002.jpg 顺序编号
    │   ├── 0001.jpg
    │   └── ...
    ├── 私聊B.md
    └── 私聊B_图片\

用法：把 `.md` 拖进网页版（或复制全文粘贴），需要看图时再把 `_图片\` 里的图按编号拖进去。
正文里图片写成 `[图片01]`，和 `_图片\01.jpg` 一一对应，AI 能对上号。

文本设计要点
------------
- 每条消息一行：`[时间] 发言人: 内容`，第一列固定，AI 和正则都好处理；
- 头部有会话名/类型/人数/消息数/时间范围，AI 一上来就知道这是什么；
- 多行消息的续行缩进 4 空格，不会和下一行的 `[时间]` 混淆；
- 图片、语音、链接、文件、位置、系统消息等都已经是可读文本（见 message_content）。
"""
import os
import re
import shutil

MARKDOWN_EXT = '.md'


def _safe(name, fallback='chat'):
    s = re.sub(r'[\\/:*?"<>|]', '_', str(name or '')).strip().rstrip('. ')
    s = re.sub(r'\s+', ' ', s)
    return (s or fallback)[:80]


def _people(records):
    """统计参与者（按出现次数排序），返回 (显示名列表, 显示名→wxid)。"""
    order = {}
    ids = {}
    for r in records:
        if r.get('role') == 'system':
            continue
        name = r.get('sender') or ''
        if not name:
            continue
        order[name] = order.get(name, 0) + 1
        if r.get('sender_id') and name not in ids:
            ids[name] = r['sender_id']
    return [n for n, _ in sorted(order.items(), key=lambda kv: -kv[1])], ids


def build_document(rows, chat, images=None, prompt=None):
    """生成 Markdown 全文。

    rows   : message_content.prepare() 的输出
    chat   : {'wxid','title','is_group'}
    images : {local_id: '01.jpg'} 图片编号映射，没有则不写图片行
    prompt : 给 AI 的指令文本；非空时插在正文之前（网页版没有系统提示词，
             所以必须内嵌在文件里 AI 才读得到）
    """
    import ai_exporter     # 复用同一套「渲染成一行文本」的逻辑，保证两种格式一致

    images = images or {}
    records = []
    for m in rows:
        if not (m.get('text') or m.get('kind') == 'image'):
            continue
        sender = m.get('sender_display') or m.get('sender_raw') or ''
        rec = {
            # 同时带两种字段名：time/sender/type 给本模块的头部统计用，
            # time_str/sender_display/kind 给 ai_exporter.render_text 用
            'time': m.get('time_str', ''),
            'sender': sender,
            'sender_id': m.get('sender_raw', ''),
            'role': 'system' if m.get('kind') == 'system' else (
                'self' if m.get('is_mine') else 'other'),
            'type': m.get('kind', 'unknown'),
            'text': m.get('text', ''),
            'time_str': m.get('time_str', ''),
            'sender_display': sender,
            'sender_raw': m.get('sender_raw', ''),
            'kind': m.get('kind', 'unknown'),
            'is_mine': m.get('is_mine', 0),
        }
        if m.get('kind') == 'image':
            idx = images.get(str(m.get('local_id', '')))
            if idx:
                # 用编号代替文件名：正文里写 [图片01]，和 _图片\01.jpg 对得上
                rec['image_index'] = idx
                rec['text'] = f'[图片{os.path.splitext(idx)[0]}]'
        # 渲染必须放在改写 text 之后，否则图片行写的是旧的 "[图片]"
        rec['render'] = ai_exporter.render_text(rec, with_image_path=False)
        records.append(rec)

    people, ids = _people(records)
    days = [r['time'][:10] for r in records if r.get('time')]
    title = chat.get('title') or chat.get('wxid') or '聊天记录'

    L = []
    L.append(f'# {title}')
    L.append('')
    L.append(f'- 会话类型：{"群聊" if chat.get("is_group") else "单聊"}')
    if people:
        listed = '、'.join(people[:30])
        if len(people) > 30:
            listed += f' 等 {len(people)} 人'
        L.append(f'- 参与者（{len(people)} 人）：{listed}')
    L.append(f'- 消息数：{len(records)} 条')
    if days:
        L.append(f'- 时间范围：{min(days)} ~ {max(days)}')
    n_img = sum(1 for r in records if r.get('image_index'))
    if n_img:
        L.append(f'- 图片：{n_img} 张，正文里写 `[图片NNNN]`，'
                 f'对应同目录下 `{_safe(title)}_图片\\NNNN.jpg`')
    L.append('')
    L.append('> 每条消息格式为 `[时间] 发言人: 内容`。'
             '`(系统)` 表示系统消息，`我` 表示本人。')
    L.append('')
    L.append('---')
    L.append('')

    # 给 AI 的指令放在正文之前：网页版没有系统提示词，只能靠文件内容传达
    if prompt:
        import ai_prompt
        head = ai_prompt.as_markdown(prompt)
        if head:
            L.append(head)

    for r in records:
        line = r.get('render') or ''
        if not line:
            continue
        if '\n' in line:
            head, rest = line.split('\n', 1)
            line = head + '\n' + '\n'.join('    ' + x for x in rest.split('\n'))
        L.append(line)
    L.append('')

    return '\n'.join(L), records


def build_image_map(rows):
    """按出现顺序给图片编号：{local_id: '0001.jpg'}。

    用 4 位定宽编号：编号位数固定，文件名排序才和出现顺序一致
    （2 位编号在超过 99 张时会乱序：100.jpg 会排到 01.jpg 前面）。
    同一个 local_id 可能出现多条图片消息（一条消息带多图），此时沿用首次编号，
    避免正文引用和文件数量对不上。
    """
    images = {}
    i = 0
    for m in rows:
        if m.get('kind') != 'image' or not m.get('image_file'):
            continue
        key = str(m.get('local_id', ''))
        if key in images:
            continue
        i += 1
        ext = os.path.splitext(str(m['image_file']))[1] or '.jpg'
        images[key] = f'{i:04d}{ext}'
    return images


def export(rows, out_root, chat, image_map=None, image_src_dir=None, prompt=None):
    """写出 `<out_root>/<会话名>.md` 与 `<out_root>/<会话名>_图片/`。

    image_map     : {local_id: '01.jpg'}，由 build_image_map() 生成（batch_export 传入）
    image_src_dir : 已解密的图片所在目录（即 ...\\图片\\），文件名是 image_file
    prompt        : 给 AI 的指令；None 表示从 AI提示词.txt / 内置默认自动读取

    返回 {'md': md路径, 'img_dir': 图片目录或 '', 'images': 张数, 'base': 名称}
    """
    os.makedirs(out_root, exist_ok=True)
    title = chat.get('title') or chat.get('wxid') or 'chat'
    base = _safe(title, chat.get('wxid', 'chat'))

    images = dict(image_map) if image_map else build_image_map(rows)

    # 按编号把图片复制成 01.jpg 02.jpg …
    img_dir = ''
    copied = 0
    if images and image_src_dir:
        img_dir = os.path.join(out_root, f'{base}_图片')
        if os.path.isdir(img_dir):
            shutil.rmtree(img_dir, ignore_errors=True)
        os.makedirs(img_dir, exist_ok=True)
        keep = {}
        for m in rows:
            if m.get('kind') != 'image' or not m.get('image_file'):
                continue
            key = str(m.get('local_id', ''))
            target = images.get(key)
            if not target:
                continue
            src = os.path.join(image_src_dir, m['image_file'])
            try:
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(img_dir, target))
                    keep[key] = target
                    copied += 1
            except OSError:
                pass
        images = keep
        if copied == 0:
            shutil.rmtree(img_dir, ignore_errors=True)
            img_dir = ''

    doc, _records = build_document(rows, chat, images, prompt=prompt)
    md_path = os.path.join(out_root, f'{base}{MARKDOWN_EXT}')
    with open(md_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(doc)

    return {'md': md_path, 'img_dir': img_dir, 'images': copied, 'base': base}
