# -*- coding: utf-8 -*-
"""UI 渲染底层：主题配色 + 背景生成 + 圆角面板合成 + 自绘控件。

为什么自己画而不用 ttk
----------------------
tkinter 的 ttk 控件外观由系统主题决定，圆角、悬停、半透明都改不了。
想要"设计感"，只能把界面画在 Canvas 上：
  · 背景 = PIL 生成的渐变 + 高斯模糊光晕（或用户自己的图）
  · 面板 = 把背景裁一块、与面板色混合后贴回，做出半透明玻璃感
  · 控件 = Canvas 图元自绘（圆角、悬停高亮、勾选框都是画出来的）

关于"透明"：tkinter 没有真正的控件级透明，所以做法是
「先算出该处背景与面板色的混合色，再用这个实色绘制」，视觉上等价。
"""
import math
import os
import sys as _sys

from PIL import Image, ImageDraw, ImageFilter, ImageTk

# ─────────────────────────── 配色 ───────────────────────────

DARK = {
    'name': 'dark',
    'bg_top': (18, 21, 32),
    'bg_bottom': (34, 27, 51),
    'blobs': [(0.80, 0.10, 0.42, (92, 62, 200)),
              (0.12, 0.88, 0.38, (26, 84, 168)),
              (0.55, 0.55, 0.30, (120, 40, 140))],
    'surface': '#1e2438',        # 面板底色
    'surface2': '#262d45',       # 次级面板 / 输入框
    'surface3': '#2e3652',       # 悬停
    'row': '#242b42',
    'row_hover': '#2c3450',
    'row_sel': '#2b3a63',
    'text': '#eef1f8',
    'text_dim': '#9aa5c0',
    'text_faint': '#8b97b5',     # 对 surface2 #262d45 约 4.6:1（原 #6b7793 只有 3.0，提示看不清）
    'accent': '#5b7cfa',
    'accent2': '#8b5cf6',
    'accent_text': '#ffffff',
    'border': '#333c5a',
    'ok': '#34d399',
    'warn': '#fbbf24',
    'err': '#f87171',
    'check': '#5b7cfa',
    'check_bg': '#2b3350',
    'shadow': (0, 0, 0, 90),
}

LIGHT = {
    'name': 'light',
    'bg_top': (246, 248, 253),
    'bg_bottom': (226, 232, 248),
    'blobs': [(0.82, 0.08, 0.40, (186, 206, 255)),
              (0.10, 0.90, 0.36, (206, 196, 255)),
              (0.52, 0.50, 0.28, (196, 226, 255))],
    'surface': '#ffffff',
    'surface2': '#f2f4fa',
    'surface3': '#e8ecf7',
    'row': '#f7f8fc',
    'row_hover': '#eef2fd',
    'row_sel': '#e4ebff',
    'text': '#1b2233',
    'text_dim': '#5d6880',
    'text_faint': '#5f6a84',     # 对 surface2 #f2f4fa 约 4.8:1（原 #8b95ab 只有 2.7，提示看不清）
    'accent': '#3d6bfa',
    'accent2': '#7b5cf5',
    'accent_text': '#ffffff',
    'border': '#dde3f0',
    'ok': '#16a34a',
    'warn': '#d97706',
    'err': '#dc2626',
    'check': '#3d6bfa',
    'check_bg': '#ffffff',
    'shadow': (60, 70, 100, 40),
}

THEMES = {'dark': DARK, 'light': LIGHT}

# 中文字体优先级：雅黑 UI 最好看，退回雅黑，再退回默认
FONT_CANDIDATES = ['Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC',
                   'Noto Sans CJK SC', 'SimHei']


def pick_font(root):
    """挑一个系统里真实存在的中文字体，避免出现方块字。"""
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    for f in FONT_CANDIDATES:
        if f in fams:
            return f
    return 'TkDefaultFont'


