# -*- coding: utf-8 -*-
"""HTML 导出器 —— Telegram 风格气泡，支持图片文件化导出。

[WeChatExportPlus 改动]
  - 内容来源改为 message_content.prepare()/extract() 的统一结果，
    图片、语音、视频、链接、文件、引用、聊天记录、系统消息都会渲染出来；
  - 引用消息用「引用块 + 正文」两段式渲染；
  - 合并转发的聊天记录用可折叠 <details> 渲染，避免长文本糊满页面；
  - 链接消息渲染成可点击的 <a>，图片走 <img>；
  - 文本一律经过 html.escape()，不会因为消息里带 < > & 而破坏页面。

对外接口保持不变：export(msgs, path, my_name, title, img_dir=None)。
`msgs` 可以是原始消息（内部会自己 prepare），也可以是已 prepare 过的行。
"""
import base64
import html as h
import os

import message_content

CSS = '''
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;background:#e8ecef;color:#1f2a37}
.page{max-width:860px;margin:0 auto;padding:12px 16px;min-height:100vh;display:flex;flex-direction:column}
.header{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:14px 20px;flex-shrink:0;margin-bottom:12px;position:sticky;top:12px;z-index:10}
.title{font-size:16px;font-weight:600;display:inline}
.meta{color:#6b7280;font-size:13px;display:inline;margin-left:12px}
.controls{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.controls input{border-radius:8px;border:1px solid #e5e7eb;padding:6px 10px;font-size:13px;font-family:inherit}
.controls input[type="search"]{width:220px}
.date-sep{text-align:center;margin:16px 0 12px}
.date-sep span{display:inline-block;background:rgba(0,0,0,.06);color:#6b7280;font-size:12px;padding:4px 14px;border-radius:12px}
.msg{display:flex;flex-direction:column;margin-bottom:10px;max-width:80%}
.msg.left{align-items:flex-start}
.msg.right{align-items:flex-end;margin-left:auto}
.msg .sender{font-size:12px;color:#6b7280;margin-bottom:3px;padding:0 4px}
.msg .sender .time{font-size:11px;opacity:.7}
.msg .bubble{padding:10px 14px;font-size:14px;line-height:1.6;word-break:break-word;box-shadow:0 1px 3px rgba(0,0,0,.08);white-space:pre-wrap}
.msg.left .bubble{background:#fff;color:#000;border-radius:12px;border-bottom-left-radius:4px}
.msg.right .bubble{background:#6ab5ff;color:#fff;border-radius:12px;border-bottom-right-radius:4px}
.msg.right .sender{text-align:right}
.msg .bubble img{max-width:300px;max-height:300px;border-radius:4px;display:block}
.msg .bubble a{color:#2563eb;text-decoration:underline;word-break:break-all}
.msg.right .bubble a{color:#e8f2ff}
.msg-list{flex:1;padding:4px 0}
.msg.sys{max-width:100%;align-items:center;margin:8px auto}
.msg.sys .bubble{background:rgba(0,0,0,.06);color:#6b7280;font-size:12px;text-align:center;border-radius:10px;box-shadow:none;padding:5px 12px}
.quote{border-left:3px solid rgba(0,0,0,.18);padding:2px 0 2px 8px;margin-bottom:6px;font-size:13px;opacity:.85;white-space:pre-wrap}
.msg.right .quote{border-left-color:rgba(255,255,255,.5)}
.rec{font-size:13px}
.rec summary{cursor:pointer;font-weight:600;outline:none}
.rec .item{padding:3px 0 3px 10px;border-left:2px solid rgba(0,0,0,.12);margin-top:4px;white-space:pre-wrap}
.highlight{background:#fef08a;border-radius:2px;padding:0 1px}
.ai-prompt{background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:12px 16px;margin-bottom:12px;flex-shrink:0}
.ai-prompt summary{cursor:pointer;font-weight:600;font-size:13px;color:#92400e;outline:none}
.ai-prompt-body{margin-top:8px;font-size:12px;line-height:1.7;color:#78350f;white-space:pre-wrap;max-height:260px;overflow:auto}
.msg.hidden{display:none}
@media(max-width:600px){.page{padding:8px}.msg{max-width:90%}.controls input[type="search"]{width:160px}}
'''

JS = '''
function filterMessages(){
  var kw=document.getElementById('searchInput').value.trim().toLowerCase();
  var msgs=document.querySelectorAll('.msg');
  for(var i=0;i<msgs.length;i++){
    var t=msgs[i].textContent.toLowerCase();
    if(!kw||t.indexOf(kw)>-1){msgs[i].classList.remove('hidden')}else{msgs[i].classList.add('hidden')}
  }
  var dates=document.querySelectorAll('.date-sep');
  for(var i=0;i<dates.length;i++){
    var n=dates[i].nextElementSibling, hv=false;
    while(n&&!n.classList.contains('date-sep')){
      if(n.classList.contains('msg')&&!n.classList.contains('hidden')){hv=true;break}
      n=n.nextElementSibling
    }
    dates[i].style.display=hv?'':'none'
  }
}
'''


