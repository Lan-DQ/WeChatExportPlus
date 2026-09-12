# -*- coding: utf-8 -*-
"""自绘控件库：圆角按钮、玻璃卡片、勾选列表、输入框、下拉框、进度条。

全部基于 Canvas 绘制以获得 ttk 给不了的外观（圆角/悬停/渐变/玻璃感）。
需要真正文本编辑的地方（输入框）用 tk.Entry 叠在画好的底上 ——
tkinter 没法自绘一个能用的文本光标编辑器，叠放是唯一稳妥做法。
"""
import tkinter as tk

from PIL import Image, ImageTk

import ui_theme as T


class Surface:
    """整个窗口的绘制表面：负责背景图、面板合成缓存、尺寸与重绘。

    所有自绘控件都画在这一个 Canvas 上（tkinter 里多 Canvas 叠层会互相遮挡，
    单 Canvas + 图元分层是最可靠的做法）。
    """

    def __init__(self, root, theme, bg_image=''):
        self.root = root
        self.theme = theme
        self.bg_image = bg_image
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0,
                                bg=T.rgb2hex(theme['bg_top']))
        self.canvas.pack(fill='both', expand=True)
        self._photo = None
        self._panel_cache = {}
        self._extra_photos = []
        self.size = (0, 0)
        self._pil_bg = None
        self._anim = None
        self.redraw_bg()

    # ── 动画 ──

    @property
    def anim(self):
        if self._anim is None:
            self._anim = T.Animator(self.canvas)
        return self._anim

    # ── 背景 ──

    def redraw_bg(self):
        w = max(2, self.canvas.winfo_width())
        h = max(2, self.canvas.winfo_height())
        if (w, h) == self.size and self._pil_bg is not None:
            return
        self.size = (w, h)
        self._pil_bg = T.make_background((w, h), self.theme, self.bg_image)
        self._photo = ImageTk.PhotoImage(self._pil_bg)
        self._panel_cache.clear()

    def set_theme(self, theme):
        self.theme = theme
        self.size = (0, 0)
        self.canvas.configure(bg=T.rgb2hex(theme['bg_top']))
        self.redraw_bg()
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=self._photo, anchor='nw', tags='__bg')

    def set_bg_image(self, path):
        self.bg_image = path
        self.size = (0, 0)
        self.redraw_bg()

    # ── 面板 ──

    def panel_photo(self, box, radius=14, alpha=0.80, fill=None, tag=''):
        """合成并缓存一块玻璃面板图。"""
        key = (tuple(int(v) for v in box), radius, round(alpha, 3),
               fill or '', tag)
        if key not in self._panel_cache:
            img = T.glass_panel(self._pil_bg, box, self.theme, radius, alpha, fill)
            self._panel_cache[key] = ImageTk.PhotoImage(img)
            if len(self._panel_cache) > 60:
                self._panel_cache.pop(next(iter(self._panel_cache)))
        return self._panel_cache[key]

    def draw_panel(self, x1, y1, x2, y2, radius=14, alpha=0.80, fill=None,
                   border=True, tags='panel'):
        """画一块玻璃面板（含 1px 边框）。"""
        ph = self.panel_photo((x1, y1, x2, y2), radius, alpha, fill, tags)
        self.canvas.create_image(x1, y1, image=ph, anchor='nw', tags=tags)
        if border:
            T.round_rect_items(self.canvas, x1 + 0.5, y1 + 0.5, x2 - 0.5, y2 - 0.5,
                               radius, '', self.theme['border'], 1, tags=tags)
        return ph

    # ── 基础图元 ──

    def rect(self, x1, y1, x2, y2, r=0, fill='', outline='', width=1, tags=''):
        return T.round_rect_items(self.canvas, x1, y1, x2, y2, r, fill,
                                  outline, width, tags=(tags,) if tags else ())

    def text(self, x, y, s, size=10, color=None, bold=False, anchor='w',
             tags='', font_family=None):
        f = (font_family or self.font, size, 'bold' if bold else 'normal')
        return self.canvas.create_text(x, y, text=s, anchor=anchor,
                                       fill=color or self.theme['text'],
                                       font=f, tags=tags)

    def image(self, x, y, pil_img, anchor='nw', tags=''):
        ph = ImageTk.PhotoImage(pil_img)
        self._extra_photos.append(ph)
        if len(self._extra_photos) > 300:
            self._extra_photos.pop(0)
        return self.canvas.create_image(x, y, image=ph, anchor=anchor, tags=tags)

    font = 'Microsoft YaHei UI'

    def keep(self, item):
        return item


