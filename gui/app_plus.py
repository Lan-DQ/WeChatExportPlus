# -*- coding: utf-8 -*-
"""微信聊天记录批量导出工具 v2.2.0 —— 自绘界面版。

功能与 v2.0.0 一致（获取密钥 / 连接数据库 / 勾选会话 / 批量导出 /
进度取消 / 预览 / 编辑给 AI 的指令），界面层是重写的：
  · 背景：渐变 + 柔和光晕（也支持放自己的图，见 ui_theme.find_bg_image）
  · 面板：玻璃拟态（背景与面板色混合后切圆角）
  · 控件：全部 Canvas 自绘，圆角走 PIL 超采样抗锯齿，悬停/按下带缓动
  · 深浅主题可切换，选择会记住
  · 导出目录、工作目录都会记住，下次开程序不用重选

v2.2.0 相对 v2.1.0 修的问题（都影响可用性）：
  · 所有按钮都点不动 —— tkinter 的 event.type 是 EventType 枚举而不是 int，
    查表永远查不到，事件被静默丢弃
  · 背景在正常启动路径下从未被画出来
  · 重绘时旧行图元不清理，反复重绘会叠加（"窗口一闪就多一个控件"）
  · 页面重建后旧控件仍留在输入名单里，用旧状态操作新画布
  · 滚轮事件没登记进映射表，只能拖滚动条
  · 复选框图用了单槽缓存，导致整个列表的勾选状态显示串味
  · 关窗口被数据库清理阻塞（现在先关窗，清理转后台）

界面层全部在 gui/ui_theme.py 与 gui/ui_widgets.py，本文件只负责业务编排。
"""
import datetime
import ctypes
import os
import re
import shutil
import sys
import threading
import tkinter as tk

