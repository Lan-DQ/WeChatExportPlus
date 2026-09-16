# -*- coding: utf-8 -*-
"""DeepSeek 官网宿主：拉起打包自带的 Electron，把它**嵌进**主窗口，并通过
本机 HTTP 控制它（挂附件 / 发送 / 截断）。

为什么用 Electron 而不是 WebView2
---------------------------------
发布包里本来就带 `electron/electron.exe`（WCDB 运行时备选，222MB），所以内嵌浏览器
**不增加任何新依赖**。而 Electron 自带 CDP，可以做到「程序化把本地文件挂到网页的
上传框上」——这是本功能的核心能力。

「嵌入」是怎么做的（2026-09-16 实测定型）
----------------------------------------
**用 owner 关系，绝不用 `SetParent`。** 三种形态的真实按键实测
（`C:\\My_AI_Gongju\\ds\\projectOne\\test_crossproc_kbd.py`）：

| 形态 | `GetParent` | 真实按键（SendInput） |
|---|---|---|
| 独立顶层窗口 | 0 | ✅ 收得到 |
| `SetParent` 成子窗口 | =宿主画布 | ❌ **收不到（空）** |
| **owner 窗口** | =宿主顶层窗口 | ✅ **收得到** |

同一个进程、同一个窗口、同一串按键，唯一变量就是有没有 `SetParent` —— 所以
"跨进程子窗口收不到真实键盘"是 Windows 的硬限制。v3.0.0 用 `SetParent`，因此它
"功能都正常、只有输入有时进不去"；v3.0.1 试过用 `AttachThreadInput` 补救，结果
赔上输入法死锁（界面变白、CPU 不高）。

owner 形态（`GWLP_HWNDPARENT` 指向宿主顶层窗口）是唯一交集：
  · 键盘通道完全正常（它本来就是顶层窗口）；
  · owner 永远压在宿主之上 → 观感就是"嵌在里面"；
  · owner 最小化时一起消失；
  · 不占任务栏、不在 Alt+Tab 里单独出现。

坑（都踩过/想清楚了）
---------------------
* **user-data-dir 必须独立**：同一个 electron.exe 还被 WCDB 服务占用着默认 profile
  `%APPDATA%\\Electron`，两个进程共用会撞 Chromium 单例锁。这里强制传 `--profile`。
* **UA 必须伪装成普通 Chrome**：用 Electron 默认 UA 时官网会顶一条「使用环境异常」。
* DPI：主程序是 PROCESS_SYSTEM_DPI_AWARE，Electron 是 per-monitor v2，而
  `win.setBounds()` 收的是**逻辑像素**（DIP）。所以 `place()` 给的物理像素必须
  ÷缩放比（见那里），否则 125% 缩放下窗口大一圈、位置也偏。
* 关程序时**不能阻塞**：清理（/quit → terminate → taskkill /T）丢到 daemon 线程；
  且要先 `detach()` 解除 owner，否则 owner 销毁会把浏览器一起带走。
"""
import ctypes
import ctypes.wintypes          # ⚠️ 必须显式 import：`import ctypes` **不会**带出 wintypes
                                # 子模块，裸写 ctypes.wintypes.DWORD() 会抛
                                # AttributeError。打包版（PyInstaller）里没有别的模块
                                # 顺带 import 它，所以只有打包后才会暴露 —— 曾经
                                # 导致 3.0.1 的键盘修复在发布版里**完全失效**，
                                # 且异常被 except 吞掉只留一行日志。
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_URL = 'https://chat.deepseek.com/'
CHROME_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

# ── Win32 ──
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
GWLP_HWNDPARENT = -8          # 顶层窗口下 = owner 句柄（**不是** 父子关系）
GA_ROOT = 2                   # GetAncestor 用它拿顶层窗口

# SetWindowLongPtrW 的参数类型必须声明：64 位下不声明会把指针截成 32 位
# （ctypes 默认按 c_int 传），HWND 一截断就是写到别的窗口上 —— 静默、且致命。
_u32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int,
                                   ctypes.c_ssize_t]
_u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

_u32.GetParent.argtypes = [ctypes.wintypes.HWND]
_u32.GetParent.restype = ctypes.wintypes.HWND
_u32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
_u32.GetAncestor.restype = ctypes.wintypes.HWND
_u32.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.wintypes.UINT]
_u32.SetWindowPos.restype = ctypes.wintypes.BOOL