class Button:
    """圆角按钮：悬停/按下用颜色缓动过渡，而不是瞬间跳色。

    丝滑的两个关键：
      1. 颜色在「静息色 ↔ 悬停色」之间按缓动曲线插值（ease_out_cubic，起快收慢）；
      2. 同一按钮同一时刻只跑一条动画，鼠标快速划入划出不会叠加。
    """

    _seq = 0

    def __init__(self, sf, x, y, w, h, text, command=None, kind='primary',
                 icon='', radius=11, font_size=11, tags=None, hover_dur=0.15):
        self.sf, self.x, self.y, self.w, self.h = sf, x, y, w, h
        self.text, self.command, self.kind, self.icon = text, command, kind, icon
        self.radius, self.font_size = radius, font_size
        Button._seq += 1
        self.tags = tags or f'btn{Button._seq}'
        self.state = 'normal'
        self.hover_dur = hover_dur
        # 动画进度：0=静息 1=悬停；按下时另外叠一层暗化
        self._hover_t = 0.0
        self._press_t = 0.0
        self._anim_key = f'anim_{self.tags}'
        self.draw()

    # ── 状态色 ──

    def _palette(self):
        """返回 (静息底色, 悬停底色, 边框色, 文字色)。

        ghost 按钮的悬停色实测只有 13 的 RGB 距离，肉眼看不出变化，
        所以这里把悬停色调得更深一点（而不是直接用 surface3）。
        """
        th = self.sf.theme
        if self.kind == 'primary':
            return th['accent'], T.lighten(th['accent'], 0.14), '', th['accent_text']
        if self.kind == 'danger':
            return th['err'], T.lighten(th['err'], 0.12), '', '#ffffff'
        rest = th['surface2']
        hot = th['surface3'] if th['name'] == 'dark' else T.darken(rest, 0.055)
        return rest, hot, th['border'], th['text']

    def _current_fill(self):
        base, hot, border, fg = self._palette()
        fill = T.lerp_color(base, hot, self._hover_t)
        if self._press_t > 0:
            fill = T.lerp_color(fill, T.darken(fill, 0.14), self._press_t)
        if self.state == 'disabled':
            fill = T.mix(fill, self.sf.theme['surface'], 0.55)
        return fill, border, fg

    def draw(self):
        cv = self.sf.canvas
        th = self.sf.theme
        cv.delete(self.tags)
        fill, border, fg = self._current_fill()
        if self.state == 'disabled':
            fg = th['text_faint']
            border = th['border']
        # 悬停时外发光：在按钮下方垫一层比自身略大的半透明圆角
        if self._hover_t > 0.02 and self.state != 'disabled':
            glow = T.lerp_color(fill, th['surface'], 0.62)
            T.round_rect_items(cv, self.x - 3, self.y - 1, self.x + self.w + 3,
                               self.y + self.h + 4, self.radius + 3, glow, '', 0,
                               tags=(self.tags,))
        T.round_rect_items(cv, self.x, self.y, self.x + self.w, self.y + self.h,
                           self.radius, fill, border if self.kind != 'primary' else '',
                           1, tags=(self.tags,))
        if self.kind == 'primary':
            # 顶部高光：随悬停略微增强，做出"被照亮"的感觉
            hi = T.lighten(fill, 0.16 + 0.10 * self._hover_t)
            T.round_rect_items(cv, self.x + 1, self.y + 1, self.x + self.w - 1,
                               self.y + self.h * 0.55, self.radius, hi, '', 0,
                               tags=(self.tags,))
        label = f'{self.icon} {self.text}'.strip() if self.icon else self.text
        cv.create_text(self.x + self.w / 2, self.y + self.h / 2 + self._press_t * 1.2,
                       text=label, fill=fg,
                       font=(self.sf.font, self.font_size, 'bold'), tags=self.tags)
        cv.tag_bind(self.tags, '<Enter>', self._on_enter)
        cv.tag_bind(self.tags, '<Leave>', self._on_leave)
        cv.tag_bind(self.tags, '<ButtonPress-1>', self._on_press)
        cv.tag_bind(self.tags, '<ButtonRelease-1>', self._on_release)

    # ── 动画 ──

    def _animate(self, attr, target, dur=None, key_suffix=''):
        cur = getattr(self, attr)
        if abs(cur - target) < 0.001:
            return
        start = cur
        key = f'{self._anim_key}{key_suffix}'

        def frame(v):
            setattr(self, attr, start + (target - start) * v)
            self.draw()

        self.sf.anim.animate(key, dur or self.hover_dur, 'out_cubic', frame)

    # ── 事件 ──

    def _on_enter(self, _e):
        if self.state == 'disabled':
            return
        self.sf.canvas.configure(cursor='hand2')
        self._animate('_hover_t', 1.0)
        self._raise()

    def _on_leave(self, _e):
        self.sf.canvas.configure(cursor='')
        self._animate('_hover_t', 0.0, 0.2)
        self._animate('_press_t', 0.0, 0.1, '_p')
        self._raise()

    def _on_press(self, _e):
        if self.state == 'disabled':
            return
        self._animate('_press_t', 1.0, 0.06, '_p')
        self._raise()

    def _on_release(self, _e):
        if self.state == 'disabled':
            return
        was = self._press_t > 0.15
        self._animate('_press_t', 0.0, 0.12, '_p')
        self._raise()
        if was and self.command:
            self.command()

    def _raise(self):
        self.sf.canvas.tag_raise(self.tags)

    def set_state(self, state):
        self.state = state
        if state == 'disabled':
            self._hover_t = self._press_t = 0.0
        self.draw()

    def set_text(self, text):
        self.text = text
        self.draw()


