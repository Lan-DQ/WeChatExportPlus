# -*- coding: utf-8 -*-
"""PDF 导出器 —— 气泡式排版，支持内嵌图片。

[WeChatExportPlus 改动] 内容来源改为 message_content 的统一解析结果：
  - 不再用 labels[lt] 那种 '[类型49]' 映射，语音/视频/链接/文件/系统消息都有可读文本；
  - 图片仍是内嵌（PDF 单文件自包含），但只在真正拿到 image_data 时才画；
  - 正文统一走 m['text']，长文本自动退回通栏排版，避免气泡被撑破。

对外接口保持不变：export(msgs, path, my_name, title)。
"""
import base64
from io import BytesIO

import message_content

FONT_REG = 'C:/Windows/Fonts/msyh.ttc'
FONT_BOLD = 'C:/Windows/Fonts/msyhbd.ttc'


def export(msgs, path, my_name='我', title='聊天记录', prompt=None):
    from fpdf import FPDF

    rows = msgs if (msgs and isinstance(msgs[0], dict) and 'kind' in msgs[0]) \
        else message_content.prepare(msgs, my_name=my_name)

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font('YH', '', FONT_REG)
    pdf.add_font('YHB', '', FONT_BOLD)
    pw = pdf.w - 2 * pdf.l_margin
    sb, rb = (106, 181, 255), (228, 228, 232)

    def rc(x, y, w, h, r, f):
        """圆角矩形。"""
        r = min(r, h / 2, w / 2)
        pdf.set_fill_color(*f)
        pdf.set_draw_color(*f)
        pdf.rect(x, y + r, w, h - 2 * r, 'F')
        pdf.rect(x + r, y, w - 2 * r, h, 'F')
        d = 2 * r
        for cx, cy in [(x, y), (x + w - d, y), (x, y + h - d), (x + w - d, y + h - d)]:
            pdf.ellipse(cx, cy, d, d, 'F')

    rendered = 0
    pdf.set_font('YHB', '', 12)
    pdf.set_text_color(31, 42, 55)
    pdf.cell(pw, 8, f'{title}  ·  {len(rows)} msgs', align='C')
    pdf.ln(8)
    pdf.set_draw_color(200, 205, 212)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + pw, pdf.get_y())
    pdf.ln(4)

    # 给 AI 的指令：单独一页放在正文之前，避免和聊天内容混在一起
    if prompt and prompt.strip():
        pdf.add_page()
        pdf.set_font('YHB', '', 12)
        pdf.set_text_color(146, 64, 14)
        pdf.multi_cell(pw, 7, '给 AI 的指令（以下不是聊天记录）')
        pdf.ln(2)
        pdf.set_font('YH', '', 9)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(pw, 5, prompt.strip())
        pdf.add_page()

    last_day = None
    for m in rows:
        day = m.get('time_str', '')[:10]
        clock = m.get('time_str', '')[11:19]
        if not day:
            continue
        is_mine = m.get('is_mine', 0)
        who = m.get('sender_display', '')

        # 日期分隔条
        if day != last_day:
            y = pdf.get_y()
            pdf.set_font('YH', '', 6)
            dw = pdf.get_string_width(day) + 4
            rc((pdf.w - dw) / 2, y, dw, 5, 2.5, (220, 224, 230))
            pdf.set_text_color(120, 125, 135)
            pdf.set_xy((pdf.w - dw) / 2, y)
            pdf.cell(dw, 5, day, align='C')
            pdf.ln(4)
            last_day = day

        # 图片消息
        if m.get('kind') == 'image' and m.get('image_data'):
            try:
                pdf.set_font('YH', '', 7)
                pdf.set_text_color(120, 125, 135)
                if is_mine:
                    nw = pdf.get_string_width(who + '  ' + clock)
                    pdf.set_x(pdf.l_margin + pw - nw - 4)
                    pdf.cell(nw + 8, 4, who + '  ' + clock, align='R')
                else:
                    pdf.set_x(pdf.l_margin + 4)
                    pdf.cell(pw, 4, who + '  ' + clock)
                pdf.ln(5)
                img_bytes = base64.b64decode(m['image_data'])
                img_w = min(140, pw * 0.5)
                img_io = BytesIO(img_bytes)
                img_x = pw - pdf.l_margin - img_w if is_mine else pdf.l_margin
                if pdf.get_y() + img_w + 10 > pdf.h - 25:
                    pdf.add_page()
                pdf.image(img_io, x=img_x, w=img_w)
                pdf.ln(4)
                rendered += 1
            except Exception:
                pass
            continue

        # 系统消息：居中灰条
        if m.get('kind') == 'system':
            text = (m.get('text') or '').strip()
            if not text:
                continue
            if pdf.get_y() + 10 > pdf.h - 25:
                pdf.add_page()
            pdf.set_font('YH', '', 8)
            pdf.set_text_color(120, 125, 135)
            pdf.multi_cell(pw, 5, text, align='C')
            pdf.ln(2)
            rendered += 1
            continue

        text = (m.get('text') or '').strip()
        if not text:
            continue
        lines = [l for l in (l.strip() for l in text.split('\n')) if l]
        if not lines:
            continue

        pdf.set_font('YH', '', 11)
        px = 3
        mx = pw * 0.6
        max_line_w = max(pdf.get_string_width(l) for l in lines)
        bw = min(mx, max_line_w + px * 2 + 4)

        text_for_render = '\n'.join(lines)
        wrapped = pdf.multi_cell(bw - px * 2, 6, text_for_render, dry_run=True, output='LINES')
        th = len(wrapped) * 6 + 2

        lines_per_page = int((pdf.h - 25 - pdf.get_y()) / (pdf.font_size * 1.2))
        long_text = len(wrapped) > lines_per_page

        # 发送者 + 时间
        pdf.set_font('YH', '', 7)
        pdf.set_text_color(120, 125, 135)
        if is_mine:
            nw = pdf.get_string_width(who + '  ' + clock)
            pdf.set_x(pdf.l_margin + pw - nw - 4)
            pdf.cell(nw + 8, 4, who + '  ' + clock, align='R')
        else:
            pdf.set_x(pdf.l_margin + 4)
            pdf.cell(pw, 4, who + '  ' + clock)
        pdf.ln(6)

        if long_text:
            pdf.set_font('YH', '', 11)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(pw - 8, 6, text_for_render)
            pdf.ln(2)
        else:
            if pdf.get_y() + th + 8 > pdf.h - 25:
                pdf.add_page()
            y0 = pdf.get_y()
            x0 = pdf.l_margin if not is_mine else pdf.l_margin + pw - bw
            fc = sb if is_mine else rb
            tc = (255, 255, 255) if is_mine else (0, 0, 0)
            rc(x0, y0, bw, th, 4, fc)
            pdf.set_text_color(*tc)
            pdf.set_xy(x0 + px, y0 + 1)
            pdf.multi_cell(bw - px * 2, 6, text_for_render)
            pdf.set_y(y0 + th + 4)
        rendered += 1

    pdf.output(path)
    return path
