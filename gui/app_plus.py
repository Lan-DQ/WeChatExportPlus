# -*- coding: utf-8 -*-
"""微信聊天记录批量导出工具 v2.0.0

在 Ray0612/WeChat-Export-Tool v1.2.0 内核（WCDB 解密 + 密钥提取 + 图片解密）之上
重建交互层：从「双击单个会话逐个导出」改为「勾选多个会话 + 统一格式 + 一次批量导出」。

界面分为两页：
  1. 首页 —— 与原版一致：微信数据目录 / 导出工作目录 / 数据库密钥 + 三个按钮；
  2. 会话页 —— 带复选框的会话列表（可全选/反选/搜索），底部统一设置导出格式与位置。

内核相关文件（scripts/、dll/、runtime/、resources/）不做改动，直接复用，
因此原工具已配置好的密钥与数据库连接方式在这里同样有效。
"""
import ctypes
import datetime
import os
import re
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ── 路径解析 ──
# PyInstaller onefile 下 sys.executable 是 exe 本体，而 __file__ 在临时解包目录，
# 因此资源目录一律以 exe 所在目录为基准。
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPTS_DIR = os.path.join(BASE, 'scripts')
EXPORTERS_DIR = os.path.join(BASE, 'exporters')
for _p in (SCRIPTS_DIR, EXPORTERS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT = BASE
DEFAULT_OUT = os.path.join(os.environ.get('USERPROFILE', BASE), 'Desktop', 'wx_export')
OUT = DEFAULT_OUT
KEY_FILE = os.path.join(OUT, 'key.txt')

try:
    os.makedirs(OUT, exist_ok=True)
except OSError:
    pass

APP_TITLE = '微信聊天记录批量导出工具'
APP_VERSION = 'v2.0.0'

# 格式下拉：显示名 → batch_export 的格式 key
FORMAT_CHOICES = [
    ('Markdown 单文件（推荐·可直接拖进 DeepSeek 网页版）', 'md'),
    ('AI 语料 JSONL（带发言人，适合入库/RAG）', 'ai'),
    ('HTML（气泡页面，含图片）', 'html'),
    ('Excel（.xlsx 表格）', 'excel'),
    ('CSV（.csv 表格）', 'csv'),
    ('PDF（含内嵌图片）', 'pdf'),
    ('TXT（纯文本）', 'txt'),
    ('JSON（结构化）', 'json'),
]
FORMAT_DEFAULT = 'md'


def load_saved_key():
    """启动时尝试加载已保存的密钥（沿用原工具的位置与格式）。"""
    candidates = [
        KEY_FILE,
        os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop',
                     'wechat_export', 'WeChat', 'key.txt'),
    ]
    for path in candidates:
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
    """扫描常见位置找 xwechat_files 目录。"""
    home = os.environ.get('USERPROFILE', 'C:')
    candidates = [
        os.path.join(home, 'Documents', 'xwechat_files'),
        os.path.join(home, 'Documents', 'WeChat Files'),
    ]
    for letter in 'CDEFGH':
        for sub in ('xwechat_files', 'WeChat Files',
                    r'wxxinxi\xwechat_files', r'储存信息\xwechat_files'):
            candidates.append(f'{letter}:\\{sub}')
    for d in candidates:
        if os.path.isdir(d):
            try:
                for entry in os.listdir(d):
                    if entry.startswith('wxid_') and os.path.isdir(os.path.join(d, entry)):
                        return d
            except OSError:
                pass
    return ''


