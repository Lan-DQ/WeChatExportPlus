'use strict';
// DeepSeek 网页版宿主（Electron 主进程）
//
// 职责：
//   1) 建一个普通窗口（不置顶、不全屏、无边框）加载 --url；
//   2) ready 后往 stdout 打一行 DSVIEW_READY JSON，把 HTTP 端口和窗口 HWND 报给 Python；
//   3) 起一个只监听 127.0.0.1 的 HTTP 控制服务，提供 attach / send / stop / state 等接口；
//   4) 通过 CDP 的 DOM.setFileInputFiles 把本地文件注入页面 input[type=file]，
//      其余页面交互一律交给 renderer.js 注入的启发式脚本。
//
// 这里【不做】任何 SetParent / 窗口内嵌：内嵌由 Python 侧负责，本进程只负责报出 HWND。
//
// 命令行：electron.exe <本目录> --url=<URL> --token=<TOKEN> --profile=<用户数据目录>
//         可选 --ds-trace 打开详细调试日志（走 stderr，不污染 stdout 的契约）
//
// 窗口生命周期（与 Python 侧约定）：窗口以 show:false 创建，避免在屏幕左上角闪一下
// 还没嵌入的 1000x700 窗口；Python 先 SetParent 把它嵌进主窗口，再调 POST /show 让
// Electron 自己 show。所以 /show、/hide 两个接口是嵌入流程的必需环节。

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const { app, BrowserWindow, nativeTheme } = require('electron');

const R = require('./renderer.js');

// UA 必须伪装成普通 Chrome，否则官网会顶出「使用环境异常」提示。
const CHROME_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36';

const VERSION = 1;

// 每个附件在"挂上"之后要给它多少毫秒的处理时间（再点发送）。
// 这个值是 Python 侧按文件类型算好传进来的兜底（文档 0.25 秒、图片 0.15 秒）；
// 只有调用方没传 settleMs 时才用它。
const SEND_SETTLE_PER_FILE_MS = 250;

// ---------- 命令行参数 ----------
function parseArgv(argv) {
  const out = { url: 'https://chat.deepseek.com/', token: '', profile: '', trace: false,
                parent: 0 };
  for (const a of argv) {
    if (a === '--ds-trace') { out.trace = true; continue; }
    const m = /^--([A-Za-z0-9_-]+)=(.*)$/.exec(a);
    if (!m) continue;
    const k = m[1];
    const v = m[2];
    if (k === 'url') out.url = v || out.url;
    else if (k === 'token') out.token = v;
    else if (k === 'profile') out.profile = v;
    // ── 实验：让 Electron 自己把窗口设成宿主窗口的**子窗口** ──
    // 为什么不自己 SetParent（见 5.18 的教训）：跨进程子窗口的键盘通道会全废。
    // Electron 的 parent 走的是它自己的原生实现（内部也设置 owner/parent），
    // 所以必须实测"键盘还能不能打字"再决定用不用。默认 0 = 老行为（独立顶层窗口）。
    else if (k === 'parent') out.parent = parseInt(v, 10) || 0;
  }
  return out;
}
const ARGS = parseArgv(process.argv.slice(1));

if (!ARGS.token) ARGS.token = crypto.randomBytes(16).toString('hex');
if (!ARGS.profile) {
  ARGS.profile = path.join(os.tmpdir(), 'dsview-profile-' + process.pid.toString(36));
}

// profile 必须在 ready 之前生效：同一个 exe 被 WCDB 服务占用着默认 profile
//（%APPDATA%\Electron），共用会撞 Chromium 的单例锁，所以走命令行开关最稳。
if (ARGS.profile) {
  try { fs.mkdirSync(ARGS.profile, { recursive: true }); } catch (e) { /* 失败也要继续试 */ }
  app.commandLine.appendSwitch('user-data-dir', ARGS.profile);
  try { app.setPath('userData', ARGS.profile); } catch (e) { /* 忽略 */ }
}

app.userAgentFallback = CHROME_UA;