# ─────────────────────────── 背景图生成 ───────────────────────────

_bg_cache = {}


def _linear_gradient(size, c1, c2):
    """对角线性渐变。用低分辨率生成再放大，比逐像素快得多且更平滑。"""
    w, h = size
    small = 256
    img = Image.new('RGB', (small, small))
    px = img.load()
    for y in range(small):
        for x in range(small):
            t = (x / small + y / small) / 2
            px[x, y] = (int(c1[0] + (c2[0] - c1[0]) * t),
                        int(c1[1] + (c2[1] - c1[1]) * t),
                        int(c1[2] + (c2[2] - c1[2]) * t))
    return img.resize((w, h), Image.BICUBIC)


def _add_blobs(img, blobs):
    """在渐变底上叠柔和光晕。

    坑：早期做法是"画实心圆 → 高斯模糊 → 用硬阈值 mask 做 composite"。
    模糊后的圆仍有可见边界，再经阈值 mask 合成会出现**斑驳的云雾块**
    （浅色主题下尤其明显，像背景脏了）。
    现在改成每个光晕单独算径向渐变 alpha 再叠加 —— 数学上连续、无硬边，
    观感是干净的光泽而不是色块。
    """
    w, h = img.size
    base = img.convert('RGBA')
    # 光晕在缩小的画布上算，最后放大回来：又快又天然平滑
    sw, sh = max(32, w // 6), max(32, h // 6)
    for (rx, ry, rr, col) in blobs:
        cx, cy = rx * sw, ry * sh
        r = rr * max(sw, sh)
        cell = Image.new('L', (sw, sh), 0)
        cd = ImageDraw.Draw(cell)
        steps = 26
        for i in range(steps, 0, -1):
            k = i / steps
            v = int(255 * (1.0 - k) ** 2)      # 中心最亮，向边缘平方衰减
            cd.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=v)
        cell = cell.resize((w, h), Image.BILINEAR)
        glow = Image.new('RGBA', (w, h), tuple(col) + (0,))
        glow.putalpha(cell)
        base = Image.alpha_composite(base, glow)
    return base.convert('RGB')