def clean_wxid(wxid):
    parts = str(wxid or '').split('_')
    if len(parts) >= 3 and parts[0] == 'wxid':
        return '_'.join(parts[:2])
    return str(wxid or '')


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f'{APP_TITLE} {APP_VERSION}')
        self.root.geometry('1040x720')
        self.root.minsize(880, 600)
        self._set_icon()

        self.key = load_saved_key() or None
        self.wcdb = None
        self.sessions = []            # 全部会话原始数据
        self.nick_map = {}            # wxid → 显示名
        self.selected = set()         # 勾选的 wxid
        self.visible = []             # 当前过滤后可见的行
        self.busy = False
        self._key_visible = False

        self.setup_menu()
        self.show_home()

    # ────────────────────── 基础设施 ──────────────────────

    def _set_icon(self):
        self._ico = ''
        for p in (os.path.join(BASE, 'icon.ico'),
                  os.path.join(BASE, 'gui', 'icon.ico')):
            if os.path.exists(p):
                try:
                    self.root.iconbitmap(p)
                    self._ico = p
                    break
                except Exception:
                    pass

    def setup_menu(self):
        m = tk.Menu(self.root)
        self.root.config(menu=m)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label='获取密钥', command=self.do_getkey)
        fm.add_command(label='连接数据库', command=self.do_connect)
        fm.add_separator()
        fm.add_command(label='打开导出目录', command=self.open_out_dir)
        fm.add_separator()
        fm.add_command(label='退出', command=self.quit_app)
        m.add_cascade(label='操作', menu=fm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label='使用说明', command=self.show_help)
        hm.add_command(label='关于', command=self.show_about)
        m.add_cascade(label='帮助', menu=hm)
        # 关窗口时也要清理后台进程，否则 WCDB/electron 会变成孤儿进程一直占着程序目录
        self.root.protocol('WM_DELETE_WINDOW', self.quit_app)

    def quit_app(self):
        """退出前关掉后台 WCDB 服务与 node/electron 进程树。"""
        try:
            if self.wcdb:
                self.wcdb.stop()
                self.wcdb = None
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def clear(self):
        for w in self.root.winfo_children():
            if isinstance(w, tk.Menu):
                continue
            w.destroy()

    def _find_node(self):
        node = os.path.join(ROOT, 'runtime', 'node.exe')
        if os.path.exists(node):
            return node
        return shutil.which('node') or shutil.which('node.exe')

    def open_out_dir(self):
        target = OUT if os.path.isdir(OUT) else DEFAULT_OUT
        try:
            os.startfile(target)
        except Exception as e:
            messagebox.showerror('错误', f'无法打开目录:\n{target}\n{e}')

    def show_help(self):
        messagebox.showinfo('使用说明', (
            '1. 确认「微信数据目录」（一般会自动检测）\n'
            '2. 确认「导出工作目录」\n'
            '3. 点「获取密钥」自动捕获，或直接粘贴 64 位密钥\n'
            '4. 点「连接数据库」\n'
            '5. 点「浏览会话」进入会话列表\n'
            '6. 勾选要导出的会话（可全选/反选/搜索）\n'
            '7. 选择导出格式，点「开始导出」，选择总目录\n\n'
            '导出结果：总目录下新建「导出_日期_时间」文件夹，\n'
            '其中每个会话一个子文件夹，并生成「导出清单.html」索引。\n\n'
            '★ 要喂给 DeepSeek 网页版：选默认的「Markdown 单文件」格式。\n'
            '  导出目录下每个会话是一个 .md 文件，双击可读，\n'
            '  也可以直接把 .md 拖进网页版对话框。\n'
            '  需要看图时，再把旁边的「<会话名>_图片」里的图按编号一起拖进去\n'
            '  （正文写 [图片01]，对应 _图片\\01.jpg）。\n\n'
            '★ 要做知识库/RAG：选「AI 语料 JSONL」，带发言人、时间、类型字段。'))

    def show_about(self):
        messagebox.showinfo('关于', (
            f'{APP_TITLE} {APP_VERSION}\n\n'
            '内核：Ray0612/WeChat-Export-Tool v1.2.0（GPL-3.0）\n'
            '本工具在其基础上重建了交互层，支持多会话批量导出。\n\n'
            '仅供导出本人的聊天记录备份使用，请遵守相关法律法规。'))

    # ────────────────────── 首页 ──────────────────────

    def show_home(self):
        self.clear()
        f = ttk.Frame(self.root, padding=36)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text=APP_TITLE, font=('', 20)).pack()
        ttk.Label(f, text=APP_VERSION, font=('', 10)).pack(pady=(0, 18))

        # 微信数据目录
        dir_f = ttk.LabelFrame(f, text='微信数据目录', padding=10)
        dir_f.pack(fill=tk.X, pady=8)
        pf = ttk.Frame(dir_f)
        pf.pack(fill=tk.X)
        detected = find_xwechat_dirs()
        self.dir_var = tk.StringVar(value=detected)
        ttk.Entry(pf, textvariable=self.dir_var, width=58).pack(side=tk.LEFT, padx=5)
        ttk.Button(pf, text='浏览', width=8,
                   command=lambda: self.dir_var.set(
                       filedialog.askdirectory() or self.dir_var.get())).pack(side=tk.LEFT, padx=2)
        if detected:
            ttk.Label(pf, text='✅ 已自动检测', foreground='green').pack(side=tk.LEFT)

        # 导出工作目录
        out_f = ttk.LabelFrame(f, text='导出工作目录', padding=10)
        out_f.pack(fill=tk.X, pady=8)
        opf = ttk.Frame(out_f)
        opf.pack(fill=tk.X)
        self.out_dir_var = tk.StringVar(value=OUT)
        ttk.Entry(opf, textvariable=self.out_dir_var, width=58).pack(side=tk.LEFT, padx=5)
        ttk.Button(opf, text='浏览', width=8, command=self._pick_out_dir).pack(side=tk.LEFT, padx=2)
        self.out_dir_label = ttk.Label(out_f, text=f'✅ {os.path.join(OUT, "WeChat")}',
                                       foreground='green')
        self.out_dir_label.pack(anchor=tk.W, padx=5)

        # 密钥
        key_f = ttk.LabelFrame(f, text='数据库密钥', padding=10)
        key_f.pack(fill=tk.X, pady=8)
        kpf = ttk.Frame(key_f)
        kpf.pack(fill=tk.X)
        ttk.Label(kpf, text='密钥:').pack(side=tk.LEFT)
        self.key_input_var = tk.StringVar(value='')
        self.key_entry = ttk.Entry(kpf, textvariable=self.key_input_var, width=54, show='*')
        self.key_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(kpf, text='显示', width=8, command=self._toggle_key_show).pack(side=tk.LEFT, padx=2)
        self.key_input_var.trace_add('write', self._on_key_changed)
        ttk.Label(key_f, text='点击「获取密钥」自动捕获，或直接粘贴已有 64 位密钥',
                  font=('', 8), foreground='gray').pack(anchor=tk.W)

        # 按钮区
        bf = ttk.Frame(f)
        bf.pack(pady=12)
        self.b1 = ttk.Button(bf, text='🔑 获取密钥', width=22, command=self.do_getkey)
        self.b1.pack(pady=3)
        self.b2 = ttk.Button(bf, text='🗄️ 连接数据库', width=22,
                            command=self.do_connect, state='disabled')
        self.b2.pack(pady=3)
        self.b3 = ttk.Button(bf, text='📤 浏览会话', width=22,
                            command=self.show_sessions, state='disabled')
        self.b3.pack(pady=3)

        self.status = ttk.Label(f, text='就绪', foreground='gray')
        self.status.pack(pady=8)
        self.key_label = ttk.Label(f, text='', foreground='green')
        self.key_label.pack()
        self.session_label = ttk.Label(f, text='', foreground='gray')
        self.session_label.pack()

        if self.sessions:
            self.session_label.config(text=f'{len(self.sessions)} 个会话已就绪')

        # 自动填充已保存的密钥
        saved = load_saved_key()
        if saved:
            self.key_input_var.set(saved)
            self.key_label.config(text='✅ 已加载保存的密钥')
            self.b2.config(state='normal')

    def _on_key_changed(self, *_):
        ok = bool(re.fullmatch(r'[0-9a-fA-F]{64}', self.key_input_var.get().strip()))
        try:
            self.b2.config(state='normal' if ok else 'disabled')
        except tk.TclError:
            pass

    def _toggle_key_show(self):
        self._key_visible = not self._key_visible
        self.key_entry.config(show='' if self._key_visible else '*')

    def _pick_out_dir(self):
        chosen = filedialog.askdirectory()
        if not chosen:
            return
        self.out_dir_var.set(chosen)
        self._setup_out_dir()

    def _setup_out_dir(self):
        global OUT, KEY_FILE
        base = self.out_dir_var.get().strip()
        if not base:
            messagebox.showerror('错误', '请先选择导出工作目录')
            return
        wechat_dir = os.path.join(base, 'WeChat')
        try:
            os.makedirs(wechat_dir, exist_ok=True)
            OUT = wechat_dir
            KEY_FILE = os.path.join(OUT, 'key.txt')
            self.out_dir_label.config(text=f'✅ {wechat_dir}')
            self.log(f'导出目录: {wechat_dir}')
            if os.path.exists(KEY_FILE):
                with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                    k = f.read().strip()
                if len(k) == 64 and not self.key_input_var.get():
                    self.key_input_var.set(k)
                    self.key = k
                    self.key_label.config(text='✅ 已加载保存的密钥')
        except OSError as e:
            messagebox.showerror('错误', f'无法创建目录:\n{wechat_dir}\n{e}')

    def log(self, msg):
        try:
            self.status.config(text=str(msg)[:90])
            self.root.update_idletasks()
        except Exception:
            pass

    # ────────────────────── 获取密钥 ──────────────────────

    def do_getkey(self):
        threading.Thread(target=self._getkey, daemon=True).start()

    def _getkey(self):
        if not messagebox.askokcancel('准备', (
                '1. 关闭微信电脑端（右键系统托盘 → 退出）\n'
                '2. 点确定后等待\n'
                '3. 看到「等待微信启动」后打开微信\n'
                '4. 微信启动过程中自动捕获密钥')):
            return

        node_exe = self._find_node()
        key_js = os.path.join(ROOT, 'scripts', 'get_key.js')
        if not node_exe or not os.path.exists(key_js):
            self.root.after(0, lambda: messagebox.showerror(
                '错误', f'找不到运行时: node={node_exe}, js={key_js}'))
            return

        status_file = os.path.join(OUT, 'key_status.txt')
        for p in (status_file, KEY_FILE):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        self.log('[*] 提权运行...')
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', node_exe, f'"{key_js}"', None, 1)
        if ret <= 32:
            self.root.after(0, lambda: messagebox.showerror(
                '提权失败',
                f'ShellExecuteW 返回 {ret}\n请手动以管理员身份运行:\n  {node_exe} "{key_js}"'))
            return

        status_map = {
            'started': '脚本已启动', 'dll_found': '找到 wx_key.dll',
            'dll_loaded': 'DLL 加载成功', 'dll_not_found': '找不到 wx_key.dll',
            'waiting_close': '等待微信关闭...', 'timeout_close': '关微信超时',
            'waiting_start': '等待微信启动... (请打开微信)', 'timeout_start': '等微信启动超时',
            'injecting': '正在注入 Hook...', 'hook_ok': 'Hook 注入成功, 等登录捕获 key...',
            'hook_failed': 'Hook 注入失败', 'polling': '等待登录中捕获 key...',
            'timeout_poll': '获取超时', 'captured': '✅ 已捕获!',
        }
        for _ in range(180):
            if os.path.exists(KEY_FILE):
                try:
                    with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                        k = f.read().strip()
                except OSError:
                    k = ''
                if len(k) == 64:
                    self.key = k
                    self.root.after(0, lambda kk=k: self._on_key_captured(kk))
                    return
            if os.path.exists(status_file):
                try:
                    with open(status_file, encoding='utf-8', errors='ignore') as f:
                        st = f.read().strip()
                    self.log(f'[*] {status_map.get(st.split(":")[0], st)}')
                except OSError:
                    pass
            threading.Event().wait(1)
        self.log('[-] 获取失败')

    def _on_key_captured(self, k):
        self.key_input_var.set(k)
        self.key_label.config(text=f'✅ Key: {k[:16]}...')
        self.b2.config(state='normal')
        self.log('✅ 密钥获取成功')

    # ────────────────────── 连接数据库 ──────────────────────

    def do_connect(self):
        manual = self.key_input_var.get().strip()
        if len(manual) == 64:
            self.key = manual
        elif not (self.key and len(self.key) == 64) and os.path.exists(KEY_FILE):
            try:
                with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                    self.key = f.read().strip()
            except OSError:
                pass
        if not self.key or len(self.key) != 64:
            messagebox.showerror('错误', '密钥无效，请先获取密钥或手动粘贴 64 位密钥')
            return
        self.b2.config(state='disabled')
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        self.log('[*] 启动 WCDB 服务...')
        try:
            os.makedirs(OUT, exist_ok=True)
            with open(KEY_FILE, 'w', encoding='utf-8') as f:
                f.write(self.key)

            from wcdb_server import WCDBClient
            data_dir = self.dir_var.get().strip()
            self.wcdb = WCDBClient()
            self.wcdb.start(self.key, data_dir)
            self.sessions = self.wcdb.get_sessions() or []

            # 预取昵称，会话列表里直接显示真实名字
            users = [s.get('username', '') for s in self.sessions
                     if s.get('username') and not str(s.get('username')).startswith('brand')]
            if users:
                try:
                    self.nick_map = self.wcdb.get_display_names(users[:500]) or {}
                except Exception:
                    self.nick_map = {}

            n = len(self.sessions)
            self.root.after(0, lambda: self._on_connected(n))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self._on_connect_failed(err))

    def _on_connected(self, n):
        self.b3.config(state='normal')
        self.b2.config(state='normal')
        self.session_label.config(text=f'{n} 个会话已就绪')
        self.log(f'✅ 已连接，{n} 个会话')
        messagebox.showinfo('成功', f'已连接数据库，共 {n} 个会话')

    def _on_connect_failed(self, err):
        self.b2.config(state='normal')
        self.log(f'❌ 连接失败')
        messagebox.showerror('连接失败', err)

    # ────────────────────── 会话页（勾选 + 统一导出） ──────────────────────

    def show_sessions(self):
        if not self.sessions:
            messagebox.showinfo('提示', '还没有会话数据，请先「连接数据库」')
            return
        self.clear()

        # 顶部标题栏
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text=f'会话列表（共 {len(self.sessions)} 个）',
                  font=('', 13)).pack(side=tk.LEFT)
        ttk.Button(top, text='返回首页', width=10, command=self.show_home).pack(side=tk.RIGHT)

        # 搜索栏
        sf = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        sf.pack(fill=tk.X)
        ttk.Label(sf, text='🔍 搜索:').pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ent = ttk.Entry(sf, textvariable=self.search_var, width=40)
        ent.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ent.focus()
        ttk.Button(sf, text='清空', width=6,
                   command=lambda: self.search_var.set('')).pack(side=tk.LEFT)

        # 选择操作栏
        sel = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        sel.pack(fill=tk.X)
        ttk.Button(sel, text='全选', width=8, command=self.select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel, text='全不选', width=8, command=self.select_none).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel, text='反选', width=8, command=self.invert_selection).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel, text='全选搜索结果', width=14,
                   command=self.select_visible).pack(side=tk.LEFT, padx=2)
        self.sel_label = ttk.Label(sel, text='已选 0 个', foreground='#2563eb')
        self.sel_label.pack(side=tk.LEFT, padx=14)
        ttk.Label(sel, text='（点击最左侧方框勾选；双击可预览消息）',
                  foreground='gray', font=('', 8)).pack(side=tk.LEFT)

        # 列表
        body = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        body.pack(fill=tk.BOTH, expand=True)
        cols = ('check', 'name', 'summary', 'time', 'wxid')
        self.tree = ttk.Treeview(body, columns=cols, show='headings', height=20)
        self.tree.heading('check', text='选')
        self.tree.heading('name', text='会话名称')
        self.tree.heading('summary', text='最后一条消息')
        self.tree.heading('time', text='时间')
        self.tree.heading('wxid', text='wxid')
        self.tree.column('check', width=40, anchor=tk.CENTER, stretch=False)
        self.tree.column('name', width=220)
        self.tree.column('summary', width=330)
        self.tree.column('time', width=130, stretch=False)
        self.tree.column('wxid', width=180, stretch=False)
        sb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Double-1>', self._on_tree_double)
        self.tree.bind('<space>', lambda e: self.toggle_focused())
        self.tree.bind('<Return>', lambda e: self._on_tree_double(e))

        # 底部统一导出栏
        bottom = ttk.LabelFrame(self.root, text='统一导出设置', padding=10)
        bottom.pack(fill=tk.X, padx=10, pady=8)

        r1 = ttk.Frame(bottom)
        r1.pack(fill=tk.X, pady=2)
        ttk.Label(r1, text='导出格式:').pack(side=tk.LEFT)
        self.fmt_var = tk.StringVar(
            value=next(label for label, key in FORMAT_CHOICES if key == FORMAT_DEFAULT))
        cb = ttk.Combobox(r1, textvariable=self.fmt_var, state='readonly', width=44,
                          values=[label for label, _ in FORMAT_CHOICES])
        cb.pack(side=tk.LEFT, padx=6)
        self.fmt_hint = ttk.Label(r1, text='', foreground='gray', font=('', 8))
        self.fmt_hint.pack(side=tk.LEFT, padx=6)
        cb.bind('<<ComboboxSelected>>', lambda e: self._update_fmt_hint())
        self._update_fmt_hint()

        r2 = ttk.Frame(bottom)
        r2.pack(fill=tk.X, pady=2)
        ttk.Label(r2, text='导出到:').pack(side=tk.LEFT)
        self.export_root_var = tk.StringVar(
            value=os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop'))
        ttk.Entry(r2, textvariable=self.export_root_var, width=58).pack(side=tk.LEFT, padx=6)
        ttk.Button(r2, text='浏览', width=8,
                   command=self._pick_export_root).pack(side=tk.LEFT, padx=2)

        r3 = ttk.Frame(bottom)
        r3.pack(fill=tk.X, pady=(6, 0))
        self.export_btn = ttk.Button(r3, text='📦 开始导出', width=20,
                                     command=self.do_batch_export)
        self.export_btn.pack(side=tk.LEFT)
        ttk.Button(r3, text='✏️ 编辑给AI的指令', width=18,
                   command=self.edit_prompt).pack(side=tk.LEFT, padx=6)
        ttk.Button(r3, text='重载指令', width=10,
                   command=self.reload_prompt).pack(side=tk.LEFT)
        self.export_hint = ttk.Label(r3, text='', foreground='gray')
        self.export_hint.pack(side=tk.LEFT, padx=10)

        self.search_var.trace_add('write', lambda *_: self.refresh_rows())
        self.refresh_rows()

    def _pick_export_root(self):
        d = filedialog.askdirectory(initialdir=self.export_root_var.get())
        if d:
            self.export_root_var.set(d)

    def _update_fmt_hint(self):
        """按当前格式提示导出结果长什么样，避免用户选错格式。"""
        hints = {
            'md': '每个会话一个 .md 文件，直接放在导出目录下，可拖进 DeepSeek 网页版',
            'ai': '每个会话一个文件夹，含 对话.jsonl（带发言人/类型/图片路径）',
            'html': '每个会话一个文件夹，含 index.html + 图片\\',
            'excel': '每个会话一个文件夹，含 <会话名>.xlsx',
            'csv': '每个会话一个文件夹，含 对话.csv',
            'pdf': '每个会话一个文件夹，含 <会话名>.pdf（图片内嵌）',
            'txt': '每个会话一个文件夹，含 对话.txt',
            'json': '每个会话一个文件夹，含 对话.json',
        }
        try:
            key = dict(FORMAT_CHOICES).get(self.fmt_var.get(), 'md')
            self.fmt_hint.config(text=hints.get(key, ''))
        except Exception:
            pass

    # ── 给 AI 的指令（随每份导出一起交给 AI） ──

    def _prompt_file(self):
        return os.path.join(BASE, 'AI提示词.txt')

    def edit_prompt(self):
        """用记事本打开 AI提示词.txt，改完保存即可生效（无需重新打包）。"""
        path = self._prompt_file()
        try:
            if not os.path.exists(path):
                # 首次：把当前生效的提示词（内置默认或已有文件）落盘，方便编辑
                import ai_prompt
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(ai_prompt.load_prompt(BASE).strip() + '\n')
            os.startfile(path)
            messagebox.showinfo(
                '编辑给 AI 的指令',
                '已用记事本打开：\n'
                f'{path}\n\n'
                '这段文字会被写进**每一份导出**（.md / txt / jsonl / html / pdf / 表格），\n'
                'DeepSeek 网页版没有系统提示词，所以要求必须写在文件里它才看得到。\n\n'
                '改完保存并关闭记事本，回到这里点「重载指令」让它立即生效。')
        except Exception as e:
            messagebox.showerror('无法打开', f'{path}\n{e}')

    def reload_prompt(self):
        try:
            import ai_prompt
            ai_prompt.clear_cache()
            text = ai_prompt.load_prompt(BASE)
            src = 'AI提示词.txt' if os.path.exists(self._prompt_file()) else '内置默认'
            preview = text.strip().split('\n')[0][:40]
            messagebox.showinfo('已重载', f'来源：{src}\n共 {len(text)} 字\n首行：{preview}…')
        except Exception as e:
            messagebox.showerror('重载失败', str(e))

    def _row_data(self, s):
        wxid = s.get('username', '') or '?'
        name = self.nick_map.get(wxid) or s.get('last_sender_display_name') or wxid
        summary = str(s.get('summary', '') or '')[:40]
        ts = s.get('last_timestamp', s.get('sort_timestamp', ''))
        if str(ts).isdigit():
            try:
                ts = datetime.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OSError):
                pass
        is_group = str(wxid).endswith('@chatroom')
        display = f'👥 {name}' if is_group else name
        return str(display)[:34], summary, str(ts)[:16], wxid

    def refresh_rows(self):
        """按搜索词重建列表，保留已勾选状态。"""
        kw = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.visible = []
        for s in self.sessions:
            wxid = s.get('username', '') or ''
            if not wxid:
                continue
            name, summary, ts, _ = self._row_data(s)
            if kw and kw not in name.lower() and kw not in wxid.lower() and kw not in summary.lower():
                continue
            mark = '☑' if wxid in self.selected else '☐'
            iid = self.tree.insert('', tk.END, values=(mark, name, summary, ts, wxid))
            self.visible.append((iid, wxid))
        self._update_sel_label()

    def _update_sel_label(self):
        try:
            self.sel_label.config(text=f'已选 {len(self.selected)} 个')
        except Exception:
            pass

    # ── 勾选 ──

    def _on_tree_click(self, event):
        """点最左侧「选」列切换勾选；点其它列只选中该行。"""
        if self.tree.identify_region(event.x, event.y) not in ('cell', 'tree'):
            return
        if self.tree.identify_column(event.x) != '#1':
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_iid(iid)
            return 'break'

    def _toggle_iid(self, iid):
        vals = list(self.tree.item(iid, 'values'))
        if not vals:
            return
        wxid = str(vals[4]) if len(vals) >= 5 else ''
        if not wxid:
            return
        if wxid in self.selected:
            self.selected.discard(wxid)
            vals[0] = '☐'
        else:
            self.selected.add(wxid)
            vals[0] = '☑'
        self.tree.item(iid, values=vals)
        self._update_sel_label()

    def toggle_focused(self):
        iid = self.tree.focus()
        if iid:
            self._toggle_iid(iid)

    def select_all(self):
        self.selected = {s.get('username') for s in self.sessions if s.get('username')}
        self.refresh_rows()

    def select_none(self):
        self.selected.clear()
        self.refresh_rows()

    def invert_selection(self):
        allw = {s.get('username') for s in self.sessions if s.get('username')}
        self.selected = allw - self.selected
        self.refresh_rows()

    def select_visible(self):
        for _iid, wxid in self.visible:
            self.selected.add(wxid)
        self.refresh_rows()

    # ── 预览（保留原功能的双击查看） ──

    def _on_tree_double(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], 'values')
        if len(vals) >= 5:
            self.show_chat(str(vals[4]))

    def show_chat(self, wxid):
        """原版的单会话预览窗口（保留）。"""
        if not self.wcdb:
            return
        try:
            total = self.wcdb.get_count(wxid)
        except Exception:
            total = 0

        title = self.nick_map.get(wxid) or wxid
        win = tk.Toplevel(self.root)
        win.title(f'{title} ({total} 条)')
        win.geometry('920x660')
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        top = ttk.Frame(win)
        top.pack(fill=tk.X, padx=6, pady=4)
        spin = ttk.Spinbox(top, from_=50, to=max(total, 200), increment=50, width=8)
        spin.set(min(500, total) or 50)
        spin.pack(side=tk.LEFT)
        info = ttk.Label(top, text=f'共 {total} 条')
        info.pack(side=tk.RIGHT)

        txt = tk.Text(win, wrap=tk.WORD, font=('微软雅黑', 10))
        scr = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=4)
        scr.pack(side=tk.RIGHT, fill=tk.Y, pady=4)

        def load():
            try:
                limit = int(spin.get())
            except (TypeError, ValueError):
                limit = 200
            try:
                raw = self.wcdb.get_messages(wxid, limit, 0)
            except Exception as e:
                info.config(text=f'错误: {e}')
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
            for d in os.listdir(self.dir_var.get() or '.'):
                if d.startswith('wxid_'):
                    my = clean_wxid(d)
                    break
            rows = message_content.prepare(raw, nick, my, '我')
            txt.config(state=tk.NORMAL)
            txt.delete('1.0', tk.END)
            for m in rows:
                who = m.get('sender_display') or ''
                if m.get('kind') == 'system':
                    txt.insert(tk.END, f"        {m.get('time_str','')}  {m.get('text','')}\n\n")
                else:
                    txt.insert(tk.END, f"{m.get('time_str','')}  {who}\n  {m.get('text','')}\n\n")
            txt.see(tk.END)
            txt.config(state=tk.DISABLED)
            info.config(text=f'{len(rows)} 条消息')

        ttk.Button(top, text='加载', command=load).pack(side=tk.LEFT, padx=4)
        load()

    # ────────────────────── 批量导出 ──────────────────────

    def do_batch_export(self):
        if self.busy:
            messagebox.showinfo('提示', '正在导出中，请稍候')
            return
        if not self.selected:
            messagebox.showwarning('提示', '请先勾选要导出的会话')
            return
        if not self.wcdb:
            messagebox.showerror('错误', '请先连接数据库')
            return

        out_root = self.export_root_var.get().strip()
        if not out_root:
            messagebox.showerror('错误', '请选择导出位置')
            return
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            messagebox.showerror('错误', f'无法创建导出目录:\n{out_root}\n{e}')
            return

        fmt = dict(FORMAT_CHOICES)[self.fmt_var.get()]
        picked = []
        for s in self.sessions:
            w = s.get('username', '')
            if w in self.selected:
                picked.append({'wxid': w, 'title': self.nick_map.get(w) or w})
        if not picked:
            messagebox.showwarning('提示', '勾选的会话无效，请重新选择')
            return

        self._open_progress_window(picked, fmt, out_root)

    def _open_progress_window(self, picked, fmt, out_root):
        self.busy = True
        self.export_btn.config(state='disabled')

        win = tk.Toplevel(self.root)
        win.title('导出中...')
        win.geometry('520x260')
        win.resizable(False, False)
        win.transient(self.root)
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        ttk.Label(win, text=f'正在导出 {len(picked)} 个会话',
                  font=('', 12)).pack(pady=(14, 4))
        cur = ttk.Label(win, text='准备中...', foreground='#2563eb')
        cur.pack(pady=2)
        bar = ttk.Progressbar(win, mode='determinate', maximum=len(picked), length=440)
        bar.pack(pady=8)

        log_box = tk.Text(win, height=7, wrap=tk.NONE, font=('Consolas', 8))
        log_box.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
        log_box.config(state=tk.DISABLED)

        btns = ttk.Frame(win)
        btns.pack(pady=(0, 12))
        cancel_btn = ttk.Button(btns, text='取消导出', width=14)
        cancel_btn.pack(side=tk.LEFT, padx=4)
        open_btn = ttk.Button(btns, text='打开导出目录', width=14, state='disabled')
        open_btn.pack(side=tk.LEFT, padx=4)
        close_btn = ttk.Button(btns, text='关闭', width=10, state='disabled')
        close_btn.pack(side=tk.LEFT, padx=4)

        cancel_flag = {'v': False}
        result = {}

        def on_cancel():
            cancel_flag['v'] = True
            cancel_btn.config(state='disabled', text='正在停止...')

        cancel_btn.config(command=on_cancel)

        def ui_log(line):
            log_box.config(state=tk.NORMAL)
            log_box.insert(tk.END, str(line) + '\n')
            log_box.see(tk.END)
            log_box.config(state=tk.DISABLED)

        def on_progress(done, total, title):
            def upd():
                bar['value'] = done - 1
                cur.config(text=f'第 {done}/{total} 个：{title}')
            self.root.after(0, upd)

        def worker():
            import batch_export
            try:
                res = batch_export.export_sessions(
                    self.wcdb, self.dir_var.get().strip(), picked, fmt, out_root,
                    progress=on_progress,
                    should_cancel=lambda: cancel_flag['v'],
                    log=lambda s: self.root.after(0, lambda ss=s: ui_log(ss)),
                    base_dir=BASE,
                )
                result.update(res)
            except Exception as e:
                import traceback
                result['fatal'] = f'{e}\n{traceback.format_exc()}'
            self.root.after(0, finish)

        def finish():
            self.busy = False
            self.export_btn.config(state='normal')
            bar['value'] = bar['maximum']
            cancel_btn.config(state='disabled')
            close_btn.config(state='normal')

            if 'fatal' in result:
                cur.config(text='导出失败')
                ui_log(result['fatal'])
                close_btn.config(command=win.destroy)
                messagebox.showerror('导出失败', result['fatal'].splitlines()[0])
                return

            ok = result.get('ok', [])
            failed = result.get('failed', [])
            cancelled = result.get('cancelled')
            root_dir = result.get('root', '')

            if cancelled:
                cur.config(text=f'已取消（完成 {len(ok)} 个）')
            else:
                cur.config(text=f'导出完成：成功 {len(ok)} 个，失败 {len(failed)} 个')
            ui_log(f'导出目录: {root_dir}')
            for f in failed:
                ui_log(f'失败 {f.get("title")}: {f.get("error")}')

            open_btn.config(state='normal',
                            command=lambda: os.startfile(root_dir) if os.path.isdir(root_dir) else None)
            close_btn.config(command=win.destroy)

            msg = f'成功导出 {len(ok)} 个会话\n'
            if failed:
                msg += f'失败 {len(failed)} 个\n'
            if cancelled:
                msg += '（已取消，未处理剩余会话）\n'
            msg += f'\n导出位置：\n{root_dir}\n\n已生成「导出清单.html」，可双击打开查看。'
            messagebox.showinfo('导出完成', msg)

        close_btn.config(command=win.destroy)
        threading.Thread(target=worker, daemon=True).start()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == '__main__':
    main()