class Entry:
    """在自绘底上叠一个 tk.Entry（tkinter 无法自绘可编辑文本框）。"""

    def __init__(self, sf, x, y, w, h, textvariable=None, show=None,
                 radius=10, font_size=10, placeholder=''):
        self.sf, self.x, self.y, self.w, self.h = sf, x, y, w, h
        self.radius, self.placeholder = radius, placeholder
        self.bg_tag = f'entbg{id(self)}'
        self.var = textvariable or tk.StringVar()
        self.entry = tk.Entry(sf.canvas, textvariable=self.var, bd=0,
                              relief='flat', highlightthickness=0,
                              font=(sf.font, font_size), show=show or '')
        self.entry.configure(bg=sf.theme['surface2'], fg=sf.theme['text'],
                             insertbackground=sf.theme['accent'],
                             selectbackground=sf.theme['accent'],
                             selectforeground='#ffffff')
        self._place()

    def _place(self):
        sf = self.sf
        sf.canvas.delete(self.bg_tag)
        # 边框比面板 border 略深：浅色主题下 1px 会被抗锯齿冲淡，太浅就看不出这是输入框
        bd = T.darken(sf.theme['border'], 0.10)
        T.round_rect_items(sf.canvas, self.x, self.y, self.x + self.w,
                           self.y + self.h, self.radius, sf.theme['surface2'],
                           bd, 1, tags=(self.bg_tag,))
        sf.canvas.tag_lower(self.bg_tag)
        self.entry.place(x=self.x + 12, y=self.y + (self.h - 20) // 2,
                         width=max(10, self.w - 24), height=20)

    def restyle(self):
        th = self.sf.theme
        self.entry.configure(bg=th['surface2'], fg=th['text'],
                             insertbackground=th['accent'],
                             selectbackground=th['accent'])
        self._place()

    def get(self):
        return self.var.get()

    def set(self, v):
        self.var.set(v)

    def set_show(self, ch):
        self.entry.configure(show=ch)

    def destroy(self):
        try:
            self.entry.destroy()
        except Exception:
            pass


