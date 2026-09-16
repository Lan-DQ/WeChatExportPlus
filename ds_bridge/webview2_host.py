# -*- coding: utf-8 -*-
"""WebView2 进程内浏览器宿主（真嵌入）。

它替代 `ds_bridge.host.DeepSeekHost`（Electron + 独立顶层窗口），
接口**刻意保持一致**：start / place / move / show / hide / summon / set_topmost /
set_theme / set_background / navigate / state / attach / send / stop / type_text /
diag / last / focused_info / shutdown。这样上层 app_plus / sender 不用大改。

为什么要换（实测结论见接管文档 5.18 / 5.31 / 5.32）：
  · `SetParent` 跨进程子窗口 → 键盘整条通道全废；
  · Electron 的 `BrowserWindow{parent}` 只是 owner 关系，做不出真子窗口；
  · "两个软件合成一个"只能靠**本进程的子控件** —— 也就是 WebView2。

实现要点（全是踩出来的，别动）：
  1. **所有 WebView2 调用都在一条专用 STA 线程上**。Tk 主线程是 STA、pythonnet 的
     CLR 要 MTA（`RPC_E_CHANGED_MODE`），所以控件不能建在主线程；而且控件有线程
     亲和性，导航/执行 JS 也必须回这条线程做。
  2. 建控件**之前**先 `CoInitializeEx(COINIT_APARTMENTTHREADED)`，否则 WinForms 碰
     Handle 时会把线程定成 MTA，之后 `CreateAsync` 直接抛 `RPC_E_CHANGED_MODE`。
  3. `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` 必须指向运行时目录，否则 `CreateAsync`
     返回 `0x80070002`（看着像"没装运行时"，其实是 loader 没找到）。
  4. 异步任务（CreateAsync / EnsureCoreWebView2Async / ExecuteScriptAsync）一律用
     **WinForms Timer 轮询**，绝不在 STA 线程上 `.Result`/`.Wait()`/自旋等待
     —— 会把消息泵堵死，然后报出一个极具误导性的 `E_NOINTERFACE`。
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time

# ────────────── 运行时与程序集（借 pywebview 自带的那份微软 SDK）──────────────


def _find_webview2_runtime():
    """找已安装的 WebView2 运行时目录（loader 不自己找，必须我们告诉它）。"""
    roots = [r'C:\Program Files (x86)\Microsoft\EdgeWebView\Application',
             r'C:\Program Files\Microsoft\EdgeWebView\Application']
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root), reverse=True):
            if os.path.isfile(os.path.join(root, name, 'msedgewebview2.exe')):
                return os.path.join(root, name)
    return ''


def _webview_sdk_dir():
    """pywebview 里那份微软程序集目录（含 Core/WinForms dll 与 x64 loader）。"""
    import webview
    return os.path.join(os.path.dirname(os.path.abspath(webview.__file__)), 'lib')


RUNTIME_DIR = os.environ.get('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER') or _find_webview2_runtime()
SDK_DIR = _webview_sdk_dir()
NATIVE_X64 = os.path.join(SDK_DIR, 'runtimes', 'win-x64', 'native')

# ⚠️⚠️ 这些模块级名字由 `_load_dotnet()` 在**第一次真正要用 WebView2 时**才填。
#    千万别挪回模块顶层做 `import clr`：打包（PyInstaller）后，冻结环境里
#    在 import 阶段初始化 .NET 运行时会**卡住整个进程**，表现是"程序活着但
#    永远不出窗口"（我踩过：加了 hidden-import 之后主窗口就再也不出现了，
#    而且因为窗口没起来、日志也没写，非常难查）。
Application = Form = FormBorderStyle = DockStyle = Timer = None
Env = WV2 = None
Thread = ThreadStart = ApartmentState = None
_load_error = ''
_dotnet_ready = False


def _load_dotnet():
    """第一次用到时才加载 pythonnet + 微软的 WebView2 程序集。

    返回 '' 表示成功；否则返回错误说明（调用方把它放进 start_error）。
    """
    global Application, Form, FormBorderStyle, DockStyle, Timer
    global Env, WV2, Thread, ThreadStart, ApartmentState
    global _dotnet_ready, _load_error
    if _dotnet_ready:
        return ''
    if _load_error:
        return _load_error
    try:
        if RUNTIME_DIR:
            os.environ['WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'] = RUNTIME_DIR
        for _p in (NATIVE_X64, RUNTIME_DIR):
            if _p and os.path.isdir(_p):
                try:
                    os.add_dll_directory(_p)
                except (OSError, AttributeError):
                    pass
                os.environ['PATH'] = _p + os.pathsep + os.environ.get('PATH', '')

        # ⚠️ 打包（onefile）里 pythonnet 的 .NET 宿主 dll 可能在包根或 `_internal/`。
        #    把这些位置加进 **DLL 搜索路径**就够了 ——
        #    ⚠️⚠️ 千万别顺手写 `os.environ.setdefault('PYTHONNET_RUNTIME', 那个目录)`：
        #    PYTHONNET_RUNTIME 的语义是"用哪个 .NET 运行时"（netfx/coreclr/mono），
        #    传目录进去会让 pythonnet 找不到运行时，报
        #    "Failed to create a .NET runtime (...)"。我就是这么坑了自己一次。
        for _cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'pythonnet_runtime'),
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '..', 'pythonnet_runtime'),
                      getattr(sys, '_MEIPASS', '') or ''):
            if _cand and os.path.isdir(_cand):
                try:
                    os.add_dll_directory(_cand)
                except (OSError, AttributeError):
                    pass

        # ⚠️⚠️ 冻结（PyInstaller）环境里必须**显式指定用 .NET Framework**：
        #    pythonnet 默认按 CoreCLR 去起运行时，在打包版里会报
        #    "Failed to create a .NET runtime (...)"；就算起来了也找不到
        #    `System.Windows.Forms`（那是 .NET Framework 的程序集，CoreCLR 里没有）。
        #    实测：`PYTHONNET_RUNTIME=netfx` → 冻结环境里 clr 正常可用。
        #    源码运行时本来也走 netfx，这里写明确，两种形态行为一致。
        os.environ['PYTHONNET_RUNTIME'] = 'netfx'

        import clr
        for _dll in ('Microsoft.Web.WebView2.Core.dll',
                     'Microsoft.Web.WebView2.WinForms.dll'):            clr.AddReference(os.path.join(SDK_DIR, _dll))
        clr.AddReference('System.Windows.Forms')
        clr.AddReference('System.Drawing')

        from System.Threading import Thread as _T, ThreadStart as _TS, \
            ApartmentState as _AS
        from System.Windows.Forms import (Application as _App, Form as _Form,
                                          FormBorderStyle as _FBS,
                                          DockStyle as _DS, Timer as _Timer)
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment as _Env
        import Microsoft.Web.WebView2.WinForms as _WV2

        Application, Form, FormBorderStyle, DockStyle, Timer = _App, _Form, _FBS, _DS, _Timer
        Env, WV2 = _Env, _WV2
        Thread, ThreadStart, ApartmentState = _T, _TS, _AS
        _dotnet_ready = True
        return ''
    except Exception as e:                          # noqa: BLE001
        _load_error = f'加载 .NET/WebView2 程序集失败：{type(e).__name__}: {e}'
        return _load_error

_u32 = ctypes.windll.user32
GWL_STYLE = -16
WS_CHILD, WS_VISIBLE, WS_POPUP = 0x40000000, 0x10000000, 0x80000000

# ⚠️ 起**控制台程序**（taskkill / powershell 这类）必须带这个标志，否则每次调用都会
# 闪一个黑框 —— 用户实测反馈"关闭后跳出来一堆黑框（可能是 cmd）"。
# `capture_output=True` 只重定向管道，**不阻止控制台窗口出现**。
NO_WINDOW_KW = {'creationflags': 0x08000000} if os.name == 'nt' else {}
SWP_NOZORDER, SWP_FRAMECHANGED, SWP_NOACTIVATE = 0x0004, 0x0020, 0x0010

# ────────────── 页面侧 helper（结构性启发式，照搬 dsview/renderer.js 的思路）──
#
# ⚠️ 这段是一个普通 Python 字符串，但里面**不要**出现反引号之外的坑：
#    它会被拼进 ExecuteScriptAsync。用 IIFE 返回一个对象挂在 window.__DSV2 上。
JS_HELPER = r"""
(function () {
  if (window.__DSV2) return { ok: true, already: true };

  var SEND_ICON_SELECTOR = 'svg path, svg rect, svg polygon, svg circle';

  function box(el) {
    if (!el || !el.getBoundingClientRect) return null;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    return { x: r.left, y: r.top, w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
  }

  function visible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    var st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0.05;
  }

  function enabled(el) {
    if (!el) return false;
    if (el.disabled) return false;
    var a = el.getAttribute && el.getAttribute('aria-disabled');
    return a !== 'true';
  }

  function editables() {
    var out = [];
    var nodes = document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""], input');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i], tag = el.tagName;
      if (tag === 'INPUT') {
        var t = (el.getAttribute('type') || 'text').toLowerCase();
        if (['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'image', 'range', 'color'].indexOf(t) >= 0) continue;
      }
      if (!visible(el)) continue;
      var r = el.getBoundingClientRect();
      if (r.width < 80 || r.height < 12) continue;
      out.push(el);
    }
    return out;
  }

  function composer() {
    var list = editables();
    if (!list.length) return null;
    var area = window.innerWidth * window.innerHeight;
    var best = null, score = -1;
    for (var i = 0; i < list.length; i++) {
      var el = list[i], r = el.getBoundingClientRect();
      var s = r.width * r.height;
      if (s > area * 0.6) continue;
      if (r.top > window.innerHeight * 0.35) s *= 3;
      if (el.getAttribute('role') === 'textbox') s *= 2;
      if (s > score) { score = s; best = el; }
    }
    return best || list[0];
  }

  function composerBox() {
    var el = composer();
    if (!el) return null;
    var node = el;
    for (var i = 0; i < 5 && node; i++) {
      var b = box(node);
      if (b && b.w > 200 && b.h > 60) return b;
      node = node.parentElement;
    }
    return box(el);
  }

  function btnInfo(btn) {
    if (!btn) return null;
    var b = box(btn);
    if (!b) return null;
    var svg = btn.querySelector('svg');
    var paths = svg ? svg.querySelectorAll(SEND_ICON_SELECTOR) : [];
    var cmds = '';
    for (var i = 0; i < paths.length; i++) {
      var t = paths[i].tagName.toLowerCase();
      var d = paths[i].getAttribute('d') || '';
      cmds += t + ':' + d + '|';
    }
    return { b: b, cmds: cmds, aria: btn.getAttribute('aria-label') || '',
             title: btn.getAttribute('title') || '', enabled: enabled(btn) };
  }

  function buttons() {
    var all = document.querySelectorAll('button, [role="button"]');
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var info = btnInfo(all[i]);
      if (!info) continue;
      info.el = all[i];
      out.push(info);
    }
    return out;
  }

  function pickSend() {
    var list = buttons(), best = null, bestScore = -1;
    for (var i = 0; i < list.length; i++) {
      var it = list[i], sc = 0;
      var c = it.cmds.toLowerCase(), a = (it.aria + ' ' + it.title).toLowerCase();
      if (/paper|plane|send|sendmessage|发送|arrow/.test(c + a)) sc += 6;
      if (c.indexOf('rect:') >= 0) sc -= 6;
      if (c.indexOf('path:') >= 0) sc += 2;
      if (it.b.cx > window.innerWidth * 0.6) sc += 3;
      if (it.b.cy > window.innerHeight * 0.5) sc += 3;
      if (!it.enabled) sc -= 8;
      if (sc > bestScore) { bestScore = sc; best = it; }
    }
    return best;
  }

  function pickStop() {
    var list = buttons();
    for (var i = 0; i < list.length; i++) {
      var it = list[i];
      if (it.cmds.indexOf('rect:') >= 0) return it;
    }
    return null;
  }

  function pickAttach() {
    var list = buttons(), best = null, bestScore = -1;
    for (var i = 0; i < list.length; i++) {
      var it = list[i], a = (it.aria + ' ' + it.title).toLowerCase();
      var c = it.cmds.toLowerCase(), sc = 0;
      if (/clip|attach|paperclip|upload|附件|回形针|plus/.test(a)) sc += 8;
      if (c.indexOf('clip') >= 0) sc += 4;
      if (it.b.cx < window.innerWidth * 0.5) sc += 3;
      if (it.b.cy > window.innerHeight * 0.5) sc += 3;
      if (sc > bestScore) { bestScore = sc; best = it; }
    }
    return best;
  }

  function attachments() {
    var sel = '[data-ds-attachment], [data-testid*="file"], [class*="file-item"], [class*="attachment"], [class*="fileItem"]';
    var out = [];
    try {
      var nodes = document.querySelectorAll(sel);
      var cb = composerBox();
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (!visible(el)) continue;
        var b = box(el);
        if (!b) continue;
        if (cb && (b.cy < cb.y - 120 || b.cy > cb.y + cb.h + 120)) continue;
        out.push(el);
      }
    } catch (e) {}
    return out;
  }

  function fileInput() {
    var nodes = document.querySelectorAll('input[type=file]');
    for (var i = 0; i < nodes.length; i++) return nodes[i];
    return null;
  }

  function composerText() {
    var el = composer();
    if (!el) return '';
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return el.value || '';
    return el.innerText || el.textContent || '';
  }

  function setComposerText(text) {
    var el = composer();
    if (!el) return { ok: false, error: 'no-composer' };
    el.focus();
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      try {
        var proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
                                              : window.HTMLInputElement.prototype;
        var d = Object.getOwnPropertyDescriptor(proto, 'value');
        if (d && d.set) d.set.call(el, text); else el.value = text;
      } catch (e) { el.value = text; }
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      el.textContent = text;
      el.dispatchEvent(new InputEvent('input', { bubbles: true }));
    }
    return { ok: true, tag: el.tagName };
  }

  function isStreaming() {
    var st = pickStop();
    if (st && visible(st.el)) return true;
    var sd = pickSend();
    if (sd && !visible(sd.el)) return true;      // 发送键被藏起来通常就是"正在生成"
    return false;
  }

  function isLoggedIn() {
    if (composer()) return true;
    var body = (document.body && (document.body.innerText || '')) || '';
    if (/请输入手机号|登录|log ?in|sign ?in/i.test(body)) return false;
    return false;
  }

  function snapshot() {
    var cb = composerBox(), sd = pickSend(), st = pickStop();
    return {
      ok: true,
      url: location.href,
      ready: document.readyState,
      hasComposer: !!composer(),
      composerBox: cb,
      composerText: composerText(),
      sendReady: !!(sd && sd.enabled && visible(sd.el)),
      sendBox: sd ? sd.b : null,
      stopVisible: !!(st && visible(st.el)),
      attachments: attachments().length,
      streaming: isStreaming(),
      loggedIn: isLoggedIn(),
      fileInput: !!fileInput(),
      title: document.title
    };
  }

  function jsonFile(name, encoded) {
    var bin = atob(encoded);
    var len = bin.length;
    var arr = new Uint8Array(len);
    for (var i = 0; i < len; i++) arr[i] = bin.charCodeAt(i);
    return new File([arr], name);
  }

  function injectFiles(list) {
    var input = fileInput();
    if (!input) return { ok: false, error: 'no-file-input' };
    try {
      var dt = new DataTransfer();
      for (var i = 0; i < list.length; i++) dt.items.add(jsonFile(list[i].name, list[i].b64));
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, attached: input.files.length };
    } catch (e) {
      return { ok: false, error: String(e && e.message || e) };
    }
  }

  function clickSend() {
    var sd = pickSend();
    if (!sd) return { ok: false, error: 'no-send-button' };
    if (!visible(sd.el)) return { ok: false, error: 'send-hidden', how: 'disabled' };
    if (!sd.enabled) return { ok: false, error: 'send-disabled', how: 'disabled' };
    try { sd.el.click(); } catch (e) { return { ok: false, error: String(e) }; }
    return { ok: true, how: 'button', box: sd.b };
  }

  function clickStop() {
    var st = pickStop();
    if (!st || !visible(st.el)) return { ok: false, error: 'no-stop-button' };
    try { st.el.click(); } catch (e) { return { ok: false, error: String(e) }; }
    return { ok: true };
  }

  function clickAttach() {
    var at = pickAttach();
    if (!at) return { ok: false, error: 'no-attach-button' };
    try { at.el.click(); } catch (e) { return { ok: false, error: String(e) }; }
    return { ok: true, box: at.b };
  }

  window.__DSV2 = {
    snapshot: snapshot,
    composer: composer,
    composerBox: composerBox,
    composerText: composerText,
    setComposerText: setComposerText,
    pickSend: pickSend,
    pickStop: pickStop,
    pickAttach: pickAttach,
    clickSend: clickSend,
    clickStop: clickStop,
    clickAttach: clickAttach,
    injectFiles: injectFiles,
    attachments: attachments,
    isStreaming: isStreaming,
    isLoggedIn: isLoggedIn,
    fileInput: fileInput
  };
  return { ok: true };
})();
"""


def _hwnd(ctrl):
    return int(ctrl.Handle.ToInt64())


class WebView2Host:
    """把 WebView2 控件嵌进给定父窗口，并提供与 DeepSeekHost 同名的控制接口。"""

    def __init__(self, parent_hwnd, user_data_dir, url='https://chat.deepseek.com/',
                 log=None, runtime_dir=''):
        self.parent = int(parent_hwnd)
        self.user_data = os.path.abspath(user_data_dir)
        self.url = url
        self.runtime = runtime_dir or RUNTIME_DIR
        self.log_fn = log or (lambda m: None)
        self.running = False
        self.start_error = ''
        self.form = None
        self.wv = None
        self.core = None
        self.hwnd = 0
        self._q = queue.Queue()          # 主线程 → STA 线程
        self._results = {}
        self._pending = {}               # rid -> (Task, deadline, kind)
        self._rid = 0
        self._state = {'ready': False, 'readyState': '', 'url': '', 'streaming': False,
                       'attachments': 0, 'loggedIn': False, 'fileInput': False,
                       'sendReady': False}
        self._last = {}
        self._attach_cache = {}          # name -> {'name','b64'}（避免重复读盘）
        self._helper_ok = False
        self._file_paths = []            # 最近一次 attach 用的真实路径（FilePicker 兜底）
        self._picker_deferral = None

    # ────────────── 生命周期 ──────────────

    def start(self, timeout=60):
        if self.running:
            return True
        if not self.runtime:
            self.start_error = ('找不到 WebView2 运行时。请安装 "Microsoft Edge WebView2 '
                               'Runtime"（或设 WEBVIEW2_BROWSER_EXECUTABLE_FOLDER）')
            return False
        err = _load_dotnet()
        if err:
            self.start_error = err
            return False
        os.makedirs(self.user_data, exist_ok=True)
        self._ready_evt = threading.Event()
        self._thread = Thread(ThreadStart(self._sta_main))
        self._thread.SetApartmentState(ApartmentState.STA)
        self._thread.IsBackground = True
        self._thread.Start()
        if not self._ready_evt.wait(timeout):
            self.start_error = self.start_error or 'WebView2 启动超时'
            return False
        if not self.running:
            return False
        self.hwnd = self._form_hwnd
        return True

    def _sta_main(self):
        try:
            self._t0 = time.time()
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)      # 见模块注释 2
            self._log(f'[t={time.time() - self._t0:.2f}s] CoInitializeEx 完成')
            Application.EnableVisualStyles()
            Application.SetCompatibleTextRenderingDefault(False)
            form = Form()
            form.FormBorderStyle = getattr(FormBorderStyle, 'None')
            form.Text = 'ds-webview2'
            form.ShowInTaskbar = False
            wv = WV2.WebView2()
            wv.Dock = DockStyle.Fill
            form.Controls.Add(wv)
            form.Show()
            self.form, self.wv = form, wv
            self._form_hwnd = _hwnd(form)
            self._log(f'[t={time.time() - self._t0:.2f}s] 控件已建：'
                      f'form={self._form_hwnd} wv={_hwnd(wv)}')
            self._env_task = Env.CreateAsync(self.user_data, None)
            self._log(f'[t={time.time() - self._t0:.2f}s] CreateAsync 已发起'
                      f'（user_data={self.user_data}）')
            timer = Timer()
            timer.Interval = 60
            timer.Tick += self._sta_tick
            timer.Start()
            self._timer = timer
            Application.Run()
        except Exception as e:                                  # noqa: BLE001
            self.start_error = f'{type(e).__name__}: {e}'
            self._log(f'STA 线程出错：{self.start_error}')
            self._ready_evt.set()

    def _sta_tick(self, sender, e):
        """STA 线程心跳：推进异步阶段 + 收 JS 结果 + 执行命令队列。"""
        try:
            t = getattr(self, '_env_task', None)
            if t is not None and t.IsCompleted and self.core is None \
                    and getattr(self, '_ctl_task', None) is None:
                if t.IsFaulted:
                    self.start_error = str(t.Exception.InnerException or t.Exception)
                    self._log(f'环境创建失败：{self.start_error}')
                    self._ready_evt.set()
                    return
                self._env = t.Result
                self._log(f'[t={time.time() - self._t0:.2f}s] 环境就绪：'
                          f'{self._env.BrowserVersionString}')
                self._ctl_task = self.wv.EnsureCoreWebView2Async(self._env)
                return
            c = getattr(self, '_ctl_task', None)
            if c is not None and c.IsCompleted and self.core is None:
                if c.IsFaulted:
                    self.start_error = str(c.Exception.InnerException or c.Exception)
                    self._log(f'CoreWebView2 创建失败：{self.start_error}')
                    self._ready_evt.set()
                    return
                self.core = self.wv.CoreWebView2
                self._wire_events()
                self.core.Navigate(self.url)
                self.running = True
                self._log(f'[t={time.time() - self._t0:.2f}s] WebView2 就绪，开始导航')
                self._ready_evt.set()
                return

            self._collect_pending()
            # ⚠️ 先把"排队的最新矩形"应用掉（place_async 的落地处，见那里的说明）
            if getattr(self, '_target_rect', None) is not None:
                _r = self._target_rect
                self._target_rect = None
                try:
                    self._op_place({'x': _r[0], 'y': _r[1], 'w': _r[2], 'h': _r[3]})
                except Exception:                               # noqa: BLE001
                    pass
            for _ in range(4):
                try:
                    rid, op, args = self._q.get_nowait()
                except queue.Empty:
                    break
                args = dict(args or {})
                args['_rid'] = rid
                self._run_cmd(rid, op, args)
        except Exception as ex:                                 # noqa: BLE001
            self._log(f'tick 出错：{type(ex).__name__}: {ex}')

    def _wire_events(self):
        """挂页面事件：导航完成、页面消息、文件选择框接管。"""
        try:
            def on_nav(sender, e):
                try:
                    self._state['ready'] = bool(e.IsSuccess)
                    self._state['url'] = str(self.core.Source)
                    self._helper_ok = False           # 新页面要重新注入 helper
                    self._log(f'导航完成 success={e.IsSuccess}')
                except Exception:                               # noqa: BLE001
                    pass

            def on_msg(sender, e):
                try:
                    msg = json.loads(str(e.WebMessageAsJson))
                    if isinstance(msg, dict):
                        for k in list(self._state):
                            if k in msg:
                                self._state[k] = msg[k]
                except Exception:                               # noqa: BLE001
                    pass

            def on_file_picker(sender, e):
                """页面弹文件选择框 → 宿主直接给出文件路径（等价于用户选了文件）。

                这是 WebView2 上替代 CDP setFileInputFiles 的正规手段。
                """
                try:
                    if not self._file_paths:
                        return
                    args = self.core.Environment.CreateCoreWebView2FilePickerRequestedEventArgs
                    # 取 deferral，让回调可以异步完成
                    deferral = e.GetDeferral()
                    self._picker_deferral = deferral
                    try:
                        from System import Array, String
                        paths = Array[String](self._file_paths)
                        result = self.core.Environment.CreateWebFilePickerResult(paths)
                        e.Handled = True
                        e.Result = result
                        self._log(f'FilePickerRequested 已接管：{len(self._file_paths)} 个文件')
                    finally:
                        deferral.Complete()
                        self._picker_deferral = None
                except Exception as ex:                         # noqa: BLE001
                    self._log(f'接管文件框失败：{type(ex).__name__}: {ex}')

            self.core.NavigationCompleted += on_nav
            self.core.WebMessageReceived += on_msg
            try:
                self.core.FilePickerRequested += on_file_picker
                self._log('FilePickerRequested 已挂上')
            except Exception as ex:                             # noqa: BLE001
                self._log(f'FilePickerRequested 不可用（挂附件会走兜底）：{ex}')
            self._on_nav, self._on_msg, self._on_picker = on_nav, on_msg, on_file_picker
        except Exception as e:                                  # noqa: BLE001
            self._log(f'挂事件失败（不致命）：{type(e).__name__}: {e}')

    # ────────────── 命令通道 ──────────────

    def _call(self, op, args=None, timeout=8):
        """从任意线程发一条命令给 STA 线程，等结果。

        ⚠️ 默认超时**只有 8 秒**（原来是 30）：这些调用都发生在 Tk 主线程上，
        STA 线程一旦忙（页面在加载、JS 在跑），默认超时太长就会让界面"卡死"
        ——用户实测反馈过"打几个数字也会卡死"，宁可这次操作失败也不要僵住。
        """
        if not self.running and op != 'shutdown':
            return {'ok': False, 'error': 'not-running'}
        self._rid += 1
        rid = self._rid
        self._q.put((rid, op, args or {}))
        end = time.time() + timeout
        while time.time() < end:
            if rid in self._results:
                return self._results.pop(rid)
            time.sleep(0.01)
        return {'ok': False, 'error': 'timeout'}

    def _run_cmd(self, rid, op, args):
        """在 STA 线程上执行命令。

        ⚠️ 操作返回 None 表示"异步、稍后填结果"（例如 eval / attach），
        这时**不要**往 self._results 塞东西，否则调用方会拿到空结果。
        """
        try:
            fn = getattr(self, '_op_' + op, None)
            if fn is None:
                self._results[rid] = {'ok': False, 'error': f'unknown-op:{op}'}
                return
            out = fn(args)
            if out is not None:
                self._results[rid] = out
        except Exception as e:                                  # noqa: BLE001
            self._results[rid] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    def _submit(self, rid, task, deadline, kind):
        self._pending[rid] = (task, deadline, kind)

    def _collect_pending(self):
        """收异步任务结果（在 tick 里；这里绝不能阻塞）。"""
        for rid, (task, deadline, kind) in list(self._pending.items()):
            if task.IsCompleted:
                del self._pending[rid]
                if task.IsFaulted:
                    self._results[rid] = {
                        'ok': False,
                        'error': str(task.Exception.InnerException or task.Exception)}
                    continue
                raw = str(task.Result)
                try:
                    val = json.loads(raw)
                except (ValueError, TypeError):
                    val = raw
                if kind == 'eval':
                    self._results[rid] = {'ok': True, 'result': val}
                elif kind == 'helper':
                    self._helper_ok = bool(val and val.get('ok'))
                    self._results[rid] = {'ok': self._helper_ok, 'result': val}
                elif kind == 'snapshot':
                    if isinstance(val, dict):
                        for k in ('ready', 'readyState', 'url', 'streaming',
                                  'attachments', 'loggedIn', 'fileInput', 'sendReady'):
                            if k in val:
                                self._state[k] = val[k]
                        self._state['ready'] = True
                    self._results[rid] = {'ok': True, 'state': dict(self._state),
                                          'snapshot': val}
                else:
                    self._results[rid] = {'ok': True, 'result': val}
            elif time.time() > deadline:
                del self._pending[rid]
                self._results[rid] = {'ok': False, 'error': 'timeout'}

    # ────────────── 操作实现（都在 STA 线程上跑）──────────────

    def _op_place(self, a):
        _u32.SetWindowPos(ctypes.c_void_p(self._form_hwnd), ctypes.c_void_p(0),
                          int(a['x']), int(a['y']), max(50, int(a['w'])),
                          max(50, int(a['h'])), SWP_NOZORDER)
        return {'ok': True}

    def place_async(self, x, y, w, h):
        """摆位但**不等结果**（界面线程专用，绝不阻塞）。

        ⚠️ 为什么需要它：最大化/拖大窗口时 `<Configure>` 会密集触发，
        每次都同步等 STA 线程应答（8 秒超时）的话，界面就会被"排队等"卡住
        ——用户实测"一开全屏就卡死"。这里只记下**最新的目标矩形**，
        让 STA 线程下一次 tick 自己摆一次：重复调用自动合并，界面零等待。
        """
        self._target_rect = (int(x), int(y), int(w), int(h))
        return {'ok': True, 'queued': True}

    def _op_reparent(self, a):
        """把控件容器窗口变成宿主窗口的子窗口（真嵌入的关键一步）。

        ⚠️⚠️ **必须幂等**：已经在目标父窗口下时只改位置，绝不再动样式/SetParent。
        事故（用户实测："一开全屏就卡死"）：窗口尺寸一变 → 界面整页重排 →
        重排里又调了一次本函数 → 每次都 `SetWindowLong` + `SetParent` +
        `SWP_FRAMECHANGED`，最大化时这类事件密集触发，WebView2 的窗口被反复
        重建层级，直接卡死。
        """
        parent = int(a['parent'])
        h = self._form_hwnd
        try:
            now_parent = int(_u32.GetParent(ctypes.c_void_p(h)) or 0)
        except Exception:                                       # noqa: BLE001
            now_parent = 0
        if now_parent == parent:
            # 已经嵌好了 —— 只摆位（这一步很轻，可以随便调）
            self._op_place(a)
            return {'ok': True, 'parent': parent, 'already': True}
        style = _u32.GetWindowLongW(ctypes.c_void_p(h), GWL_STYLE)
        _u32.SetWindowLongW(ctypes.c_void_p(h), GWL_STYLE,
                            (style & ~WS_POPUP) | WS_CHILD | WS_VISIBLE)
        _u32.SetParent(ctypes.c_void_p(h), ctypes.c_void_p(parent))
        _u32.SetWindowPos(ctypes.c_void_p(h), ctypes.c_void_p(0),
                          int(a.get('x', 0)), int(a.get('y', 0)),
                          max(50, int(a.get('w', 400))), max(50, int(a.get('h', 300))),
                          SWP_NOZORDER | SWP_FRAMECHANGED)
        return {'ok': True, 'parent': _u32.GetParent(ctypes.c_void_p(h))}

    def _op_show(self, a):
        _u32.ShowWindow(ctypes.c_void_p(self._form_hwnd), 5)
        return {'ok': True}

    def _op_hide(self, a):
        _u32.ShowWindow(ctypes.c_void_p(self._form_hwnd), 0)
        return {'ok': True}

    def _op_summon(self, a):
        # 子控件不能"置前台"；把焦点给宿主窗口，并让页面 composer 拿到焦点
        try:
            _u32.SetFocus(ctypes.c_void_p(self.parent))
        except Exception:                                       # noqa: BLE001
            pass
        return {'ok': True, 'focused': True}

    def _op_set_topmost(self, a):
        # 真子控件本来就在父窗口里，不需要置顶
        return {'ok': True, 'on': False}

    def _op_set_theme(self, a):
        """把软件主题同步给内嵌页面（浅色/深色跟着软件变）。

        走 WebView2 官方通道 `Profile.PreferredColorScheme`：
          0=Auto（跟随系统） 1=Light 2=Dark
        它会让页面里的 `prefers-color-scheme` 媒体查询跟着变 —— 官网自己的
        深浅色就是靠这个媒体查询切的。

        ⚠️ 直接在这里**回读**一次，把真实生效值写进日志/返回值，
        免得"以为设了、其实没设"（这个坑我在别处踩过）。
        """
        mode = a.get('mode', 'light')
        want = 2 if mode == 'dark' else 1
        out = {'ok': True, 'mode': mode, 'want': want}
        try:
            self.core.Profile.PreferredColorScheme = want
            out['now'] = int(self.core.Profile.PreferredColorScheme)
            out['ok'] = (out['now'] == want)
        except Exception as e:                                  # noqa: BLE001
            out['ok'] = False
            out['error'] = f'{type(e).__name__}: {e}'
        return out

    def _op_set_background(self, a):
        color = str(a.get('color', ''))
        try:
            from System.Drawing import ColorTranslator
            self.wv.DefaultBackgroundColor = ColorTranslator.FromHtml(color)
        except Exception as e:                                  # noqa: BLE001
            return {'ok': False, 'error': str(e)}
        return {'ok': True}

    def _op_navigate(self, a):
        self.core.Navigate(str(a['url']))
        return {'ok': True}

    def _op_eval(self, a):
        """执行 JS（**不阻塞**）：挂 Task，后续 tick 收结果。"""
        rid = a['_rid']
        js = str(a.get('expr', ''))
        self._submit(rid, self.core.ExecuteScriptAsync(js),
                     time.time() + float(a.get('timeout', 20)), 'eval')
        return None

    def _op_helper(self, a):
        """注入页面侧 helper（幂等）。"""
        rid = a['_rid']
        self._submit(rid, self.core.ExecuteScriptAsync(JS_HELPER),
                     time.time() + 20, 'helper')
        return None

    def _op_snapshot(self, a):
        rid = a['_rid']
        js = ('(function(){ if(!window.__DSV2) return {ok:false,error:"no-helper"};'
              ' return window.__DSV2.snapshot(); })()')
        self._submit(rid, self.core.ExecuteScriptAsync(js),
                     time.time() + 15, 'snapshot')
        return None

    def _op_attach_js(self, a):
        """把文件内容送进页面的 file input（DataTransfer 方式，不弹任何框）。"""
        rid = a['_rid']
        payload = a.get('files') or []
        arg = json.dumps(payload)
        js = ('(function(){ if(!window.__DSV2) return {ok:false,error:"no-helper"};'
              ' return window.__DSV2.injectFiles(' + arg + '); })()')
        self._submit(rid, self.core.ExecuteScriptAsync(js),
                     time.time() + float(a.get('timeout', 120)), 'attach')
        return None

    def _op_click_attach(self, a):
        rid = a['_rid']
        self._submit(rid, self.core.ExecuteScriptAsync(
            '(function(){ return window.__DSV2 ? window.__DSV2.clickAttach() '
            ': {ok:false,error:"no-helper"}; })()'), time.time() + 15, 'attach')
        return None

    def _op_click_send(self, a):
        rid = a['_rid']
        self._submit(rid, self.core.ExecuteScriptAsync(
            '(function(){ return window.__DSV2 ? window.__DSV2.clickSend() '
            ': {ok:false,error:"no-helper"}; })()'), time.time() + 15, 'send')
        return None

    def _op_click_stop(self, a):
        rid = a['_rid']
        self._submit(rid, self.core.ExecuteScriptAsync(
            '(function(){ return window.__DSV2 ? window.__DSV2.clickStop() '
            ': {ok:false,error:"no-helper"}; })()'), time.time() + 15, 'stop')
        return None

    def _op_set_text(self, a):
        rid = a['_rid']
        arg = json.dumps(str(a.get('text', '')))
        self._submit(rid, self.core.ExecuteScriptAsync(
            '(function(){ return window.__DSV2 ? window.__DSV2.setComposerText('
            + arg + ') : {ok:false,error:"no-helper"}; })()'), time.time() + 15, 'eval')
        return None

    # ────────────── 对上层暴露的接口（与 DeepSeekHost 同名）──────────────

    def place(self, x, y, w, h):
        return self._call('place', {'x': x, 'y': y, 'w': w, 'h': h})

    def reparent(self, parent, x=0, y=0, w=400, h=300):
        return self._call('reparent', {'parent': parent, 'x': x, 'y': y,
                                       'w': w, 'h': h})

    def move(self, x, y, w, h):
        return self.place(x, y, w, h)

    def show(self):
        return self._call('show')

    def hide(self):
        """藏起来（**短超时**）。

        ⚠️ 关窗路径上也会调它：如果 STA 线程正好忙（比如在等一个同步 JS），
        用默认 30s 超时会让"点叉号"卡住。关窗时宁可放弃隐藏也不能卡。
        """
        if not self.running:
            return {'ok': False, 'error': 'not-running'}
        return self._call('hide', timeout=2.5)

    def summon(self):
        return self._call('summon')

    def set_topmost(self, on=True):
        return self._call('set_topmost', {'on': bool(on)})

    def set_theme(self, mode):
        return self._call('set_theme', {'mode': mode})

    def set_background(self, color):
        return self._call('set_background', {'color': color})

    def navigate(self, url):
        return self._call('navigate', {'url': url})

    def eval_js(self, expr, timeout=20):
        return self._call('eval', {'expr': expr, 'timeout': timeout}, timeout + 5)

    def ensure_helper(self):
        """注入页面侧 helper（幂等；页面导航后要重新注入）。"""
        if self._helper_ok:
            return {'ok': True, 'cached': True}
        r = self._call('helper', timeout=8)
        return r

    def state(self):
        """页面状态快照（与 Electron 版 /state 的字段保持一致）。"""
        if not self.running:
            return dict(self._state)
        r = self._call('snapshot', timeout=5)
        if r.get('ok') and isinstance(r.get('state'), dict):
            return r['state']
        return dict(self._state)

    def last(self):
        return dict(self._last)

    def focused_info(self):
        r = self.eval_js('(function(){var a=document.activeElement;return a ? '
                         '{tag:a.tagName,type:a.type||"",placeholder:a.placeholder||""} '
                         ': null;})()')
        return {'ok': bool(r.get('ok')), 'info': r.get('result')}

    def type_text(self, text, submit=False):
        """写文字进 composer（等价 Electron 版 /type，不依赖键盘焦点）。"""
        self.ensure_helper()
        r = self._call('set_text', {'text': text}, timeout=20)
        if submit and r.get('ok'):
            time.sleep(0.3)
            s = self.send()
            r = dict(r)
            r['send'] = s
        return r

    # ────────────── 挂附件 ──────────────

    def attach(self, paths, timeout_ms=180000, settle_ms=None):
        """把本地文件挂到页面输入区。

        两条路（按顺序试）：
          1. **DataTransfer 注入**：读文件 → base64 → 页面里造 File 塞进 input.files
             并派发 change。不弹任何对话框，最接近 CDP 的效果。
          2. **FilePickerRequested 兜底**：先点"回形针"按钮让页面弹文件框，
             宿主在事件里直接给出路径（页面侧等价于用户选了文件）。
        settle_ms 由调用方按文件类型给（文档慢、图片快），这里只负责等。
        """
        self.ensure_helper()
        files = []
        for p in paths:
            p = os.path.abspath(p)
            try:
                with open(p, 'rb') as f:
                    data = f.read()
            except OSError as e:
                self._last_attach = {'ok': False, 'error': f'读文件失败 {p}: {e}'}
                return self._last_attach
            import base64
            files.append({'name': os.path.basename(p), 'b64': base64.b64encode(data).decode('ascii')})
        expected = len(files)

        r = self._call('attach_js', {'files': files,
                                     'timeout': max(60, expected * 2)},
                       timeout=max(60, expected * 2 + 30))
        attached = 0
        if r.get('ok') and isinstance(r.get('result'), dict):
            attached = int(r['result'].get('attached') or 0)
        if attached < expected:
            # 兜底：走系统文件框 + 宿主接管
            self._file_paths = [os.path.abspath(p) for p in paths]
            self._call('click_attach', timeout=15)
            time.sleep(1.0)
            r2 = self.state()
            attached = int(r2.get('attachments') or 0)

        settle = settle_ms if settle_ms is not None else 250
        time.sleep(max(0.0, settle / 1000.0))
        st = self.state()
        out = {'ok': attached >= expected, 'attached': attached, 'expected': expected,
               'sendReady': bool(st.get('sendReady')), 'diag': st}
        self._last_attach = out
        self._last = dict(self._last, attach=out)
        return out

    # ────────────── 发送 / 停止 ──────────────

    def send(self, method='auto', text=None):
        """点发送键；text 不为空就先写进输入框。

        ⚠️ 发送键会"闪禁用"：要求**连续两次可用**再点（和 Electron 版同一套判据，
        见接管文档 5.3）。绝不轻易退化到回车 —— 那会把没传完的消息提交上去。
        """
        if text:
            self.type_text(text, submit=False)
        tried = []
        ready = 0
        end = time.time() + 60
        while time.time() < end:
            st = self.state()
            if st.get('sendReady'):
                ready += 1
                if ready >= 2:
                    break
            else:
                ready = 0
            time.sleep(0.25)
        tried.append({'step': 'wait-send-ready', 'ready': ready})
        r = self._call('click_send', timeout=20)
        out = {'ok': bool(r.get('ok')), 'how': (r.get('result') or {}).get('how', 'button'),
               'tried': tried, 'detail': r}
        self._last_send = out
        self._last = dict(self._last, send=out)
        return out

    def stop(self):
        r = self._call('click_stop', timeout=15)
        out = {'ok': bool(r.get('ok')), 'detail': r}
        self._last_stop = out
        self._last = dict(self._last, stop=out)
        return out

    def diag(self):
        st = self.state()
        return {'ok': True, 'state': st, 'lastAttach': self._last_attach,
                'lastSend': self._last_send, 'lastStop': self._last_stop}

    def shutdown(self, wait=True):
        """关掉浏览器（**绝不阻塞**）。

        ⚠️ 这里刻意"短超时 + 不等待"：
          · `form.Close()` 必须在 STA 线程上做，但那一刻 STA 线程可能正忙
            （页面在加载、JS 在跑）→ 同步等它就会把"关程序"卡住；
          · 关程序本来就该秒关，剩下的收尾（子进程、临时目录）交给调用方/系统。
        所以：先把 running 置 False（后续调用直接返回），再投一次"关窗"命令
        （最多等 2 秒），然后**杀掉 WebView2 的子进程**，最后才返回。
        """
        self.running = False
        try:
            self._rid += 1
            self._q.put((self._rid, 'close_form', {}))
        except Exception:                                       # noqa: BLE001
            pass
        end = time.time() + 2.0
        while time.time() < end:
            try:
                if self.form is None:
                    break
            except Exception:                                   # noqa: BLE001
                break
            time.sleep(0.05)
        self._kill_children()
        return True

    def _op_close_form(self, a):
        """在 STA 线程上关掉容器窗口（让那 4 个 WebView2 线程能退）。"""
        try:
            if self.form is not None:
                self.form.Close()
        except Exception:                                       # noqa: BLE001
            pass
        return {'ok': True}

    @staticmethod
    def _kill_children():
        """把 WebView2 的 msedgewebview2.exe 子进程收掉。

        ⚠️ 为什么必须做：WebView2 会拉起若干 `msedgewebview2.exe` 子进程，
        它们会**占住临时目录**（PyInstaller onefile 的 `_MEIxxxx`），
        导致退出时报 `Failed to remove temporary directory`，甚至卡住不退。
        只按"父进程是当前进程"来匹配，绝不误杀用户的其它浏览器。
        """
        try:
            import subprocess
            me = os.getpid()
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
                  "Where-Object { $_.ParentProcessId -eq %d -or $_.CommandLine -like "
                  "'*%s*' } | Select-Object -ExpandProperty ProcessId" % (me, me))
            r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               capture_output=True, text=True, timeout=12,
                               **NO_WINDOW_KW)
            for tok in (r.stdout or '').split():
                if tok.isdigit():
                    subprocess.run(['taskkill', '/PID', tok, '/T', '/F'],
                                   capture_output=True, timeout=8, **NO_WINDOW_KW)
        except Exception:                                       # noqa: BLE001
            pass

    def _log(self, msg):
        try:
            self.log_fn(str(msg))
        except Exception:                                       # noqa: BLE001
            pass
