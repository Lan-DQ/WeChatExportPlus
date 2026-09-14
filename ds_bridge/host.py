# -*- coding: utf-8 -*-
"""DeepSeek 官网宿主：拉起打包自带的 Electron，把它**真嵌入**进主窗口，并通过
本机 HTTP 控制它（挂附件 / 发送 / 截断）。

为什么用 Electron 而不是 WebView2
---------------------------------
发布包里本来就带 `electron/electron.exe`（WCDB 运行时备选，222MB），所以内嵌浏览器
**不增加任何新依赖**。而 Electron 自带 CDP，可以做到「程序化把本地文件挂到网页的
上传框上」——这是本功能的核心能力。

真嵌入是怎么做到的（已实测通过）
--------------------------------
1. Electron 建一个普通窗口并把 HWND 报给 Python；
2. Python 侧用 `SetParent` + `WS_CHILD` 把它挂到 Tk 主窗口上，再用 `SetWindowPos`
   摆到指定矩形 —— 之后它就是一个真正的子窗口，跟着主窗口移动/裁剪；
3. 显示/隐藏交给 Electron 自己的 `show()/hide()`（用 Win32 ShowWindow 直接显示一个
   Electron 隐藏窗口有画不出来的风险，所以走它自己的 API）。

坑（都踩过/想清楚了）
---------------------
* **user-data-dir 必须独立**：同一个 electron.exe 还被 WCDB 服务占用着默认 profile
  `%APPDATA%\\Electron`，两个进程共用会撞 Chromium 单例锁。这里强制传 `--profile`。
* **UA 必须伪装成普通 Chrome**：用 Electron 默认 UA 时官网会顶一条「使用环境异常」。
* DPI：主程序是 PROCESS_SYSTEM_DPI_AWARE，Electron 是 per-monitor v2。高 DPI 下
  若发现子窗口缩放不对，可给启动参数加 `--force-device-scale-factor=1`（见 start()）。
* 关程序时**不能阻塞**：清理（/quit → terminate → taskkill /T）丢到 daemon 线程。
"""
import ctypes
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
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


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
        self._embedded = False
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
                               capture_output=True, timeout=10)
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
                               capture_output=True, timeout=10)
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

    def diag(self):
        return self._post('/diag', {}, 15)

    def show(self):
        return self._post('/show', {}, 10)

    def hide(self):
        return self._post('/hide', {}, 10)

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

    # ── 真嵌入（Win32）──

    def embed(self, parent_hwnd, x, y, w, h):
        """把浏览器窗口挂到主窗口上并摆到 (x,y,w,h)。返回是否成功。"""
        if not self.running or not self.hwnd:
            return False
        try:
            child = ctypes.c_void_p(self.hwnd)
            parent = ctypes.c_void_p(int(parent_hwnd))
            _u32.SetParent(child, parent)
            style = _u32.GetWindowLongPtrW(child, GWL_STYLE)
            _u32.SetWindowLongPtrW(child, GWL_STYLE,
                                   (style & ~WS_POPUP) | WS_CHILD)
            self._setpos(x, y, w, h)
            self._embedded = True
            return True
        except (OSError, ValueError, AttributeError) as e:
            self.log(f'嵌入失败：{e}')
            return False

    def _setpos(self, x, y, w, h):
        try:
            _u32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, int(x), int(y),
                              int(w), int(h), SWP_NOZORDER | SWP_FRAMECHANGED)
        except (OSError, ValueError, AttributeError):
            pass

    def move(self, x, y, w, h):
        """主窗口缩放/切页时重新摆放。"""
        if self._embedded and self.running:
            self._setpos(x, y, w, h)

    def detach(self):
        """脱离主窗口（回桌面），防止主窗口销毁时把它一起带走。"""
        if self._embedded and self.hwnd:
            try:
                _u32.SetParent(ctypes.c_void_p(self.hwnd), None)
            except (OSError, ValueError, AttributeError):
                pass
            self._embedded = False


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

        def do_embed():
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            ok = h.embed(hwnd, 20, 20, 1000, 700)
            print('embed:', ok)
            print('show:', h.show())

        root.after(500, do_embed)
        root.mainloop()
    finally:
        h.shutdown(wait=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