class CheckList:
    """自绘会话列表：真圆角复选框、悬停高亮、选中底色、头像色块、滚动。

    取代 ttk.Treeview —— Treeview 的行高/表头/勾选框样式都改不了。
    """

    HEADER_H = 30
    ROW_H = 44

    def __init__(self, sf, x, y, w, h, on_toggle=None, on_open=None, radius=14):
        self.sf, self.x, self.y, self.w, self.h = sf, x, y, w, h
        self.radius = radius
        self.on_toggle, self.on_open = on_toggle, on_open
        self.items = []            # [{'wxid','title','preview','time','group','sel'}]
        self.view = []             # 当前可见（过滤后）下标
        self.scroll = 0
        self.hover_row = -1
        self._photos = []
        self._bind()

    # ── 数据 ──

    def set_items(self, items):
        self.items = items
        self.scroll = 0
        self.refresh_view()

    def refresh_view(self, keyword=''):
        kw = (keyword or '').strip().lower()
        if kw:
            self.view = [i for i, it in enumerate(self.items)
                         if kw in it['title'].lower() or kw in it['wxid'].lower()
                         or kw in (it.get('preview') or '').lower()]
        else:
            self.view = list(range(len(self.items)))
        self.scroll = min(self.scroll, max(0, len(self.view) - 1))
        self.draw()

    @property
    def visible_rows(self):
        return max(1, (self.h - self.HEADER_H) // self.ROW_H)

    def selected_wxids(self):
        return [it['wxid'] for it in self.items if it.get('sel')]

    def set_all(self, value, only_visible=False):
        targets = ([self.items[i] for i in self.view] if only_visible else self.items)
        for it in targets:
            it['sel'] = value
        self.draw()

    def invert(self):
        for it in self.items:
            it['sel'] = not it.get('sel')
        self.draw()

    def toggle_index(self, i):
        if 0 <= i < len(self.items):
            self.items[i]['sel'] = not self.items[i].get('sel')
            if self.on_toggle:
                self.on_toggle(self.items[i])
            self.draw()

    # ── 交互 ──

    def _bind(self):
        cv = self.sf.canvas
        cv.tag_bind('listbody', '<Motion>', self._on_motion)
        cv.tag_bind('listbody', '<Leave>', self._on_leave)
        cv.tag_bind('listbody', '<Button-1>', self._on_click)
        cv.tag_bind('listbody', '<Double-Button-1>', self._on_double)
        # 注意：滚轮不能挂在 tag 上（Tk 只允许 tag_bind 用 key/button/motion/enter/leave），
        # 只能绑在 widget 上，然后在回调里判断指针是否落在列表区域内。
        cv.bind('<MouseWheel>', self._on_wheel, add='+')
        cv.bind('<Button-4>', lambda e: self._wheel_at(e, -1), add='+')
        cv.bind('<Button-5>', lambda e: self._wheel_at(e, 1), add='+')

    def _inside(self, x, y):
        return (self.x <= x <= self.x + self.w
                and self.y + self.HEADER_H <= y <= self.y + self.h)

    def _row_at(self, y):
        if y < self.y + self.HEADER_H or y > self.y + self.h:
            return -1
        idx = int((y - self.y - self.HEADER_H) // self.ROW_H) + self.scroll
        return idx if 0 <= idx < len(self.view) else -1

    def _on_motion(self, e):
        i = self._row_at(e.y)
        if i != self.hover_row:
            old = self.hover_row
            self.hover_row = i
            # 有旧行时要让旧行淡出，所以整体重绘一次即可（行数少，代价可接受）
            self.draw()
            if i >= 0:
                self.sf.anim.stop_prefix('rowhover')
                self._hover_fade = 0.0

                def frame(v):
                    self._hover_fade = v
                    self.draw()

                self.sf.anim.animate('rowhover', 0.13, 'out_cubic', frame)

    def _on_leave(self, _e):
        if self.hover_row != -1:
            self.hover_row = -1
            self.sf.anim.stop_prefix('rowhover')
            self.draw()

    def _on_click(self, e):
        i = self._row_at(e.y)
        if i < 0:
            return
        self.toggle_index(self.view[i])

    def _on_double(self, e):
        i = self._row_at(e.y)
        if i >= 0 and self.on_open:
            self.on_open(self.items[self.view[i]])

    def _wheel_at(self, e, d):
        if self._inside(e.x, e.y):
            self._wheel(d)

    def _on_wheel(self, e):
        if self._inside(e.x, e.y):
            self._wheel(-1 if e.delta > 0 else 1)

    def _wheel(self, d):
        maxs = max(0, len(self.view) - self.visible_rows)
        ns = min(maxs, max(0, self.scroll + d))
        if ns != self.scroll:
            self.scroll = ns
            self.draw()

    # ── 绘制 ──

    def draw(self):
        sf, cv, th = self.sf, self.sf.canvas, self.sf.theme
        cv.delete('listbody')
        sf.draw_panel(self.x, self.y, self.x + self.w, self.y + self.h,
                      self.radius, 0.84, tags='listbody')

        # 表头
        cv.create_text(self.x + 18, self.y + 17, text='会话列表', anchor='w',
                       fill=th['text'], font=(sf.font, 12, 'bold'), tags='listbody')
        cv.create_text(self.x + 148, self.y + 18, anchor='w',
                       text=f"共 {len(self.items)} 个 · 已选 {len(self.selected_wxids())} 个",
                       fill=th['text_dim'], font=(sf.font, 9), tags='listbody')
        cv.create_line(self.x + 12, self.y + self.HEADER_H - 1,
                       self.x + self.w - 12, self.y + self.HEADER_H - 1,
                       fill=th['border'], tags='listbody')

        y0 = self.y + self.HEADER_H
        rows = self.view[self.scroll:self.scroll + self.visible_rows]
        n = 0
        for n, idx in enumerate(rows):
            it = self.items[idx]
            ry = y0 + n * self.ROW_H
            selected = bool(it.get('sel'))
            hovered = (idx == self.hover_row)
            if selected:
                T.round_rect_items(cv, self.x + 10, ry + 3, self.x + self.w - 10,
                                   ry + self.ROW_H - 3, 10, th['row_sel'], '',
                                   0, tags=('listbody',))
            elif hovered:
                T.round_rect_items(cv, self.x + 10, ry + 3, self.x + self.w - 10,
                                   ry + self.ROW_H - 3, 10, th['row_hover'], '',
                                   0, tags=('listbody',))

            # 真·圆角复选框
            bx, by, bs = self.x + 18, ry + self.ROW_H / 2 - 9, 18
            box_fill = th['check'] if selected else th['check_bg']
            T.round_rect_items(cv, bx, by, bx + bs, by + bs, 5, box_fill,
                               '' if selected else th['border'], 1,
                               tags=('listbody',))
            if selected:
                cv.create_line(bx + 4.5, by + 9.5, bx + 7.5, by + 12.8,
                               fill='#ffffff', width=2.2, capstyle='round',
                               tags=('listbody',))
                cv.create_line(bx + 7.5, by + 12.8, bx + 13.5, by + 5.6,
                               fill='#ffffff', width=2.2, capstyle='round',
                               tags=('listbody',))

            # 头像色块
            ax = bx + bs + 12
            av = T.rounded_avatar(it['title'], 30, T.avatar_color_for(it['wxid']))
            sf.image(ax, ry + self.ROW_H / 2 - 15, av, tags='listbody')

            tx = ax + 40
            marker = '👥 ' if it.get('group') else ''
            cv.create_text(tx, ry + 14, text=marker + it['title'], anchor='w',
                           fill=th['text'], font=(sf.font, 10, 'bold'),
                           tags='listbody')
            cv.create_text(tx, ry + 30, text=(it.get('preview') or '')[:46],
                           anchor='w', fill=th['text_dim'],
                           font=(sf.font, 9), tags='listbody')
            if it.get('time'):
                cv.create_text(self.x + self.w - 20, ry + 22, text=it['time'],
                               anchor='e', fill=th['text_faint'],
                               font=(sf.font, 9), tags='listbody')

        if not rows:
            cv.create_text(self.x + self.w / 2, y0 + 60,
                           text='没有匹配的会话', fill=th['text_faint'],
                           font=(sf.font, 11), tags='listbody')
            return

        # 滚动条
        total = len(self.view)
        if total > self.visible_rows:
            track_y1, track_y2 = y0 + 4, self.y + self.h - 8
            track_h = track_y2 - track_y1
            thumb_h = max(28, track_h * self.visible_rows / total)
            maxs = total - self.visible_rows
            ty = track_y1 + (track_h - thumb_h) * (self.scroll / maxs if maxs else 0)
            T.round_rect_items(cv, self.x + self.w - 8, ty, self.x + self.w - 3,
                               ty + thumb_h, 2.5, th['border'], '', 0,
                               tags=('listbody',))


class Checkbox:
    """带文字的自绘勾选框（用于「导出并清除之前的导出内容」这类开关）。"""

    def __init__(self, sf, x, y, w, text, value=False, on_change=None,
                 font_size=10, box=18):
        self.sf, self.x, self.y, self.w = sf, x, y, w
        self.text, self.value = text, value
        self.on_change, self.font_size, self.box = on_change, font_size, box
        self.tag = f'cb{id(self)}'
        self._t = 1.0 if value else 0.0
        self._hover = False
        self.bind()
        self.draw()

    def bind(self):
        cv = self.sf.canvas
        cv.tag_bind(self.tag, '<Enter>', self._enter)
        cv.tag_bind(self.tag, '<Leave>', self._leave)
        cv.tag_bind(self.tag, '<Button-1>', self._click)

    def _enter(self, _e):
        self._hover = True
        self.sf.canvas.configure(cursor='hand2')
        self.draw()

    def _leave(self, _e):
        self._hover = False
        self.sf.canvas.configure(cursor='')
        self.draw()

    def _click(self, _e):
        self.toggle()

    def toggle(self):
        self.value = not self.value
        target = 1.0 if self.value else 0.0
        start = self._t

        def frame(v):
            self._t = start + (target - start) * v
            self.draw()

        # 打勾时用短动画，视觉上有反馈
        self.sf.anim.animate(f'cb{id(self)}', 0.13, 'out_cubic', frame)
        if self.on_change:
            self.on_change(self.value)

    def set_value(self, v):
        self.value = bool(v)
        self._t = 1.0 if self.value else 0.0
        self.draw()

    def draw(self):
        cv, th, sf = self.sf.canvas, self.sf.theme, self.sf
        cv.delete(self.tag)
        t = self._t
        bx, by, bs = self.x, self.y, self.box
        fill = T.lerp_color(th['check_bg'], th['check'], t)
        border = '' if t > 0.5 else (th['accent'] if self._hover else th['border'])
        T.round_rect_items(cv, bx, by, bx + bs, by + bs, 5, fill, border, 1,
                           tags=(self.tag,))
        if t > 0.05:
            # 勾的两笔随进度生长，做出"划上去"的感觉
            cx, cy = bx + bs / 2, by + bs / 2
            p1 = (bx + 4.5, by + bs * 0.52)
            p2 = (bx + bs * 0.42, by + bs * 0.72)
            p3 = (bx + bs - 4.0, by + bs * 0.28)
            k = min(1.0, t / 0.5)
            mx = p1[0] + (p2[0] - p1[0]) * k
            my = p1[1] + (p2[1] - p1[1]) * k
            col = '#ffffff' if t > 0.5 else th['accent']
            cv.create_line(p1[0], p1[1], mx, my, fill=col, width=2.2,
                           capstyle='round', tags=(self.tag,))
            if t > 0.5:
                k2 = (t - 0.5) / 0.5
                ex = p2[0] + (p3[0] - p2[0]) * k2
                ey = p2[1] + (p3[1] - p2[1]) * k2
                cv.create_line(p2[0], p2[1], ex, ey, fill=col, width=2.2,
                               capstyle='round', tags=(self.tag,))
        col = th['text'] if self._hover else th['text_dim']
        cv.create_text(bx + bs + 10, by + bs / 2, text=self.text, anchor='w',
                       fill=col, font=(sf.font, self.font_size), tags=(self.tag,))


class ProgressBar:
    """圆角进度条，进度变化带缓动（避免从 0 直接跳到 100 的生硬感）。"""

    def __init__(self, sf, x, y, w, h, radius=6):
        self.sf, self.x, self.y, self.w, self.h, self.radius = sf, x, y, w, h, radius
        self.value = 0
        self.maximum = 100
        self._shown = 0.0

    def set_max(self, m):
        self.maximum = max(1, m)
        self._shown = min(self._shown, self.maximum)
        self.draw()

    def set(self, v, animate=True):
        self.value = v
        if not animate:
            self._shown = v
            self.draw()
            return
        start = self._shown

        def frame(t):
            self._shown = start + (v - start) * t
            self.draw()

        self.sf.anim.animate(f'prog{id(self)}', 0.28, 'out_quart', frame)

    def draw(self):
        cv, th = self.sf.canvas, self.sf.theme
        cv.delete(f'prog{id(self)}')
        tag = (f'prog{id(self)}',)
        T.round_rect_items(cv, self.x, self.y, self.x + self.w, self.y + self.h,
                           self.radius, T.mix(th['surface2'], th['border'], 0.5),
                           '', 0, tags=tag)
        frac = min(1.0, max(0.0, self._shown / self.maximum))
        fw = (self.w - 4) * frac
        if fw > 2:
            T.round_rect_items(cv, self.x + 2, self.y + 2, self.x + 2 + fw,
                               self.y + self.h - 2, self.radius - 1,
                               th['accent'], '', 0, tags=tag)


class Dropdown:
    """自绘下拉选择框（ttk.Combobox 的弹出列表样式改不了，只能用自绘的）。

    展开时在 Canvas 上画一个覆盖层，而不是弹原生菜单 ——
    这样圆角、配色、动画都能和整体一致。
    """

    def __init__(self, sf, x, y, w, h, options, value=None, on_change=None,
                 radius=10, font_size=10):
        self.sf, self.x, self.y, self.w, self.h = sf, x, y, w, h
        self.options = list(options)
        self.value = value if value in self.options else (
            self.options[0] if self.options else '')
        self.on_change = on_change
        self.radius, self.font_size = radius, font_size
        self.open = False
        self.hover = -1
        self._list_geom = None
        self.tag = f'dd{id(self)}'
        self._list_geom = None
        self.hover_box_top = False
        self.bind()
        self.draw()          # 必须显式画一次，否则控件是隐形的

    def bind(self):
        cv = self.sf.canvas
        cv.tag_bind(self.tag, '<Enter>', self._enter)
        cv.tag_bind(self.tag, '<Leave>', self._leave)
        cv.tag_bind(self.tag, '<Button-1>', self._toggle)

    def _enter(self, _e):
        if not self.open:
            self.hover_box_top = True
            self.sf.canvas.configure(cursor='hand2')
            self.draw()

    def _leave(self, _e):
        self.hover_box_top = False
        if not self.open:
            self.sf.canvas.configure(cursor='')
        self.draw()

    def _toggle(self, _e):
        self.open = not self.open
        self.draw()
        if self.open:
            self.sf.canvas.tag_raise(self.tag)
        else:
            self.sf.canvas.configure(cursor='')
        self.bind()

    def set_value(self, v):
        if v in self.options:
            self.value = v
            self.draw()
            if self.on_change:
                self.on_change(v)

    def draw(self):
        cv, th, sf = self.sf.canvas, self.sf.theme, self.sf
        cv.delete(self.tag)
        hot = getattr(self, 'hover_box_top', False) or self.open
        bg = T.lerp_color(th['surface2'], th['surface3'], 1.0 if hot else 0.0)
        T.round_rect_items(cv, self.x, self.y, self.x + self.w, self.y + self.h,
                           self.radius, bg, th['border'], 1, tags=(self.tag,))
        cv.create_text(self.x + 14, self.y + self.h / 2, text=self.value,
                       anchor='w', fill=th['text'],
                       font=(sf.font, self.font_size), tags=self.tag)
        cv.create_text(self.x + self.w - 14, self.y + self.h / 2,
                       text='▴' if self.open else '▾', fill=th['text_dim'],
                       font=(sf.font, 9), tags=self.tag)

        if self.open:
            items = self.options
            ih = 32
            ly = self.y + self.h + 4
            # 向上弹：空间不够时
            if ly + len(items) * ih > sf.canvas.winfo_height() - 10:
                ly = max(6, self.y - len(items) * ih - 4)
            lh = len(items) * ih + 8
            sf.draw_panel(self.x, ly, self.x + self.w, ly + lh, 10, 0.97,
                          border=True, tags=self.tag)
            for i, opt in enumerate(items):
                iy = ly + 4 + i * ih
                if opt == self.value or i == self.hover:
                    T.round_rect_items(cv, self.x + 4, iy, self.x + self.w - 4,
                                       iy + ih, 7,
                                       th['row_sel'] if opt == self.value
                                       else th['row_hover'], '', 0, tags=(self.tag,))
                cv.create_text(self.x + 14, iy + ih / 2, text=opt, anchor='w',
                               fill=th['text'], font=(sf.font, self.font_size),
                               tags=self.tag)
                cv.tag_bind(self.tag, '<Motion>', self._motion)
            self._list_geom = (ly, ih, len(items))
            cv.tag_bind(self.tag, '<Button-1>', self._pick)
        else:
            self._list_geom = None
            cv.tag_bind(self.tag, '<Button-1>', self._toggle)

    def _motion(self, e):
        if not self.open or not self._list_geom:
            return
        ly, ih, n = self._list_geom
        idx = int((e.y - ly - 4) // ih)
        idx = idx if 0 <= idx < n else -1
        if idx != self.hover:
            self.hover = idx
            self.draw()

    def _pick(self, e):
        if not self._list_geom:
            self._toggle(e)
            return
        ly, ih, n = self._list_geom
        idx = int((e.y - ly - 4) // ih)
        if 0 <= idx < n:
            self.value = self.options[idx]
            self.open = False
            self.draw()
            self.sf.canvas.configure(cursor='')
            if self.on_change:
                self.on_change(self.value)
        elif not (self.x <= e.x <= self.x + self.w
                  and self.y <= e.y <= self.y + self.h):
            self.open = False
            self.draw()

    def close(self):
        if self.open:
            self.open = False
            self.draw()