def _esc(text):
    return h.escape(str(text or ''))


def _render_body(m, img_dir, img_state):
    """渲染一条消息的气泡内部 HTML。"""
    kind = m.get('kind', 'unknown')
    text = m.get('text') or ''
    extra = m.get('extra') or {}

    # 图片：batch_export 已把图片落到 <会话目录>/图片/<name>，
    # 这里只负责引用；若拿不到文件名则退回 base64 内嵌。
    if kind == 'image':
        name = m.get('image_file')
        if name:
            return f'<img src="图片/{_esc(name)}" alt="图片" loading="lazy">'
        if m.get('image_data'):
            ext = m.get('image_ext') or 'png'
            return (f'<img src="data:image/{ext};base64,{m["image_data"]}" '
                    f'alt="图片" loading="lazy">')
        return _esc(text or '[图片]')

    # 合并转发的聊天记录：折叠展示
    if kind == 'chatrecord':
        lines = text.split('\n')
        head = _esc(lines[0])
        items = ''.join(f'<div class="item">{_esc(l.strip())}</div>'
                        for l in lines[1:] if l.strip())
        return f'<details class="rec"><summary>{head}</summary>{items}</details>'

    # 引用消息：引用块 + 正文
    if kind == 'quote':
        lines = text.split('\n')
        quoted = lines[0] if lines and lines[0].startswith('↩') else ''
        body = '\n'.join(lines[1:]) if quoted else text
        parts = []
        if quoted:
            parts.append(f'<div class="quote">{_esc(quoted)}</div>')
        if body.strip():
            parts.append(_esc(body))
        return ''.join(parts) or _esc(text)

    # 链接：把 URL 变成可点击链接
    if kind == 'link':
        url = extra.get('url') or ''
        lines = [l for l in text.split('\n') if l.strip()]
        parts = []
        for line in lines:
            if url and line.strip() == url:
                parts.append(f'<a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(url)}</a>')
            else:
                parts.append(_esc(line))
        return '<br>'.join(parts)

    return _esc(text)


def export(msgs, path, my_name='我', title='聊天记录', img_dir=None, prompt=None):
    """导出 HTML。img_dir 指定后，图片保存到该目录并用相对路径引用。

    prompt 非空时，页面顶部插一段「给 AI 的指令」（可折叠），
    这样把 HTML 交给 AI 时要求不会丢。
    """
    rows = msgs if (msgs and isinstance(msgs[0], dict) and 'kind' in msgs[0]) \
        else message_content.prepare(msgs, my_name=my_name)

    msg_html = []
    last_date = None
    img_state = {'n': 0}
    rendered = 0

    for m in rows:
        body = _render_body(m, img_dir, img_state)
        if not body:
            continue
        ts = str(m.get('create_time') or '')
        day = m.get('time_str', '')[:10]
        clock = m.get('time_str', '')[11:19]
        if not day:
            continue

        if day != last_date:
            msg_html.append(f'      <div class="date-sep"><span>{_esc(day)}</span></div>\n')
            last_date = day

        who = _esc(m.get('sender_display', ''))
        if m.get('kind') == 'system':
            msg_html.append(
                f'      <div class="msg sys"><div class="bubble">{body}</div></div>\n')
        else:
            side = 'right' if m.get('is_mine') else 'left'
            msg_html.append(
                f'      <div class="msg {side}">\n'
                f'        <div class="sender">{who}  <span class="time">{_esc(clock)}</span></div>\n'
                f'        <div class="bubble">{body}</div>\n'
                f'      </div>\n')
        rendered += 1

    prompt_html = ''
    if prompt and prompt.strip():
        import html as _h
        body = ''.join(f'<div>{_h.escape(l) or "&nbsp;"}</div>'
                       for l in prompt.strip().split('\n'))
        prompt_html = (
            '  <details class="ai-prompt" open>\n'
            '    <summary>给 AI 的指令（本页内容为微信聊天记录导出，请遵守以下要求）</summary>\n'
            f'    <div class="ai-prompt-body">{body}</div>\n'
            '  </details>\n')

    doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
<div class="header">
<div><h1 class="title">{_esc(title)}</h1><span class="meta">共 {rendered} 条消息</span></div>
<div class="controls">
<input id="searchInput" type="search" placeholder="搜索消息..." oninput="filterMessages()" />
</div>
</div>
{prompt_html}<div class="msg-list" id="msgList">
{''.join(msg_html)}  </div>
</div>
<script>{JS}</script>
</body>
</html>'''

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path