# ⚠️⚠️ 起**控制台程序**（taskkill/cmd/wmic 这类）必须带这个标志，否则每次调用都会
# 闪一个黑框 —— 用户实测反馈"关闭后跳出来一堆黑框（可能是 cmd）"就是这个：
# 退出时 WCDB 与浏览器各自都会 taskkill（有的还会重试），一次退出能闪好几个。
# `capture_output=True` 只重定向管道，**不阻止控制台窗口出现**。
CREATE_NO_WINDOW = 0x08000000
NO_WINDOW_KW = ({'creationflags': CREATE_NO_WINDOW} if os.name == 'nt' else {})


def find_electron(base):
    """找打包自带的 electron.exe。"""
    for rel in (os.path.join('electron', 'electron.exe'),
                os.path.join('runtime', 'electron.exe')):
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return p
    return ''


def find_dsview(base):
    """找 Electron 侧的应用目录（dsview/main.js）。"""
    p = os.path.join(base, 'dsview')
    return p if os.path.isfile(os.path.join(p, 'main.js')) else ''


def _dpi_scale_for(hwnd):
    """该窗口所在显示器的缩放比（1.0 / 1.25 / 1.5 …）。

    为什么需要：宿主（Tk）是 DPI 感知的，给的是**物理像素**；而 Electron 的
    `win.setBounds()` 收的是**逻辑像素（DIP）**。125% 缩放下不换算，窗口就会
    大 25%、位置也偏（实测：要 820x560 得到 1025x700）。
    """
    try:
        dpi = _u32.GetDpiForWindow(ctypes.c_void_p(int(hwnd)))
        if dpi:
            return dpi / 96.0
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except (OSError, ValueError, AttributeError):
        return 1.0


