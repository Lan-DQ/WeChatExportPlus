# -*- coding: utf-8 -*-
"""总索引页 `导出清单.html`。

列出本次批量导出的每个会话，点击会话名直接打开对应产物：
  - HTML 格式 → 打开该会话文件夹的 index.html
  - AI 格式   → 打开 对话.txt（人也能直接读；机器读 对话.jsonl）
  - 其它格式  → 打开该会话文件夹里唯一的文件
同时显示消息数、图片数、导出时间，方便确认这次导出了什么。
"""
import datetime
import html as h
import os

CSS = '''
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;background:#f5f6f8;color:#1f2a37;padding:24px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin-bottom:4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:16px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #eef0f3}
th{background:#fafbfc;color:#6b7280;font-weight:500;font-size:13px}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbfc}
a{color:#2563eb;text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}
.num{color:#374151;font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;background:#eef2ff;color:#4338ca;margin-left:6px}
.badge.grp{background:#ecfdf5;color:#047857}
.path{font-size:12px;color:#9ca3af;font-family:ui-monospace,Consolas,monospace}
.prompt{background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:12px 16px;margin-bottom:16px}
.prompt summary{cursor:pointer;font-weight:600;font-size:13px;color:#92400e;outline:none}
.prompt-body{margin-top:8px;font-size:12px;line-height:1.7;color:#78350f;white-space:pre-wrap;max-height:300px;overflow:auto}
'''

# 每种格式下，索引页默认打开的主文件
_MAIN = {
    'md': None,            # md 格式的 main 就是 <会话名>.md，直接用 item['main']
    'ai': '对话.txt',
    'html': 'index.html',
}


def _first_file(chat_dir):
    try:
        for f in sorted(os.listdir(chat_dir)):
            full = os.path.join(chat_dir, f)
            if os.path.isfile(full):
                return f
    except OSError:
        pass
    return ''


def export(root, items, fmt, cancelled=False, prompt=None):
    """在 root 下写 导出清单.html。

    items: batch_export 返回的 ok 列表，元素含 dir/title/wxid/count/images/main。
    """
    from urllib.parse import quote

    rows_html = []
    for it in items:
        d = it.get('dir', '')
        title = it.get('title') or it.get('wxid') or ''
        is_group = str(it.get('wxid', '')).endswith('@chatroom')
        chat_dir = os.path.join(root, d)
        if fmt == 'md':
            # md 的目标文件直接躺在根目录，不在子文件夹里
            target = it.get('main') or ''
            href = quote(target) if target else ''
        else:
            target = it.get('main') or _MAIN.get(fmt) or _first_file(chat_dir)
            href = quote(f'{d}/{target}'.replace('\\', '/')) if target else quote(d + '/')
        badge = '<span class="badge grp">群聊</span>' if is_group else '<span class="badge">单聊</span>'
        n_img = it.get('images') or 0
        img_txt = f'<span class="num">{n_img}</span>' if n_img else '<span class="path">—</span>'
        name_html = (f'<a href="{href}">{h.escape(target or title)}</a>'
                     if href else h.escape(title))
        rows_html.append(
            f'<tr><td>{name_html}{badge}</td>'
            f'<td class="num">{it.get("count", 0)}</td>'
            f'<td>{img_txt}</td>'
            f'<td class="path">{h.escape(d)}</td></tr>'
        )

    total_msgs = sum(int(i.get('count') or 0) for i in items)
    total_imgs = sum(int(i.get('images') or 0) for i in items)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    fmt_note = {
        'md': 'Markdown 单文件格式：每个会话一个 <b>.md</b> 文件，直接放在本目录下，'
              '双击即可阅读，也<b>可以直接拖进 DeepSeek 网页版</b>。'
              '需要看图时，再把旁边的 <b>&lt;会话名&gt;_图片\\</b> 里的图按编号拖进去'
              '（正文里写 <code>[图片0001]</code>，对应 <code>_图片\\0001.jpg</code>）。',
        'ai': 'AI 语料格式：每个会话文件夹里有 <b>对话.jsonl</b>（一行一条消息，带发言人/时间/类型/图片路径，适合入库或 RAG）和 <b>对话.txt</b>（纯文本，适合直接阅读或整篇喂模型）。',
        'html': 'HTML 格式：点击会话名打开聊天页面，图片在同目录 图片/ 下。',
        'pdf': 'PDF 格式：点击会话名打开 PDF（图片已内嵌）。',
        'excel': 'Excel 格式：点击会话名打开 xlsx。',
        'csv': 'CSV 格式：点击会话名打开 csv（UTF-8 BOM，Excel 可直接打开）。',
        'txt': 'TXT 格式：点击会话名打开纯文本。',
        'json': 'JSON 格式：点击会话名打开 json（结构化数据）。',
    }.get(fmt, '')

    warn = ''
    if cancelled:
        warn = '<div class="warn">本次导出被中途取消，下面是已完成的会话。</div>'

    prompt_html = ''
    if prompt and prompt.strip():
        body = ''.join(f'<div>{h.escape(l) or "&nbsp;"}</div>'
                       for l in prompt.strip().split('\n'))
        prompt_html = (
            '<details class="prompt" open>'
            '<summary>已写入每份导出的「给 AI 的指令」'
            '（文本会随每个 .md 一起交给 AI；同名文件也放在了本目录下）</summary>'
            f'<div class="prompt-body">{body}</div></details>')

    doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>导出清单</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<h1>导出清单</h1>
<div class="sub">导出时间 {now} · 共 {len(items)} 个会话 · {total_msgs} 条消息 · {total_imgs} 张图片</div>
{f'<div class="sub">{fmt_note}</div>' if fmt_note else ''}
{warn}
{prompt_html}
<div class="card">
<table>
<thead><tr><th>会话</th><th>消息数</th><th>图片</th><th>文件夹</th></tr></thead>
<tbody>
{''.join(rows_html) if rows_html else '<tr><td colspan="4" class="path">没有成功导出的会话</td></tr>'}
</tbody>
</table>
</div>
</div>
</body>
</html>'''

    path = os.path.join(root, '导出清单.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path