// ---------- 日志 ----------
function trace(...parts) {
  if (!ARGS.trace) return;
  try { process.stderr.write('DSVIEW_TRACE ' + parts.map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ') + '\n'); } catch (e) {}
}
function writeStdout(line) {
  try { fs.writeSync(1, line + '\n'); } catch (e) {
    try { process.stdout.write(line + '\n'); } catch (e2) {}
  }
}

// ---------- 全局状态 ----------
const S = {
  win: null,
  server: null,
  port: 0,
  hwnd: '0',
  pageReady: false,
  helperReady: false,
  diagFile: path.join(ARGS.profile || os.tmpdir(), 'dsdiag.json'),
  last: {
    attach: null,   // 最近一次 attach 的判定过程
    send: null,     // 最近一次 send 的判定过程
    stop: null      // 最近一次 stop 的判定过程
  },
  diag: null,       // 最近一次 /diag 快照（内存里留一份）
  quitting: false
};

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
// /bounds 的排查日志：只在宿主设了 DSVIEW_BOUNDS_DIAG 时写（平时零开销）。
// 为什么要写文件：stdout 被宿主读走、stderr 被丢弃，排查"某个接口超时"时
// 只有落文件才能看到"停在 handler 的哪一行"。
const BOUNDS_DIAG = process.env.DSVIEW_BOUNDS_DIAG || '';
// 宿主是否已把本窗口设成它的 owner（Python 用 Win32 设的，Electron 自己看不到，
// 所以由宿主通过 /owner 告知）。有 owner 时 /bounds **绝不能** show() 一个隐藏窗口
// —— 那会让主进程卡死，见下面 /bounds 里的说明。
let OWNER_SET = false;
function bdiag(msg) {
  if (!BOUNDS_DIAG) return;
  try { fs.appendFileSync(BOUNDS_DIAG, new Date().toISOString() + ' ' + msg + '\n'); } catch (e) {}
}
function nowIso() { return new Date().toISOString(); }
function trimText(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n) + '…' : s; }

// ---------- 页面求值 ----------
async function evalJs(expr) {
  const wc = S.win && S.win.webContents;
  if (!wc) return { ok: false, reason: '窗口/页面已销毁' };
  try {
    return await wc.executeJavaScript(expr, true);
  } catch (e) {
    return { ok: false, reason: '页面执行失败: ' + trimText((e && e.message) || e, 300) };
  }
}
async function ensureHelper() {
  if (S.helperReady) return true;
  const r = await evalJs(R.buildHelper());
  if (r === true) {
    S.helperReady = true;
    installKeyWatch();          // 顺带装按键计数器（诊断"打字进不去"用）
    return true;
  }
  trace('helper 注入失败', r);
  return false;
}

// ---------- 按键到达监视（只计数，不干预页面） ----------
// 诊断"光标在闪但打字/粘贴都进不去"时，它是唯一能区分
// 「键盘消息根本没送到页面」和「送到了但页面没处理」的证据。
let _keyWatchInstalled = false;

function installKeyWatch() {
  if (_keyWatchInstalled) return;
  _keyWatchInstalled = true;
  const expr = `(() => {
    if (window.__DSVIEW_KEYWATCH__) return true;
    var W = { keydown: 0, keypress: 0, beforeinput: 0, paste: 0, composing: 0,
              lastKey: '', lastAt: 0, focusIn: 0, lastTarget: '', docFocus: null };
    window.__DSVIEW_KEYWATCH__ = W;
    function mark(kind, e) {
      try {
        W[kind] += 1;
        W.lastAt = Date.now();
        W.docFocus = document.hasFocus();
        var t = e && e.target;
        W.lastTarget = t ? (t.tagName + (t.type ? '/' + t.type : '')) : '';
        if (e && e.data) W.lastKey = String(e.data).slice(0, 12);
        else if (e && e.key !== undefined) W.lastKey = String(e.key).slice(0, 12);
      } catch (err) {}
    }
    document.addEventListener('keydown', function (e) { mark('keydown', e); }, true);
    document.addEventListener('keypress', function (e) { mark('keypress', e); }, true);
    document.addEventListener('beforeinput', function (e) { mark('beforeinput', e); }, true);
    document.addEventListener('paste', function (e) { mark('paste', e); }, true);
    document.addEventListener('compositionstart', function (e) { mark('composing', e); }, true);
    document.addEventListener('focusin', function (e) { mark('focusIn', e); }, true);
    return true;
  })()`;
  evalJs(expr).catch(() => {});
}
async function pageState() {
  if (!(await ensureHelper())) return { ok: false, reason: 'helper 未注入' };
  const st = await evalJs(R.exprGetState());
  return st && typeof st === 'object' ? st : { ok: false, reason: '页面状态读取失败' };
}
async function pageDiag() {
  if (!(await ensureHelper())) return { ok: false, reason: 'helper 未注入' };
  return await evalJs(R.exprDiag());
}

// ---------- CDP 文件注入 ----------
function cdpAttach() {
  const wc = S.win && S.win.webContents;
  if (!wc) throw new Error('窗口已销毁');
  if (!wc.debugger.isAttached()) wc.debugger.attach('1.3');
}
function cdpDetach() {
  try {
    const wc = S.win && S.win.webContents;
    if (wc && wc.debugger.isAttached()) wc.debugger.detach();
  } catch (e) { /* 忽略 */ }
}
function cdpSend(method, params) {
  const wc = S.win && S.win.webContents;
  if (!wc) return Promise.reject(new Error('窗口已销毁'));
  return wc.debugger.sendCommand(method, params || {});
}
// 用 CDP 把绝对路径塞进页面的 input[type=file]，会正常触发 change 事件（含中文名）。
async function cdpSetFiles(files) {
  const doc = await cdpSend('DOM.getDocument', { depth: -1, pierce: false });
  const rootId = doc && doc.root && doc.root.nodeId;
  if (!rootId) throw new Error('CDP getDocument 没拿到 root nodeId');
  const q = await cdpSend('DOM.querySelector', { nodeId: rootId, selector: 'input[type=file]' });
  if (!q || !q.nodeId) return { ok: false, reason: 'CDP querySelector 没找到 input[type=file]' };
  await cdpSend('DOM.setFileInputFiles', { nodeId: q.nodeId, files: files });
  return { ok: true, nodeId: q.nodeId };
}

// ---------- 通用：等待页面加载完 ----------
async function waitPageReady(timeoutMs) {
  const end = Date.now() + (timeoutMs || 30000);
  while (Date.now() < end) {
    if (S.win && !S.win.isDestroyed()) {
      const wc = S.win.webContents;
      if (!wc.isLoading() && wc.getURL() && wc.getURL() !== 'about:blank') return true;
    }
    await sleep(120);
  }
  return false;
}

// ---------- 键盘事件注入 ----------
// 为什么需要 CDP 而不是只靠 webContents.sendInputEvent：
// 本窗口是 focusable:false（不能让宿主抢走 Python 窗口的焦点），系统级键盘输入进不来，
// sendInputEvent 在这种窗口上不可靠；而 CDP 的 Input.dispatchKeyEvent 直接在渲染进程
// 注入，和 DOM.setFileInputFiles 一样不依赖窗口焦点。两条路都试，CDP 优先、IPC 兜底。
function keyParams(key) {
  const base = { key, code: key, windowsVirtualKeyCode: key === 'Enter' ? 13 : 27, nativeVirtualKeyCode: key === 'Enter' ? 13 : 27 };
  return {
    rawKeyDown: Object.assign({ type: 'rawKeyDown' }, base),
    char: key === 'Enter' ? Object.assign({ type: 'char', text: '\r', unmodifiedText: '\r' }, base) : null,
    keyUp: Object.assign({ type: 'keyUp' }, base)
  };
}
async function dispatchKey(key) {
  const params = keyParams(key);
  const wc = S.win && !S.win.isDestroyed() ? S.win.webContents : null;
  const attempts = [];
  // 1) CDP
  try {
    cdpAttach();
    await cdpSend('Input.dispatchKeyEvent', params.rawKeyDown);
    if (params.char) await cdpSend('Input.dispatchKeyEvent', params.char);
    await cdpSend('Input.dispatchKeyEvent', params.keyUp);
    attempts.push({ via: 'cdp', ok: true });
    cdpDetach();
    return { ok: true, via: 'cdp', attempts };
  } catch (e) {
    attempts.push({ via: 'cdp', ok: false, error: trimText((e && e.message) || e, 200) });
  } finally {
    cdpDetach();
  }
  // 2) 兜底：webContents.sendInputEvent（keyDown/char/keyUp 都要发）
  if (wc) {
    try {
      wc.sendInputEvent({ type: 'keyDown', keyCode: key });
      if (key === 'Enter') wc.sendInputEvent({ type: 'char', keyCode: key });
      wc.sendInputEvent({ type: 'keyUp', keyCode: key });
      attempts.push({ via: 'sendInputEvent', ok: true });
      return { ok: true, via: 'sendInputEvent', attempts };
    } catch (e) {
      attempts.push({ via: 'sendInputEvent', ok: false, error: trimText((e && e.message) || e, 200) });
    }
  }
  return { ok: false, via: 'none', attempts };
}

// ---------- 业务动作 ----------
// 找不到 input[type=file] 时先点附件按钮（多级启发式），再重查；轮询等附件真的挂上。
async function doAttach(files, timeoutMs, settleMsHint) {
  const t0 = Date.now();
  const total = timeoutMs && timeoutMs > 0 ? timeoutMs : 120000;
  const deadline = t0 + total;
  const names = files.map((f) => path.basename(f));
  const expected = files.length;
  const rec = {
    at: nowIso(), kind: 'attach', files: names.length, expected,
    requested: names, how: null, diag: '', filesExist: [], missing: [],
    error: null, attached: 0, picker: null, inject: null, waited: null
  };

  // 存在性预检：路径不存在时 CDP 会整批失败，不如提前报清楚
  for (const f of files) {
    let exists = false;
    try { exists = fs.statSync(f).isFile(); } catch (e) { exists = false; }
    if (!exists) rec.missing.push(f);
  }
  if (rec.missing.length) {
    rec.diag = '有 ' + rec.missing.length + ' 个文件路径不存在或不是文件，已中止注入（CDP 会整批失败）';
    rec.error = 'missing-files';
    S.last.attach = rec;
    return {
      ok: false, attached: 0, expected, failed: rec.missing, how: 'input',
      diag: rec.diag + '：' + rec.missing.slice(0, 3).join(' | ')
    };
  }

  const st0 = await pageState();
  if (!st0.ok && st0.reason) trace('attach 前读状态失败', st0.reason);

  await ensureHelper();
  const info = await evalJs(R.exprAttachInfo());
  rec.picker = info && info.picker ? info.picker : null;

  let hasInput = !!(info && info.hasFileInput);
  if (!hasInput) {
    // 点了附件按钮之后，input[type=file] 有可能会被动态创建出来，等一会儿再查
    const waitEnd = Math.min(deadline, Date.now() + 8000);
    let probe = null;
    while (Date.now() < waitEnd) {
      await sleep(300);
      probe = await evalJs(R.exprFileInputSummary());
      if (probe && probe.exists) { hasInput = true; break; }
    }
    if (!hasInput) {
      const extra = await evalJs(R.exprPickFileInput());
      rec.diag = '页面没有 input[type=file]，且' + (info && info.pickerOpen ? '点击附件按钮后仍未出现' : '未能点到附件按钮')
        + '（登录态判断=' + (st0.loggedIn === null ? 'unknown' : String(st0.loggedIn)) + '）';
      rec.error = 'no-file-input';
      rec.pickerProbe = extra;
      rec.pageHint = st0 && st0.url ? st0.url : '';
      S.last.attach = rec;
      return {
        ok: false, attached: 0, expected, failed: files.slice(), how: 'clicked-attach',
        diag: rec.diag + (info && info.picker && info.picker.reason ? '；附件按钮诊断：' + info.picker.reason : '')
      };
    }
  }

  // 注入
  let how = 'input';
  try {
    cdpAttach();
    const inj = await cdpSetFiles(files);
    rec.inject = inj;
    if (!inj.ok) {
      rec.diag = inj.reason || 'CDP 注入失败';
      rec.error = 'inject-failed';
      S.last.attach = rec;
      return { ok: false, attached: 0, expected, failed: files.slice(), how, diag: rec.diag };
    }
    // 记录注入后 input.files 的真实长度，用于兜底判断
    const after = await evalJs(`(() => { var f = document.querySelector('input[type=file]'); return f ? f.files.length : -1; })()`);
    rec.injectedFiles = after;
  } catch (e) {
    rec.diag = 'CDP 注入异常: ' + trimText((e && e.message) || e, 300);
    rec.error = 'cdp-error';
    S.last.attach = rec;
    return { ok: false, attached: 0, expected, failed: files.slice(), how, diag: rec.diag };
  } finally {
    cdpDetach();
  }

  // 把本次注入的文件名登记到页面里，/state 与停止按钮判定都要靠它
  await evalJs(R.exprSetAttached(names));

  // 轮询等页面把附件真的挂上（附件条目数达到 expected 且**上传完成**）
  //
  // ⚠️ 「数量够了」不等于「可以发了」：条目是先画出来的，文件还在往服务器传。
  // 传完之前点发送，会发出去一条不完整的消息（用户实测遇到过）。所以必须等到
  // 页面上没有"上传中/进度"标记才算就绪。
  const pollEnd = Math.min(deadline, Date.now() + Math.max(12000, Math.min(total - 5000, 60000)));
  let best = 0;
  let lastCount = null;
  let samples = 0;
  let sawUploading = 0;
  while (Date.now() < pollEnd) {
    await sleep(300);
    samples++;
    const c = await evalJs(R.exprCountWithNames(names));
    if (c && typeof c.count === 'number') {
      lastCount = c;
      if (c.count > best) best = c.count;
      if (c.uploading) { sawUploading++; continue; }   // 还在上传：继续等
      if (c.count >= expected) break;
    }
  }
  rec.waited = { ms: Date.now() - t0, samples, last: lastCount, sawUploading };
  rec.attached = best > expected ? expected : best;
  rec.how = how;
  // ⚠️ 每个文件要留出处理时间（用户定：文档 0.5 秒、图片 0.3 秒；由 Python 侧
  // 按扩展名算好传进来，没传就按 0.5 秒/文件兜底）。
  //
  // 为什么光等"发送键可用"还不够：发送键变可用只是站点**渲染层**的信号，
  // 它内部还要把每个文件接进会话状态（解析/上传）。提前点发送，消息会带着
  // 还没就绪的附件提交，官网随后报"服务器繁忙 / 请删除异常文件再发送"。
  const perFile = SEND_SETTLE_PER_FILE_MS;
  const settleNeed = Math.min(60000,
    settleMsHint > 0 ? settleMsHint : files.length * perFile);
  const settleElapsed = Date.now() - t0;
  const settleRest = settleNeed - settleElapsed;
  rec.settleMs = settleRest > 0 ? settleRest : 0;
  if (settleRest > 0) await sleep(settleRest);
  // 复查一次数量有没有掉（掉说明站点还没接稳）
  try {
    const again = await evalJs(R.exprCountWithNames(names));
    if (again && typeof again.count === 'number') {
      lastCount = again;
      if (again.count > rec.attached) rec.attached = again.count;
      if (again.count < rec.attached) {
        rec.diag = '稳定复查时附件数从 ' + rec.attached + ' 降到 ' + again.count;
      }
    }
  } catch (e) {}
  // 容忍度：页面上的"可见附件条目数"只是**渲染层**的观测值，可能因为虚拟滚动、
  // 图片缩略图、官网自己的上限而少数几个。为几个文件把整轮发送中止，代价远大于
  // 收益（用户要的是把数据喂进去，不是逐字节对账）。所以：
  //   · 差 ≤ max(2, 10%) 视为成功，但把差额如实写进 diag；
  //   · 一个都没挂上才算失败（这时才值得重试）。
  const tol = Math.max(1, Math.ceil(expected * 0.1));
  const floor = Math.max(1, expected - tol);
  if (rec.attached < expected) {
    // 兜底：有些实现不把文件名渲染成文本，但 input.files 已经是满的
    const fi = await evalJs(R.exprFileInputSummary());
    const injected = typeof rec.injectedFiles === 'number' ? rec.injectedFiles : 0;
    rec.diag = '页面可见附件只数到 ' + rec.attached + '/' + expected
      + '（input.files=' + injected + '，fileInput=' + (fi && fi.exists ? 'yes' : 'no') + '）';
    if (injected >= expected) {
      rec.attached = expected;
      rec.diag += '；以 input.files 长度为准判定为已挂载';
    } else if (rec.attached >= floor) {
      rec.diag += '；差额在容忍范围内，继续发送（这几个文件可能没挂上）';
    }
  } else {
    rec.diag = 'CDP 注入 ' + expected + ' 个文件，页面已渲染 ' + rec.attached + ' 个附件条目'
      + (sawUploading ? '（等到了上传完成）' : '');
  }

  S.last.attach = rec;
  const failed = rec.attached >= expected ? [] : files.slice();

  // ⚠️ 最后一步：**等发送键变成可用**。
  // 挂了附件之后站点还要把文件接进去（本地预览 → 真正可发送），这期间发送键带
  // ds-button--disabled，点了等于没点 —— 用户实测原话："要等文件加载完才可以用"。
  // 必须等到它可用，attach 才算真正完成，否则后面的 send 一定失败。
  const ready = await waitSendReady(60000);
  rec.sendReady = ready;
  if (!ready.readyMs && ready.readyMs !== 0) rec.sendReady = ready;
  rec.diag = (rec.diag || '') + (ready.ready
    ? '；发送键已可用（等了 ' + (ready.waitedMs / 1000).toFixed(1) + ' 秒）'
    : '；等了 ' + Math.round((ready.waitedMs || 0) / 1000) + ' 秒发送键仍不可用（' + (ready.reason || '') + '）')
    + '；文件处理等待共 ' + (settleNeed / 1000).toFixed(1) + ' 秒';

  return {
    ok: (rec.attached >= expected || rec.attached >= floor) && !!ready.ready,
    attached: rec.attached,
    expected,
    failed,
    how,
    sendReady: !!ready.ready,
    diag: trimText(rec.diag, 500)
  };
}

// 等发送键可用（挂了附件后站点要处理一会儿）。
// 要求**连续两次**看到可用才算数：实测发送键会在可用/禁用之间来回跳，
// 抓到一次瞬时可用就点，很容易落空。
// 返回 { ready, waitedMs, how, disabled, reason }
async function waitSendReady(timeoutMs) {
  const t0 = Date.now();
  const total = timeoutMs && timeoutMs > 0 ? timeoutMs : 60000;
  let last = null;
  let streak = 0;
  while (Date.now() - t0 < total) {
    await ensureHelper();
    last = await evalJs(R.exprSendReady());
    if (last && last.ok && last.disabled === false) {
      streak++;
      if (streak >= 2) return { ready: true, waitedMs: Date.now() - t0, how: last.how };
    } else {
      streak = 0;
    }
    await sleep(400);
  }
  return {
    ready: false, waitedMs: Date.now() - t0,
    how: last && last.how, disabled: last ? last.disabled : null,
    reason: (last && (last.reason || (last.disabled ? '发送键仍为禁用态' : ''))) || '未知'
  };
}

async function doSend(method, text) {
  const mode = method === 'button' || method === 'enter' ? method : 'auto';
  const rec = { at: nowIso(), kind: 'send', mode, text: text || '', tried: [], result: null, cleared: null, error: null };
  await ensureHelper();

  // 需要"顺带发一句话"（最后一批的「我已发送完毕」）：先聚焦输入框再插入文字，
  // 这样文字和附件在同一条消息里发出去。
  if (text) {
    try {
      const f = await evalJs(R.exprFocusComposer());
      if (f && f.ok) {
        await sleep(150);
        try { S.win.webContents.insertText(String(text)); } catch (e) {}
        await sleep(250);
        rec.textInserted = true;
      } else {
        rec.textInserted = false;
        rec.textInsertError = (f && f.reason) || '聚焦输入框失败';
        trace('插入文字失败（继续发附件）', rec.textInsertError);
      }
    } catch (e) {
      rec.textInserted = false;
      rec.textInsertError = String((e && e.message) || e);
    }
  }

  // 站点自己报了发送失败（页面出现"请检查网络后重试"/"服务器繁忙"/"请删除异常文件"之类）
  const siteFailed = async () => {
    try {
      const t = await evalJs('(document.body.innerText||"").slice(-600)');
      const s = String(t || '');
      // "服务器繁忙" 是单个附件级的失败提示，"请删除异常文件再发送" 是整体提示。
      // 实测：一次挂 40 个文件时它们会同时出现（官网单条消息其实接不下这么多），
      // 30 个以内正常。
      if (/请检查网络|发送失败|重试|服务器繁忙|异常文件/.test(s)) {
        return { failed: true, tail: s.slice(-200) };
      }
    } catch (e) {}
    return { failed: false };
  };

  // 发送是否真的发生：优先「输入框被清空」，其次「刚挂的附件已从页面消失」
  // （生成开始后站点常把输入框设成只读，此时输入框读不出内容，只能靠附件消失判断）
  //
  // ⚠️ 还有第三种情况：**纯文字消息**（比如用户自己发「我已发送完毕」）。它没有附件，
  // 发出去后输入框立刻变只读，前两条证据都拿不到 —— 以前会误报"没发出去"。
  // 所以再加一条：发送前没在生成、发送后开始生成了，就是成功提交。
  const streamingBefore = !!(await evalJs(R.exprStreaming()));
  const sendConfirmed = async (budgetMs) => {
    const until = Date.now() + (budgetMs || 2500);
    let cleared = null;
    let att = null;
    for (;;) {
      await sleep(350);
      const sf = await siteFailed();
      if (sf.failed) {
        rec.siteFailed = sf;
        return { confirmed: false, via: 'site-error', cleared, attachments: att, tail: sf.tail };
      }
      cleared = await evalJs(R.exprComposerEmpty());
      rec.cleared = cleared;
      if (cleared && cleared.ok && cleared.empty) {
        await evalJs(R.exprClearAttached());
        return { confirmed: true, via: 'composer-empty', cleared };
      }
      att = await evalJs(R.exprAttachmentsCleared());
      rec.attachmentsCleared = att;
      if (att && att.cleared) {
        await evalJs(R.exprClearAttached());
        return { confirmed: true, via: 'attachments-cleared', cleared, attachments: att };
      }
      // 纯文字消息：没有附件可"消失"，输入框又因为生成中变只读 ——
      // 只要"发之前没生成、现在开始生成了"，就说明消息提交成功。
      if (!streamingBefore) {
        const nowStreaming = !!(await evalJs(R.exprStreaming()));
        if (nowStreaming) {
          await evalJs(R.exprClearAttached());
          return { confirmed: true, via: 'streaming-started', cleared, attachments: att };
        }
      }
      if (Date.now() >= until) break;
    }
    return { confirmed: false, via: 'no-evidence', cleared, attachments: att };
  };

  const tryEnter = async () => {
    if (!S.win || S.win.isDestroyed()) return { ok: false, reason: '窗口已销毁' };
    const focus = await evalJs(R.exprFocusComposer());
    if (!focus || !focus.ok) return { ok: false, reason: (focus && focus.reason) || '输入框聚焦失败', focus };
    await sleep(120);
    const k = await dispatchKey('Enter');
    return { ok: k.ok, via: k.via, attempts: k.attempts, focus };
  };

  let buttonSent = false;
  if (mode === 'button' || mode === 'auto') {
    // ⚠️ 按钮路径要**重试**，不能一次不成就退化到回车。
    // 实测：挂完附件后发送键会在"可用/禁用"之间来回跳（站点还在处理/上传），
    // 抓到一个短暂的可用瞬间点下去可能落空；而回车会把还没传完的消息直接提交，
    // 站点随后报"请检查网络后重试"——看起来就是"没发出去"。
    const btnDeadline = Date.now() + 60000;
    let lastReady = null;
    while (Date.now() < btnDeadline) {
      const rd = await waitSendReady(20000);
      rec.sendReady = rd;
      lastReady = rd;
      if (!rd.ready) {
        rec.tried.push({ how: 'button', out: { ok: false, how: 'disabled', reason: rd.reason } });
        break;                      // 一直不可用，交给下面的兜底
      }
      const clicked = await evalJs(R.exprClickSend());
      rec.tried.push({ how: 'button', out: clicked });
      if (clicked && clicked.ok) {
        // 上传附件可能要十几秒，确认窗口给足 45 秒
        const cf = await sendConfirmed(45000);
        rec.result = { ok: !!cf.confirmed, how: 'button', detail: clicked, confirm: cf };
        rec.confirmed = cf.confirmed;
        rec.confirmVia = cf.via;
        if (cf.confirmed) {
          S.last.send = rec;
          return { ok: true, how: 'button', waitedMs: rd.waitedMs };
        }
        if (cf.via === 'site-error') {
          rec.error = 'site-send-failed';
          rec.diag = '官网提示发送失败：' + (cf.tail || '');
          S.last.send = rec;
          return { ok: false, how: 'failed', reason: rec.diag };
        }
      }
      await sleep(800);              // 没确认也没报错：等一会儿再试一次点击
    }
    if (!lastReady || !lastReady.ready) {
      rec.error = 'send-button-not-ready';
      rec.diag = '发送键等了很久仍不可用：' + ((lastReady && lastReady.reason) || '');
    }
    buttonSent = !!(rec.result && rec.result.ok);
  }
  // 只有在按钮路径彻底没戏时才用回车兜底（回车更容易把没传完的消息提交出去）
  if (!buttonSent && (mode === 'enter' || mode === 'auto')) {
    if (mode === 'auto') {
      rec.diag = (rec.diag ? rec.diag + '；' : '') + '按钮路径没成功，改用回车兜底';
    }
    const e = await tryEnter();
    rec.tried.push({ how: 'enter', out: e });
    if (!e.ok) {
      rec.error = e.reason || 'enter 发送失败';
      S.last.send = rec;
      return { ok: false, how: 'failed' };
    }
    // 回车路径同样要给足上传时间（别把没传完的消息算成成功）
    const cf = await sendConfirmed(30000);
    rec.result = { ok: cf.confirmed, how: 'enter', detail: e, confirm: cf };
    rec.confirmed = cf.confirmed;
    rec.confirmVia = cf.via;
    S.last.send = rec;
    if (cf.via === 'site-error') return { ok: false, how: 'failed', reason: '官网提示发送失败：' + (cf.tail || '') };
    return { ok: cf.confirmed, how: cf.confirmed ? 'enter' : 'failed' };
  }
  const ok = !!(rec.result && rec.result.ok);
  S.last.send = rec;
  return { ok, how: ok ? rec.result.how : 'failed' };
}

async function doStop(timeoutMs) {
  const t0 = Date.now();
  const total = timeoutMs && timeoutMs > 0 ? timeoutMs : 20000;
  const deadline = t0 + total;
  const rec = { at: nowIso(), kind: 'stop', click: null, escape: null, beforeStreaming: null, afterStreaming: null, waitedMs: 0, error: null, diag: '' };
  await ensureHelper();

  const st0 = await pageState();
  rec.beforeStreaming = st0 ? !!st0.streaming : null;
  rec.stopCandidateBefore = st0 && st0.extra ? st0.extra.stopCandidate : null;

  // 本来就没在生成：没什么可停的，直接成功返回（避免把「无事可做」误报成失败）
  if (rec.beforeStreaming === false && !(rec.stopCandidateBefore)) {
    rec.stopped = true;
    rec.how = 'none';
    rec.diag = '页面当前不在生成状态，也没有找到停止按钮，无需停止';
    S.last.stop = rec;
    return { ok: true, stopped: true, how: 'none', diag: rec.diag };
  }

  let how = 'none';
  const clicked = await evalJs(R.exprClickStop());
  rec.click = clicked;
  if (clicked && clicked.ok) how = 'button';

  let stopped = false;
  // 先轮询看按钮点击是否生效
  let pollEnd = Math.min(deadline, Date.now() + Math.min(total, 8000));
  while (Date.now() < pollEnd) {
    await sleep(400);
    const st = await pageState();
    if (st && st.streaming === false) { stopped = true; break; }
  }

  if (!stopped) {
    // 契约要求：找不到停止按钮时必须兜底尝试 Escape
    const wc = S.win && !S.win.isDestroyed() ? S.win.webContents : null;
    if (wc) {
      const focus = await evalJs(R.exprFocusComposer());
      await sleep(100);
      const k = await dispatchKey('Escape');
      rec.escape = { sent: k.ok, via: k.via, attempts: k.attempts, focus };
      if (how === 'none') how = 'escape';
    } else {
      rec.escape = { sent: false, reason: '窗口已销毁' };
    }
    const escEnd = Math.min(deadline, Date.now() + Math.max(1500, total - (Date.now() - t0) - 200));
    while (Date.now() < escEnd) {
      await sleep(400);
      const st = await pageState();
      if (st && st.streaming === false) { stopped = true; if (how === 'none') how = 'escape'; break; }
    }
  }

  const stEnd = await pageState();
  rec.afterStreaming = stEnd ? !!stEnd.streaming : null;
  rec.waitedMs = Date.now() - t0;
  if (!stopped && rec.afterStreaming === false) stopped = true;
  if (stopped && how === 'none') how = 'button';
  rec.stopped = stopped;
  rec.how = stopped ? how : 'none';
  if (stopped) {
    rec.diag = how === 'escape'
      ? '未点到停止按钮，用 Escape 中断后页面报告 streaming=false'
      : '点击停止按钮后页面报告 streaming=false';
  } else {
    rec.error = '点了停止键并尝试 Escape 后，页面仍报告 streaming=true';
    rec.diag = rec.error + '（停止按钮候选：' + JSON.stringify((rec.click && rec.click.candidates) || null).slice(0, 200) + '）';
  }
  S.last.stop = rec;
  return { ok: stopped, stopped, how: rec.how, diag: rec.diag };
}

// ---------- 诊断落盘 ----------
function writeDiag(snapshot) {
  const payload = {
    at: nowIso(),
    version: VERSION,
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    profile: ARGS.profile,
    url: ARGS.url,
    port: S.port,
    hwnd: S.hwnd,
    pageReady: S.pageReady,
    helperReady: S.helperReady,
    snapshot: snapshot || null,
    lastAttach: S.last.attach,
    lastSend: S.last.send,
    lastStop: S.last.stop
  };
  const file = S.diagFile;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(payload, null, 2), 'utf8');
    return { ok: true, file };
  } catch (e) {
    return { ok: false, file, reason: '写诊断文件失败: ' + trimText((e && e.message) || e, 200) };
  }
}