# ── 路径解析（PyInstaller onefile 下 __file__ 在临时解包目录，必须以 exe 为准）──
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPTS_DIR = os.path.join(BASE, 'scripts')
EXPORTERS_DIR = os.path.join(BASE, 'exporters')
GUI_DIR = os.path.join(BASE, 'gui')
for _p in (SCRIPTS_DIR, EXPORTERS_DIR, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import ui_theme as T            # noqa: E402
import ui_widgets as W          # noqa: E402

ROOT = BASE
DEFAULT_OUT = os.path.join(os.environ.get('USERPROFILE', BASE), 'Desktop', 'wx_export')
OUT = DEFAULT_OUT
KEY_FILE = os.path.join(OUT, 'key.txt')
SETTINGS_FILE = os.path.join(ROOT, '.ui_settings')

APP_TITLE = '微信聊天记录批量导出工具'
APP_VERSION = 'v2.2.0'

FORMAT_CHOICES = [
    ('Markdown 单文件（推荐）', 'md'),
    ('AI 语料 JSONL（适合入库/RAG）', 'ai'),
    ('HTML（气泡页面，含图片）', 'html'),
    ('Excel（.xlsx 表格）', 'excel'),
    ('CSV（.csv 表格）', 'csv'),
    ('PDF（含内嵌图片）', 'pdf'),
    ('TXT（纯文本）', 'txt'),
    ('JSON（结构化）', 'json'),
]
FORMAT_HINTS = {
    'md': '每个会话一个 .md，直接放在导出目录下，可拖进 DeepSeek 网页版',
    'ai': '每个会话一个文件夹，含 对话.jsonl（带发言人/类型/图片路径）',
    'html': '每个会话一个文件夹，含 index.html + 图片\\',
    'excel': '每个会话一个文件夹，含 <会话名>.xlsx',
    'csv': '每个会话一个文件夹，含 对话.csv',
    'pdf': '每个会话一个文件夹，含 <会话名>.pdf（图片内嵌）',
    'txt': '每个会话一个文件夹，含 对话.txt',
    'json': '每个会话一个文件夹，含 对话.json',
}
FORMAT_DEFAULT = 'md'


def load_saved_key():
    for path in (KEY_FILE, os.path.join(os.environ.get('USERPROFILE', 'C:'),
                                       'Desktop', 'wechat_export', 'WeChat', 'key.txt')):
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8', errors='ignore') as f:
                    k = f.read().strip()
                if len(k) == 64:
                    return k
            except OSError:
                pass
    return ''


def find_xwechat_dirs():
    home = os.environ.get('USERPROFILE', 'C:')
    cands = [os.path.join(home, 'Documents', 'xwechat_files'),
             os.path.join(home, 'Documents', 'WeChat Files')]
    for letter in 'CDEFGH':
        for sub in ('xwechat_files', 'WeChat Files',
                    r'wxxinxi\xwechat_files', r'储存信息\xwechat_files'):
            cands.append(f'{letter}:\\{sub}')
    for d in cands:
        if os.path.isdir(d):
            try:
                for e in os.listdir(d):
                    if e.startswith('wxid_') and os.path.isdir(os.path.join(d, e)):
                        return d
            except OSError:
                pass
    return ''


def clean_wxid(wxid):
    parts = str(wxid or '').split('_')
    if len(parts) >= 3 and parts[0] == 'wxid':
        return '_'.join(parts[:2])
    return str(wxid or '')


def load_settings():
    d = {'theme': 'light'}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                for line in f:
                    if '=' in line:
                        k, v = line.split('=', 1)
                        d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def save_settings(d):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            for k, v in d.items():
                f.write(f'{k}={v}\n')
    except OSError:
        pass


class App:
    # 页面布局常量
    W, H = 1060, 700

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f'{APP_TITLE} {APP_VERSION}')
        self.root.geometry(f'{self.W}x{self.H}')
        self.root.minsize(940, 640)
        self._set_icon()

        self.settings = load_settings()
        self.theme = T.THEMES.get(self.settings.get('theme', 'light'), T.THEMES['light'])
        # 覆盖模式开关（记住用户上次的选择）
        self._clear_before = self.settings.get('clear_before', '0') == '1'

        self.key = load_saved_key() or None
        self.wcdb = None
        self.sessions = []
        self.nick_map = {}
        self.busy = False
        self.page = 'home'
        self._widgets = []          # 需要在重绘时重建的自绘控件
        self._entries = []          # 真实 tk.Entry（叠在 Canvas 上，必须显式销毁）
        self._sel_item = None       # 「已选 N 个」文本图元 id（只建一次，避免堆积）
        self._key_visible = False
        self._toast_job = None

        self.sf = W.Surface(self.root, self.theme,
                            T.find_bg_image(BASE))
        self.sf.font = T.pick_font(self.root)
        self.root.update_idletasks()
        self.sf.redraw_bg()

        self.root.protocol('WM_DELETE_WINDOW', self.quit_app)
        self.root.bind('<Configure>', self._on_resize)
        self.root.bind('<Escape>', lambda e: self._close_popups())

        self.build_home()

    # ────────────────────── 基础设施 ──────────────────────

    def _set_icon(self):
        self._ico = ''
        for p in (os.path.join(BASE, 'icon.ico'), os.path.join(GUI_DIR, 'icon.ico')):
            if os.path.exists(p):
                try:
                    self.root.iconbitmap(p)
                    self._ico = p
                    break
                except Exception:
                    pass

    def _clear_page(self):
        """切换页面前彻底清场。

        三个必须都做，少一个就会出现"新页面画在旧页面上"的重叠：
          1. 删掉除背景外的所有 Canvas 图元（不能靠 tag —— 手画的图元没打标签）；
          2. 销毁真实 tk 控件（Entry 是叠在 Canvas 上的，不随图元一起消失）；
          3. 清空自绘控件的引用表与动画队列（否则旧动画还在跑，去操作已删除的图元）。
        """
        cv = self.sf.canvas
        for iid in cv.find_all():
            if '__bg' not in cv.gettags(iid):
                cv.delete(iid)
        # 自绘控件都注册在 Surface 上当输入消费者，切页/重建时必须注销干净。
        # 只遍历 self._widgets 是不够的：CheckList 等不一定被登记进去，
        # 残留的旧实例会继续收事件（用旧的坐标和状态去操作新画布），
        # 表现为"某一行/某个控件行为诡异"。
        self.sf.unregister_all_input()
        for w in self._widgets:
            if hasattr(w, 'unbind'):
                try:
                    w.unbind()
                except Exception:
                    pass
        for w in self._entries:
            try:
                w.destroy()
            except Exception:
                pass
        self._entries.clear()
        self._widgets.clear()
        self._sel_item = None      # 旧图元已被删除，引用必须清掉
        if getattr(self, '_anim', None):
            self.sf.anim.clear()
        self.sf.canvas.configure(cursor='')

    def _on_resize(self, e):
        if e.widget is not self.root:
            return
        if (self.W, self.H) == (e.width, e.height):
            return
        self.W, self.H = e.width, e.height
        # 拖动窗口时不必每像素重绘，停顿后再重排
        if getattr(self, '_rs_job', None):
            self.root.after_cancel(self._rs_job)
        self._rs_job = self.root.after(140, self._relayout)

    def _relayout(self):
        """窗口尺寸变了，整页重排。

        ⚠️ 必须走 _clear_page()，不能只 canvas.delete('all')：
        早期这里只删图元不清控件表，结果
          · 旧控件仍留在输入消费者名单里 —— 一次点击被新旧两份控件同时响应；
          · 旧 CheckList 的 hover_row 等状态残留，新画的一遍按旧索引判悬停，
            表现为"某一行固定显示错"；
          · 展开中的下拉框、悬浮提示等图元被删但对象还在。
        用户可见现象是"小窗正常、一全屏导出格式框变成两个"。
        """
        self._rs_job = None
        self._clear_page()          # 删图元 + 注销控件 + 清动画（幂等）
        self.sf.size = (0, 0)       # 让背景按新尺寸重算
        self.sf.redraw_bg()
        (self.build_home if self.page == 'home' else self.build_sessions)()

    def _close_popups(self):
        for w in self._widgets:
            if hasattr(w, 'close'):
                w.close()

    def toast(self, msg, kind='info', ms=2600):
        """右下角浮出提示，几秒后自己消失（比弹窗打断感小）。"""
        th = self.theme
        col = {'info': th['accent'], 'ok': th['ok'], 'err': th['err'],
               'warn': th['warn']}.get(kind, th['accent'])
        cv = self.sf.canvas
        cv.delete('toast')
        w, h = 340, 46
        x2, y2 = self.W - 26, self.H - 26
        x1, y1 = x2 - w, y2 - h
        self.sf.draw_panel(x1, y1, x2, y2, 12, 0.96, border=True, tags='toast')
        T.round_rect_items(cv, x1, y1, x1 + 4, y2, 2, col, '', 0, tags=('toast',))
        cv.create_text(x1 + 20, (y1 + y2) / 2, text=msg, anchor='w',
                       fill=th['text'], font=(self.sf.font, 10), tags='toast')

        # 淡入
        start_y = y2 + 30

        def frame(v):
            cv.delete('toast')
            yy = start_y + (y1 - start_y) * v
            self.sf.draw_panel(x1, yy, x2, yy + h, 12, 0.96, border=True,
                               tags='toast')
            T.round_rect_items(cv, x1, yy, x1 + 4, yy + h, 2, col, '', 0,
                               tags=('toast',))
            cv.create_text(x1 + 20, yy + h / 2, text=msg, anchor='w',
                           fill=th['text'], font=(self.sf.font, 10), tags='toast')

        self.sf.anim.animate('toastin', 0.22, 'out_cubic', frame)
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(ms, lambda: cv.delete('toast'))

    def busy_on(self, text='处理中…'):
        self.busy = True
        cv = self.sf.canvas
        cv.delete('busy')
        w, h = 260, 76
        x1, y1 = (self.W - w) / 2, (self.H - h) / 2
        self.sf.draw_panel(x1, y1, x1 + w, y1 + h, 14, 0.97, tags='busy')
        cv.create_text(x1 + w / 2, y1 + 28, text=text, fill=self.theme['text'],
                       font=(self.sf.font, 11, 'bold'), tags='busy')
        pb = W.ProgressBar(self.sf, x1 + 30, y1 + 48, w - 60, 8)
        pb.set_max(100)
        self._busy_bar = pb
        self._busy_phase = 0

        def pulse():
            if not self.busy:
                return
            self._busy_phase = (self._busy_phase + 9) % 100
            pb.set(self._busy_phase, animate=False)
            self.root.after(90, pulse)

        pulse()

    def busy_off(self):
        self.busy = False
        self.sf.canvas.delete('busy')

    # ────────────────────── 通用绘制片段 ──────────────────────

    def draw_header(self, subtitle):
        th = self.theme
        self.sf.text(38, 44, APP_TITLE, 18, th['text'], True)
        self.sf.text(38, 70, subtitle, 10, th['text_dim'])
        lbl = '🌙 深色' if self.theme['name'] == 'light' else '☀ 浅色'
        self._widgets.append(
            W.Button(self.sf, self.W - 150, 34, 112, 38, lbl, kind='ghost',
                     font_size=10, command=self.toggle_theme, hover_dur=0.12))

    def toggle_theme(self):
        name = 'dark' if self.theme['name'] == 'light' else 'light'
        self.theme = T.THEMES[name]
        self.settings['theme'] = name
        save_settings(self.settings)
        # 行底色/复选框的图里烤着颜色，换主题必须让它们重画
        for w in self._widgets:
            if hasattr(w, '_clear_photo_cache'):
                try:
                    w._clear_photo_cache()
                except Exception:
                    pass
        self.sf.set_theme(self.theme)
        self.sf.font = T.pick_font(self.root)
        self._widgets.clear()
        (self.build_home if self.page == 'home' else self.build_sessions)()

    def _fit_text(self, s, max_px, size):
        """按像素宽度裁剪文本，超出时保留开头与结尾（路径的关键信息在两头）。

        直接按字符数截断会把 'C:\\Users\\LanDeQuan\\Documents\\xwechat_files'
        变成 '...eQuan\\Documents\\xwechat_files' —— 开头最关键的部分反而没了。
        """
        s = str(s or '')
        if not s:
            return ''
        try:
            import tkinter.font as tkfont
            f = tkfont.Font(family=self.sf.font, size=size)
            if f.measure(s) <= max_px:
                return s
            ell = '…'
            lo, hi = 0, len(s)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                head = mid // 2 + mid % 2
                tail = mid - head
                cand = s[:head] + ell + (s[-tail:] if tail else '')
                if f.measure(cand) <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            head = lo // 2 + lo % 2
            tail = lo - head
            return s[:head] + ell + (s[-tail:] if tail else '')
        except Exception:
            return s if len(s) <= 26 else s[:13] + '…' + s[-12:]

    def draw_info_card(self, x, y, w, title, value, note, ok=True, on_click=None):
        """一张信息卡。on_click 不为空时右下角出现「浏览」按钮。"""
        th = self.theme
        self.sf.draw_panel(x, y, x + w, y + 96, 14, 0.82)
        self.sf.text(x + 18, y + 24, title, 9, th['text_dim'])
        avail = w - 36 - (86 if on_click else 0)
        self.sf.text(x + 18, y + 50, self._fit_text(value, avail, 11),
                     11, th['text'], True)
        self.sf.text(x + 18, y + 74, ('✓ ' if ok else '· ') + note, 9,
                     th['ok'] if ok else th['text_dim'])
        if on_click:
            self._widgets.append(
                W.Button(self.sf, x + w - 84, y + 60, 66, 26, '浏览',
                         kind='ghost', font_size=9, radius=8,
                         command=on_click, hover_dur=0.12))

    # ────────────────────── 首页 ──────────────────────

    def build_home(self):
        self.page = 'home'
        self._clear_page()
        th = self.theme
        cv = self.sf.canvas

        self.draw_header('批量勾选会话 · 一键导出 AI 语料')

        detected = find_xwechat_dirs()
        self.data_dir = detected or getattr(self, 'data_dir', '')
        # 工作目录记忆：上次选过的优先（选择的目录才好记，桌面是默认值不算）
        saved_out = self.settings.get('out_root', '')
        self.out_root = getattr(self, 'out_root',
                                saved_out if saved_out and os.path.isdir(saved_out)
                                else DEFAULT_OUT)

        gap, margin = 20, 38
        cw = (self.W - margin * 2 - gap * 2) // 3
        top = 106
        self.draw_info_card(margin, top, cw, '微信数据目录',
                            self.data_dir or '未检测到',
                            '已自动检测' if detected else '请点「浏览」选择',
                            bool(detected), self.pick_data_dir)
        self.draw_info_card(margin + cw + gap, top, cw, '导出工作目录',
                            self.out_root, '密钥与日志存放处', True,
                            self.pick_out_dir)
        has_key = len(self.key_input_value() or '') == 64
        self.draw_info_card(margin + (cw + gap) * 2, top, cw, '数据库密钥',
                            ('•' * 20) if has_key else '未设置',
                            '已加载保存的密钥' if has_key else '点下方「获取密钥」',
                            has_key)

        # 密钥输入
        ey = top + 118
        self.sf.draw_panel(margin, ey, self.W - margin, ey + 96, 14, 0.82)
        self.sf.text(margin + 18, ey + 24, '密钥', 9, th['text_dim'])
        self.key_var = tk.StringVar(value=self.key_input_value())
        self._key_entry = W.Entry(self.sf, margin + 18, ey + 34,
                                  self.W - margin * 2 - 140, 32,
                                  textvariable=self.key_var,
                                  show='' if self._key_visible else '*')
        self._entries.append(self._key_entry)
        self._key_entry.entry.bind('<KeyRelease>', lambda e: self._on_key_changed())
        self.key_var.trace_add('write', lambda *_: self._on_key_changed())
        self._widgets.append(W.Button(
            self.sf, self.W - margin - 110, ey + 34, 92, 32,
            '隐藏' if self._key_visible else '显示', kind='ghost', font_size=9,
            radius=8, command=self.toggle_key_show, hover_dur=0.12))
        self.sf.text(margin + 18, ey + 80,
                     '点「获取密钥」自动捕获，或直接粘贴 64 位密钥', 8, th['text_faint'])

        # 三个主操作
        by = ey + 124
        bw, bh = 240, 46
        bx = (self.W - bw) // 2
        self.btn_key = W.Button(self.sf, bx, by, bw, bh, '获取密钥', kind='primary',
                                icon='🔑', font_size=12, command=self.do_getkey)
        self.btn_conn = W.Button(self.sf, bx, by + bh + 12, bw, bh, '连接数据库',
                                 kind='primary', icon='🗄', font_size=12,
                                 command=self.do_connect)
        self.btn_browse = W.Button(self.sf, bx, by + (bh + 12) * 2, bw, bh,
                                   '浏览会话', kind='primary', icon='📤',
                                   font_size=12, command=self.build_sessions)
        self._widgets += [self.btn_key, self.btn_conn, self.btn_browse]

        self.status_y = by + (bh + 12) * 3 + 14
        self.sf.text(self.W / 2, self.status_y, self._status_text(), 9,
                     th['text_dim'], anchor='center', tags='status')

        if self.sessions:
            self.btn_conn.set_state('normal')
            self.btn_browse.set_state('normal')
        if len(self.key_input_value() or '') == 64:
            self.btn_conn.set_state('normal')
            self.status('已加载保存的密钥', 'ok')

        # 启动后自动填一次密钥
        if not self.key_var.get():
            saved = load_saved_key()
            if saved:
                self.key_var.set(saved)
                self.key = saved

    def _status_text(self):
        return getattr(self, '_status', '就绪')

    def status(self, msg, kind='info'):
        self._status = msg
        col = {'info': self.theme['text_dim'], 'ok': self.theme['ok'],
               'err': self.theme['err'], 'warn': self.theme['warn']}.get(
                   kind, self.theme['text_dim'])
        try:
            self.sf.canvas.itemconfigure('status', text=msg, fill=col)
        except Exception:
            pass

    def key_input_value(self):
        v = getattr(self, 'key_var', None)
        if v:
            return v.get().strip()
        return self.key or ''

    def _on_key_changed(self):
        ok = bool(re.fullmatch(r'[0-9a-fA-F]{64}', self.key_input_value()))
        if getattr(self, 'btn_conn', None):
            self.btn_conn.set_state('normal' if ok else 'disabled')

    def toggle_key_show(self):
        self._key_visible = not self._key_visible
        self._key_entry.set_show('' if self._key_visible else '*')

    def pick_data_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.data_dir or '/')
        if d:
            self.data_dir = d
            self.build_home()

    def pick_out_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.out_root or '/')
        if not d:
            return
        global OUT, KEY_FILE
        self.out_root = d
        try:
            os.makedirs(d, exist_ok=True)
            OUT = d
            KEY_FILE = os.path.join(OUT, 'key.txt')
        except OSError as e:
            self.toast(f'目录不可写：{e}', 'err')
            return
        # 记住这个目录，下次开程序直接用
        self.settings['out_root'] = d
        save_settings(self.settings)
        self.build_home()
        self.toast('导出工作目录已更新', 'ok')

    # ────────────────────── 获取密钥 / 连接 ──────────────────────

    def do_getkey(self):
        if self.busy:
            return
        from tkinter import messagebox
        if not messagebox.askokcancel('准备', (
                '1. 关闭微信电脑端（右键系统托盘 → 退出）\n'
                '2. 点确定后等待\n'
                '3. 看到「等待微信启动」后打开微信\n'
                '4. 微信启动过程中自动捕获密钥')):
            return
        threading.Thread(target=self._getkey, daemon=True).start()

    def _getkey(self):
        node = self._find_node()
        js = os.path.join(ROOT, 'scripts', 'get_key.js')
        if not node or not os.path.exists(js):
            self.root.after(0, lambda: self.toast(f'找不到运行时: {node}', 'err'))
            return
        self.root.after(0, lambda: self.busy_on('等待捕获密钥…'))
        status_file = os.path.join(OUT, 'key_status.txt')
        for p in (status_file, KEY_FILE):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', node, f'"{js}"', None, 1)
        if ret <= 32:
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self.toast('提权失败，请手动以管理员运行', 'err'))
            return

        status_map = {
            'started': '脚本已启动', 'dll_found': '找到 wx_key.dll',
            'dll_loaded': 'DLL 加载成功', 'dll_not_found': '找不到 wx_key.dll',
            'waiting_close': '等待微信关闭…', 'timeout_close': '关微信超时',
            'waiting_start': '等待微信启动…（请打开微信）', 'timeout_start': '等微信启动超时',
            'injecting': '正在注入 Hook…', 'hook_ok': 'Hook 成功，等登录捕获',
            'hook_failed': 'Hook 注入失败', 'polling': '等待登录中捕获…',
            'timeout_poll': '获取超时', 'captured': '已捕获',
        }
        for _ in range(180):
            if os.path.exists(KEY_FILE):
                try:
                    with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                        k = f.read().strip()
                except OSError:
                    k = ''
                if len(k) == 64:
                    self.root.after(0, self.busy_off)
                    self.root.after(0, lambda kk=k: self._key_captured(kk))
                    return
            if os.path.exists(status_file):
                try:
                    with open(status_file, encoding='utf-8', errors='ignore') as f:
                        st = f.read().strip()
                    msg = status_map.get(st.split(':')[0], st)
                    self.root.after(0, lambda m=msg: self.status('密钥：' + m))
                except OSError:
                    pass
            threading.Event().wait(1)
        self.root.after(0, self.busy_off)
        self.root.after(0, lambda: self.toast('密钥获取失败', 'err'))

    def _key_captured(self, k):
        self.key = k
        self.key_var.set(k)
        self.build_home()
        self.toast('密钥获取成功', 'ok')

    def _find_node(self):
        n = os.path.join(ROOT, 'runtime', 'node.exe')
        return n if os.path.exists(n) else (shutil.which('node') or '')

    def do_connect(self):
        if self.busy:
            return
        manual = self.key_input_value()
        if len(manual) == 64:
            self.key = manual
        elif not (self.key and len(self.key) == 64) and os.path.exists(KEY_FILE):
            try:
                with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                    self.key = f.read().strip()
            except OSError:
                pass
        if not self.key or len(self.key) != 64:
            self.toast('密钥无效，请先获取或粘贴 64 位密钥', 'err')
            return
        self.busy_on('连接数据库…')
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            os.makedirs(OUT, exist_ok=True)
            with open(KEY_FILE, 'w', encoding='utf-8') as f:
                f.write(self.key)
            from wcdb_server import WCDBClient
            cli = WCDBClient()
            cli.start(self.key, self.data_dir)
            if getattr(self, '_closing', False):
                # 启动期间用户关窗了。quit_app 已经把手里的 wcdb 置空去关了，
                # 这个刚建好的没人管 —— 不收掉它会残留进程占着目录。
                try:
                    cli.stop()
                except Exception:
                    pass
                return
            self.wcdb = cli
            self.sessions = self.wcdb.get_sessions() or []
            users = [s.get('username', '') for s in self.sessions
                     if s.get('username') and not str(s.get('username')).startswith('brand')]
            if users:
                try:
                    self.nick_map = self.wcdb.get_display_names(users[:500]) or {}
                except Exception:
                    self.nick_map = {}
            n = len(self.sessions)
            self.last_error = ''
            if getattr(self, '_closing', False):
                return
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self._connected(n))
        except Exception as e:
            import traceback
            err = str(e)
            # 留存完整堆栈：界面上给用户看简短版，自动化测试里能取到完整原因
            self.last_error = f'{err}\n{traceback.format_exc()}'
            if getattr(self, '_closing', False):
                return          # 窗口已销毁，再 after() 会抛 TclError
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self.toast(f'连接失败：{err[:60]}', 'err'))

    def _connected(self, n):
        self.build_home()
        self.toast(f'已连接数据库，共 {n} 个会话', 'ok')

    # ────────────────────── 会话页 ──────────────────────

    def _session_items(self):
        items = []
        for s in self.sessions:
            w = s.get('username', '') or ''
            if not w:
                continue
            name = self.nick_map.get(w) or s.get('last_sender_display_name') or w
            ts = s.get('last_timestamp', s.get('sort_timestamp', ''))
            if str(ts).isdigit():
                try:
                    ts = datetime.datetime.fromtimestamp(int(ts)).strftime('%m-%d %H:%M')
                except (ValueError, OSError):
                    pass
            items.append({'wxid': w, 'title': str(name)[:34],
                          'preview': str(s.get('summary', '') or '')[:46],
                          'time': str(ts)[:16],
                          'group': str(w).endswith('@chatroom'),
                          'sel': w in getattr(self, '_selected', set())})
        # 有消息的排前面
        items.sort(key=lambda it: 0 if it['preview'] else 1)
        return items

    def build_sessions(self):
        if not self.sessions:
            self.toast('还没有会话数据，请先「连接数据库」', 'warn')
            return
        self.page = 'sessions'
        self._clear_page()
        th = self.theme
        if not hasattr(self, '_selected'):
            self._selected = set()

        self.draw_header(f'共 {len(self.sessions)} 个会话 · 勾选后统一导出')
        # 选中数量显示在标题栏右侧（不占列表表头，避免和勾选框挤在一起）
        self.sf.text(self.W - 176, 66, '尚未勾选', 10, th['accent'], bold=True,
                     anchor='e', tags='selhint')

        # 返回 + 选择操作
        top = 96
        self._widgets.append(W.Button(self.sf, 38, top, 92, 34, '← 返回', kind='ghost',
                                      font_size=10, radius=9, command=self.build_home,
                                      hover_dur=0.12))
        bx = 142
        for label, cb in (('全选', lambda: self._sel_all(True)),
                          ('全不选', lambda: self._sel_all(False)),
                          ('反选', self._sel_invert),
                          ('全选搜索结果', lambda: self._sel_all(True, True))):
            w_ = 108 if label == '全选搜索结果' else 74
            self._widgets.append(W.Button(self.sf, bx, top, w_, 34, label,
                                          kind='ghost', font_size=10, radius=9,
                                          command=cb, hover_dur=0.12))
            bx += w_ + 8

        # 搜索框（放在列表右上）
        self.search_var = tk.StringVar()
        self._search_entry = W.Entry(self.sf, self.W - 38 - 300, top, 300, 34,
                                     textvariable=self.search_var)
        self._entries.append(self._search_entry)
        self.search_var.trace_add('write', lambda *_: self._on_search())
        self._search_ph = self.sf.text(self.W - 38 - 300 + 14, top + 17,
                                       '搜索会话…', 10, th['text_faint'])

        # 列表
        list_top = top + 48
        list_h = self.H - list_top - 200
        self.list = W.CheckList(self.sf, 38, list_top, self.W - 76, list_h,
                                on_toggle=self._on_toggle, on_open=self.show_chat)
        self.list.set_items(self._session_items())

        # 底部导出栏（高度要够放：标签+输入框 / 勾选行 / 提示行，共 3 行）
        BAR_H = 132
        by = self.H - BAR_H - 18
        self.sf.draw_panel(38, by, self.W - 38, by + BAR_H, 14, 0.88)
        self.sf.text(56, by + 22, '导出格式', 9, th['text_dim'])
        self._fmt = W.Dropdown(self.sf, 56, by + 32, 300, 34,
                               [lbl for lbl, _ in FORMAT_CHOICES],
                               value=next(lbl for lbl, k in FORMAT_CHOICES
                                          if k == getattr(self, '_fmt_key',
                                                          FORMAT_DEFAULT)),
                               on_change=self._on_fmt_change)
        self.sf.text(374, by + 22, '导出到', 9, th['text_dim'])
        # 导出目录记忆：优先用上次选过的（存在设置里），否则默认桌面
        default_root = os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop')
        saved = self.settings.get('export_root', '')
        self.exp_root = getattr(self, 'exp_root',
                                saved if saved and os.path.isdir(saved) else default_root)
        self._exp_entry = W.Entry(self.sf, 374, by + 32, 300, 34)
        self._exp_entry.set(self.exp_root)
        self._entries.append(self._exp_entry)
        self._widgets.append(W.Button(self.sf, 682, by + 32, 74, 34, '浏览',
                                      kind='ghost', font_size=10, radius=9,
                                      command=self.pick_export_root, hover_dur=0.12))

        # 覆盖选项：勾上后每次导出前先清掉上一次的导出内容，省磁盘
        self._clear_cb = W.Checkbox(
            self.sf, 56, by + 78, 360,
            '导出并清除之前的导出内容（覆盖，省空间）',
            value=bool(getattr(self, '_clear_before', False)),
            on_change=self._on_clear_toggle, font_size=9)

        self.btn_export = W.Button(self.sf, self.W - 38 - 220, by + 26, 220, 44,
                                   '开始导出', kind='primary', icon='📦',
                                   font_size=13, command=self.do_export)
        self.btn_prompt = W.Button(self.sf, self.W - 38 - 220, by + 78, 220, 28,
                                   '✏ 编辑给 AI 的指令', kind='ghost', font_size=9,
                                   radius=8, command=self.edit_prompt)
        self._widgets += [self.btn_export, self.btn_prompt]

        # 提示放独立一行，靠右对齐到按钮左边界，避免与按钮重叠
        self._fmt_hint = self.sf.text(56, by + 110,
                                      FORMAT_HINTS.get(getattr(self, '_fmt_key',
                                                               FORMAT_DEFAULT), ''),
                                      8, th['text_faint'])
        # 这三个也是输入消费者，必须登记以便页面切换时注销（否则旧实例残留收事件）
        self._widgets += [self.list, self._fmt, self._clear_cb]
        self._update_sel_label()

    def _on_clear_toggle(self, value):
        self._clear_before = bool(value)
        self.settings['clear_before'] = '1' if value else '0'
        save_settings(self.settings)
        if value:
            self.toast('开启后，每次导出会先删除上一次的导出内容', 'warn', 3200)

    def _on_fmt_change(self, label):
        self._fmt_key = dict(FORMAT_CHOICES)[label]
        try:
            self.sf.canvas.itemconfigure(self._fmt_hint,
                                         text=FORMAT_HINTS.get(self._fmt_key, ''))
        except Exception:
            pass

    def _on_search(self):
        if getattr(self, '_search_entry', None):
            has = bool(self.search_var.get())
            try:
                self.sf.canvas.itemconfigure(self._search_ph, state='hidden' if has
                                             else 'normal')
            except Exception:
                pass
        if getattr(self, 'list', None):
            self.list.refresh_view(self.search_var.get())
            self._update_sel_label()

    def _on_toggle(self, item):
        if item.get('sel'):
            self._selected.add(item['wxid'])
        else:
            self._selected.discard(item['wxid'])
        self._update_sel_label()

    def _update_sel_label(self):
        """把「已选 N 个」写进顶部标题栏（而不是列表表头）。

        表头已经有「共 N 个 · 已选 M 个」（CheckList 自己画的），
        重复显示既冗余又会和勾选框挤在一起。
        """
        n = len(self._selected)
        try:
            self.sf.canvas.itemconfigure(
                'selhint', text=(f'已选 {n} 个会话' if n else '尚未勾选'))
        except Exception:
            pass

    def _sel_all(self, value, only_visible=False):
        self.list.set_all(value, only_visible)
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()

    def _sel_invert(self):
        self.list.invert()
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()

    def pick_export_root(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.exp_root or '/')
        if d:
            self.exp_root = d
            self._exp_entry.set(d)
            self._remember_export_root(d)

    def _remember_export_root(self, path):
        """把导出目录记进设置，下次开程序直接用它。"""
        if not path:
            return
        self.settings['export_root'] = path
        try:
            save_settings(self.settings)
        except Exception:
            pass

    # ────────────────────── 导出 ──────────────────────

    def do_export(self):
        if self.busy:
            self.toast('正在导出中，请稍候', 'warn')
            return
        if not self.wcdb:
            self.toast('请先连接数据库', 'err')
            return
        picked = [it for it in self.list.items if it.get('sel')]
        if not picked:
            self.toast('请先勾选要导出的会话', 'warn')
            return
        self.exp_root = self._exp_entry.get().strip()
        if not self.exp_root:
            self.toast('请选择导出位置', 'err')
            return
        self._remember_export_root(self.exp_root)   # 手输的也记住
        try:
            os.makedirs(self.exp_root, exist_ok=True)
        except OSError as e:
            self.toast(f'无法创建导出目录：{e}', 'err')
            return
        fmt = getattr(self, '_fmt_key', FORMAT_DEFAULT)
        clear_before = bool(getattr(self, '_clear_before', False))

        # 覆盖前先算清楚会删什么，让用户确认 —— 导出目录可能选在桌面这种地方，
        # 闷声删除是不可接受的
        if clear_before:
            import batch_export as _be
            info = _be.scan_previous_exports(self.exp_root)
            if info['dirs'] or info['files']:
                mb = info['bytes'] / 1048576
                names = '\n'.join('  · ' + d for d in info['dirs'][:8])
                if len(info['dirs']) > 8:
                    names += f'\n  · …另外 {len(info["dirs"]) - 8} 个'
                from tkinter import messagebox
                if not messagebox.askokcancel(
                        '确认覆盖',
                        f'将删除 {self.exp_root} 里上一次导出的内容：\n\n'
                        f'{names}\n'
                        f'{("  · " + chr(10).join("  · " + f for f in info["files"])) if info["files"] else ""}\n\n'
                        f'共占用约 {mb:.1f} MB。\n\n'
                        '只删本工具生成的「导出_日期_时间」目录和索引文件，\n'
                        '其它文件一律不动。\n\n确定继续？'):
                    return
            else:
                self.toast('没有找到上一次的导出内容', 'info', 2000)

        self._open_export_window(
            [{'wxid': it['wxid'], 'title': it['title']} for it in picked],
            fmt, self.exp_root, clear_before=clear_before)

    def _open_export_window(self, picked, fmt, out_root, clear_before=False):
        """导出进度窗（独立 Toplevel，进度条与日志都在上面）。"""
        th = self.theme
        self.busy = True
        win = tk.Toplevel(self.root)
        win.title('导出中…')
        win.geometry('620x400')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=T.rgb2hex(th['bg_top']))
        cv.pack(fill='both', expand=True)
        bg = T.make_background((620, 400), th, '')
        ph = __import__('PIL.ImageTk', fromlist=['PhotoImage']).PhotoImage(bg)
        cv.create_image(0, 0, image=ph, anchor='nw')
        win._bg = ph

        cv.create_text(30, 34, text=f'正在导出 {len(picked)} 个会话', anchor='w',
                       fill=th['text'], font=(self.sf.font, 14, 'bold'))
        cur = cv.create_text(30, 66, text='准备中…', anchor='w',
                             fill=th['accent'], font=(self.sf.font, 10, 'bold'))

        bar_bg = 30, 88, 590, 100
        T.round_rect_items(cv, *bar_bg, 6, T.mix(th['surface2'], th['border'], 0.5),
                           '', 0)

        def set_prog(frac):
            cv.delete('pbar')
            w = (bar_bg[2] - bar_bg[0] - 4) * max(0.0, min(1.0, frac))
            if w > 2:
                T.round_rect_items(cv, bar_bg[0] + 2, bar_bg[1] + 2,
                                   bar_bg[0] + 2 + w, bar_bg[3] - 2, 5,
                                   th['accent'], '', 0, tags=('pbar',))

        set_prog(0)
        cv.create_text(30, 122, text='日志', anchor='w', fill=th['text_dim'],
                       font=(self.sf.font, 9))

        log_bg = (30, 140, 590, 300)
        T.round_rect_items(cv, *log_bg, 10, th['surface'], th['border'], 1)
        log_ids = []

        def add_log(line):
            if len(log_ids) > 9:
                cv.delete(log_ids.pop(0))
                for i, iid in enumerate(log_ids):
                    cv.coords(iid, 44, 162 + i * 15)
            iid = cv.create_text(44, 162 + len(log_ids) * 15, text=str(line)[:78],
                                 anchor='w', fill=th['text_dim'],
                                 font=(self.sf.font, 8))
            log_ids.append(iid)

        cancel = {'v': False}

        # 弹窗内的按钮用 ttk：Toplevel 上叠自绘控件需要自己管重绘，
        # 而弹窗是临时界面，样式差异影响小，优先选稳妥。
        from tkinter import ttk
        bf = tk.Frame(win, bg=T.rgb2hex(th['bg_top']))
        bf.place(x=30, y=316)
        cancel_btn = ttk.Button(bf, text='取消导出', width=12)
        cancel_btn.pack(side='left', padx=(0, 8))
        open_btn = ttk.Button(bf, text='打开导出目录', width=14, state='disabled')
        open_btn.pack(side='left', padx=8)
        close_btn = ttk.Button(bf, text='关闭', width=10, state='disabled')
        close_btn.pack(side='left', padx=8)

        def on_cancel():
            cancel['v'] = True
            cancel_btn.config(state='disabled', text='正在停止…')

        cancel_btn.config(command=on_cancel)

        def on_progress(done, total, title):
            def upd():
                set_prog((done - 1) / max(1, total))
                cv.itemconfigure(cur, text=f'第 {done}/{total} 个：{title}')
            self.root.after(0, upd)

        result = {}

        def worker():
            import batch_export
            try:
                result.update(batch_export.export_sessions(
                    self.wcdb, self.data_dir, picked, fmt, out_root,
                    progress=on_progress,
                    should_cancel=lambda: cancel['v'],
                    log=lambda s: self.root.after(0, lambda ss=s: add_log(ss)),
                    base_dir=BASE,
                    clear_before=clear_before))
            except Exception as e:
                import traceback
                result['fatal'] = f'{e}\n{traceback.format_exc()}'
            self.root.after(0, finish)

        def finish():
            self.busy = False
            set_prog(1.0)
            cancel_btn.config(state='disabled')
            close_btn.config(state='normal', command=win.destroy)
            if 'fatal' in result:
                cv.itemconfigure(cur, text='导出失败', fill=th['err'])
                add_log(result['fatal'].splitlines()[0])
                self.toast('导出失败，详见日志', 'err')
                return
            ok = result.get('ok', [])
            failed = result.get('failed', [])
            root_dir = result.get('root', '')
            cv.itemconfigure(cur,
                             text=(f'已取消（完成 {len(ok)} 个）' if result.get('cancelled')
                                   else f'完成：成功 {len(ok)} 个，失败 {len(failed)} 个'),
                             fill=th['ok'] if not failed else th['warn'])
            add_log(f'导出目录：{root_dir}')
            for f in failed:
                add_log(f'失败 {f.get("title")}：{f.get("error")}')
            open_btn.config(state='normal',
                            command=lambda: os.startfile(root_dir)
                            if os.path.isdir(root_dir) else None)
            self.toast(f'导出完成：成功 {len(ok)} 个', 'ok')

        threading.Thread(target=worker, daemon=True).start()

    # ────────────────────── 预览窗口 ──────────────────────

    def show_chat(self, item):
        """双击会话 → 预览消息（保留原功能）。"""
        if not self.wcdb:
            return
        wxid = item['wxid']
        try:
            total = self.wcdb.get_count(wxid)
        except Exception:
            total = 0
        th = self.theme
        win = tk.Toplevel(self.root)
        win.title(f"{item['title']}（{total} 条）")
        win.geometry('940x680')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        txt = tk.Text(win, wrap='word', font=(self.sf.font, 10), bd=0,
                      bg=th['surface'], fg=th['text'], insertbackground=th['accent'],
                      padx=14, pady=12, highlightthickness=0)
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        txt.pack(fill='both', expand=True, padx=10, pady=10)
        txt.tag_configure('who', foreground=th['accent'],
                          font=(self.sf.font, 10, 'bold'))
        txt.tag_configure('sys', foreground=th['text_faint'])
        txt.insert('end', '加载中…\n')
        txt.config(state='disabled')

        def load():
            try:
                raw = self.wcdb.get_messages(wxid, 500, 0)
            except Exception as e:
                txt.config(state='normal')
                txt.delete('1.0', 'end')
                txt.insert('end', f'读取失败：{e}\n')
                txt.config(state='disabled')
                return
            import message_content
            senders = list({m.get('sender_username', '') for m in raw
                            if m.get('sender_username')})
            if wxid not in senders:
                senders.append(wxid)
            try:
                nick = self.wcdb.get_display_names(senders) or {}
            except Exception:
                nick = {}
            my = ''
            try:
                for d in os.listdir(self.data_dir or '.'):
                    if d.startswith('wxid_'):
                        my = clean_wxid(d)
                        break
            except OSError:
                pass
            rows = message_content.prepare(raw, nick, my, '我')
            txt.config(state='normal')
            txt.delete('1.0', 'end')
            for m in rows:
                if m.get('kind') == 'system':
                    txt.insert('end', f"        {m.get('time_str','')}  "
                                      f"{m.get('text','')}\n\n", 'sys')
                else:
                    txt.insert('end', f"{m.get('time_str','')}  ")
                    txt.insert('end', f"{m.get('sender_display','')}\n", 'who')
                    txt.insert('end', f"  {m.get('text','')}\n\n")
            txt.config(state='disabled')
            win.title(f"{item['title']}（{len(rows)} 条）")

        win.after(60, load)

    # ────────────────────── 给 AI 的指令 ──────────────────────

    def edit_prompt(self):
        path = os.path.join(BASE, 'AI提示词.txt')
        try:
            if not os.path.exists(path):
                import ai_prompt
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(ai_prompt.load_prompt(BASE).strip() + '\n')
            os.startfile(path)
            self.toast('已用记事本打开，改完保存即可生效', 'ok', 4000)
        except Exception as e:
            self.toast(f'无法打开：{e}', 'err')

    # ────────────────────── 退出 ──────────────────────

    def quit_app(self):
        """关窗。

        要点：**先把窗口关掉，再收尾**。收尾里 wcdb.stop() 会走
        terminate → wait(5s) → taskkill(/T, 10s)，全放在 destroy() 之前
        就会让"点叉号"卡住最多十几秒。现在窗口立即消失，杀进程丢到后台。
        """
        self._closing = True
        wcdb = self.wcdb
        self.wcdb = None
        try:
            self.root.destroy()
        except Exception:
            pass
        if wcdb is not None:
            import threading

            def _cleanup():
                try:
                    wcdb.stop()
                except Exception:
                    pass

            t = threading.Thread(target=_cleanup, daemon=True,
                                 name='wcdb-shutdown')
            t.start()

    def run(self):
        self.root.mainloop()