def make_background(size, theme, image_path=''):
    """生成背景图。image_path 存在时用用户的图（自动裁切+压暗保证可读性）。"""
    key = (size, theme['name'], image_path, os.path.getmtime(image_path)
           if image_path and os.path.exists(image_path) else 0)
    if key in _bg_cache:
        return _bg_cache[key]

    w, h = size
    if w < 2 or h < 2:
        w, h = 2, 2

    if image_path and os.path.exists(image_path):
        try:
            src = Image.open(image_path).convert('RGB')
            # 等比铺满窗口后居中裁切
            sr, tr = src.width / src.height, w / h
            if sr > tr:
                nw = int(src.height * tr)
                src = src.crop(((src.width - nw) // 2, 0,
                                (src.width - nw) // 2 + nw, src.height))
            else:
                nh = int(src.width / tr)
                src = src.crop((0, (src.height - nh) // 2,
                                src.width, (src.height - nh) // 2 + nh))
            img = src.resize((w, h), Image.LANCZOS)
            img = img.filter(ImageFilter.GaussianBlur(1.2))
            # 压暗/提亮，保证正文可读
            veil = (12, 14, 22) if theme['name'] == 'dark' else (250, 251, 255)
            over = Image.new('RGB', (w, h), veil)
            img = Image.blend(img, over, 0.55 if theme['name'] == 'dark' else 0.62)
        except Exception:
            img = _add_blobs(_linear_gradient((w, h), theme['bg_top'],
                                              theme['bg_bottom']), theme['blobs'])
    else:
        img = _add_blobs(_linear_gradient((w, h), theme['bg_top'],
                                          theme['bg_bottom']), theme['blobs'])

    _bg_cache.clear()          # 只留最近一张，避免内存堆积
    _bg_cache[key] = img
    return img


def find_bg_image(base_dir):
    """找用户自定义背景图：程序目录下的 背景.png / 背景.jpg / background.*"""
    if not base_dir:
        return ''
    for n in ('背景.png', '背景.jpg', '背景.jpeg', '背景.webp',
              'background.png', 'background.jpg', 'bg.png', 'bg.jpg'):
        p = os.path.join(base_dir, n)
        if os.path.exists(p):
            return p
    return ''


# ─────────────────────────── 颜色工具 ───────────────────────────

def hex2rgb(s):
    s = s.lstrip('#')
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(c):
    return '#%02x%02x%02x' % (int(c[0]), int(c[1]), int(c[2]))


def mix(c1, c2, t):
    """按 t 混合两色（t=0 取 c1）。"""
    a = hex2rgb(c1) if isinstance(c1, str) else c1
    b = hex2rgb(c2) if isinstance(c2, str) else c2
    return rgb2hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def lighten(c, t=0.12):
    return mix(c, '#ffffff', t)


def darken(c, t=0.12):
    return mix(c, '#000000', t)


def _hex_or_rgb(c):
    """接受 '#rrggbb' 或 (r,g,b) 两种写法。"""
    return hex2rgb(c) if isinstance(c, str) else tuple(c)[:3]


# ─────────────────────── 圆角绘制（Canvas 低质量路径） ───────────────────────

def round_rect_items(cv, x1, y1, x2, y2, r, fill='', outline='', width=1, tags=(),
                     ret_ids=False):
    """在 Canvas 上画圆角矩形，返回图元 id 列表。

    ⚠️ 不推荐用于看得见的控件：实测这条路径的圆角**实际上是方的**。
    用 6 个实心块拼圆角时，Tk 的 create_oval 填充范围比几何圆略小，
    块与块之间留 1px 缝、被背景色填上，圆角就被啃掉了 ——
    沿边缘只有 2 种颜色（毫无过渡），这就是肉眼看到的锯齿/颗粒感。

    需要好看的圆角请用 raster_rrect()（PIL 4x 超采样，过渡色 2→8 种）。
    这里保留给不显眼的内部用途（分隔线、边框、临时调试）。
    """
    ids = []
    if x2 - x1 < 1 or y2 - y1 < 1:
        return ids
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    if fill:
        kw = dict(fill=fill, outline=fill, width=0, tags=tags)
        if r <= 0:
            ids.append(cv.create_rectangle(x1, y1, x2, y2, **kw))
        else:
            # 矩形主体 + 四角弧。注意 create_arc 的签名是
            # create_arc(x1,y1,x2,y2, start=, extent=, ...) —— start/extent 必须
            # 用关键字传，位置参数只接受 4 个坐标（否则报 wrong # coordinates）。
            ids.append(cv.create_rectangle(x1 + r, y1, x2 - r, y2, **kw))
            ids.append(cv.create_rectangle(x1, y1 + r, x2, y2 - r, **kw))
            for (ax1, ay1, ax2, ay2, st) in (
                    (x1, y1, x1 + 2 * r, y1 + 2 * r, 90),
                    (x2 - 2 * r, y1, x2, y1 + 2 * r, 0),
                    (x2 - 2 * r, y2 - 2 * r, x2, y2, 270),
                    (x1, y2 - 2 * r, x1 + 2 * r, y2, 180)):
                ids.append(cv.create_arc(ax1, ay1, ax2, ay2, start=st, extent=90,
                                         style='pieslice', **kw))
    if outline:
        ids.append(cv.create_rectangle(x1, y1, x2, y2, fill='', outline=outline,
                                       width=width, tags=tags))
    return ids


# ─────────────────── 高质量圆角（PIL 超采样，抗锯齿） ───────────────────

_rrect_cache = {}
RRECT_SCALE = 4          # 4x 超采样后缩小；实测边缘过渡色 2 种 → 8 种


def raster_rrect(w, h, radius, fill=None, outline=None, width=1, alpha=255,
                 scale=RRECT_SCALE):
    """渲染一个抗锯齿的圆角矩形，返回 RGBA 图（带缓存）。

    这是解决"颗粒感"的正解：Tk 的 Canvas 不抗锯齿，靠拼块怎么拼边缘都是阶梯状。
    做法是在 scale 倍画布上画好再用 LANCZOS 缩小 —— 缩放本身产生亚像素过渡，
    实测边缘过渡色从 2 种提升到 8 种，配合按钮/列表使用观感提升明显。
    """
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    radius = max(0, min(radius, w / 2, h / 2))
    key = (w, h, round(radius, 2), fill, outline, width, alpha, scale)
    hit = _rrect_cache.get(key)
    if hit is not None:
        return hit

    S = max(1, int(scale))
    big = Image.new('RGBA', (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    box = [0, 0, w * S - 1, h * S - 1]
    rr = radius * S
    f = (_hex_or_rgb(fill) + (alpha,)) if fill else None
    if outline:
        d.rounded_rectangle(box, rr, fill=f,
                            outline=_hex_or_rgb(outline) + (alpha,),
                            width=max(1, int(round(width * S))))
    else:
        d.rounded_rectangle(box, rr, fill=f)
    img = big.resize((w, h), Image.LANCZOS)

    if len(_rrect_cache) > 600:
        for k in list(_rrect_cache)[:200]:
            _rrect_cache.pop(k, None)
    _rrect_cache[key] = img
    return img


def clear_raster_cache():
    _rrect_cache.clear()


def glass_panel(bg_img, box, theme, radius=14, alpha=0.80, fill=None):
    """把背景裁一块、混上面板色、切圆角，返回可直接贴的 PIL 图。

    这就是 tkinter 里模拟「半透明玻璃面板」的办法：
    预先算出混合结果，再用实色贴图。
    """
    x1, y1, x2, y2 = [int(v) for v in box]
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    base = bg_img.crop((x1, y1, x1 + w, y1 + h)).convert('RGB')
    col = hex2rgb(fill or theme['surface'])
    over = Image.new('RGB', (w, h), col)
    mixed = Image.blend(base, over, alpha)

    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=radius, fill=255)
    out = base.copy()
    out.paste(mixed, (0, 0), mask)

    # 顶部一道极淡的高光，玻璃质感的关键
    hl = Image.new('L', (w, h), 0)
    ImageDraw.Draw(hl).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                                         outline=90, width=1)
    edge = Image.new('RGB', (w, h), hex2rgb(lighten(fill or theme['surface'], 0.35)))
    out.paste(edge, (0, 0), hl)
    return out


def make_shadow(size, radius=14, spread=10, color=(0, 0, 0, 70)):
    """生成一张柔和的投影图（比 Canvas 画线自然）。"""
    w, h = size
    pad = spread * 2
    img = Image.new('RGBA', (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([pad, pad, pad + w, pad + h], radius=radius, fill=color)
    return img.filter(ImageFilter.GaussianBlur(spread))


def avatar_text(name):
    """取头像用字：跳过数字、空白和英文标点，取第一个"有辨识度"的字。

    否则「26詹院」会显示成「2」、「[高]杨涵」会显示成「[」，都不好看。
    中文标点（如「（」）保留 —— 至少比数字有信息量。
    """
    for ch in str(name or '').strip():
        if ch.isspace() or ch.isdigit():
            continue
        if ch.isascii() and not ch.isalnum():
            continue
        return ch
    s = str(name or '').strip()
    return s[0] if s else '?'


def rounded_avatar(text, size, color, fg='#ffffff', font=None):
    """用首字生成圆形头像色块（自绘列表里用来区分发言人/会话）。"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size - 1, size - 1], fill=hex2rgb(color))
    ch = avatar_text(text)
    if ch:
        try:
            from PIL import ImageFont
            f = ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc', int(size * 0.46))
        except Exception:
            f = ImageFont.load_default()
        bb = d.textbbox((0, 0), ch, font=f)
        d.text(((size - (bb[2] - bb[0])) / 2 - bb[0],
                (size - (bb[3] - bb[1])) / 2 - bb[1]), ch, font=f, fill=fg)
    return img


AVATAR_COLORS = ['#5b7cfa', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981',
                 '#06b6d4', '#ef4444', '#6366f1', '#14b8a6', '#f97316']


def avatar_color_for(key):
    """按名字稳定地取一个头像色（同一个人每次颜色一致）。"""
    h = 0
    for ch in str(key):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


# ─────────────────────────── 缓动与插值 ───────────────────────────

def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_out_quart(t):
    return 1 - (1 - t) ** 4


def ease_in_out(t):
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


EASINGS = {'out_cubic': ease_out_cubic, 'out_quart': ease_out_quart,
           'in_out': ease_in_out, 'linear': lambda t: t}


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_color(c1, c2, t):
    """两色之间插值，返回 hex。'丝滑'的核心就是这个。"""
    a = hex2rgb(c1) if isinstance(c1, str) else c1
    b = hex2rgb(c2) if isinstance(c2, str) else c2
    return rgb2hex((a[0] + (b[0] - a[0]) * t,
                    a[1] + (b[1] - a[1]) * t,
                    a[2] + (b[2] - a[2]) * t))


class Animator:
    """极简动画驱动：给整个 Surface 共用一条 tk after 循环。

    每个动画是一个 dict：{key, t0, dur, ease, on_frame, on_done}。
    同一 key 重复注册会替换旧的 —— 这样鼠标快速划入划出也不会叠加出乱动。
    """

    FPS_MS = 16          # ≈60fps；实测 tkinter 能跟上且 CPU 占用可接受

    def __init__(self, widget):
        self.widget = widget
        self._items = {}
        self._job = None
        self._seq = 0
        self.errors = []      # 帧回调异常留存，便于界面上提示/排查

    def animate(self, key, dur=0.16, ease='out_cubic', on_frame=None, on_done=None):
        self._seq += 1
        self._items[key] = {
            't0': None, 'dur': max(0.001, dur),
            'ease': EASINGS.get(ease, ease_out_cubic),
            'on_frame': on_frame, 'on_done': on_done, 'id': self._seq,
        }
        self._ensure_running()

    def stop(self, key):
        self._items.pop(key, None)

    def stop_prefix(self, prefix):
        for k in [k for k in self._items if str(k).startswith(prefix)]:
            self._items.pop(k, None)

    def clear(self):
        self._items.clear()

    def _ensure_running(self):
        if self._job is None:
            self._job = self.widget.after(self.FPS_MS, self._tick)

    def _tick(self):
        import time as _t
        now = _t.perf_counter()
        done = []
        for key, it in list(self._items.items()):
            if it['t0'] is None:
                it['t0'] = now
            p = min(1.0, (now - it['t0']) / it['dur'])
            v = it['ease'](p)
            try:
                if it['on_frame']:
                    it['on_frame'](v)
            except Exception:
                # 帧回调异常不能让它中断整个动画循环，但也不能不管：
                # 这里收集起来，由调用方通过 last_error 取用（见 Surface.anim_errors）
                self.errors.append(f'{key}: {_sys.exc_info()[1]!r}')
                if len(self.errors) > 20:
                    del self.errors[:10]
                done.append(key)
                continue
            if p >= 1.0:
                done.append(key)
        for key in done:
            it = self._items.pop(key, None)
            if it and it.get('on_done'):
                try:
                    it['on_done']()
                except Exception:
                    pass
        if self._items:
            self._job = self.widget.after(self.FPS_MS, self._tick)
        else:
            self._job = None