// ---------- HTTP 控制服务 ----------
function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > 4 * 1024 * 1024) { req.destroy(); resolve({}); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (e) { resolve({ __parseError: trimText((e && e.message) || e, 200) }); }
    });
    req.on('error', () => resolve({}));
  });
}
function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  try {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length, 'Cache-Control': 'no-store' });
    res.end(body);
  } catch (e) { /* 连接已断，忽略 */ }
}

function startServer() {
  return new Promise((resolve, reject) => {
    const server = http.createServer(async (req, res) => {
      let url;
      try { url = new URL(req.url, 'http://127.0.0.1'); } catch (e) { return sendJson(res, 400, { ok: false, error: 'bad-url' }); }
      const route = url.pathname;
      bdiag('请求 ' + req.method + ' ' + route);
      // token 校验：不带或不对一律 403
      if (req.headers['x-token'] !== ARGS.token) return sendJson(res, 403, { ok: false, error: 'forbidden' });
      trace('HTTP', req.method, route);
      // 控制接口按路径分发，GET/POST 都接受：契约要求调用方用 POST，
      // 但客户端把 /diag 之类误发成 GET 时不该莫名 404，保持一致更好排查。
      try {
        if (route === '/ping') return sendJson(res, 200, { ok: true, version: VERSION });

        // 调试用：把最近一次 attach/send/stop 的内部记录摊开。
        // 排查"为什么没发出去"时，这个比任何猜测都直接。
        if (route === '/last') {
          return sendJson(res, 200, { ok: true, last: S.last });
        }

        if (route === '/state') {
          const st = await pageState();
          if (!st || st.ok === false) {
            return sendJson(res, 200, {
              ok: true, url: currentUrl(), ready: S.pageReady, readyState: '',
              loggedIn: null, streaming: false, attachments: 0, fileInput: false,
              note: (st && st.reason) || '页面状态不可读'
            });
          }
          return sendJson(res, 200, {
            ok: true,
            url: st.url || currentUrl(),
            ready: S.pageReady,
            // 页面自己报的就绪状态也要给出去：S.pageReady 是主进程侧的标志，
            // 万一它和真实状态不同步（历史上就卡过一次），调用方还有第二条判据。
            readyState: st.readyState || '',
            hasComposer: !!st.hasComposer,
            loggedIn: st.loggedIn === undefined ? null : st.loggedIn,
            streaming: !!st.streaming,
            attachments: typeof st.attachments === 'number' ? st.attachments : 0,
            fileInput: !!st.fileInput,
            extra: { hasComposer: !!st.hasComposer, attachCount: st.attachCount, stopCandidate: st.stopCandidate, textHint: !!st.textHint }
          });
        }

        if (route === '/attach') {
          const body = await readBody(req);
          const files = Array.isArray(body.files) ? body.files.filter((f) => typeof f === 'string' && f) : [];
          if (!files.length) return sendJson(res, 200, { ok: false, attached: 0, expected: 0, failed: [], how: 'input', diag: 'body.files 为空或不是字符串数组' });
          const out = await doAttach(files, Number(body.timeoutMs) || 120000,
                                     Number(body.settleMs) || 0);
          return sendJson(res, 200, out);
        }

        if (route === '/send') {
          const body = await readBody(req);
          const out = await doSend(typeof body.method === 'string' ? body.method : 'auto',
                                   typeof body.text === 'string' ? body.text : '');
          return sendJson(res, 200, out);
        }

        if (route === '/stop') {
          const body = await readBody(req);
          const out = await doStop(Number(body.timeoutMs) || 20000);
          return sendJson(res, 200, out);
        }

        if (route === '/navigate') {
          const body = await readBody(req);
          const target = typeof body.url === 'string' && body.url ? body.url : '';
          if (!target) return sendJson(res, 200, { ok: false, error: 'body.url 为空' });
          const wc = S.win && !S.win.isDestroyed() ? S.win.webContents : null;
          if (!wc) return sendJson(res, 200, { ok: false, error: '窗口已销毁' });
          S.pageReady = false;
          S.helperReady = false;
          wc.loadURL(target).catch((e) => trace('navigate 失败', String((e && e.message) || e)));
          return sendJson(res, 200, { ok: true });
        }

        // 往输入框里写字（可顺便回车发送）。
        // 这是**不依赖操作系统焦点**的通道：CDP/Electron 直接把文本交给渲染层，
        // 所以即使跨进程子窗口的键盘输入出了问题，用户也能把话发给 AI。
        if (route === '/type') {
          const body = await readBody(req);
          const text = String(body.text || '');
          const submit = !!body.submit;
          await ensureHelper();
          // ⚠️ 必须先把光标放进"要写的那个框"：insertText 是往**文档当前选区**插入的，
          //    不认我们打算写哪个。页面有两个输入框时（登录页的手机号+验证码），
          //    不先聚焦就会写错框（验收脚本抓到过：写进了旧的 textarea）。
          const tgt = await evalJs(R.exprFocusTarget());
          const focused = await evalJs(R.exprFocusComposer());
          if (!focused || !focused.ok) {
            return sendJson(res, 200, { ok: false, error: (focused && focused.reason) || '输入框聚焦失败' });
          }
          await sleep(120);
          // 按目标元素类型选注入手段（不再"先 insertText 再核实+回退"）：
          //   TEXTAREA / contenteditable → insertText（实测有效，且能唤醒框架的输入绑定）
          //   INPUT                     → 直接设 value + 派发 input/change
          //     （实测 insertText 对 <input> **静默无效**：返回成功、value 不变；
          //       即使先 focus() 也照样写不进去。登录页就是 <input type="tel">。）
          const isInput = !!(tgt && tgt.ok && tgt.tag === 'input');
          let inserted = false;
          let via = '';
          let value = '';
          if (isInput) {
            // usePin=true：用 focusTarget 钉住的句柄写，避免中间 activeElement 被挪走
            const fell = await evalJs(R.exprSetComposerValue(text, true, true));
            inserted = !!(fell && fell.ok);
            via = 'setValue';
            value = (fell && fell.value) || '';
          } else {
            try { S.win.webContents.insertText(text); inserted = true; via = 'insertText'; } catch (e) {}
          }
          if (!inserted) {
            return sendJson(res, 200, { ok: false, via: via,
                                        error: '写不进输入框（目标 tag=' +
                                               ((tgt && tgt.tag) || '?') + '）', target: tgt || null });
          }
          await sleep(200);
          let sent = null;
          if (submit) sent = await doSend('auto');
          return sendJson(res, 200, { ok: inserted, inserted: inserted, via: via,
                                      value: value, target: tgt || null,
                                      submit: submit, send: sent });
        }

        // ── 窗口位置/尺寸：不再 SetParent 嵌入，改成"独立顶层窗口跟着主窗口走" ──
        // 为什么要放弃 SetParent（2026-09-15 实测）：
        //   跨进程 SetParent 之后，键盘**整条通道**都废了 —— 不但键盘队列消息送不到，
        //   连直接 PostMessage 到 Chrome_RenderWidgetHostHWND 的 WM_CHAR 也不生效
        //   （实测：独立窗口 value='138' 成功；嵌入后同样投递 value=''）。
        //   而独立顶层窗口是正常的 Win32 窗口，点一下就拿到焦点，打字/粘贴全都正常。
        if (route === '/bounds') {
          bdiag('进入 /bounds');
          const body = await readBody(req);
          bdiag('读到 body ' + JSON.stringify(body));
          let x = Math.round(Number(body.x) || 0);
          let y = Math.round(Number(body.y) || 0);
          let w = Math.max(200, Math.round(Number(body.w) || 800));
          let h = Math.max(150, Math.round(Number(body.h) || 600));
          let applied = false;
          try {
            // ⚠️⚠️ 有 owner 时**绝不**在这里 show()。实测（2026-09-16）：owner 设好
            // 之后，对"隐藏中"的窗口调 show() 会让 Electron 主进程**卡死**（/show
            // 一直不出结果，宿主 10s 超时）。触发条件是 "owned + hidden"，无 owner
            // 时 show() 正常（0.03s）。
            //   · 有 owner：显示交给宿主的 /show —— 它在那之前会先摘掉 owner
            //     （见 host.show()），所以永远不会踩到这个组合；
            //   · 无 owner（独立顶层窗口，含 v3.0.0 的用法）：保持原行为，
            //     `/bounds` 顺带把窗口显示出来（`ds_window_accept.py` 依赖这条）。
            if (!OWNER_SET) {
              if (!S.win.isVisible()) S.win.show();
            }
            // ⚠️ 子窗口模式下坐标是**相对父窗口客户区**的；独立窗口模式才是屏幕坐标。
            //    不区分的话子窗口会被摆到屏幕外面（表现为"嵌进去就看不见了"）。
            const useParent = !!ARGS.parent;
            bdiag('调用 setBounds ' + JSON.stringify({ x: x, y: y, w: w, h: h, useParent: useParent }));
            S.win.setBounds({ x: x, y: y, width: w, height: h },
                            useParent ? ['parent'] : []);
            applied = true;
            bdiag('setBounds 完成');
          } catch (e) {
            bdiag('setBounds 抛错 ' + String((e && e.message) || e));
          }
          return sendJson(res, 200, { ok: applied, bounds: { x: x, y: y, w: w, h: h },
                                      visible: (() => { try { return S.win.isVisible(); } catch (e) { return null; } })() });
        }

        // `/focus` 与 `/summon` 共用：**只读一次焦点快照，立刻返回**。
        //
        // ⚠️⚠️ 这三条都是 2026-09-16 用诊断日志（每个请求落文件）定位出来的，
        //    任何一条加回来都会让"退回页签再进来"卡住：
        //   1. **不 await 等焦点确认**。旧实现在这里轮询 `isFocused()`
        //      （最多 10×60ms + 一轮 120ms），拿不到前台就一直等 —— 单次请求就能
        //      把整条 HTTP 通道占到宿主 10s 超时，连带 `set_theme()` 也超时。
        //   2. **不调 `win.show()`**。窗口有 owner 之后，对 **owned + hidden** 的窗口
        //      show() 会让 Electron 主进程**同步卡住**（日志里下一个请求空了 63 秒）。
        //      显示交给宿主：`host.show()` 会"先摘 owner 再 show"（见那边说明）。
        //   3. **不调 `win.focus()` / `win.moveTop()`**。owner 窗口上这两下同样会卡
        //      （日志里空了 71 秒）。owner 本来就永远压在宿主之上，不需要 moveTop；
        //      键盘焦点由 Windows 在用户点击时自己交。
        //
        // 所以这里只读状态、不改状态 —— 实测 0.00s，且 focused 值如实反映现实。
        const focusNow = () => {
          if (!S.win || S.win.isDestroyed()) return { ok: false, error: '窗口已销毁' };
          const wc = S.win.webContents;
          let focused = false;
          try { focused = !!wc.isFocused(); } catch (e) { focused = false; }
          return { ok: true, focused: focused,
                   winFocused: (() => { try { return S.win.isFocused(); } catch (e) { return null; } })(),
                   wcFocused: (() => { try { return wc.isFocused(); } catch (e) { return null; } })() };
        };

        if (route === '/summon') {
          return sendJson(res, 200, focusNow());
        }

        // 键盘焦点给回页面。切走再切回来时 WebContents 会丢掉自己的焦点，
        // 表现就是"点进输入框打字没反应"。
        //
        // ⚠️⚠️ 这里**绝对不能调 showInactive()**。它按定义是"显示但不激活"，
        // 会把上一行刚拿到的焦点又撤掉 —— 而 Chromium 认为自己没焦点时**直接不处理
        // 键盘输入**。旧代码正是「focus() → webContents.focus() → showInactive()」，
        // 于是 /focus 永远返回 focused:false，嵌入页面永远收不到真实按键
        // （2026-09-15 实测定位，这是"打字没反应"的直接原因）。
        if (route === '/focus') {
          const r = focusNow();
          trace('/focus ->', r.focused);
          return sendJson(res, 200, r);
        }

        // 页面里"用户最后点过的输入框"是什么（诊断 + 界面提示）
        if (route === '/focused') {
          if (!(await ensureHelper())) return sendJson(res, 200, { ok: false, error: 'helper 未注入' });
          const info = await evalJs(R.exprFocusedInfo());
          return sendJson(res, 200, { ok: !!(info && info.ok), info: info || null });
        }

        // 置顶开关。独立顶层窗口的代价是"切到别的程序它也不会自动让位"，
        // 所以用 alwaysOnTop 让它只压在主窗口上面，不压别的应用。
        if (route === '/topmost') {
          const body = await readBody(req);
          const on = !!body.on;
          try { S.win.setAlwaysOnTop(on, 'normal'); } catch (e) {}
          return sendJson(res, 200, { ok: true, on: on,
                                      isTop: (() => { try { return S.win.isAlwaysOnTop(); } catch (e) { return null; } })() });
        }

        // 嵌入式流程的一环：Python 先 SetParent 到自己的窗口，再调 /show 让 Electron 自己显示
        if (route === '/show') {
          if (!S.win || S.win.isDestroyed()) return sendJson(res, 200, { ok: false, visible: false, error: '窗口已销毁' });
          S.win.show();
          return sendJson(res, 200, { ok: true, visible: S.win.isVisible() });
        }

        if (route === '/hide') {
          if (!S.win || S.win.isDestroyed()) return sendJson(res, 200, { ok: false, visible: false, error: '窗口已销毁' });
          S.win.hide();
          return sendJson(res, 200, { ok: true, visible: false });
        }

        // 窗口底色跟着宿主那块面板走。目的只有一个：**消除边界感**。
        // 页面还没画出来/切换主题的那一瞬间，窗口底色会露出来；如果它是纯白、
        // 而主窗口那块面板是别的颜色，就会闪出一个"白角"，看起来就是两个软件。
        // （frame:false 的无边框窗口在 Windows 上本来就没有系统描边/阴影。）
        if (route === '/bg') {
          const body = await readBody(req);
          const color = String(body.color || '').trim();
          let ok = false;
          if (/^#[0-9a-fA-F]{6}$/.test(color)) {
            try { S.win.setBackgroundColor(color); ok = true; } catch (e) {}
          }
          return sendJson(res, 200, { ok: ok, color: color });
        }

        // 深浅色跟着宿主的软件走：改的是 Chromium 的 prefers-color-scheme，
        // 官网自己那套浅色/深色配色会跟着切。
        if (route === '/theme') {
          const body = await readBody(req);
          const mode = ['dark', 'light', 'system'].indexOf(body.mode) >= 0 ? body.mode : 'system';
          try { nativeTheme.themeSource = mode; } catch (e) {}
          return sendJson(res, 200, {
            ok: true,
            mode: nativeTheme.themeSource,
            dark: !!nativeTheme.shouldUseDarkColors
          });
        }

        // 把浏览器摆到指定坐标，**只移动**：不动显隐、不抢焦点（宿主拖动/提示恢复用）。
        // ⚠️ 这里**绝对不能 show()**：提示让位/弹窗让位期间窗口是被有意藏起来的，
        //    在这里 show 就等于"把浏览器又盖回提示上面"（用户实测反馈过的遮挡问题）。
        //    需要显示时由宿主显式走 /show（它会先摘 owner 再 show）。
        // ⚠️ 也不 setBounds 之外的任何激活类调用（moveTop/focus）。
        if (route === '/restore') {
          const body = await readBody(req);
          if (!S.win || S.win.isDestroyed()) return sendJson(res, 200, { ok: false, error: '窗口已销毁' });
          let applied = false;
          try {
            const has = (typeof body.x === 'number' && typeof body.y === 'number'
                         && typeof body.w === 'number' && typeof body.h === 'number');
            if (has) {
              const useParent = !!ARGS.parent;
              bdiag('/restore setBounds ' + JSON.stringify(body) + ' useParent=' + useParent);
              S.win.setBounds({ x: Math.round(body.x), y: Math.round(body.y),
                                width: Math.max(200, Math.round(body.w)),
                                height: Math.max(150, Math.round(body.h)) },
                              useParent ? ['parent'] : []);
            }
            applied = true;
          } catch (e) {
            trace('/restore 失败', String((e && e.message) || e));
          }
          return sendJson(res, 200, { ok: applied, visible: (() => {
            try { return S.win.isVisible(); } catch (e) { return null; } })() });
        }

        // 窗口形态自检（真嵌入实验/验收用）：父窗口、是否相对父窗口定位、边框状态。
        // 为什么要这条：`BrowserWindow{parent}` 之后，窗口到底有没有变成**子窗口**、
        // 坐标是屏幕坐标还是相对父窗口，光看 Python 侧的 GetParent 不够（Electron
        // 可能只是建了 owner 关系）。这里把 native 侧的答案一次问清楚。
        if (route === '/wininfo') {
          const out = { ok: true, pid: process.pid, hwnd: S.hwnd };
          try {
            const buf = S.win.getNativeWindowHandle();
            const h = (buf && buf.length >= 8) ? buf.readBigUInt64LE(0) : BigInt(0);
            out.nativeHwnd = h.toString();
            // 用 koffi/ffi 太笨重；Win32 的 GetParent 交给调用方（Python）去查即可
          } catch (e) {}
          try { out.bounds = S.win.getBounds(); } catch (e) {}
          try { out.contentBounds = S.win.getContentBounds(); } catch (e) {}
          try { out.visible = S.win.isVisible(); } catch (e) {}
          try { out.focused = S.win.isFocused(); } catch (e) {}
          out.parentArg = ARGS.parent || 0;
          out.topmost = (() => { try { return S.win.isAlwaysOnTop(); } catch (e) { return null; } })();
          return sendJson(res, 200, out);
        }

        // 执行一段页面侧表达式并回读结果。
        // 用途：验收"真实键盘到底打进去了没有" —— 光看 win32 焦点/页面 hasFocus()
        // 都不算数（v3.0.0 就是"光标在闪但字进不去"），必须回读**页面里真实的文本**。
        // 只用来读，测试脚本用；不对外暴露（仍受 X-DSH-Token 保护）。
        if (route === '/debug/eval') {
          const body = await readBody(req);
          const expr = String((body && body.expr) || '');
          let out = null, err = '';
          try {
            out = await S.win.webContents.executeJavaScript(expr, true);
          } catch (e) {
            err = String((e && e.message) || e);
          }
          return sendJson(res, 200, { ok: !err, result: out === undefined ? null : out, error: err });
        }

        // 把页面的输入框聚焦好（**只聚焦，不写任何文字**）。
        // 用途：验收"真实键盘"时要把变量隔离掉 —— 不能既用注入文字又把焦点交给页面，
        // 否则分不清字是"打进去的"还是"注入进去的"。
        if (route === '/debug/focus-composer') {
          if (!(await ensureHelper())) return sendJson(res, 200, { ok: false, error: 'helper 未注入' });
          const r = await evalJs(R.exprFocusComposer());
          return sendJson(res, 200, { ok: !!(r && r.ok), result: r || null });
        }

        // 告知 Electron：宿主已把本窗口设成它的 owner（或已解除）。
        // 目的只有一个 —— 让 /bounds 知道"有 owner 时不能对隐藏窗口 show()"
        // （那个组合会让主进程卡死，见 /bounds 里的说明）。
        if (route === '/owner') {
          const body = await readBody(req);
          OWNER_SET = !!body.on;
          bdiag('OWNER_SET -> ' + OWNER_SET);
          return sendJson(res, 200, { ok: true, ownerSet: OWNER_SET });
        }

        // 把页面上"还没发出去"的附件点掉（取消发送后用）。
        // ⚠️ 为什么需要：取消后附件会留在输入区，下一次发送的 `_ensure_idle()`
        // 会判定"输入区还挂着 N 个附件"而拒绝继续，用户看到的是"取消了却一直
        // 让我等 / 让我自己去页面上删"（用户实测反馈）。
        if (route === '/debug/clear-attachments') {
          if (!(await ensureHelper())) return sendJson(res, 200, { ok: false, error: 'helper 未注入' });
          const before = await evalJs(R.exprCountWithNames([]));
          let removed = 0, err = '';
          try {
            const r = await evalJs(R.exprRemoveAttachments());
            if (r && typeof r === 'object') {
              removed = Number(r.removed || 0);
              err = String(r.error || '');
            } else {
              removed = Number(r || 0);
            }
          } catch (e) {
            err = String((e && e.message) || e);
          }
          trace('/debug/clear-attachments ->', { before: before, removed: removed, err: err });
          return sendJson(res, 200, { ok: !err, removed: removed, before: before, error: err });
        }

        if (route === '/diag') {
          const snap = await pageDiag();
          S.diag = snap;
          const w = writeDiag(snap);
          if (!w.ok) return sendJson(res, 200, { ok: false, file: w.file, error: w.reason });
          return sendJson(res, 200, { ok: true, file: w.file });
        }

        // 仅调试：--ds-trace 时开放，用于排查启发式为什么选中/没选中某个元素。
        // 不传 expr 时返回助手对象的函数名清单。
        if (route === '/eval') {
          if (!ARGS.trace) return sendJson(res, 404, { ok: false, error: 'unknown-route', route });
          const body = await readBody(req);
          if (typeof body.expr !== 'string' || !body.expr) {
            const names = await evalJs(`Object.keys(window.${R.HELPER_NAME} || {})`);
            return sendJson(res, 200, { ok: true, helperKeys: names });
          }
          const out = await evalJs(body.expr);
          return sendJson(res, 200, { ok: true, result: out });
        }

        if (route === '/quit') {
          sendJson(res, 200, { ok: true });
          S.quitting = true;
          setTimeout(() => { try { app.exit(0); } catch (e) { process.exit(0); } }, 80);
          return;
        }

        return sendJson(res, 404, { ok: false, error: 'unknown-route', route });
      } catch (e) {
        const msg = trimText((e && e.stack) || e, 500);
        trace('HTTP 处理异常', msg);
        return sendJson(res, 500, { ok: false, error: 'handler-exception', detail: msg });
      }
    });
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      S.server = server;
      S.port = server.address().port;
      resolve(S.port);
    });
  });
}