def _boot_log(msg):
    """启动期插桩。

    打包成 --windowed 后没有控制台，卡住时无法诊断；这里把关键步骤写文件，
    卡在哪一步一目了然，也方便用户出问题时把日志发过来。
    """
    try:
        with open(os.path.join(ROOT, '.boot.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.datetime.now().strftime("%H:%M:%S")} {msg}\n')
    except Exception:
        pass


def _boot_log_done():
    """启动成功后删掉插桩日志，不给用户目录留垃圾。

    只在确认窗口已经建好之后调用 —— 失败时这份日志正是排查依据。
    """
    try:
        os.remove(os.path.join(ROOT, '.boot.log'))
    except OSError:
        pass


def main():
    """启动入口。

    打包成 --windowed 后没有控制台，任何启动期异常都会静默消失（表现为
    "双击没反应"）。所以这里统一捕获并写日志，同时弹一个尽量朴素的原生
    错误框 —— 用最基础的 tkinter，保证即使自绘界面初始化失败也能显示出来。
    """
    # 必须在创建 Tk() 之前声明 DPI 感知：否则在 125%/150% 缩放的显示器上，
    # Windows 会把整个窗口位图拉伸，圆角和文字都会发虚。
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        _boot_log('--- 启动 ---')
        _boot_log(f'frozen={getattr(sys, "frozen", False)} BASE={BASE}')
        _boot_log('创建 App')
        app = App()
        # 把窗口状态写进日志：打包后没有控制台，这是确认"窗口真的出来了"的唯一可靠办法
        try:
            app.root.update_idletasks()
            app.root.update()
            _boot_log(f'窗口 id={app.root.winfo_id()} '
                      f'mapped={bool(app.root.winfo_ismapped())} '
                      f'size={app.root.winfo_width()}x{app.root.winfo_height()} '
                      f'canvas_items={len(app.sf.canvas.find_all())} '
                      f'font={app.sf.font}')
        except Exception as e:
            _boot_log(f'窗口状态检查异常: {e}')
        _boot_log('App 创建完成，进入 mainloop')
        app.root.after(1500, _boot_log_done)      # 窗口稳定后清掉插桩日志
        app.run()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        log_path = os.path.join(ROOT, '启动错误.log')
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f'{APP_TITLE} {APP_VERSION}\n')
                f.write(f'frozen={getattr(sys, "frozen", False)}  '
                        f'BASE={BASE}\n\n{tb}\n')
        except OSError:
            log_path = '(日志写入失败)'
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            r = _tk.Tk()
            r.withdraw()
            _mb.showerror('启动失败',
                          f'{tb.strip().splitlines()[-1]}\n\n'
                          f'完整信息已写入：\n{log_path}')
            r.destroy()
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