class DeepSeekHost:
    """一只 Electron 进程 = 一个 DeepSeek 页面。懒启动，用完 shutdown()。"""

    def __init__(self, base, url=DEFAULT_URL, log=None, extra_args=(), profile=''):
        self.base = base
        self.url = url or DEFAULT_URL
        self.log = log or (lambda *_a, **_k: None)
        self.extra_args = list(extra_args)
        self.proc = None
        self.port = 0
        self.hwnd = 0
        self.token = ''
        # 登录态存这里。**发布包必须排除它**（里面有官网 cookie）；
        # 也绝不能和 WCDB 服务用的默认 profile 混用（Chromium 单例锁会打架）。
        self.profile = profile or os.path.join(base, 'ds_profile')
        # 浏览器窗口是否已按主窗口摆好位（独立顶层窗口方案用）
        self._placed = False
        # 摆位的"物理像素 → Electron 逻辑像素"乘数：None = 还没校准（见 place()）
        self._bounds_scale = None
        self._bounds_calibrated = False
        # 「嵌入形态」= owner 关系（**不是** SetParent 子窗口，见 set_owner()）。
        # _parent_hwnd 是 owner 顶层窗口句柄；为 0 表示当前是普通独立窗口。
        self._parent_hwnd = 0
        self._reader = None
        self.start_error = ''

    # ── 生命周期 ──

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=45):
        """拉起进程并等握手。成功返回 True；失败把原因放进 self.start_error。"""
        if self.running:
            return True
        exe = find_electron(self.base)
        app = find_dsview(self.base)
        if not exe:
            self.start_error = f'找不到 electron.exe（应在 {self.base}\\electron\\）'
            return False
        if not app:
            self.start_error = f'找不到 dsview 应用目录（应在 {self.base}\\dsview\\）'
            return False
        try:
            os.makedirs(self.profile, exist_ok=True)
        except OSError as e:
            self.start_error = f'无法创建 profile 目录：{e}'
            return False

        self.token = ''.join(random.choice('0123456789abcdef') for _ in range(32))
        cmd = [exe, app,
               f'--url={self.url}',
               f'--token={self.token}',
               f'--profile={self.profile}',
               f'--ua={CHROME_UA}']
        cmd += self.extra_args
        self.log(f'启动内嵌浏览器：{os.path.basename(exe)} dsview')
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=self.base, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            self.start_error = f'启动 electron 失败：{e}'
            return False

        # ⚠️ 读握手必须走独立线程 + 队列：直接在界面线程 readline()，一旦 electron
        #    只启动不说话，就会把整个 Tk 主线程挂死（超时也救不回来）。
        self._ready_seen = False
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name='dsview-stdout')
        self._reader.start()

        line = self._read_ready(timeout)
        if not line:
            self.start_error = self.start_error or '内嵌浏览器启动超时（没等到 DSVIEW_READY）'
            self._kill_tree()
            return False
        try:
            data = json.loads(line.split('DSVIEW_READY', 1)[1].strip())
            self.port = int(data.get('port') or 0)
            self.hwnd = int(data.get('hwnd') or 0)
        except (ValueError, TypeError, IndexError) as e:
            self.start_error = f'握手数据解析失败：{e}（原始行：{line[:120]}）'
            self._kill_tree()
            return False
        if not self.port or not self.hwnd:
            self.start_error = f'握手数据不完整：{line[:120]}'
            self._kill_tree()
            return False

        self._ready_seen = True      # 之后的 stdout 只当日志，不再进队列
        self.log(f'内嵌浏览器就绪（端口 {self.port}）')
        return True

    def _pump(self):
        """把子进程 stdout 搬进队列/日志（唯一读管道的地方）。"""
        try:
            for line in self.proc.stdout:
                s = (line or '').rstrip()
                if not self._ready_seen:
                    self._q.put(s)
                elif s.strip():
                    self.log(f'[dsview] {s[:200]}')
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(None)        # EOF：让 _read_ready 能立刻醒过来

    def _read_ready(self, timeout):
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.start_error = '内嵌浏览器启动超时'
                return None
            try:
                line = self._q.get(timeout=min(0.3, left))
            except queue.Empty:
                if self.proc.poll() is not None:
                    self.start_error = f'electron 提前退出（code={self.proc.returncode}）'
                    return None
                continue
            if line is None:
                self.start_error = f'electron 没输出握手就退出了（code={self.proc.poll()}）'
                return None
            if 'DSVIEW_ERR' in line:
                # Electron 侧明确报错：立刻带着原因失败，别白等到超时
                self.start_error = '宿主启动失败：' + line.split('DSVIEW_ERR', 1)[1].strip()[:200]
                return None
            if 'DSVIEW_READY' in line:
                return line.strip()
            if line.strip():
                self.log(f'[dsview] {line[:200]}')

    def _kill_tree(self):
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                # 先按进程树杀：Electron 主进程会派生 gpu/network/renderer 子进程，
                # 只 terminate 主进程会留下孤儿进程占着 electron.exe 和 dll。
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.proc.pid)],
                               capture_output=True, timeout=10, **NO_WINDOW_KW)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, subprocess.SubprocessError):
            pass

    def shutdown(self, wait=False):
        """关掉宿主。默认后台线程清理，绝不阻塞界面线程。"""
        proc = self.proc
        if not proc:
            return
        self.proc = None

        def work():
            try:
                self._post('/quit', {}, timeout=1.5)
            except Exception:  # noqa: BLE001
                pass
            # 先 /quit（优雅），再 **taskkill /T /F**（连子进程一起）。
            # 顺序很重要：必须在主进程还活着时按树杀 —— 先 terminate 主进程的话，
            # 树就断了，gpu/network/renderer 会变成孤儿进程，占着 electron.exe 和
            # dll 不放（实测后果：关掉程序后进程管理器里一堆 electron 残留，
            # 下次重新打包会因为文件被占用而失败）。
            try:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                               capture_output=True, timeout=10, **NO_WINDOW_KW)
            except Exception:  # noqa: BLE001
                pass
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    # ── HTTP 控制 ──

    def _request(self, path, payload=None, timeout=30, method=None):
        if not self.port:
            return {'ok': False, 'error': '宿主未启动'}
        url = f'http://127.0.0.1:{self.port}{path}'
        data = None
        headers = {'X-Token': self.token}
        m = method
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
            m = m or 'POST'
        req = urllib.request.Request(url, data=data, headers=headers, method=m or 'GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode('utf-8', 'replace')
            return json.loads(body) if body.strip() else {'ok': True}
        except urllib.error.HTTPError as e:
            return {'ok': False, 'error': f'HTTP {e.code}', 'detail': e.read()[:200].decode('utf-8', 'replace')}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {'ok': False, 'error': str(e)}

    def _post(self, path, payload=None, timeout=30):
        return self._request(path, payload if payload is not None else {}, timeout, 'POST')

    def ping(self):
        return self._request('/ping', None, 5, 'GET')

    def state(self):
        return self._request('/state', None, 10, 'GET')

    def navigate(self, url):
        return self._post('/navigate', {'url': url}, 30)

    def set_theme(self, mode):
        """让内嵌网页跟着软件的深浅色走（Electron nativeTheme.themeSource）。

        mode: 'dark' | 'light' | 'system'
        """
        mode = mode if mode in ('dark', 'light', 'system') else 'system'
        return self._post('/theme', {'mode': mode}, 10)

    def set_background(self, color):
        """把浏览器窗口的底色同步成主窗口那块面板的颜色。

        目的：**消除边界感**（用户反馈"这完全是两个软件"）。页面还没画出来、
        或者切换主题的那一瞬间，窗口底色会露出来；纯白底配深色面板就会闪白角。
        """
        color = str(color or '').strip()
        if not color.startswith('#') or len(color) != 7:
            return {'ok': False, 'error': 'bad-color'}
        return self._post('/bg', {'color': color}, 5)

    def diag(self):
        return self._post('/diag', {}, 15)

    def show(self):
        """显示浏览器窗口。

        ⚠️⚠️ **owner 设好之后，对"隐藏中"的窗口调 show() 会让 Electron 主进程卡死**
        （实测：`/show` 10 秒超时，Electron 侧诊断停在 `show()` 那一行里不出来）。
        触发条件很明确：
        | 序列 | 结果 |
        |---|---|
        | 先 show → 再设 owner | ✅ 0.05s |
        | owner + 已可见 | ✅ 0.00s |
        | **owner + 隐藏 → show()** | ❌ **卡死** |

        而"退回导出页再进来"正是 `hide()` → `show()`，所以这条必须绕开。
        绕法：**show 之前先摘掉 owner，show 完立刻挂回去**（两步都是 0.00s，安全）。
        """
        t0 = time.time()
        owner = self._parent_hwnd
        if owner:
            # 摘掉 owner（纯 Win32，瞬间完成），避免在 owned + hidden 状态下 show
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, 0)
            except (OSError, ValueError, AttributeError):
                pass
        r = self._post('/show', {}, 10)
        if owner:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, int(owner))
            except (OSError, ValueError, AttributeError):
                pass
        self.log('show: %.2fs %s' % (time.time() - t0, 'ok' if r.get('ok') else r.get('error')))
        return r

    def hide(self):
        """隐藏浏览器窗口。

        ⚠️ 和 `show()` 同理：**有 owner 时先摘掉 owner 再 hide**，否则 Electron 主进程
        会对这个 owned 窗口同步卡住（实测 `hide` 拖到宿主 10s 超时，`_ds_leave()` 因此
        要 18 秒 —— 用户看到的就是"切回导出页卡一下"）。
        """
        t0 = time.time()
        owner = self._parent_hwnd
        if owner:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, 0)
            except (OSError, ValueError, AttributeError):
                pass
        r = self._post('/hide', {}, 10)
        if owner:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, int(owner))
            except (OSError, ValueError, AttributeError):
                pass
        self.log('hide: %.2fs %s' % (time.time() - t0, 'ok' if r.get('ok') else r.get('error')))
        return r

    def focus(self):
        """把键盘焦点交给浏览器页面。返回是否成功。

        ⚠️ 历史（别再走回去）：v3.0.0~3.0.1 用 `AttachThreadInput` 把宿主线程和
        Chromium 线程接在一条输入队列上，再从宿主侧 `SetFocus`；两个后果：
        ① 永久连接会让输入法（MSCTF/IME）跨进程死锁，界面"未响应"、CPU 却不高；
        ② 即便接上也时灵时不灵（用户实测"光标在闪，但打字和 Ctrl+V 都进不去"）。

        现在浏览器是 **owner 窗口**（顶层窗口 + owner 关系，不是 `SetParent` 子窗口），
        所以焦点交给 Electron 自己办最可靠 —— 它调 win.focus() + webContents.focus()，
        浏览器进程本来就是"用户刚点过的那个软件"。**不需要任何 AttachThreadInput。**
        """
        if not self.running:
            return False
        return self.summon()

    # ── 真嵌入（Win32）──

    def set_owner(self, parent_hwnd):
        """把浏览器窗口设成宿主窗口的 **owner**（不是 child！）。

        ⚠️⚠️ **为什么必须是 owner、绝不能是 `SetParent` 子窗口**（2026-09-16 实测，
        见 `C:\\My_AI_Gongju\\ds\\projectOne\\test_crossproc_kbd.py`）：

        | 形态 | 真实按键（SendInput） |
        |---|---|
        | 独立顶层窗口 | ✅ 收得到（'crossproc-4711'） |
        | `SetParent` 成子窗口 | ❌ **收不到（空）** |
        | **owner 窗口** | ✅ **收得到** |

        同一个进程、同一个窗口、同一串按键，**唯一变量就是有没有 SetParent** —— 所以
        "跨进程子窗口收不到真实键盘"是 Windows 的硬限制（`AttachThreadInput` +
        `SetFocus` 也救不回来，只会再赔上 IME 死锁）。而 owner 关系是**顶层窗口**，
        键盘通道完全正常，同时又能：
          · 永远压在 owner 之上（观感就是"嵌在里面"）；
          · owner 最小化时一起消失；
          · 不占任务栏、不在 Alt+Tab 里单独出现。
        这正是"看起来像嵌入"与"键盘能用"的唯一交集。

        ⚠️ 传进来的必须是**顶层窗口**句柄（owner 不能是子窗口），Tk 里用
        `winfo_id()` 拿到的画布句柄需要 `GetAncestor(GA_ROOT)` 换成顶层窗口。
        """
        if not self.running or not self.hwnd:
            return False
        try:
            child = ctypes.c_void_p(self.hwnd)
            # 万一以前被设成过子窗口，先还原成顶层窗口（幂等，重复调用无害）
            style = int(_u32.GetWindowLongPtrW(child, GWL_STYLE))
            if style & WS_CHILD:
                _u32.SetWindowLongPtrW(child, GWL_STYLE,
                                       (style & ~WS_CHILD) | WS_POPUP)
            _u32.SetWindowLongPtrW(child, GWLP_HWNDPARENT, int(parent_hwnd))
            _u32.SetWindowPos(child, ctypes.c_void_p(0), 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                              | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            self._parent_hwnd = int(parent_hwnd)
            # 告诉 Electron "现在有 owner 了"：它必须在 /bounds 里停止对隐藏窗口
            # show()（那个组合会让主进程卡死，见 show() 的说明）。
            try:
                self._post('/owner', {'on': True}, 8)
            except (OSError, ValueError):
                pass
            self.log(f'已设为嵌入形态（owner={int(parent_hwnd)}）')
            return True
        except (OSError, ValueError, AttributeError) as e:
            self.log(f'设 owner 失败：{type(e).__name__}: {e}')
            return False


    # ── 真嵌入（Win32）──

    def embed_child(self, parent_hwnd, x, y, w, h):
        """把浏览器挂成宿主的**真子窗口**（`SetParent` + `WS_CHILD`）。

        这就是 **v3.0.0 的形态**，用户实测评价："拖动特别好，就像完全就是里面自带的
        东西，不会一卡一卡不会变位置"。原因很直接：
        **子窗口是 Windows 自己搬着父窗口走的** —— 不需要任何"跟随"逻辑、不需要
        发 HTTP 让它挪位置，所以拖动零延迟、松手不偏移。

        ⚠️ 坐标是**父窗口客户区坐标**（子窗口天然如此），所以直接给 `_ds_rect()`
        的结果，**不做任何 DPI 换算**（v3.0.0 就是这么写的；加了换算反而会偏）。

        ⚠️ 代价（2026-09-16 实测，见 `test_crossproc_kbd.py`）：跨进程子窗口
        **可能收不到真实键盘**（当时用合成按键测不出来，用户手按也报告过"打不了字"）。
        所以这个后端是**可选**的：观感最好，但要先确认还能打字（`ds_backend=child`）。
        """
        if not self.running or not self.hwnd:
            return False
        try:
            child = ctypes.c_void_p(self.hwnd)
            parent = ctypes.c_void_p(int(parent_hwnd))
            # 已经在目标父窗口下就只改位置/样式，别重复 SetParent（幂等）
            now = int(_u32.GetParent(child) or 0)
            if now != int(parent_hwnd):
                _u32.SetParent(child, parent)
            style = int(_u32.GetWindowLongPtrW(child, GWL_STYLE))
            if not (style & WS_CHILD) or (style & WS_POPUP):
                _u32.SetWindowLongPtrW(child, GWL_STYLE,
                                       (style & ~WS_POPUP) | WS_CHILD | WS_VISIBLE)
            # ⚠️ 子窗口不能带 WS_EX_NOACTIVATE（会直接挡掉键盘）
            ex = int(_u32.GetWindowLongPtrW(child, GWL_EXSTYLE))
            if ex & WS_EX_NOACTIVATE:
                _u32.SetWindowLongPtrW(child, GWL_EXSTYLE, ex & ~WS_EX_NOACTIVATE)
            self._setpos(x, y, w, h)
            self._embedded = True
            self._parent_hwnd = int(parent_hwnd)
            self.log(f'真子窗口嵌入完成：parent={int(parent_hwnd)} rect=({x},{y},{w},{h})')
            return True
        except (OSError, ValueError, AttributeError) as e:
            self.log(f'子窗口嵌入失败：{type(e).__name__}: {e}')
            return False

    def _setpos(self, x, y, w, h):
        """摆一个**子窗口**（坐标＝父窗口客户区，无 DPI 换算）。"""
        if not self.hwnd:
            return False
        try:
            _u32.SetWindowPos(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(0),
                              int(x), int(y), max(120, int(w)), max(80, int(h)),
                              SWP_NOZORDER | SWP_FRAMECHANGED)
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def embed(self, parent_hwnd, x, y, w, h):
        """把浏览器摆成「嵌入」形态（owner 关系 + 摆到 (x,y,w,h)）。

        ⚠️ (x,y,w,h) 是**屏幕坐标**（独立顶层窗口语义），不是父窗口客户区坐标 ——
        和 v3.0.0 的 SetParent 版不同。调用方用 _ds_screen_rect() 给坐标。

        为什么不再用 SetParent：跨进程子窗口收不到真实键盘（实测见 set_owner()）。
        """
        if not self.set_owner(parent_hwnd):
            return False
        return self.place(x, y, w, h)


    def attach(self, paths, timeout_ms=180000, settle_ms=None):
        """把文件挂到页面的上传框上。

        settle_ms：这批文件在"挂上"之后还要等多少毫秒才允许点发送。
        由调用方按文件类型给（文档慢、图片快），不给就用页面侧的默认值。
        """
        body = {'files': [os.path.abspath(p) for p in paths],
                'timeoutMs': int(timeout_ms)}
        if settle_ms is not None:
            body['settleMs'] = int(settle_ms)
        return self._post('/attach', body,
                          timeout=max(30, int(timeout_ms / 1000) + 30))

    def send(self, method='auto', text=''):
        """点发送。

        text 非空时，先把这段文字填进输入框，再和附件一起发出去
        （最后一批用它附带「我已发送完毕」）。
        """
        body = {'method': method}
        if text:
            body['text'] = text
        # 确认窗口本身可能长达 45 秒（等附件上传完），HTTP 超时给足
        return self._post('/send', body, 90)

    def stop(self, timeout_ms=20000):
        return self._post('/stop', {'timeoutMs': int(timeout_ms)},
                          timeout=max(20, int(timeout_ms / 1000) + 15))

    # ── 窗口摆放：屏幕坐标（owner 形态下也一样）──

    def place(self, x, y, w, h):
        """把浏览器窗口摆到屏幕坐标 (x,y,w,h)，单位是**物理像素**。

        ⚠️ 为什么必须用屏幕坐标：浏览器窗口是 **owner 窗口**（顶层窗口 + owner
        关系），不是 `SetParent` 子窗口 —— 子窗口才用父窗口客户区坐标。所以
        owner 形态下"摆位"= 屏幕坐标摆一个顶层窗口。

        ⚠️⚠️ **单位换算**（2026-09-16 实测定型）。历史上这里写着"`setBounds()` 收 DIP，
        所以物理像素要 ÷1.25"，但**实测不成立**：本机（DPI=120/125%）反复量到
        "发多少得多少" —— 发 512 DIP 得 513 物理像素、发 798 得 799，**乘数 = 1.0**。
        而按 1.25 除会让窗口只有应得的 80%（要 640x411 只摆出 512x330）。
        所以默认**按 1:1 发物理像素**；万一某个环境真的会缩放，`place()` 里有一步
        自校准：量回真实像素、反推乘数、按新值重摆（只在偏差 >2px 时触发）。
        """
        if not self.running:
            return False
        scale = self._bounds_scale if self._bounds_scale else 1.0
        body = {'x': int(round(int(x) / scale)), 'y': int(round(int(y) / scale)),
                'w': max(200, int(round(int(w) / scale))),
                'h': max(150, int(round(int(h) / scale)))}
        r = self._post('/bounds', body, 10)
        self._placed = bool(r.get('ok'))
        if not self._placed:
            self.log('place 失败：%s' % r)
        # ── 自校准：量回真实像素，反推"Electron 实际生效的乘数" ──
        if self._placed and not self._bounds_calibrated:
            try:
                rc = ctypes.wintypes.RECT()
                if _u32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rc)):
                    got_w = rc.right - rc.left
                    # 默认按 1:1 发；只有量回来"明显不是我们要的尺寸"才校准。
                    if got_w > 0 and abs(got_w - int(w)) > 4:
                        self._bounds_scale = max(0.5, min(3.0, got_w / float(w)))
                        self._bounds_calibrated = True
                        b2 = {'x': int(round(int(x) / self._bounds_scale)),
                              'y': int(round(int(y) / self._bounds_scale)),
                              'w': max(200, int(round(int(w) / self._bounds_scale))),
                              'h': max(150, int(round(int(h) / self._bounds_scale)))}
                        r2 = self._post('/bounds', b2, 10)
                        self._placed = bool(r2.get('ok'))
                        self.log('摆位缩放自校准：1.000 → %.3f（已按新值重摆）'
                                 % self._bounds_scale)
                    else:
                        self._bounds_calibrated = True
            except (OSError, ValueError, AttributeError):
                pass
        # 排查 DPI 换算时打开（会显示在界面底部一行，平时别开）
        if os.environ.get('DSVIEW_DIAG_PLACE'):
            self.log('place: 物理(%d,%d,%d,%d) ÷%.3f → 逻辑(%d,%d,%d,%d)'
                     % (x, y, w, h, scale, body['x'], body['y'], body['w'], body['h']))        # elif abs(scale - 1.0) > 0.01:
        #     self.log('place: 物理(%d,%d,%d,%d) ÷%.2f → 逻辑(%d,%d,%d,%d)'
        #              % (x, y, w, h, scale, body['x'], body['y'], body['w'], body['h']))
        return self._placed

    def set_topmost(self, on=True):
        """让浏览器窗口置顶（只压在主窗口上面）。

        独立顶层窗口的代价：切到别的程序它不会自动让位。alwaysOnTop 能解决
        "看着像嵌在里面"，同时避免它盖住别人的窗口。

        ⚠️ **owner 形态下要摘掉 owner 再设**：实测 owned 窗口上设 alwaysOnTop 会让
        Electron 主进程同步卡住（宿主 10s 超时）。owner 关系本身已经保证"压在宿主
        之上"，所以这个调用对 embed 后端其实没有意义 —— app_plus 也已经不对 embed
        调它了；这里只是保证万一被调到也不会卡。
        """
        if not self.running:
            return False
        owner = self._parent_hwnd
        if owner:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, 0)
            except (OSError, ValueError, AttributeError):
                pass
        r = self._post('/topmost', {'on': bool(on)}, 8)
        if owner:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd),
                                       GWLP_HWNDPARENT, int(owner))
            except (OSError, ValueError, AttributeError):
                pass
        return bool(r.get('ok'))

    def focused_info(self):
        """页面上"用户最后点过的输入框"是什么（给界面提示用）。"""
        if not self.running:
            return {}
        r = self._post('/focused', {}, 8)
        return r.get('info') or {}

    def eval_js(self, expr, timeout=15):
        """在页面里执行一段表达式并回读结果（**诊断/验收用**）。

        它是"真实键盘到底打进去了没有"的唯一可信判据 —— win32 焦点、页面
        `document.hasFocus()` 全都可能对而字进不去（v3.0.0 的"光标在闪、字进不去"
        就是这样）。只有回读**页面里真实的文本**才算数。
        """
        if not self.running:
            return {'ok': False, 'error': 'not-running'}
        r = self._post('/debug/eval', {'expr': str(expr)}, timeout)
        if not r.get('ok'):
            self.log(f'eval_js 失败：{r.get("error")}')
        return r

    def focus_composer(self, timeout=10):
        """让页面把输入框聚焦好（**只聚焦，不写任何字**）。

        验收"真实键盘"时必须用它把变量隔离：不能用 type_text（那是注入），
        否则分不清字是打进去的还是注入进去的。
        """
        if not self.running:
            return False
        r = self._post('/debug/focus-composer', {}, timeout)
        return bool(r.get('ok'))

    def clear_attachments(self, timeout=15):
        """把页面上"还没发出去"的附件全部点掉。

        ⚠️ 为什么必须有这条（用户实测反馈）：取消发送后，页面上已经挂上去的附件
        会**留在输入区**。下一次发送的 `_ensure_idle()` 会因此判定"输入区还挂着 N 个
        附件"而**拒绝继续**，用户看到的就是"取消了却一直让我等/让我自己去页面上删"。

        做法：找页面上的附件条目（`[data-ds-attachment]`），逐个点掉它的删除按钮。
        返回 {'ok', 'removed', 'before', 'error'}。
        """
        if not self.running:
            return {'ok': False, 'error': 'not-running'}
        return self._post('/debug/clear-attachments', {}, timeout)

    def move_win32(self, x, y, w, h):
        """★ **不走 HTTP** 直接摆位（owner 窗口，屏幕坐标，物理像素）。

        ⚠️ 为什么需要它（2026-09-16 实测到根上）：
        owner 窗口不是子窗口，主窗口一动就得由我们主动跟着摆。原先每次跟随都要
        `POST /bounds` → Electron `setBounds` —— **一次 HTTP 往返**，拖动时每秒几十次，
        用户看到的就是"一卡一卡"。而摆位本来就是两个 Win32 调用能办的事，直接做即可。

        ⚠️ 这里必须**物理像素**（`SetWindowPos` 的口径），所以把布局表的屏幕坐标换算成
        Electron 的逻辑像素的活还是留给 `place()`（它有自校准）；本方法只负责
        "把已经算好的物理像素立刻应用上去"。
        """
        if not self.running or not self.hwnd:
            return False
        try:
            _u32.SetWindowPos(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(0),
                              int(x), int(y), max(120, int(w)), max(80, int(h)),
                              SWP_NOZORDER | SWP_NOACTIVATE)
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def restore_silent(self, x=None, y=None, w=None, h=None, show=False):
        """把浏览器摆到指定屏幕坐标：**只移动，不抢焦点**；是否显示由调用方定。

        ⚠️ 为什么区分"移动"和"显示"（2026-09-16 实测定型）：
          1. **不聚焦**：拖动窗口时这条每秒被调很多次，抢前台会把用户正在打字的
             焦点抢走。
          2. `show=True`（默认 False）时才 `show()`。**拖动/让位期间必须 show=False**：
             窗口是被有意藏起来让位给提示/弹窗的，一 show 就又盖回提示上面了
             （用户实测："这个窗口会遮挡提示框/弹出的词"）；但**位置仍要跟着更新**，
             否则让位那段时间里主窗口挪了、浏览器还停在旧位置 —— 让位结束后就会看到
             错位（用户实测："单纯拖动就会错位"）。
          3. `/restore` 在有 owner 时**不自己 show**（owned + 隐藏的 show 会让
             Electron 主进程卡死），所以需要显示时必须走 `show()`。
        """
        body = {}
        if None not in (x, y, w, h):
            body = {'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h)}
        if show:
            # 必须先摘 owner → show → 挂回（见 show() 的说明），再做纯移动
            try:
                self.show()
            except (OSError, ValueError):
                pass
        r = self._post('/restore', body, 8)
        return bool(r.get('ok'))

    def type_text(self, text, submit=False):
        """把文字写进页面上**当前激活的输入框**（可选直接回车发送）。

        这是不依赖操作系统键盘的通道（Electron 的 insertText / value setter），
        用来在"键盘进不去"时也能把手机号、验证码、要发给 AI 的话送进页面。
        """
        return self._post('/type', {'text': str(text or ''), 'submit': bool(submit)},
                          120 if submit else 25)

    def summon(self):
        """把浏览器窗口激活到前台并让页面拿到键盘焦点（点官网页签时调用）。

        由 Electron 自己 focus() —— 它的进程就是"用户刚点过的那个软件"，
        不需要宿主再用 AttachThreadInput 那套跨进程技巧（那套还会让输入法死锁）。

        ⚠️ 走 `/focus` 而不是 `/summon`：`/summon` 现在是**空操作**（理由见 main.js，
        owned 窗口上 show/focus/moveTop 都会让主进程同步卡住）。`/focus` 只下达聚焦
        指令并回一个**快照**，不 await 等确认，所以既快又能如实汇报焦点状态。
        """
        if not self.running:
            return False
        t0 = time.time()
        r = self._post('/focus', {}, 10)
        ok = bool(r.get('focused'))
        self.log('summon: %.2fs 页面拿到焦点=%s (win=%s wc=%s)'
                 % (time.time() - t0, ok, r.get('winFocused'), r.get('wcFocused')))
        return ok

    def move(self, x, y, w, h):
        """主窗口缩放/切页时重新摆放。

        ⚠️ 两种形态**都**是屏幕坐标 + 物理像素（owner 窗口也是顶层窗口），
        所以一律走 place()（它内部要 ÷ 缩放比换算成 Electron 的 DIP）。
        """
        if not self.running:
            return False
        if self._placed or self._parent_hwnd:
            return self.place(x, y, w, h)
        return False

    def detach(self):
        """解除 owner 关系（主窗口要销毁时用）。

        ⚠️ owner 窗口在 owner 销毁时会**跟着一起被销毁**，所以关程序前必须先解除，
        否则 Electron 会随主窗口一起消失、收尾时看到的是"卡住"。
        ⚠️ 这里**只解除 owner**，不做 SetParent —— 我们从来就没把它变成子窗口。
        """
        if self.hwnd and self._parent_hwnd:
            try:
                _u32.SetWindowLongPtrW(ctypes.c_void_p(self.hwnd), GWLP_HWNDPARENT, 0)
                self._parent_hwnd = 0
                self._placed = False
                try:
                    self._post('/owner', {'on': False}, 8)   # 同步告知 Electron
                except (OSError, ValueError):
                    pass
                return
            except (OSError, ValueError, AttributeError):
                pass
        self.hide()


def main():
    """手动调试用：python -m ds_bridge.host [base]  —— 起一个独立窗口看看能不能用。"""
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = DeepSeekHost(base, log=print)
    try:
        if not h.start():
            print('启动失败：', h.start_error)
            return 1
        print('状态：', h.state())
        import tkinter as tk
        root = tk.Tk()
        root.geometry('1100x760+80+60')

        def do_place():
            # 摆到主窗口右侧，并激活浏览器窗口（独立顶层窗口方案）
            root.update_idletasks()
            x = root.winfo_rootx() + 420
            y = root.winfo_rooty() + 60
            ok = h.place(x, y, 640, 660)
            print('place:', ok)
            print('summon:', h.summon())

        root.after(500, do_place)
        root.mainloop()
    finally:
        h.shutdown(wait=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