function currentUrl() {
  try {
    const wc = S.win && !S.win.isDestroyed() ? S.win.webContents : null;
    return wc ? wc.getURL() : ARGS.url;
  } catch (e) { return ARGS.url; }
}

// ---------- 窗口 ----------
function createWindow() {
  // ⚠️ 只有显式给了 --parent=<HWND> 才建子窗口；默认仍是"独立顶层窗口 + 跟着摆位"
  //    （2026-09-15 实测：跨进程 SetParent 会让键盘整条通道全废，见 5.18）。
  //    这条分支是**实验性的**，用它之前必须验"能不能真打字"。
  const asChild = !!ARGS.parent;
  const win = new BrowserWindow({
    // show:false —— 摆好位（Python 的 /bounds）之前绝不显示；
    // 这样不会在屏幕左上角闪一个还没摆位的 1000x700 窗口。
    show: false,
    frame: false,
    width: 1000,
    height: 700,
    x: 0,
    y: 0,
    // 子窗口模式：交给 Electron 自己设 parent，它比手工 SetParent 更"正规"
    // （焦点/owner 语义由它维护）。父窗口句柄从命令行来。
    ...(asChild ? { parent: ARGS.parent } : {}),
    // 底色由宿主用 /bg 同步成主窗口那块面板的颜色（默认给个浅灰蓝，
    // 比纯白更接近面板色，避免加载瞬间闪白角）。
    backgroundColor: '#f7f8fc',
    // Windows 11 会给无边框窗口加圆角，而宿主那块矩形是直的 ——
    // 关掉它，四角才对得上（少一处"两个软件"的边界感）。
    roundedCorners: false,
    // focusable 必须是 true —— 用户要在这个页面里输入手机号/验证码。
    // （早期设成 false，窗口带 WS_EX_NOACTIVATE，键盘输入根本进不来，
    //   表现为"官网能看不能填"。Enter/Escape 走 CDP 注入，不依赖焦点。）
    focusable: true,
    skipTaskbar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // 隐藏期间也要正常渲染（否则隐藏窗口里的页面会停摆）
      paintWhenInitiallyHidden: true,
      backgroundThrottling: false,
      spellcheck: false
    }
  });
  S.win = win;
  try { win.setMenuBarVisibility(false); } catch (e) {}
  try {
    const h = win.getNativeWindowHandle();
    S.hwnd = (h && h.length >= 8 ? h.readBigUInt64LE(0) : BigInt(0)).toString();
  } catch (e) {
    S.hwnd = '0';
    trace('读 HWND 失败', String((e && e.message) || e));
  }

  win.webContents.setUserAgent(CHROME_UA);

  // ⚠️ 就绪标志不能挂在 did-start-loading 上。
  // 官网页加载完之后还会不断有子资源/接口活动，Electron 会为此再发
  // did-start-loading（却没有对应的 did-finish-load），标志位就永久卡在 false ——
  // 用户看到的现象是：页面明明已经好了，程序却说"还在加载，不能发送"。
  // 现在改成：只认「主框架导航开始」为失效条件，完成后就一直是就绪。
  win.webContents.on('did-start-navigation', (e, url, isInPlace, isMainFrame) => {
    if (isMainFrame && !isInPlace) {
      S.pageReady = false;
      S.helperReady = false;
      trace('did-start-navigation', url);
    }
  });
  win.webContents.on('did-finish-load', async () => {
    S.pageReady = true;
    S.helperReady = false;
    // 尽早把助手脚本注入，省掉第一次 API 调用的等待
    try { await ensureHelper(); } catch (e) {}
    trace('did-finish-load', currentUrl());
  });
  win.webContents.on('did-fail-load', (e, code, desc, url, isMainFrame) => {
    if (isMainFrame) {
      S.pageReady = false;
      S.helperReady = false;
      trace('did-fail-load', code, desc, url);
    }
  });
  win.webContents.on('render-process-gone', (e, details) => {
    trace('render-process-gone', details);
    S.pageReady = false;
    S.helperReady = false;
  });
  win.on('closed', () => { S.win = null; });

  win.loadURL(ARGS.url).catch((e) => trace('初次加载失败', String((e && e.message) || e)));
  return win;
}

