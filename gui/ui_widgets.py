# -*- coding: utf-8 -*-
"""自绘控件库：圆角按钮、玻璃卡片、勾选列表、输入框、下拉框、进度条。

全部基于 Canvas 绘制以获得 ttk 给不了的外观（圆角/悬停/渐变/玻璃感）。
需要真正文本编辑的地方（输入框）用 tk.Entry 叠在画好的底上 ——
tkinter 没法自绘一个能用的文本光标编辑器，叠放是唯一稳妥做法。
"""
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

import ui_theme as T


# tk 事件 type 是数字常量（见 tkinter.EventType），映射成语义名供控件判断。
_EVENT_NAMES = {
    4: 'ButtonPress-1', 5: 'ButtonRelease-1', 6: 'Motion', 7: 'Enter', 8: 'Leave',
    22: 'Configure',
}


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
        self._input_owners = []     # 注册的输入消费者（按注册顺序）
        self._modal = None          # 当前独占输入的控件（如展开的下拉框）
        self._install_input_pump()
        self.redraw_bg()

    # ── 输入分发 ──
    #
    # 多个自绘控件都绑在同一个 Canvas 上，靠 tk 的绑定顺序决定谁先收到事件，
    # 非常脆弱（后来绑的控件会被先绑的 break 掉）。这里改成集中分发：
    #   1. 有 modal（展开的下拉框等）时，只给它处理；
    #   2. 否则按注册顺序询问各控件，谁命中谁处理并停止。
    # 控件只需实现 on_input(event) 返回 True 表示已消费。

    def register_input(self, owner):
        if owner not in self._input_owners:
            self._input_owners.append(owner)

    def unregister_input(self, owner):
        if owner in self._input_owners:
            self._input_owners.remove(owner)
        if self._modal is owner:
            self._modal = None

    def set_modal(self, owner):
        self._modal = owner

    def clear_modal(self, owner=None):
        if owner is None or self._modal is owner:
            self._modal = None

    def dispatch_input(self, event):
        """把 tk 事件转成语义名后分发给控件。

        ⚠️ 坑：tk 的 event.type 是**数字常量**（4=ButtonPress, 5=ButtonRelease,
        6=Motion, 7=Enter, 8=Leave …），不是 'Motion' 这种字符串。
        早期直接拿它和字符串比较，结果所有控件都判定为"不是我的事件"，
        表现为鼠标移上去没反应、点了没动静。这里统一映射成语义名。
        """
        name = _EVENT_NAMES.get(event.type)
        if name is None:
            return None
        event.semantic = name
        if self._modal is not None:
            try:
                if self._modal.on_input(event):
                    return 'break'
            except Exception:
                self._modal = None
            return None
        for owner in list(self._input_owners):
            try:
                if owner.on_input(event):
                    return 'break'
            except Exception:
                pass
        return None

    def _install_input_pump(self):
        cv = self.canvas

        def handler(event):
            return self.dispatch_input(event)

        cv.bind('<Motion>', handler, add='+')
        cv.bind('<Button-1>', handler, add='+')
        cv.bind('<ButtonRelease-1>', handler, add='+')
        cv.bind('<MouseWheel>', handler, add='+')
        cv.bind('<Leave>', handler, add='+')

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
        self._hovering = False
        self._pressed_inside = False
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

    # ── 构建（只做一次）──

    def draw(self):
        """建立按钮的所有图元并绑定事件。

        重要（踩过的坑）：
        1. 早期版本每帧 delete + 重建图元，导致每帧重绑事件、悬停抖动、
           按下后 Release 丢失（点了没反应）。现在图元只建一次，
           动画只换颜色。
        2. 圆角早期用 Canvas 拼块画，实测边缘毫无过渡（方角 + 阶梯），
           肉眼就是"颗粒感"。现在背景用 PIL 4x 超采样渲染成图，
           文本仍由 Canvas 绘制（保持清晰）。
        """
        cv, th = self.sf.canvas, self.sf.theme
        cv.delete(self.tags)
        self._items = {}
        self._glow_photo = None
        self._body_photo = None

        # 外发光：固定尺寸、固定位置（不随悬停变形，避免抖动）
        self._items['glow'] = [cv.create_image(
            self.x - 3, self.y - 2, anchor='nw', tags=self.tags)]
        self._items['body'] = [cv.create_image(
            self.x, self.y, anchor='nw', tags=self.tags)]
        label = f'{self.icon} {self.text}'.strip() if self.icon else self.text
        self._items['label'] = [cv.create_text(
            self.x + self.w / 2, self.y + self.h / 2, text=label,
            fill=th['accent_text'], font=(self.sf.font, self.font_size, 'bold'),
            tags=self.tags)]

        # 接入 Surface 的统一输入分发（不用 tag_bind）
        self.sf.register_input(self)
        self._paint()

    def on_input(self, e):
        """统一入口。返回 True 表示已消费。"""
        if self.state == 'disabled':
            return False
        et = getattr(e, 'semantic', e.type)
        if et == 'Motion':
            inside = self._in_bounds(e.x, e.y)
            if inside != self._hovering:
                self._hovering = inside
                if inside:
                    self.sf.canvas.configure(cursor='hand2')
                    self._animate('_hover_t', 1.0)
                else:
                    self.sf.canvas.configure(cursor='')
                    self._animate('_hover_t', 0.0, 0.2)
                    self._animate('_press_t', 0.0, 0.1, '_p')
                    self._pressed_inside = False
                self._raise()
            return False
        if et in ('Button-1', 'ButtonPress-1'):
            if self._in_bounds(e.x, e.y):
                self._pressed_inside = True
                self._animate('_press_t', 1.0, 0.06, '_p')
                self._raise()
                return True
            return False
        if et == 'ButtonRelease-1':
            was = self._pressed_inside and self._press_t > 0.15
            self._pressed_inside = False
            self._animate('_press_t', 0.0, 0.12, '_p')
            if was and self.command:
                self.command()
                return True
            return False
        if et == 'Leave':
            if self._hovering:
                self._hovering = False
                self.sf.canvas.configure(cursor='')
                self._animate('_hover_t', 0.0, 0.2)
                self._animate('_press_t', 0.0, 0.1, '_p')
                self._raise()
            return False
        return False

    def _in_bounds(self, x, y):
        return (self.x <= x <= self.x + self.w
                and self.y <= y <= self.y + self.h)

    def unbind(self):
        self.sf.unregister_input(self)

    # ── 重绘（只换图片与颜色，不重建图元）──

    def _paint(self):
        cv, th = self.sf.canvas, self.sf.theme
        if not getattr(self, '_items', None):
            return
        fill, border, fg = self._current_fill()
        if self.state == 'disabled':
            fg = th['text_faint']

        # 发光：从"与面板同色"渐变到按钮色的淡化版
        glow_col = T.lerp_color(th['surface'], fill, 0.55 * self._hover_t)
        disp = 'normal' if (self._hover_t > 0.02 and self.state != 'disabled') \
            else 'hidden'
        if disp == 'normal':
            img = T.raster_rrect(self.w + 6, self.h + 6, self.radius + 3,
                                 fill=glow_col)
            self._glow_photo = ImageTk.PhotoImage(img)
            for i in self._items['glow']:
                cv.itemconfigure(i, image=self._glow_photo, state='normal')
        else:
            for i in self._items['glow']:
                cv.itemconfigure(i, state='hidden')

        # 按钮主体：PIL 超采样 → 抗锯齿圆角
        img = T.raster_rrect(self.w, self.h, self.radius, fill=fill,
                             outline=(None if self.kind == 'primary' else border),
                             width=1)
        self._body_photo = ImageTk.PhotoImage(img)
        for i in self._items['body']:
            cv.itemconfigure(i, image=self._body_photo)

        # 顶部高光：用一层渐变图叠出"被照亮"的感觉（比矩形切一刀自然）
        hi_a = int(38 + 26 * self._hover_t) if self.kind == 'primary' else 0
        if hi_a > 0:
            if getattr(self, '_hi_photo', None) is None or self._hi_alpha != hi_a:
                hi = Image.new('RGBA', (self.w, self.h), (0, 0, 0, 0))
                hd = ImageDraw.Draw(hi)
                for yy in range(int(self.h * 0.55)):
                    k = 1.0 - yy / max(1, self.h * 0.55)
                    hd.line([(0, yy), (self.w, yy)],
                            fill=(255, 255, 255, int(hi_a * k)))
                mask = Image.new('L', (self.w, self.h), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0, 0, self.w - 1, self.h - 1], self.radius, fill=255)
                hi.putalpha(Image.composite(hi.getchannel('A'),
                                            Image.new('L', (self.w, self.h), 0),
                                            mask))
                self._hi_photo = ImageTk.PhotoImage(hi)
                self._hi_alpha = hi_a
            if getattr(self, '_hi_img_id', None) is None:
                self._hi_img_id = cv.create_image(
                    self.x, self.y, anchor='nw', image=self._hi_photo,
                    tags=self.tags)
            else:
                cv.itemconfigure(self._hi_img_id, image=self._hi_photo,
                                 state='normal')
            self._items.setdefault('hi', []).append(self._hi_img_id) \
                if self._hi_img_id not in self._items.get('hi', []) else None
            cv.tag_raise(self._hi_img_id, self._items['body'][0])
        elif getattr(self, '_hi_img_id', None) is not None:
            cv.itemconfigure(self._hi_img_id, state='hidden')

        for i in self._items['label']:
            cv.itemconfigure(i, fill=fg)
            cv.coords(i, self.x + self.w / 2,
                      self.y + self.h / 2 + self._press_t * 1.2)

    # 兼容旧调用名
    def redraw(self):
        self.draw()

    # ── 动画 ──

    def _animate(self, attr, target, dur=None, key_suffix=''):
        cur = getattr(self, attr)
        if abs(cur - target) < 0.001:
            return
        start = cur
        key = f'{self._anim_key}{key_suffix}'

        def frame(v):
            setattr(self, attr, start + (target - start) * v)
            self._paint()          # 只改颜色，不重建图元

        self.sf.anim.animate(key, dur or self.hover_dur, 'out_cubic', frame)

    # ── 事件 ──
    # 注：旧的 _on_enter/_on_leave/_on_press/_on_release（tag_bind 版）已删除。
    # 它们的问题是把事件挂在 tag 上，而 draw() 每次都 delete+重建图元，
    # 等于把 Tk 正在派发事件的那个图元删掉 —— 表现为"按下有动画、弹起丢失、
    # 点了没反应"。现在统一走 on_input()（见上）。

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
        self.tag = f'cl{id(self)}'
        self.items = []            # [{'wxid','title','preview','time','group','sel'}]
        self.view = []             # 当前可见（过滤后）下标
        self.scroll = 0
        self.hover_row = -1
        self._avatar_cache = {}    # (wxid,size) -> PhotoImage，避免重复生成
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

    CHECKBOX_HIT = 40      # 行左侧这么多像素内算"点在勾选框上"

    def _bind(self):
        """向 Surface 注册自己为输入消费者（不再各自 bind 到 Canvas）。

        早期做法是每个控件各自 cv.bind(...)，靠绑定顺序决定谁先收到事件，
        结果 CheckList 先绑定就会把下拉框的点击 break 掉。
        现在由 Surface 集中分发，顺序和消费权都是明确的。
        """
        self.sf.register_input(self)

    def unbind(self):
        """注销输入注册。页面切换时由 App 调用，否则旧实例会残留继续收事件。"""
        self.sf.unregister_input(self)

    def on_input(self, e):
        """统一入口：返回 True 表示已消费该事件。"""
        et = getattr(e, 'semantic', e.type)
        if et == 'Motion':
            self._on_motion(e)
            return False
        if et == 'Leave':
            self._on_leave(e)
            return False
        if et == 'ButtonPress-1':
            return self._on_click(e) == 'break'
        if et in ('ButtonRelease-1',):
            self._scroll_release(e)
            return False
        if et in ('MouseWheel', 'Button-4', 'Button-5'):
            if self._inside(e.x, e.y):
                if et == 'MouseWheel':
                    self._wheel(-1 if e.delta > 0 else 1)
                else:
                    self._wheel(-1 if et == 'Button-4' else 1)
                return True
        return False

    def _scroll_press(self, e):
        g = self._thumb_geom()
        if not g:
            return None
        sx, ty1, ty2, ty, th, track_h = g
        if not (self.x + self.w - 16 <= e.x <= self.x + self.w):
            return None
        if ty <= e.y <= ty + th:
            self._drag_off = e.y - ty
        else:
            self._drag_off = th / 2
            self._scroll_to_y(e.y - self._drag_off, ty1, track_h, th)
        return 'break'

    def _inside(self, x, y):
        return (self.x <= x <= self.x + self.w
                and self.y <= y <= self.y + self.h)

    def _inside_body(self, x, y):
        return (self.x <= x <= self.x + self.w
                and self.y + self.HEADER_H <= y <= self.y + self.h)

    def _row_at(self, y):
        if y < self.y + self.HEADER_H or y > self.y + self.h:
            return -1
        idx = int((y - self.y - self.HEADER_H) // self.ROW_H) + self.scroll
        return idx if 0 <= idx < len(self.view) else -1

    def _on_motion(self, e):
        # 没落在列表里就不消费事件，让后面的控件（如下拉框）自己处理
        if not self._inside_body(e.x, e.y):
            if self.hover_row != -1:
                old = self.hover_row
                self.hover_row = -1
                self._repaint_rows([old])
            return None
        i = self._row_at(e.y)
        if i != self.hover_row:
            old = self.hover_row
            self.hover_row = i
            self._repaint_rows([old, i])

    def _on_leave(self, _e):
        if self.hover_row != -1:
            old = self.hover_row
            self.hover_row = -1
            self._repaint_rows([old])

    def _row_tag(self, idx):
        return f'{self.tag}_row{idx}'

    def _repaint_rows(self, idxs):
        """只重画指定行（含该行的选中/悬停背景、勾选框、文字）。

        不动面板、不重建头像图 —— 这是悬停流畅的关键。
        """
        cv = self.sf.canvas
        rows = self.view[self.scroll:self.scroll + self.visible_rows]
        for idx in idxs:
            if idx is None or idx < 0 or idx not in rows:
                # 已滚出可视区，直接丢掉旧图元
                cv.delete(self._row_tag(idx) if idx is not None and idx >= 0 else '')
                continue
            cv.delete(self._row_tag(idx))
            self._draw_row(idx, rows.index(idx))
        # 行图元必须压在面板之上
        for idx in idxs:
            if idx is not None and idx >= 0:
                cv.tag_raise(self._row_tag(idx))

    def _draw_row(self, idx, slot):
        """画一行（slot 是它在可视区内的序号）。"""
        sf, cv, th = self.sf, self.sf.canvas, self.sf.theme
        it = self.items[idx]
        t = self._row_tag(idx)
        y0 = self.y + self.HEADER_H
        ry = y0 + slot * self.ROW_H
        selected = bool(it.get('sel'))
        hovered = (idx == self.hover_row)

        if selected or hovered:
            # 行高亮也用超采样渲染，保证圆角平滑
            if getattr(self, '_hl_photo_key', None) != (selected,):
                hl = T.raster_rrect(self.w - 20, self.ROW_H - 6, 10,
                                    fill=(th['row_sel'] if selected
                                          else th['row_hover']), alpha=235)
                self._hl_photo = ImageTk.PhotoImage(hl)
                self._hl_photo_key = (selected,)
            cv.create_image(self.x + 10, ry + 3, image=self._hl_photo,
                            anchor='nw', tags=(t,))
        # 复选框
        bx, by, bs = self.x + 18, ry + self.ROW_H / 2 - 9, 18
        box_fill = th['check'] if selected else th['check_bg']
        if getattr(self, '_ck_photo_key', None) != (selected,):
            ck = T.raster_rrect(bs, bs, 5, fill=box_fill,
                                outline=(None if selected else th['border']),
                                width=1)
            self._ck_photo = ImageTk.PhotoImage(ck)
            self._ck_photo_key = (selected,)
        cv.create_image(bx, by, image=self._ck_photo, anchor='nw', tags=(t,))
        if selected:
            cv.create_line(bx + 4.5, by + 9.5, bx + 7.5, by + 12.8, fill='#ffffff',
                           width=2.2, capstyle='round', tags=(t,))
            cv.create_line(bx + 7.5, by + 12.8, bx + 13.5, by + 5.6, fill='#ffffff',
                           width=2.2, capstyle='round', tags=(t,))
        # 头像（走缓存，不重复生成）
        ax = bx + bs + 12
        ph = self._avatar(it['title'], it['wxid'], 30)
        cv.create_image(ax, ry + self.ROW_H / 2 - 15, image=ph, anchor='nw', tags=(t,))
        # 文字
        tx = ax + 40
        marker = '👥 ' if it.get('group') else ''
        cv.create_text(tx, ry + 14, text=marker + it['title'], anchor='w',
                       fill=th['text'], font=(sf.font, 10, 'bold'), tags=(t,))
        cv.create_text(tx, ry + 30, text=(it.get('preview') or '')[:46], anchor='w',
                       fill=th['text_dim'], font=(sf.font, 9), tags=(t,))
        if it.get('time'):
            cv.create_text(self.x + self.w - 20, ry + 22, text=it['time'],
                           anchor='e', fill=th['text_faint'],
                           font=(sf.font, 9), tags=(t,))

    def _avatar(self, title, wxid, size):
        """头像图缓存。同一会话只生成一次 —— 早期每帧新建 300 张，内存与耗时都浪费。"""
        key = (wxid, size)
        if key not in self._avatar_cache:
            img = T.rounded_avatar(title, size, T.avatar_color_for(wxid))
            self._avatar_cache[key] = ImageTk.PhotoImage(img)
            if len(self._avatar_cache) > 400:
                self._avatar_cache.pop(next(iter(self._avatar_cache)))
        return self._avatar_cache[key]

    def _hit(self, e):
        """判断点在哪儿：返回 (行下标, 'check'|'row'|'')。"""
        if not self._inside_body(e.x, e.y):
            return -1, ''
        i = self._row_at(e.y)
        if i < 0:
            return -1, ''
        # 左侧区域算勾选框
        if e.x <= self.x + self.CHECKBOX_HIT:
            return i, 'check'
        return i, 'row'

    def _on_click(self, e):
        """点左侧方块 → 勾选/取消；点该行其它位置 → 打开会话详情。

        ⚠️ 关键：没命中列表区域时必须返回 None（不消费事件）。
        早期这里无条件 return 'break'，而 Tk 遇到 'break' 会**中止后续所有
        处理器** —— 于是绑在它之后的下拉框永远收不到点击，表现为
        "鼠标移到导出格式上没反应、点了像卡死"。实测日志：
            [CheckList._on_click] (156,137) 返回='break'   ← 明明没点到行
        """
        i, where = self._hit(e)
        if i < 0:
            return None
        if where == 'check':
            self.toggle_index(i)
        elif where == 'row' and self.on_open:
            self.on_open(self.items[i])
        return 'break'

    def _on_double(self, e):
        i, where = self._hit(e)
        if i >= 0 and where == 'row' and self.on_open:
            self.on_open(self.items[i])
            return 'break'
        return None

    def _wheel_at(self, e, d):
        if self._inside(e.x, e.y):
            self._wheel(d)

    # ── 滚动条拖动 ──

    def _thumb_geom(self):
        """返回滚动条轨道与滑块的几何 (x, track_y1, track_y2, thumb_y, thumb_h)。"""
        total = len(self.view)
        y0 = self.y + self.HEADER_H
        track_y1, track_y2 = y0 + 4, self.y + self.h - 8
        track_h = max(1, track_y2 - track_y1)
        if total <= self.visible_rows:
            return None
        thumb_h = max(28, track_h * self.visible_rows / total)
        maxs = total - self.visible_rows
        ty = track_y1 + (track_h - thumb_h) * (self.scroll / maxs if maxs else 0)
        return (self.x + self.w - 8, track_y1, track_y2, ty, thumb_h, track_h)

    def _scroll_press(self, e):
        g = self._thumb_geom()
        if not g:
            return
        sx, ty1, ty2, ty, th, track_h = g
        # 点在滚动条竖条上（含左右 12px 容差）
        if not (self.x + self.w - 16 <= e.x <= self.x + self.w):
            return
        if ty <= e.y <= ty + th:
            self._drag_off = e.y - ty          # 抓住滑块拖动
        else:
            self._drag_off = th / 2            # 点轨道空白：跳到该位置
            self._scroll_to_y(e.y - self._drag_off, ty1, track_h, th)
        return 'break'

    def _scroll_drag(self, e):
        if getattr(self, '_drag_off', None) is None:
            return
        g = self._thumb_geom()
        if not g:
            return
        sx, ty1, ty2, ty, th, track_h = g
        self._scroll_to_y(e.y - self._drag_off, ty1, track_h, th)
        return 'break'

    def _scroll_release(self, _e):
        self._drag_off = None

    def _scroll_to_y(self, ty, track_y1, track_h, thumb_h):
        total = len(self.view)
        maxs = total - self.visible_rows
        if maxs <= 0:
            return
        frac = (ty - track_y1) / max(1, track_h - thumb_h)
        ns = int(round(max(0.0, min(1.0, frac)) * maxs))
        if ns != self.scroll:
            self.scroll = ns
            self.draw()

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
        for slot, idx in enumerate(rows):
            self._draw_row(idx, slot)

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
    """带文字的自绘勾选框。

    与 Button 同样的教训：图元只建一次，动画只改属性；事件只绑一次。
    早期版本每帧重建 + 重绑，会导致悬停抖动、点击丢失。
    """

    def __init__(self, sf, x, y, w, text, value=False, on_change=None,
                 font_size=10, box=18):
        self.sf, self.x, self.y, self.w = sf, x, y, w
        self.text, self.value = text, value
        self.on_change, self.font_size, self.box = on_change, font_size, box
        self.tag = f'cb{id(self)}'
        self._t = 1.0 if value else 0.0
        self._hover = False
        self.draw()

    def draw(self):
        cv, th, sf = self.sf.canvas, self.sf.theme, self.sf
        cv.delete(self.tag)
        bx, by, bs = self.x, self.y, self.box
        it = {}
        # 方框用 PIL 超采样渲染（抗锯齿圆角），而不是 Canvas 拼块
        it['box'] = [cv.create_image(bx, by, anchor='nw', tags=(self.tag,))]
        it['tick1'] = [cv.create_line(bx + 4.5, by + bs * 0.52,
                                      bx + bs * 0.42, by + bs * 0.72,
                                      fill='#ffffff', width=2.2, capstyle='round',
                                      tags=(self.tag,))]
        it['tick2'] = [cv.create_line(bx + bs * 0.42, by + bs * 0.72,
                                      bx + bs - 4.0, by + bs * 0.28,
                                      fill='#ffffff', width=2.2, capstyle='round',
                                      tags=(self.tag,))]
        it['label'] = [cv.create_text(bx + bs + 10, by + bs / 2, text=self.text,
                                      anchor='w', fill=th['text_dim'],
                                      font=(sf.font, self.font_size),
                                      tags=(self.tag,))]
        self._items = it
        self._box_photo = None
        # 接入统一输入分发（不用 tag_bind，避免图元重建后事件断裂）
        self.sf.register_input(self)
        self._paint()

    def on_input(self, e):
        et = getattr(e, 'semantic', e.type)
        if et == 'Motion':
            inside = self._in_bounds(e.x, e.y)
            if inside != self._hover:
                self._hover = inside
                if inside:
                    self.sf.canvas.configure(cursor='hand2')
                else:
                    self.sf.canvas.configure(cursor='')
                self._paint()
            return False
        if et in ('Button-1', 'ButtonPress-1'):
            if self._in_bounds(e.x, e.y):
                self.toggle()
                return True
            return False
        if et == 'Leave':
            if self._hover:
                self._hover = False
                self.sf.canvas.configure(cursor='')
                self._paint()
            return False
        return False

    def _in_bounds(self, x, y):
        # 命中区覆盖方框 + 右侧文字，避免文字区域点不动
        return (self.x <= x <= self.x + self.w
                and self.y - 4 <= y <= self.y + self.box + 4)

    def unbind(self):
        self.sf.unregister_input(self)

    def _paint(self):
        cv, th = self.sf.canvas, self.sf.theme
        if not getattr(self, '_items', None):
            return
        t = self._t
        fill = T.lerp_color(th['check_bg'], th['check'], t)
        border = '' if t > 0.5 else (th['accent'] if self._hover else th['border'])
        img = T.raster_rrect(self.box, self.box, 5, fill=fill,
                             outline=(border or None), width=1)
        self._box_photo = ImageTk.PhotoImage(img)
        for i in self._items['box']:
            cv.itemconfigure(i, image=self._box_photo)
        # 勾的两笔随进度生长
        show1 = t > 0.05
        show2 = t > 0.5
        col = '#ffffff' if t > 0.5 else th['accent']
        bx, by, bs = self.x, self.y, self.box
        p1 = (bx + 4.5, by + bs * 0.52)
        p2 = (bx + bs * 0.42, by + bs * 0.72)
        p3 = (bx + bs - 4.0, by + bs * 0.28)
        k = min(1.0, t / 0.5)
        for i in self._items['tick1']:
            cv.itemconfigure(i, state='normal' if show1 else 'hidden', fill=col)
            if show1:
                cv.coords(i, p1[0], p1[1],
                          p1[0] + (p2[0] - p1[0]) * k, p1[1] + (p2[1] - p1[1]) * k)
        for i in self._items['tick2']:
            cv.itemconfigure(i, state='normal' if show2 else 'hidden', fill=col)
            if show2:
                k2 = (t - 0.5) / 0.5
                cv.coords(i, p2[0], p2[1],
                          p2[0] + (p3[0] - p2[0]) * k2, p2[1] + (p3[1] - p2[1]) * k2)
        for i in self._items['label']:
            cv.itemconfigure(i, fill=th['text'] if self._hover else th['text_dim'])

    def toggle(self):
        self.value = not self.value
        target = 1.0 if self.value else 0.0
        start = self._t

        def frame(v):
            self._t = start + (target - start) * v
            self._paint()

        self.sf.anim.animate(f'cb{id(self)}', 0.13, 'out_cubic', frame)
        if self.on_change:
            self.on_change(self.value)

    def set_value(self, v):
        self.value = bool(v)
        self._t = 1.0 if self.value else 0.0
        self._paint()


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
        self._row_tag = f'ddr{id(self)}'
        self._list_geom = None
        self.hover_box_top = False
        self.bind()
        self.draw()          # 必须显式画一次，否则控件是隐形的

    def bind(self):
        """注册为输入消费者；展开时独占输入（modal）。

        展开的弹层会和下方列表/按钮区域重叠，如果让列表先去判命中，
        点击选项会被列表 break 掉 —— 所以展开期间由 Surface 把输入
        全部转给本控件，收起后交还。
        """
        self.sf.register_input(self)

    def on_input(self, e):
        et = getattr(e, 'semantic', e.type)
        if et == 'Motion':
            self._on_motion(e)
            return False
        if et in ('Button-1', 'ButtonPress-1'):
            return self._on_press(e) == 'break'
        if et == 'Leave':
            self._on_leave_global(e)
            return False
        return False

    # ── 几何判定 ──

    def _in_box(self, x, y):
        return (self.x <= x <= self.x + self.w
                and self.y <= y <= self.y + self.h)

    def _in_list(self, x, y):
        if not self.open or not self._list_geom:
            return False
        ly, ih, n = self._list_geom
        return (self.x <= x <= self.x + self.w and ly <= y <= ly + 4 + n * ih)

    def _option_at(self, x, y):
        if not self._in_list(x, y):
            return -1
        ly, ih, n = self._list_geom
        i = int((y - ly - 4) // ih)
        return i if 0 <= i < n else -1

    def _on_motion(self, e):
        if self._in_box(e.x, e.y):
            if not self.open and not self.hover_box_top:
                self.hover_box_top = True
                self.sf.canvas.configure(cursor='hand2')
                self.draw()
            return
        if self.open:
            i = self._option_at(e.x, e.y)
            if i != self.hover:
                self.hover = i
                self._redraw_list_only()
            self.sf.canvas.configure(cursor='hand2' if i >= 0 else '')
            return
        if self.hover_box_top:
            self.hover_box_top = False
            self.sf.canvas.configure(cursor='')
            self.draw()

    def _on_leave_global(self, _e):
        """鼠标离开整个画布：复位悬停态并收起弹层。"""
        if self.hover_box_top:
            self.hover_box_top = False
            self.sf.canvas.configure(cursor='')
            if not self.open:
                self.draw()

    def _on_press(self, e):
        # 展开状态下遇到任何点击都吃掉（弹层是模态的）：点选项=选中，
        # 点别处=收起。这样不会被下方列表抢走事件。
        if self.open:
            i = self._option_at(e.x, e.y)
            if i >= 0:
                self._pick_index(i)
            else:
                self.open = False
                self.hover = -1
                self.sf.clear_modal(self)
                self.draw()
                self.sf.canvas.configure(cursor='')
            return 'break'
        if self._in_box(e.x, e.y):
            self._toggle(e)
            return 'break'
        return None

    def _pick_index(self, i):
        self.value = self.options[i]
        self.open = False
        self.hover = -1
        self.sf.clear_modal(self)
        self.draw()
        self.sf.canvas.configure(cursor='')
        if self.on_change:
            self.on_change(self.value)

    def _toggle(self, _e=None):
        self.open = not self.open
        self.hover = -1
        self.draw()
        if self.open:
            self.sf.set_modal(self)
        else:
            self.sf.clear_modal(self)
            self.sf.canvas.configure(cursor='')

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
        # 用抗锯齿渲染，和其它控件风格统一
        bg = T.lerp_color(th['surface2'], th['surface3'], 1.0 if hot else 0.0)
        self._box_photo = ImageTk.PhotoImage(
            T.raster_rrect(self.w, self.h, self.radius, fill=bg,
                           outline=th['border'], width=1))
        it = {'box': cv.create_image(self.x, self.y, anchor='nw',
                                     image=self._box_photo, tags=self.tag)}
        cv.create_text(self.x + 14, self.y + self.h / 2, text=self.value,
                       anchor='w', fill=th['text'],
                       font=(sf.font, self.font_size), tags=self.tag)
        cv.create_text(self.x + self.w - 14, self.y + self.h / 2,
                       text='▴' if self.open else '▾', fill=th['text_dim'],
                       font=(sf.font, 9), tags=self.tag)
        self._items = it

        if self.open:
            items = self.options
            ih = 32
            ly = self.y + self.h + 4
            # 下面空间不够就向上弹
            if ly + len(items) * ih > sf.canvas.winfo_height() - 10:
                ly = max(6, self.y - len(items) * ih - 4)
            lh = len(items) * ih + 8
            self._list_geom = (ly, ih, len(items))
            sf.draw_panel(self.x, ly, self.x + self.w, ly + lh, 10, 0.97,
                          border=True, tags=self.tag)
            self._redraw_list_only()
        else:
            self._list_geom = None

    def _redraw_list_only(self):
        """只重画展开的选项行（含高亮），不做面板合成。供悬停调用。

        性能要点（踩过的坑）：展开的弹层面板是 PIL 合成的，很贵。
        早期版本在 _motion 里调 draw()，鼠标在选项上移动一下就要重新合成
        整块面板，直接卡死。现在悬停只重画这几行文字与高亮块。
        """
        if not self.open or not self._list_geom:
            return
        cv, th, sf = self.sf.canvas, self.sf.theme, self.sf
        ly, ih, n = self._list_geom
        for iid in list(cv.find_withtag(self._row_tag)):
            cv.delete(iid)
        for i, opt in enumerate(self.options):
            iy = ly + 4 + i * ih
            if opt == self.value or i == self.hover:
                T.round_rect_items(cv, self.x + 4, iy, self.x + self.w - 4,
                                   iy + ih, 7,
                                   th['row_sel'] if opt == self.value
                                   else th['row_hover'], '', 0,
                                   tags=(self.tag, self._row_tag))
            cv.create_text(self.x + 14, iy + ih / 2, text=opt, anchor='w',
                           fill=th['text'], font=(sf.font, self.font_size),
                           tags=(self.tag, self._row_tag))
        cv.tag_raise(self._row_tag)

    def _motion(self, e):
        """兼容旧调用（现在由 _on_motion 统一处理）。"""
        self._on_motion(e)

    def _pick(self, e):
        """兼容旧调用：按坐标选中。"""
        i = self._option_at(e.x, e.y)
        if i >= 0:
            self._pick_index(i)

    def close(self):
        if self.open:
            self.open = False
            self.hover = -1
            self.draw()

    def unbind(self):
        """注销输入注册（页面切换时调用，避免旧实例继续收事件）。"""
        self.sf.unregister_input(self)
