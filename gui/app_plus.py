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
import time
import tkinter as tk

# ── 路径解析（PyInstaller onefile 下 __file__ 在临时解包目录，必须以 exe 为准）──
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPTS_DIR = os.path.join(BASE, 'scripts')
EXPORTERS_DIR = os.path.join(BASE, 'exporters')
GUI_DIR = os.path.join(BASE, 'gui')
DS_BRIDGE_DIR = os.path.join(BASE, 'ds_bridge')
for _p in (SCRIPTS_DIR, EXPORTERS_DIR, GUI_DIR, DS_BRIDGE_DIR, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import ui_theme as T            # noqa: E402
import ui_widgets as W          # noqa: E402
import session_tags             # noqa: E402

ROOT = BASE
DEFAULT_OUT = os.path.join(os.environ.get('USERPROFILE', BASE), 'Desktop', 'wx_export')
OUT = DEFAULT_OUT
KEY_FILE = os.path.join(OUT, 'key.txt')
SETTINGS_FILE = os.path.join(ROOT, '.ui_settings')
# 会话标签单独一份文件：.ui_settings 是逐行 key=value、非原子写，
# 塞进会随数据量增长而变脆（详见 gui/session_tags.py 的说明）。
TAGS_FILE = os.path.join(ROOT, session_tags.FILE_NAME)
# 内嵌 DeepSeek 页签的用户数据目录（登录态存这里；**不能随发布包发出去**）
DS_PROFILE_DIR = os.path.join(ROOT, 'ds_profile')

APP_TITLE = '微信聊天记录批量导出工具'
APP_VERSION = 'v3.0.0'

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


def _fmt_dur(sec):
    """把秒数说成人话（进度窗显示剩余时间用）。"""
    sec = max(0, int(sec))
    if sec < 60:
        return f'{sec} 秒'
    return f'{sec // 60} 分 {sec % 60} 秒'


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


def _documents_dirs():
    """可能的「文档」目录。

    不能只会拼 `%USERPROFILE%\\Documents` —— 很多人把「文档」重定向到了 OneDrive
    （`%USERPROFILE%\\OneDrive\\Documents` 或 `OneDrive - 公司名\\Documents`），
    这时老写法那个路径根本不存在。
    """
    home = os.environ.get('USERPROFILE', 'C:')
    out = [os.path.join(home, 'Documents')]
    try:
        import glob as _glob
        out += _glob.glob(os.path.join(home, 'OneDrive*', 'Documents'))
    except Exception:
        pass
    return out


def _wechat_configured_dirs():
    """微信**自己记录**的数据存储位置（用户改过位置时，只有这里是对的）。

    - 微信 3.x：注册表 `HKCU\\Software\\Tencent\\WeChat\\FileSavePath`
    - 微信 4.x：`%APPDATA%\\Tencent\\xwechat\\config\\*.ini`
      （实测内容就是 `MyDocument:`，和 3.x 同一套约定；改过位置就是自定义路径）

    `MyDocument:` / `MyDocuments:` 都表示"文档"目录。
    读注册表/文件失败一律忽略，绝不影响主流程。
    """
    raw = []
    try:
        import winreg
        for sub in (r'Software\Tencent\WeChat', r'Software\Tencent\Weixin',
                    r'Software\Tencent\WeChat4'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                    for name in ('FileSavePath', 'DataSavePath', 'SavePath'):
                        try:
                            v, _t = winreg.QueryValueEx(k, name)
                            if isinstance(v, str) and v.strip():
                                raw.append(v.strip())
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass
    try:
        cfg = os.path.join(os.environ.get('APPDATA', ''), 'Tencent', 'xwechat', 'config')
        if os.path.isdir(cfg):
            for fn in os.listdir(cfg):
                if not fn.lower().endswith('.ini'):
                    continue
                try:
                    with open(os.path.join(cfg, fn), encoding='utf-8',
                              errors='ignore') as f:
                        v = f.read().strip()
                except OSError:
                    continue
                if v:
                    raw.append(v)
    except Exception:
        pass

    out = []
    for v in raw:
        v = str(v).strip().strip('"').strip()
        if not v:
            continue
        low = v.lower().rstrip('\\/')
        if low in ('mydocument:', 'mydocument', 'mydocuments:', 'mydocuments'):
            out.extend(_documents_dirs())
        elif re.match(r'^[a-zA-Z]:', v) or v.startswith('\\\\'):
            out.append(os.path.expandvars(v))
    return out


def _is_wechat_data_dir(d):
    """判定标准：里面有 `wxid_xxx` 子文件夹（老规矩，避免误选到别的目录）。"""
    try:
        for e in os.listdir(d):
            if e.startswith('wxid_') and os.path.isdir(os.path.join(d, e)):
                return True
    except OSError:
        pass
    return False


def _resolve_wechat_data(root):
    """在 root 或它的下一层找微信数据目录；找不到返回 ''。"""
    if root and os.path.isdir(root):
        if _is_wechat_data_dir(root):
            return root
        for sub in ('xwechat_files', 'WeChat Files'):
            p = os.path.join(root, sub)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p
    return ''


def find_xwechat_dirs(extra_roots=()):
    """找微信数据目录（返回"含 wxid_xxx 子文件夹"的那一层），找不到返回 ''。

    查找顺序（越靠前越可信）：
      1. **微信自己记录的位置**（注册表 / `%APPDATA%\\Tencent\\xwechat\\config\\*.ini`）
         —— 用户把数据挪到自定义目录时，只有这里能对上；
      2. 「文档」目录（含 OneDrive 重定向那几种）；
      3. 各盘符根目录**及其下一层**（覆盖 `D:\\我的资料\\xwechat_files` 这类）。

    以前只查几个固定位置、且只认 C~H 盘的根目录，所以用户一改存储位置就得手动点
    「浏览」——issue #1 就是这个原因。
    """
    for r in list(extra_roots) + _wechat_configured_dirs():
        hit = _resolve_wechat_data(r)
        if hit:
            return hit

    for d in _documents_dirs():
        for sub in ('xwechat_files', 'WeChat Files'):
            p = os.path.join(d, sub)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p

    # 各盘符：根目录 + 下一层（下一层能覆盖"我自己建了个文件夹放微信数据"）
    skip = {'windows', 'program files', 'program files (x86)', 'programdata',
            '$recycle.bin', 'system volume information', 'recovery',
            'perflogs', 'users', 'appdata'}
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        drive = f'{letter}:\\'
        if not os.path.isdir(drive):
            continue
        for name in ('xwechat_files', 'WeChat Files',
                     r'wxxinxi\xwechat_files', r'储存信息\xwechat_files'):
            p = os.path.join(drive, name)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p
        try:
            entries = list(os.scandir(drive))
        except OSError:
            continue
        for ent in entries:
            try:
                if not ent.is_dir() or ent.name.lower() in skip:
                    continue
            except OSError:
                continue
            for name in ('xwechat_files', 'WeChat Files'):
                p = os.path.join(ent.path, name)
                if os.path.isdir(p) and _is_wechat_data_dir(p):
                    return p
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

        # 会话标签：单独一份 JSON。读坏了也只是标签没了，不会连累主题/目录等设置。
        self.tag_store = session_tags.TagStore(TAGS_FILE)
        # 内嵌 DeepSeek 页签：宿主**懒启动** —— 不点那个页签就绝不拉起 electron。
        self.ds = None
        self.ds_out_root = self.settings.get('ds_root', '')
        self.ds_units = []
        self.ds_selected = set()
        # 只发文档（默认开：图片占绝大多数体积、且官网单条消息更容易被它们挤爆）
        self._ds_docs_only = self.settings.get('ds_docs_only', '1') == '1'
        # 每批文件数（默认 20，实测上限在 30~40 之间）
        try:
            self._ds_limit = int(self.settings.get('ds_batch', str(self.DS_LIMIT)))
        except ValueError:
            self._ds_limit = self.DS_LIMIT
        if self._ds_limit not in self.DS_LIMIT_CHOICES:
            self._ds_limit = self.DS_LIMIT
        self._ds_visible = False
        self._ds_win = None

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
        # 控件回调里抛异常时别再静默吞掉（写日志 + 提示一次）
        self._last_ui_err = ''
        self.sf.on_error = self._ui_error
        self.root.update_idletasks()
        self.sf.redraw_bg()

        self.root.protocol('WM_DELETE_WINDOW', self.quit_app)
        self.root.bind('<Configure>', self._on_resize)
        self.root.bind('<Escape>', lambda e: self._close_popups())

        self.build_home()
        # 自动化/自测用：设 WXEXPORT_START_PAGE=ds 就直接进「DeepSeek 官网」页签
        # （合成的鼠标消息进不了自绘输入层，所以留一个入口来验证官网页的生命周期）
        if os.environ.get('WXEXPORT_START_PAGE') == 'ds':
            self.root.after(250, self.build_ds)

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

        另外：**离开官网页时必须把内嵌浏览器藏起来**。它是独立的 Win32 子窗口，
        不随 Canvas 图元一起消失。这个判断必须放在这里，因为 build_home /
        build_sessions 会被很多地方直接调用（不只是 _rebuild_page 那条路），
        只挂在 _rebuild_page 上会漏 —— 实际就漏了：退出官网页后网页还留在那儿。
        各页构建函数都是先设 self.page 再调本函数，所以这里能拿到目标页。
        """
        if getattr(self, 'page', '') != 'ds':
            self._ds_leave()
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
        self._rebuild_page()

    def _page_builder(self):
        """当前页面对应的构建函数。"""
        return {'home': self.build_home, 'sessions': self.build_sessions,
                'ds': self.build_ds}.get(self.page, self.build_home)

    def _rebuild_page(self):
        """按 self.page 重建整页。

        换页前先把内嵌浏览器藏起来 —— 它是**独立的 Win32 子窗口**，
        不随 Canvas 图元一起被删，不藏就会浮在新页面上。
        （_clear_page 里也有一道同样的保险，见那里的说明。）

        发送中禁止切页：浏览器一被隐藏，Chromium 会把后台页降频，上传/发送会卡住。
        """
        if getattr(self, '_ds_sending', False) and self.page != 'ds':
            self.page = 'ds'
            self.toast('正在发送到 DeepSeek，暂时不能切页（可点进度窗的「取消」）',
                       'warn', 3500)
            return
        if self.page != 'ds':
            self._ds_leave()
        self._page_builder()()

    def _close_popups(self):
        for w in self._widgets:
            if hasattr(w, 'close'):
                w.close()

    def _ui_error(self, exc):
        """界面回调里抛出的异常：写 `.ui_errors.log` + 弹一次提示。

        为什么需要：自绘控件的回调是在 Surface.dispatch_input 里被调用的，
        以前那里 `except Exception: pass`。后果是"点了没反应/弹出一个空框"
        这类问题完全没有线索（标签弹窗就是这么被坑过一次）。
        """
        import traceback
        txt = ''.join(traceback.format_exception_only(type(exc), exc)).strip()
        try:
            with open(os.path.join(ROOT, '.ui_errors.log'), 'a',
                      encoding='utf-8') as f:
                f.write(f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {txt}\n')
                f.write(traceback.format_exc() + '\n')
        except OSError:
            pass
        if txt != self._last_ui_err:
            self._last_ui_err = txt
            try:
                self.toast(f'界面出错了（已记到 .ui_errors.log）：{txt[:64]}',
                           'err', 5000)
            except Exception:
                pass

    def _toast_rect(self, w=340, h=46):
        """提示框该摆在哪儿。

        ⚠️ 官网页签上右下角会被**内嵌浏览器挡住** —— 浏览器是独立的 Win32 子窗口，
        永远画在 Tk 画布之上，画布上画的提示它盖得住。所以那里必须挪到浏览器
        没占的那一列（左边清单区）。抽成纯函数是为了能被测试直接断言。
        """
        if self.page == 'ds' and self._ds_visible:
            w = min(w, self.DS_LEFT_W + 40)
            x1 = 38
            y2 = self.H - 46                     # 让开最下面那行日志
            return x1, y2 - h, x1 + w, y2
        x2, y2 = self.W - 26, self.H - 26
        return x2 - w, y2 - h, x2, y2

    def toast(self, msg, kind='info', ms=2600):
        """右下角浮出提示，几秒后自己消失（比弹窗打断感小）。"""
        th = self.theme
        col = {'info': th['accent'], 'ok': th['ok'], 'err': th['err'],
               'warn': th['warn']}.get(kind, th['accent'])
        cv = self.sf.canvas
        cv.delete('toast')
        w, h = 340, 46
        x1, y1, x2, y2 = self._toast_rect(w, h)
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
        # 页签入口：DeepSeek 官网页 ⇄ 导出页。自绘界面里没有真 tab 控件，
        # 用两个按钮当页签（位置和主题按钮并排，右侧留 8px 间隙）。
        if self.page == 'ds':
            tab_label, tab_cmd = '← 返回导出页', self._leave_ds
        else:
            tab_label, tab_cmd = '💬 DeepSeek 官网', self.build_ds
        self._widgets.append(
            W.Button(self.sf, self.W - 300, 34, 142, 38, tab_label, kind='ghost',
                     font_size=10, command=tab_cmd, hover_dur=0.12))
        lbl = '🌙 深色' if self.theme['name'] == 'light' else '☀ 浅色'
        self._widgets.append(
            W.Button(self.sf, self.W - 150, 34, 112, 38, lbl, kind='ghost',
                     font_size=10, command=self.toggle_theme, hover_dur=0.12))

    def _leave_ds(self):
        """从官网页返回：有会话就回会话页，否则回首页。"""
        if getattr(self, '_ds_sending', False):
            self.toast('正在发送，等它发完（或点进度窗的「取消」）再切页', 'warn', 3200)
            return
        if self.sessions:
            self.build_sessions()
        else:
            self.build_home()

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
        self._rebuild_page()

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
                          'tags': self.tag_store.tags_of(w),
                          'sel': w in getattr(self, '_selected', set())})
        # 有消息的排前面
        items.sort(key=lambda it: 0 if it['preview'] else 1)
        return items

    def build_sessions(self):
        # 先认领页面 + 收起内嵌网页，再做"有没有数据"的判断 ——
        # 否则在没有会话数据时会提前 return，官网页的浏览器窗口就留在屏幕上了
        # （实测踩到：切回会话页，网页还在）。
        self.page = 'sessions'
        self._ds_leave()
        if not self.sessions:
            self.toast('还没有会话数据，请先「连接数据库」', 'warn')
            return
        self._clear_page()
        th = self.theme
        if not hasattr(self, '_selected'):
            self._selected = set()

        self.draw_header(f'共 {len(self.sessions)} 个会话 · 勾选后统一导出')
        # 选中数量显示在标题栏右侧（不占列表表头，避免和勾选框挤在一起）。
        # 右边要给「官网对话」页签按钮留位置，所以结束在 W-312 而不是 W-176。
        self.sf.text(self.W - 312, 66, '尚未勾选', 10, th['accent'], bold=True,
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
        # 「打包成标签」：把当前勾选的会话一次性打上某个标签（可选已有的，也可新建）
        self._widgets.append(W.Button(self.sf, bx, top, 118, 34, '🏷 打包成标签',
                                      kind='ghost', font_size=10, radius=9,
                                      command=self.open_tag_dialog, hover_dur=0.12))

        # 搜索框（放在列表右上）
        self.search_var = tk.StringVar()
        self._search_entry = W.Entry(self.sf, self.W - 38 - 300, top, 300, 34,
                                     textvariable=self.search_var)
        self._entries.append(self._search_entry)
        self.search_var.trace_add('write', lambda *_: self._on_search())
        self._search_ph = self.sf.text(self.W - 38 - 300 + 14, top + 17,
                                       '搜索会话…', 10, th['text_faint'])

        # 标签行：点一个标签 = 把「带这个标签的会话」全部勾上（替换当前勾选）
        row_y = top + 42
        CHIP_H = 26
        self._chip_y = row_y
        self._draw_tag_chips(row_y, CHIP_H)

        # 列表
        list_top = row_y + CHIP_H + 10
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

    # ────────────────────── 会话标签 ──────────────────────

    def _text_px(self, s, size):
        """文本像素宽度（用来给 chip 定宽；取不到就按每字 8px 估）。"""
        try:
            import tkinter.font as tkfont
            return tkfont.Font(family=self.sf.font, size=size).measure(str(s))
        except Exception:
            return len(str(s)) * 8

    def _draw_tag_chips(self, y, h):
        """画标签行：每个标签一个 chip，点一下就把带该标签的会话全部勾上。

        chip 就是自绘 Button（没有现成的 chip 控件），标签多的时候按可用宽度
        截断，最后一个 chip 固定是「管理」，点开标签管理窗。
        """
        th = self.theme
        tags = self.tag_store.all_tags()
        counts = self.tag_store.counts()
        x = 38
        right_limit = self.W - 38 - 120          # 给「管理」留位置
        if not tags:
            self.sf.text(38, y + h // 2,
                         '还没有标签：「勾选几个会话 → 🏷 打包成标签」，'
                         '下次点标签就能一键勾上这些会话',
                         9, th['text_faint'])
        for t in tags:
            label = f'#{t}' + (f' {counts.get(t, 0)}' if counts.get(t) else '')
            w_ = min(170, max(56, self._text_px(label, 9) + 26))
            if x + w_ > right_limit:
                self.sf.text(x + 4, y + h // 2, '…', 10, th['text_faint'])
                break
            self._widgets.append(W.Button(
                self.sf, x, y, w_, h, label, kind='ghost', font_size=9, radius=9,
                command=(lambda tag=t: self._sel_by_tag(tag)), hover_dur=0.12))
            x += w_ + 6
        # 「管理」恒在最右：新建/删除/清空标签都从这里进
        self._widgets.append(W.Button(
            self.sf, self.W - 38 - 110, y, 110, h, '🏷 标签管理', kind='ghost',
            font_size=9, radius=9, command=self.open_tag_dialog, hover_dur=0.12))

    def _sel_by_tag(self, tag):
        """按标签勾选：**替换**当前勾选（用户明确要的行为）。"""
        wxids = self.tag_store.wxids_with(tag)
        if not wxids:
            self.toast(f'没有会话带「{tag}」标签', 'warn')
            return
        self.list.set_selected_wxids(wxids)
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()
        self.toast(f'已按标签「{tag}」勾选 {len(self._selected)} 个会话', 'ok')

    def _refresh_sessions(self, keep_search=True, keep_scroll=True):
        """标签改动后刷新会话页（整页重建最省心，也避免控件表/图元残留）。

        重建会丢掉搜索词与滚动位置，这里手动续上。
        """
        kw = self.search_var.get() if (keep_search and getattr(self, 'search_var', None)) else ''
        scroll = self.list.scroll if (keep_scroll and getattr(self, 'list', None)) else 0
        self.build_sessions()
        if kw:
            self.search_var.set(kw)
            self._on_search()
        if scroll and getattr(self, 'list', None):
            self.list.scroll = min(scroll, max(0, len(self.list.view) - 1))
            self.list.draw()

    def _apply_tag(self, tag, wxids):
        """把标签贴到这批会话上并落盘。"""
        tag = session_tags.clean_tag(tag)
        if not tag:
            self.toast('标签名不能为空', 'err')
            return False
        if not wxids:
            self.toast('请先勾选要打标签的会话', 'warn')
            return False
        added = self.tag_store.assign(wxids, tag)
        if not self.tag_store.save():
            self.toast('标签保存失败（文件可能被占用），本次改动没有落盘', 'err', 4000)
            return False
        self.toast(f'已给 {len(wxids)} 个会话打上「{tag}」（新增 {added} 个）', 'ok')
        self._refresh_sessions()
        return True

    def _untag_selection(self, tag):
        wxids = sorted(self._selected)
        if not wxids:
            self.toast('请先勾选会话', 'warn')
            return
        self.tag_store.untag(wxids, tag)
        self.tag_store.save()
        self.toast(f'已从 {len(wxids)} 个会话上摘掉「{tag}」', 'ok')
        self._refresh_sessions()

    def _delete_tag(self, tag):
        n = self.tag_store.delete_tag(tag)
        self.tag_store.save()
        self.toast(f'已删除标签「{tag}」（影响 {n} 个会话）', 'ok')
        self._refresh_sessions()

    def open_tag_dialog(self):
        """标签窗：给勾选的会话打标签。

        布局（自上而下）：
          1. 顶部一大行：**输入新标签名** → 「新建并贴上」（回车也行）；
          2. 已有标签列表：每个标签一行，三件事都能做 ——
             「贴到勾选的会话」/「只勾选这些会话」/「删除标签」；
          3. 底部：清除勾选会话的标签、关闭。

        用普通 tk 控件放在独立 Toplevel 里（和自绘 Canvas 无关）。
        ⚠️ 这里任何一处抛异常，用户看到的就是"跳出一个空框、点不动" ——
        因为 Button 的 command 是在 Surface 分发层被调用的（异常已改为上报日志）。
        """
        th = self.theme
        sel = sorted(self._selected)
        win = tk.Toplevel(self.root)
        win.title('会话标签')
        win.geometry('560x460')
        win.minsize(500, 380)
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        win.lift()
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        bg = T.rgb2hex(th['bg_top'])
        fg = T.rgb2hex(th['text'])
        dim = T.rgb2hex(th['text_dim'])
        accent = T.rgb2hex(th['accent'])
        font = self.sf.font

        head = tk.Frame(win, bg=bg)
        head.pack(fill='x', padx=18, pady=(16, 4))
        tk.Label(head, text=f'已勾选 {len(sel)} 个会话', bg=bg, fg=accent,
                 font=(font, 12, 'bold')).pack(side='left')
        tk.Label(head, text='（勾选变化后重新打开这个窗口即可）', bg=bg, fg=dim,
                 font=(font, 8)).pack(side='left', padx=(8, 0))

        # ① 新建
        tk.Label(win, text='① 给这批会话新建一个标签（输入名字后回车或点右边按钮）',
                 bg=bg, fg=dim, font=(font, 9)).pack(anchor='w', padx=18, pady=(10, 4))
        line = tk.Frame(win, bg=bg)
        line.pack(fill='x', padx=18)
        var = tk.StringVar()
        ent = tk.Entry(line, textvariable=var, font=(font, 12))
        ent.pack(side='left', fill='x', expand=True, ipady=5)

        def create(_e=None):
            if not sel:
                self.toast('还没有勾选会话', 'warn')
                return
            if self._apply_tag(var.get(), sel):
                win.destroy()

        tk.Button(line, text='新建并贴上', font=(font, 10), relief='flat',
                  cursor='hand2', bg=accent, fg='#ffffff', activebackground=accent,
                  command=create).pack(side='left', padx=(8, 0))
        ent.bind('<Return>', create)
        ent.focus_set()

        # ② 已有标签
        tk.Label(win, text='② 或者用已有标签（可以直接把它的会话集合勾上）',
                 bg=bg, fg=dim, font=(font, 9)).pack(anchor='w', padx=18, pady=(14, 4))
        box = tk.Frame(win, bg=bg)
        box.pack(fill='both', expand=True, padx=18)
        tags = self.tag_store.all_tags()
        counts = self.tag_store.counts()
        if not tags:
            tk.Label(box, text='（还没有标签）', bg=bg, fg=dim,
                     font=(font, 9)).pack(anchor='w')

        def apply_existing(tag):
            if not sel:
                self.toast('还没有勾选会话', 'warn')
                return
            if self._apply_tag(tag, sel):
                win.destroy()

        def select_only(tag):
            win.destroy()
            self._sel_by_tag(tag)

        for t in tags:
            row = tk.Frame(box, bg=bg)
            row.pack(fill='x', pady=2)
            tk.Label(row, text=f'#{t}', bg=bg, fg=accent, width=10, anchor='w',
                     font=(font, 10, 'bold')).pack(side='left')
            tk.Label(row, text=f'{counts.get(t, 0)} 个会话', bg=bg, fg=dim,
                     width=9, anchor='w', font=(font, 9)).pack(side='left')
            tk.Button(row, text='贴到勾选的会话', font=(font, 9), relief='flat',
                      cursor='hand2',
                      command=(lambda tag=t: apply_existing(tag))).pack(side='left')
            tk.Button(row, text='只勾选这些会话', font=(font, 9), relief='flat',
                      cursor='hand2',
                      command=(lambda tag=t: select_only(tag))).pack(side='left',
                                                                     padx=(6, 0))
            tk.Button(row, text='删除', font=(font, 9), relief='flat', fg='#c0392b',
                      cursor='hand2',
                      command=(lambda tag=t: (self._delete_tag(tag), win.destroy()))
                      ).pack(side='right')

        # ③ 清除
        foot = tk.Frame(win, bg=bg)
        foot.pack(fill='x', padx=18, pady=(8, 14))
        tk.Button(foot, text='清除这批会话的全部标签', font=(font, 10), relief='flat',
                  cursor='hand2',
                  command=lambda: (self._clear_tags_of_selection(), win.destroy())
                  ).pack(side='left')
        tk.Button(foot, text='关闭', font=(font, 10), relief='flat',
                  cursor='hand2', command=win.destroy).pack(side='right')

    def _clear_tags_of_selection(self):
        wxids = sorted(self._selected)
        if not wxids:
            self.toast('请先勾选会话', 'warn')
            return
        self.tag_store.clear(wxids)
        self.tag_store.save()
        self.toast(f'已清除 {len(wxids)} 个会话的标签', 'ok')
        self._refresh_sessions()

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
        # 导出完顺手就能投喂：直接把这次导出目录填进官网页的发送清单
        send_btn = ttk.Button(bf, text='→ 去官网发送', width=14, state='disabled')
        send_btn.pack(side='left', padx=8)
        close_btn = ttk.Button(bf, text='关闭', width=10, state='disabled')
        close_btn.pack(side='left', padx=8)

        def go_send():
            root_dir = result.get('root', '')
            if not os.path.isdir(root_dir):
                return
            self.ds_out_root = root_dir
            self.ds_units = []
            self.settings['ds_root'] = root_dir
            try:
                save_settings(self.settings)
            except Exception:
                pass
            win.destroy()
            self.build_ds()

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
            send_btn.config(state='normal' if os.path.isdir(root_dir) else 'disabled',
                            command=go_send)
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

    # ────────────────────── DeepSeek 官网页 ──────────────────────

    DS_LEFT_W = 330          # 左栏（发送清单）宽度
    # 每批文件数：**默认 30**（用户定）。
    # 注意别再往上调：实测单条消息 40 个附件就会被官网拒收（每个附件显示
    # "服务器繁忙"、整条消息报"请删除异常文件再发送"），30 是实测的安全上限。
    DS_LIMIT = 30
    DS_LIMIT_CHOICES = [10, 20, 30]
    # 每个附件在"挂上"之后还要等多久才点发送（用户定：文档 0.5 秒、图片 0.3 秒）
    DOC_SETTLE_MS = 500
    IMG_SETTLE_MS = 300

    def _ds_rect(self):
        """内嵌浏览器在主窗口客户区里占的矩形（canvas 坐标 = 客户区坐标）。"""
        x = 38 + self.DS_LEFT_W + 14
        y = 160
        w = max(320, self.W - x - 38)
        h = max(220, self.H - y - 38)
        return x, y, w, h

    def _ds_log(self, msg):
        """宿主进程/发送过程的日志：写到窗口底部一行，同时留在 self._ds_lines。"""
        line = str(msg)[:120]
        self._ds_lines = (getattr(self, '_ds_lines', []) + [line])[-60:]
        try:
            self.sf.canvas.itemconfigure('dslog', text=line)
        except Exception:
            pass

    def _ds_apply_theme(self):
        """把软件当前的深浅色同步给内嵌网页（官网自己也有两套配色）。

        走 Electron 的 nativeTheme.themeSource —— 页面里的
        `prefers-color-scheme` 媒体查询会跟着变，官网的配色也就跟着换了。
        """
        if not self.ds or not self.ds.running:
            return
        mode = 'dark' if self.theme['name'] == 'dark' else 'light'
        try:
            self.ds.set_theme(mode)
        except Exception:  # noqa: BLE001
            pass

    def build_ds(self):
        """DeepSeek 官网页：左栏选要发的东西，右边直接就是官网（真嵌入）。"""
        self.page = 'ds'
        self._clear_page()
        th = self.theme
        self.draw_header(f'内嵌 DeepSeek 官网 · 按批自动投喂（每批 ≤{self._ds_limit} 个文件）')

        top = 96
        self._widgets.append(W.Button(self.sf, 38, top, 150, 34, '选择导出文件夹',
                                      kind='primary', font_size=10, radius=9,
                                      command=self._ds_pick_root, hover_dur=0.12))
        bx = 196
        for label, cb, w_ in (('全选', lambda: self._ds_sel_all(True), 72),
                              ('全不选', lambda: self._ds_sel_all(False), 76),
                              ('演练分批', lambda: self._ds_send(dry_run=True), 92),
                              ('诊断', self._ds_diag, 72)):
            self._widgets.append(W.Button(self.sf, bx, top, w_, 34, label,
                                          kind='ghost', font_size=10, radius=9,
                                          command=cb, hover_dur=0.12))
            bx += w_ + 8
        self.btn_ds_send = W.Button(self.sf, self.W - 38 - 170, top, 170, 34,
                                    '🚀 开始发送', kind='primary', font_size=11,
                                    radius=9, command=self._ds_send)
        self._widgets.append(self.btn_ds_send)

        # 说明行（勾选统计）+ 右侧醒目警示，各占一行（挤在一行会互相压字）
        self.sf.text(38, 132, '', 9, th['text_dim'], tags='dshint')
        # ⚠️ 官网对"单条消息里的附件数量"很敏感：实测一批 40 个附件就会被拒收
        # （每个附件显示"服务器繁忙"、整条消息报"请删除异常文件再发送"）。
        # 图片尤其容易触发，所以这条放最显眼的位置（浏览器区域之外）。
        self.sf.text(self.W - 38, 148,
                     '⚠ 一批附件别超 30 个（图片尤其容易触发官网「服务器繁忙」）'
                     '—— 建议只发文档，或点左下角调小每批数量',
                     9, th['warn'], anchor='e', tags='dswarn')
        # 导出路径放左栏底部（浏览器盖不到那一片）
        self.sf.text(38, self.H - 108, '', 8, th['text_faint'], tags='dspath')
        # 底部一行日志
        self.sf.text(38, self.H - 24, '', 8, th['text_faint'], tags='dslog')

        # 左栏：发送清单（复用会话页那套 CheckList）
        list_y = 164
        # 底部要给「只发文档 / 每批数量」两个选项留一行（它们必须待在 x<382 的左栏里：
        # 内嵌浏览器是独立子窗口、永远盖在画布之上，放在右边会被它挡住点不到）
        list_h = max(120, self.H - list_y - 124)
        self.list = W.CheckList(self.sf, 38, list_y, self.DS_LEFT_W, list_h,
                                on_toggle=self._ds_on_toggle, on_open=None,
                                header='发送清单')
        self.list.set_items(self._ds_items())
        self._widgets.append(self.list)

        # 选项行（左下角，浏览器盖不到）
        op_y = self.H - 92
        self._docs_cb = W.Checkbox(
            self.sf, 38, op_y + 3, 176, '只发文档（建议）',
            value=bool(self._ds_docs_only), on_change=self._ds_on_docs_only,
            font_size=9)
        self._widgets.append(self._docs_cb)
        # 每批数量用**点击循环**的按钮，不用下拉框：下拉展开的浮层会被内嵌浏览器挡住
        self._batch_btn = W.Button(
            self.sf, 224, op_y, 144, 24, self._batch_label(), kind='ghost',
            font_size=9, radius=8, command=self._ds_cycle_batch, hover_dur=0.12)
        self._widgets.append(self._batch_btn)

        # 右侧：浏览器区域（先画一个占位框，真窗口盖在上面）
        x, y, w, h = self._ds_rect()
        self.sf.draw_panel(x - 6, y - 6, x + w + 6, y + h + 6, 14, 0.55)
        self.sf.text(x + w / 2, y + h / 2, '正在准备内嵌浏览器…', 10,
                     th['text_faint'], anchor='center', tags='dsplaceholder')

        self._ds_update_hint()
        # 上次用过的导出目录还在的话自动列出来，省得每次重新选
        if not self.ds_units and os.path.isdir(self.ds_out_root or ''):
            self._ds_scan()
        self.root.after(120, self._ds_boot)

    def _ds_boot(self):
        """页签画完再启动宿主：避免拉起 electron 时界面还是一片空白。"""
        if self.page != 'ds':
            return
        err = self._ds_ensure_host()
        if err:
            x, y, w, h = self._ds_rect()
            try:
                self.sf.canvas.itemconfigure('dsplaceholder',
                                             text=f'内嵌浏览器启动失败：{err}')
            except Exception:
                pass
            self.toast(f'内嵌浏览器启动失败：{err}', 'err', 5000)
            return
        try:
            self.sf.canvas.delete('dsplaceholder')
        except Exception:
            pass
        self._ds_place()

    def _ds_ensure_host(self):
        """确保 Electron 宿主在跑（懒启动）。返回 '' 表示成功，否则返回错误说明。"""
        if self.ds is not None and self.ds.running:
            return ''
        if getattr(self, '_ds_starting', False):
            return ''
        self._ds_starting = True
        try:
            from ds_bridge import host as ds_host
        except ImportError as e:
            self._ds_starting = False
            return f'缺少 ds_bridge 模块（{e}）'
        try:
            self.ds = ds_host.DeepSeekHost(ROOT, log=self._ds_log,
                                           profile=DS_PROFILE_DIR)
            if not self.ds.start():
                err = self.ds.start_error or '未知原因'
                self.ds = None
                return err
        finally:
            self._ds_starting = False
        return ''

    def _ds_place(self):
        """把浏览器窗口摆到右侧区域；第一次还要 SetParent 嵌进来。"""
        if not self.ds or not self.ds.running:
            return
        x, y, w, h = self._ds_rect()
        if getattr(self.ds, '_embedded', False):
            # 已经嵌过一次：只需挪位置（SetParent 一次就够，别每次重建都重挂）
            self.ds.move(x, y, w, h)
            if not self._ds_visible:
                self.ds.show()
                self._ds_visible = True
            self._ds_apply_theme()
            return
        try:
            parent = ctypes.windll.user32.GetParent(self.root.winfo_id()) or \
                self.root.winfo_id()
        except Exception:
            parent = self.root.winfo_id()
        if self.ds.embed(parent, x, y, w, h):
            self.ds.show()
            self._ds_visible = True
            self._ds_apply_theme()

    def _ds_leave(self):
        """离开本页就把浏览器藏起来（它是独立 Win32 子窗口，不会随画布清掉）。"""
        if self._ds_visible and self.ds is not None:
            try:
                self.ds.hide()
            except Exception:
                pass
            self._ds_visible = False

    # ── 清单 ──

    def _ds_on_docs_only(self, value):
        """只发文档：默认开。实测官网单条消息容易被大量图片挤爆（30 个以内才稳）。"""
        self._ds_docs_only = bool(value)
        self.settings['ds_docs_only'] = '1' if value else '0'
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self.list.set_items(self._ds_items())
        self._ds_update_hint()
        if value:
            self.toast('已切到「只发文档」：图片不会再被勾选发送（可以自己拖给网页版）',
                       'ok', 3600)

    def _batch_label(self):
        return f'每批 {self._ds_limit} 个 ⇄'

    def _ds_cycle_batch(self):
        """点一下换一个批量（10 → 20 → 30 → 10）。

        为什么不用下拉框：下拉展开的浮层是画在 Tk 画布上的，而内嵌浏览器是独立
        Win32 子窗口、永远盖在画布之上 —— 浮层会被它挡住，等于选不了。
        """
        try:
            i = self.DS_LIMIT_CHOICES.index(self._ds_limit)
        except ValueError:
            i = len(self.DS_LIMIT_CHOICES) - 1
        self._ds_limit = self.DS_LIMIT_CHOICES[(i + 1) % len(self.DS_LIMIT_CHOICES)]
        self.settings['ds_batch'] = str(self._ds_limit)
        try:
            save_settings(self.settings)
        except Exception:
            pass
        try:
            self._batch_btn.set_text(self._batch_label())
        except Exception:
            pass
        self._ds_update_hint()

    def _ds_on_batch(self, label):
        """兼容旧接口：万一还有地方按标签设置。"""
        try:
            self._ds_limit = int(str(label))
        except ValueError:
            self._ds_limit = self.DS_LIMIT
        self.settings['ds_batch'] = str(self._ds_limit)
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._ds_update_hint()

    def _ds_effective_units(self):
        """真正要发的勾选项（应用「只发文档」过滤）。"""
        units = self._ds_selected_units()
        if self._ds_docs_only:
            units = [u for u in units if u.get('kind') != 'image']
        return units

    def _ds_pick_root(self):
        from tkinter import filedialog
        init = self.ds_out_root if os.path.isdir(self.ds_out_root or '') else \
            os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop')
        d = filedialog.askdirectory(initialdir=init, title='选择要发送的导出文件夹')
        if not d:
            return
        self.ds_out_root = d
        self.settings['ds_root'] = d
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._ds_scan()

    def _ds_scan(self):
        from ds_bridge import plan as ds_plan
        self.ds_units = ds_plan.scan_export_dir(self.ds_out_root or '')
        self.ds_selected = {u['id'] for u in self.ds_units}     # 默认全选
        if not self.ds_units:
            self.toast('这个文件夹里没有可发送的内容（找 .md/.txt/.html 和 *_图片 目录）',
                       'warn', 4000)
        self.list.set_items(self._ds_items())
        self._ds_update_hint()

    def _ds_items(self):
        items = []
        for u in self.ds_units:
            skipped = self._ds_docs_only and u['kind'] == 'image'
            items.append({'wxid': u['id'], 'title': u['label'][:34],
                          'preview': (u['detail'] + '（已跳过：只发文档）') if skipped
                          else u['detail'],
                          'time': '图片' if u['kind'] == 'image' else '文档',
                          'group': u['kind'] == 'image', 'tags': [],
                          'sel': (u['id'] in self.ds_selected) and not skipped})
        return items

    def _ds_on_toggle(self, item):
        if item.get('sel'):
            self.ds_selected.add(item['wxid'])
        else:
            self.ds_selected.discard(item['wxid'])
        self._ds_update_hint()

    def _ds_sel_all(self, value):
        self.list.set_all(value)
        self.ds_selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._ds_update_hint()

    def _ds_selected_units(self):
        return [u for u in self.ds_units if u['id'] in self.ds_selected]

    def _ds_update_hint(self):
        from ds_bridge import plan as ds_plan
        units = self._ds_effective_units()
        s = ds_plan.summarize(units, self._ds_limit)
        root = self.ds_out_root or '未选择（点左上「选择导出文件夹」）'
        mode = '只发文档' if self._ds_docs_only else '文档+图片'
        txt = (f'{mode} · 每批 {self._ds_limit} 个 ｜ '
               f'已选 {len(units)} 项 · {s["files"]} 个文件'
               f'（文档 {s["docs"]}' + (f' + 图片 {s["images"]}' if not self._ds_docs_only else '')
               + f'）｜ 分 {s["batches"]} 批')
        try:
            self.sf.canvas.itemconfigure('dshint', text=txt)
            self.sf.canvas.itemconfigure(
                'dspath', text='导出目录：' + self._fit_text(root, self.DS_LEFT_W - 10, 8))
        except Exception:
            pass

    # ── 诊断 ──

    def _ds_diag(self):
        """页面结构诊断：选择器失效时用它导出候选元素，方便回来改。"""
        if self._ds_ensure_host():
            self.toast('内嵌浏览器还没起来', 'err')
            return
        r = self.ds.diag()
        if r.get('ok') and r.get('file'):
            try:
                os.startfile(r['file'])
                self.toast('已打开诊断文件', 'ok')
            except Exception:
                self.toast(f'诊断已写出：{r["file"]}', 'ok', 4000)
        else:
            self.toast(f'诊断失败：{r.get("error")}', 'err')

    # ── 发送 ──

    def _ds_preflight(self):
        """发送前的体检：页面在不在、登录了没、找得到上传入口吗。

        为什么要这一步：没登录时页面上**根本没有** input[type=file]，
        直接开跑只会得到一堆"挂附件失败"，甚至还可能让启发式去点到登录表单
        的按钮。所以这里先挡住，并明确告诉用户要做什么。
        返回 '' 表示可以发，否则返回给用户看的错误说明。
        """
        if not self.ds or not self.ds.running:
            return '内嵌浏览器没在运行，请先点「DeepSeek 官网」页签'
        st = self.ds.state()
        if not st.get('ok'):
            return f'读不到内嵌页面状态：{st.get("error") or st}'
        # 就绪判定要**两条路都认**：主进程侧的 S.pageReady 有可能和真实状态脱节
        # （历史上它被 did-start-loading 打回 false 后卡住，页面明明好了却报
        #  "还在加载，不能发送"）。页面自己报的 document.readyState 是更可靠的判据。
        ready_flag = bool(st.get('ready'))
        rs = str(st.get('readyState') or '').lower()
        if not ready_flag and rs != 'complete':
            return f'内嵌页面还在加载（页面状态 {rs or "未知"}），等它显示出来再试'
        if st.get('loggedIn') is False:
            return '还没登录 DeepSeek：请先在右侧页面里用手机号/密码登录，再点发送'
        if not st.get('fileInput'):
            return ('页面上找不到上传入口（input[type=file]）—— '
                    '可能是没登录、或官网改版了。可以点「诊断」把页面结构导出来反馈')
        return ''

    def _ds_refresh_prompt_file(self):
        """发送前把导出目录里的「给AI的指令.txt」刷新成**当前**模板。

        为什么必须做：导出目录里的那个文件是**导出当时**写进去的。用户后来改了
        `AI提示词.txt`（例如加了「分批接收协议」），旧导出目录里还是老文案 ——
        从那儿发送，喂给 AI 的就是过时指令（用户实测踩到过这个坑）。
        每次发送前重写一遍，保证协议永远是最新的。
        """
        root = self.ds_out_root or ''
        if not root or not os.path.isdir(root):
            return ''
        path = os.path.join(root, '给AI的指令.txt')
        try:
            import ai_prompt
            ai_prompt.clear_cache()                 # 用户刚改过模板也要立即生效
            text = (ai_prompt.load_prompt(BASE) or '').strip()
        except Exception:  # noqa: BLE001
            return ''
        if not text:
            return ''
        try:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text + '\n')
            return path
        except OSError:
            return ''

    def _ds_send(self, dry_run=False):
        from tkinter import messagebox
        from ds_bridge import plan as ds_plan
        if getattr(self, '_ds_sending', False):
            self.toast('正在发送中', 'warn')
            return
        units = self._ds_effective_units()
        if not units:
            self.toast('请先在左边勾选要发送的内容'
                       + ('（当前是「只发文档」模式，图片被跳过）'
                          if self._ds_docs_only else ''), 'warn', 4000)
            return
        paths = ds_plan.expand_units(units)
        miss = ds_plan.missing_files(paths)
        if miss:
            self.toast(f'有 {len(miss)} 个文件已经不在磁盘上，请重新选择目录', 'err', 4000)
            return
        # 把旧导出目录里的指令文件刷新成最新版（否则喂给 AI 的是过时协议）
        refreshed = self._ds_refresh_prompt_file()
        if refreshed:
            self._ds_log('已把「给AI的指令.txt」刷新为最新指令')
        over = ds_plan.find_oversize(paths)
        if over and not messagebox.askokcancel(
                '有文件超过 100MB',
                f'{len(over)} 个文件超过网页版单文件上限（100MB），发送可能失败：\n'
                + '\n'.join('  · ' + os.path.basename(p) for p, _ in over[:5]) +
                '\n\n仍然继续？'):
            return
        batches = ds_plan.plan_batches(paths, self._ds_limit)
        if not batches:
            self.toast('没有要发送的文件', 'warn')
            return
        if not dry_run:
            if self._ds_ensure_host():
                self.toast('内嵌浏览器没起来，无法发送', 'err')
                return
            problem = self._ds_preflight()
            if problem:
                self._ds_log('不能发送：' + problem)     # 也留一行在左下角（提示可能被浏览器挡）
                self.toast(problem, 'warn', 6000)
                return
            head = '、'.join(os.path.basename(b[0]) for b in batches[:3])
            if not messagebox.askokcancel(
                    '确认发送',
                    f'将向当前对话发送 {len(paths)} 个文件，分 {len(batches)} 批'
                    f'（每批 ≤{self._ds_limit}）：\n\n'
                    f'批次大小：{"+".join(str(len(b)) for b in batches)}\n'
                    f'从 {head} … 开始\n\n'
                    '每批发出后会等它自然答完（协议下每批只回一句「我已接收上述信息」），'
                    '只有超过 25 秒还没答完才截断。\n'
                    '最后一批会自动附带一句「我已发送完毕」，触发它开始正式分析。\n'
                    '发送期间请不要切换页面。\n\n确定开始？'):
                return
        self._ds_open_progress(len(paths), batches, dry_run)

    def _ds_open_progress(self, n_files, batches, dry_run):
        th = self.theme
        win = tk.Toplevel(self.root)
        win.title('演练分批' if dry_run else '正在发送到 DeepSeek 网页版…')
        win.geometry('620x400')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass
        bg = T.rgb2hex(th['bg_top'])
        tk.Label(win, text=(f'{"演练" if dry_run else "发送"} {n_files} 个文件 · '
                            f'{len(batches)} 批（每批 ≤{self._ds_limit}）'),
                 bg=bg, fg=T.rgb2hex(th['text']),
                 font=(self.sf.font, 13, 'bold')).pack(anchor='w', padx=18, pady=(14, 2))
        cur = tk.Label(win, text='准备中…', bg=bg, fg=T.rgb2hex(th['accent']),
                       font=(self.sf.font, 10, 'bold'), wraplength=580,
                       justify='left')
        cur.pack(anchor='w', padx=18, fill='x')
        # 剩余时间单独一行，由界面每秒自己刷新 —— 只靠批次边界上报的那一次，
        # 用户盯着看会觉得"数字不动/不准"（尤其每批要等十几秒文件处理）。
        eta_lbl = tk.Label(win, text='', bg=bg, fg=T.rgb2hex(th['text_dim']),
                           font=(self.sf.font, 9), wraplength=580, justify='left')
        eta_lbl.pack(anchor='w', padx=18, fill='x')
        bar = tk.Canvas(win, height=12, bg=bg, highlightthickness=0)
        bar.pack(fill='x', padx=18, pady=(8, 4))

        def draw_bar(done, total):
            bar.delete('all')
            w = max(1, bar.winfo_width() - 4)
            bar.create_rectangle(2, 3, 2 + w, 9, fill=T.rgb2hex(th['surface2']),
                                 outline='')
            if total:
                bar.create_rectangle(2, 3, 2 + w * done / total, 9,
                                     fill=T.rgb2hex(th['accent']), outline='')

        log = tk.Text(win, height=13, bg=T.rgb2hex(th['surface']),
                      fg=T.rgb2hex(th['text_dim']), bd=0,
                      font=(self.sf.font, 9))
        log.pack(fill='both', expand=True, padx=18, pady=(6, 8))
        log.configure(state='disabled')
        self._ds_cancel_flag = False

        def close_win():
            """关窗：发送中先问一句，然后置取消标记并关掉。

            上一版的毛病：只置了取消标记、窗口却不关 —— 而且是**静默**的，
            用户点 X 以为关掉了，其实发送在后台被取消；演练跑完窗口也关不掉。
            """
            if getattr(self, '_ds_sending', False):
                from tkinter import messagebox
                if not messagebox.askokcancel(
                        '还在发送', '发送还没结束。\n\n确定要停下并关闭这个窗口吗？'):
                    return
                self._ds_cancel_flag = True
            try:
                win.destroy()
            except Exception:
                pass

        btn = tk.Button(win, text='取消', font=(self.sf.font, 10), relief='flat',
                        cursor='hand2',
                        command=lambda: (setattr(self, '_ds_cancel_flag', True),
                                         btn.configure(state='disabled'),
                                         cur.configure(text='正在停下了…')))
        btn.pack(anchor='e', padx=18, pady=(0, 14))
        win.protocol('WM_DELETE_WINDOW', close_win)

        ctx = {'win': win, 'cur': cur, 'log': log, 'bar': bar, 'bar_draw': draw_bar}
        self._ds_sending = True
        self._ds_done_batches = 0
        self.btn_ds_send.set_state('disabled')

        # ⚠️ 工作线程**绝对不要碰 Tk**。
        # 以前这里直接 `self.root.after(0, ...)`：那不是线程安全的（主线程不在
        # mainloop 里时会抛 "main thread is not in main loop"），更糟的是线程里
        # 一抛异常就没人调 finish()，界面会**永远卡在"正在发送中"**、连切页都被
        # 挡住。现在改成：线程只往队列里丢消息，主线程用 after 轮询消费。
        import queue
        q = queue.Queue()
        # 剩余时间由界面自己每秒算：只靠批次边界上报一次，等十几秒文件处理的时候
        # 数字一直不动，看起来就是"算错了"。
        prog = {'done': 0, 'total': len(batches), 't0': time.time(), 'marks': []}

        def ui_log(msg):
            q.put(('log', str(msg)))

        def ui_progress(done, total, text):
            q.put(('prog', (done, total, text)))

        def ui_eta():
            try:
                done = min(prog['done'], prog['total'])
                total = prog['total']
                marks = prog['marks']
                if done <= 0:
                    txt = f'共 {total} 批 · 正在准备第 1 批（每批要先给文件留处理时间）'
                else:
                    if len(marks) >= 2:
                        avg = (marks[-1] - marks[0]) / (len(marks) - 1)
                    elif marks:
                        avg = max(1.0, time.time() - marks[0])
                    else:
                        avg = 20.0
                    remain = avg * max(0, total - done)
                    txt = (f'已完成 {done}/{total} 批 · 预计还需 {_fmt_dur(remain)}'
                           f' · 每批约 {_fmt_dur(avg)}')
                eta_lbl.configure(text=txt)
            except Exception:
                pass
            if getattr(self, '_ds_sending', False):
                try:
                    win.after(1000, ui_eta)
                except Exception:
                    pass

        def drain():
            """主线程侧：把队列里的消息搬到界面上。返回是否收到结束信号。"""
            for _ in range(200):
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    return False
                if kind == 'log':
                    try:
                        log.configure(state='normal')
                        log.insert('end', payload + '\n')
                        log.see('end')
                        log.configure(state='disabled')
                        self._ds_log(payload)
                    except Exception:
                        pass
                elif kind == 'prog':
                    done, total, text = payload
                    self._ds_done_batches = done
                    if done > prog['done']:
                        prog['done'] = done
                        prog['marks'].append(time.time())
                    try:
                        cur.configure(text=text)
                        draw_bar(done, total)
                    except Exception:
                        pass
                elif kind == 'done':
                    finish(payload)
                    return True
            return False

        def poll():
            if drain():
                return
            try:
                win.after(80, poll)
            except Exception:
                pass

        def worker():
            from ds_bridge import sender as ds_sender
            try:
                s = ds_sender.BatchSender(self.ds, limit=self._ds_limit,
                                          dry_run=dry_run, log=ui_log)
                res = s.run(batches, on_progress=ui_progress,
                            should_cancel=lambda: self._ds_cancel_flag)
            except Exception as e:      # noqa: BLE001
                import traceback
                q.put(('log', f'发送线程崩了：{e}'))
                q.put(('log', traceback.format_exc().splitlines()[-1]))
                res = {'ok': False, 'batches': len(batches), 'sent_batches': 0,
                       'sent_files': 0, 'failed': [], 'stopped': 0,
                       'cancelled': False, 'error': f'内部错误：{e}'}
            q.put(('done', res))

        def finish(res):
            self._ds_sending = False
            try:
                self.btn_ds_send.set_state('normal')
            except Exception:
                pass
            summary = (f'完成 {res["sent_batches"]}/{res["batches"]} 批 · '
                       f'{res["sent_files"]} 个文件 · 截断 {res["stopped"]} 次'
                       f'{"（已取消）" if res["cancelled"] else ""}')
            if res.get('error'):
                summary += f'；错误：{res["error"]}'
            if res['failed']:
                summary += f'；失败 {len(res["failed"])} 批'
                hint = ('附件可能已经挂在内嵌页面上了：请切到官网页签，'
                        '手动点发送或把附件删掉，再重新「开始发送」。')
                if any(k in (res.get('error') or '')
                       for k in ('异常文件', '服务器繁忙', '请检查网络', '发送失败')):
                    hint = ('官网拒收（多半是一批文件太多）：把左下角「每批 30 个」'
                            '点一下改成更小的值，并把官网页面上残留的附件删掉，'
                            '再重新「开始发送」。')
                summary += '\n' + hint
            try:
                cur.configure(text=summary)
            except Exception:
                pass
            try:
                log.configure(state='normal')
                log.insert('end', summary + '\n')
                log.see('end')
                log.configure(state='disabled')
            except Exception:
                pass
            self._ds_log(summary)
            self.toast(summary, 'ok' if res['ok'] else 'warn', 5000)
            try:
                win.title('发送结束')
                # 结束后按钮变成「关闭」——演练跑完必须能关掉窗口
                btn.configure(text='关闭', state='normal', command=close_win)
            except Exception:
                pass

        draw_bar(0, len(batches))
        win.update_idletasks()          # 先布局一次，进度条初始宽度才是对的
        draw_bar(0, len(batches))
        bar.bind('<Configure>',
                 lambda e: draw_bar(getattr(self, '_ds_done_batches', 0),
                                    len(batches)))
        win.after(80, poll)             # 主线程轮询（after 只在主线程调用）
        win.after(300, ui_eta)          # 剩余时间每秒刷新
        threading.Thread(target=worker, daemon=True, name='ds-send').start()

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
        ds = self.ds
        self.ds = None
        try:
            self.root.destroy()
        except Exception:
            pass

        def _cleanup():
            # 内嵌浏览器：/quit → terminate → taskkill，全程在后台，不挡关窗
            if ds is not None:
                try:
                    ds.detach()
                except Exception:
                    pass
                try:
                    ds.shutdown(wait=True)
                except Exception:
                    pass
            if wcdb is not None:
                try:
                    wcdb.stop()
                except Exception:
                    pass

        if wcdb is not None or ds is not None:
            import threading
            t = threading.Thread(target=_cleanup, daemon=True, name='shutdown')
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