// ---------- 启动 ----------
app.on('window-all-closed', () => {
  // 窗口被关掉（例如嵌入式父窗口销毁）就退出，避免留下孤儿进程
  if (!S.quitting) { try { app.exit(0); } catch (e) { process.exit(0); } }
});
process.on('uncaughtException', (e) => {
  trace('uncaughtException', String((e && e.stack) || e));
  writeStdout('DSVIEW_ERR ' + JSON.stringify({ error: trimText((e && e.message) || e, 300) }));
});
process.on('unhandledRejection', (e) => {
  trace('unhandledRejection', String((e && e.stack) || e));
});

app.whenReady().then(async () => {
  createWindow();
  let port = 0;
  try {
    port = await startServer();
  } catch (e) {
    writeStdout('DSVIEW_ERR ' + JSON.stringify({ error: 'HTTP 服务启动失败: ' + trimText((e && e.message) || e, 300) }));
    app.exit(1);
    return;
  }
  // 第一行就可能被 Python 读到空行，所以 ready 行必须是独立的一行且立即 flush
  writeStdout('DSVIEW_READY ' + JSON.stringify({ port: port, hwnd: S.hwnd, version: VERSION }));
  trace('ready', { port, hwnd: S.hwnd, profile: ARGS.profile, url: ARGS.url });
  // 等页面首次加载完再写一份初始诊断，方便未登录时就能看 DOM
  waitPageReady(30000).then(async () => {
    try {
      const snap = await pageDiag();
      S.diag = snap;
      writeDiag(snap);
      trace('初始诊断已写出', S.diagFile);
    } catch (e) { trace('初始诊断失败', String((e && e.message) || e)); }
  });
}).catch((e) => {
  writeStdout('DSVIEW_ERR ' + JSON.stringify({ error: 'whenReady 失败: ' + trimText((e && e.message) || e, 300) }));
  app.exit(1);
});
