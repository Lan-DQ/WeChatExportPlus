'use strict';
// DeepSeek 网页版宿主的「页面内启发式」集合。
//
// 本文件在渲染进程里运行，但入口是 buildHelper()：它返回一段可被
// webContents.executeJavaScript 执行的 JS 源码字符串。因此这里的逻辑
// 只依赖浏览器 DOM API，不依赖 Node / Electron 的任何东西。
//
// 设计要点：
//   1) 官网 DOM 会变、且未登录时页面上根本没有 input[type=file]，
//      所以所有选择都必须是「多级启发式 + 候选诊断」，不能写死选择器。
//   2) 任何一步失败都返回结构化原因（ok:false + reason + candidates），
//      绝不抛异常把主进程的调用链打断。
//   3) 所有判定过程都进日志缓冲区，供 /diag 落盘。

const HELPER_NAME = '__DSVIEW_HELPER__';

/** 生成注入到页面里的助手对象源码。 */
function buildHelper() {
  return String.raw`(() => {
  if (window.${HELPER_NAME} && window.${HELPER_NAME}.__v === 1) return true;

  // ---------- 基础工具 ----------
  function txt(el) {
    try {
      if (!el) return '';
      var t = el.innerText;
      if (typeof t === 'string' && t) return t;
      t = el.textContent;
      return typeof t === 'string' ? t : '';
    } catch (e) { return ''; }
  }
  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function clsOf(el) {
    try {
      var c = el.getAttribute && el.getAttribute('class');
      return typeof c === 'string' ? c : (typeof el.className === 'string' ? el.className : '');
    } catch (e) { return ''; }
  }
  function rectOf(el) {
    var r = { x: 0, y: 0, w: 0, h: 0 };
    try {
      var b = el.getBoundingClientRect();
      r = { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) };
    } catch (e) {}
    return r;
  }
  // 该元素在它自己的祖先里是否被 display:none / 隐藏样式压住。
  // 注意：不能只看 getBoundingClientRect —— display:none 的元素在浏览器里
  // 仍然可能返回非零尺寸（第一次布局尚未计算时尤其如此），必须逐级查 computedStyle。
  function shownInAncestors(el) {
    try {
      var n = el;
      var depth = 0;
      while (n && n.nodeType === 1 && depth < 60) {
        var w = (n.ownerDocument && n.ownerDocument.defaultView) || window;
        var st = w.getComputedStyle(n);
        if (!st) break;
        if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
        if (parseFloat(st.opacity || '1') < 0.05) return false;
        n = n.parentElement;
        depth++;
      }
      return true;
    } catch (e) { return true; }
  }
  function visible(el) {
    try {
      if (!shownInAncestors(el)) return false;
      var r = el.getBoundingClientRect();
      var w = r.width, h = r.height;
      // 窗口刚创建/隐藏时整页可能还没布局（尺寸全为 0），此时不靠尺寸判定，交给祖先可见性
      var docW = document.documentElement ? document.documentElement.clientWidth : 0;
      var laidOut = docW > 0 && document.body && document.body.getBoundingClientRect().height > 0;
      if (laidOut) {
        if (w < 4 || h < 4) return false;
      } else if (w === 0 && h === 0) {
        return false;
      }
      return true;
    } catch (e) { return false; }
  }
  function pathOf(el) {
    var parts = [];
    var n = el;
    var depth = 0;
    while (n && n.nodeType === 1 && depth < 6) {
      var s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; parts.unshift(s); break; }
      var c = clsOf(n).split(/\s+/).filter(Boolean)[0];
      if (c) s += '.' + c;
      parts.unshift(s);
      n = n.parentElement;
      depth++;
    }
    return parts.join(' > ');
  }
  var __idx = 0;
  function tagCandidate(el) {
    __idx += 1;
    try { el.setAttribute('data-dsh-idx', String(__idx)); } catch (e) {}
    return __idx;
  }
  function clickable(el) {
    try {
      // 点到 <svg>/<path> 子元素不一定能触发按钮的 click 监听，必须上溯到真正可点的宿主
      var target = el;
      if (el.tagName === 'SVG' || el.tagName === 'svg' || /^(path|rect|circle|line|polygon|g)$/i.test(el.tagName)) {
        var host = el.closest ? el.closest('button,[role="button"],a[href],label') : null;
        if (host) target = host;
      }
      if (target.click) { target.click(); return true; }
      target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return true;
    } catch (e) { return false; }
  }
  function scrollTo(el) { try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { try { el.scrollIntoView(); } catch (e2) {} } }

  var LOG = [];
  function log(kind, data) {
    try {
      LOG.push({ at: new Date().toISOString(), kind: kind, data: data });
      if (LOG.length > 80) LOG.shift();
    } catch (e) {}
  }

  // ---------- 本次会话已注入的附件名单 ----------
  // /state 拿不到调用方传入的路径，所以把最近一次 attach 注入的文件名记在页面里，
  // 这样 /state.attachments 和停止按钮判定都能用上「确切的文件名」这个最强证据。
  var ATT = { names: [], expected: 0, at: null, submittedCount: 0 };
  function setAttached(names) {
    var arr = [];
    if (names && names.length) {
      for (var i = 0; i < names.length; i++) {
        var n = String(names[i] || '');
        if (!n) continue;
        // 全路径只留文件名；页面上的附件条目只会显示文件名
        var parts = n.split(/[\/]/);
        arr.push(parts[parts.length - 1]);
      }
    }
    ATT.names = arr;
    ATT.expected = arr.length;
    ATT.at = new Date().toISOString();
    log('setAttached', { n: arr.length, head: arr.slice(0, 3) });
    return { ok: true, expected: ATT.expected };
  }
  function clearAttached() {
    // 清空「当前待发附件」，但保留 submittedCount —— 它记录本次会话确实提交过消息，
    // 是「只读输入框 = 正在生成」这个判定的前提。
    if (ATT.names.length > 0) ATT.submittedCount += 1;
    ATT.names = [];
    ATT.expected = 0;
    return { ok: true, submittedCount: ATT.submittedCount };
  }
  function attachedInfo() { return { names: ATT.names, expected: ATT.expected, at: ATT.at, submittedCount: ATT.submittedCount }; }

  // ---------- 输入区锚点 ----------
  // 发送/停止/附件按钮都是「输入区容器」里的相对位置，先找锚点再找按钮。
  // 关键：生成中站点常把输入框设成 contenteditable="false"（只读/禁用），
  // 此时锚点绝不能消失 —— 否则停止按钮就无从定位。所以分档取：
  //   1) 可编辑（contenteditable=true）
  //   2) role=textbox / role=combobox
  //   3) 刚被设成 contenteditable=false 的输入框
  function visibleEditables(sel) {
    var out = [];
    try {
      var list = document.querySelectorAll(sel);
      for (var i = 0; i < list.length; i++) {
        var el = list[i];
        if (el.tagName === 'INPUT') continue;
        if (!visible(el)) continue;
        var r = el.getBoundingClientRect();
        if (r.width < 80 || r.height < 12) continue;
        out.push(el);
      }
    } catch (e) {}
    return out;
  }
  function editableNodes() {
    var tiers = [
      'textarea, [contenteditable="true"], [contenteditable=""]',
      '[role="textbox"], [role="combobox"]',
      '[contenteditable="false"]'
    ];
    for (var t = 0; t < tiers.length; t++) {
      var got = visibleEditables(tiers[t]);
      if (got.length) return got;
    }
    return [];
  }
  // 优先取页面最靠下的输入框（聊天输入区在底部），避免误取搜索框。
  function findComposer() {
    var list = editableNodes();
    if (!list.length) return null;
    list.sort(function (a, b) {
      var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (rb.top + rb.height) - (ra.top + ra.height);
    });
    return list[0];
  }
  function composeContainer(anchor, depth) {
    var n = anchor;
    for (var i = 0; i < depth && n && n.parentElement; i++) n = n.parentElement;
    return n || anchor;
  }
  // 输入卡片 = 「输入行」+ 它上面那条「附件条」。
  //
  // 为什么必须精确到这个范围：附件的文字条目和图片缩略图，在"还没发出去"和
  // "已经发出去的消息里"长得一模一样。用 body 或往上 6 层当范围，会把**已发出去的
  // 消息里的附件**也算进来 —— 实测后果：一条 50 个附件的消息发出去之后，程序数到
  // 37 个"残留附件"，误判成没发出去而中止整轮。
  //
  // 实测的真实层级（chat.deepseek.com，2026-09 版）：
  //   textarea
  //     └ div._24fad49
  //        └ div._020ab5b         ← 输入行：含 textarea + 工具条 + 发送键
  //           └ div._77cefa5 …    ← 输入卡片：**附件条是输入行的兄弟**，在这一层
  // 所以：先找"同时含输入框和发送键"的输入行，再看它的父层有没有附件标记，
  // 有就取父层（卡片），没有就还是输入行。
  function inputCard() {
    var anchor = findComposer();
    if (!anchor) return null;
    var row = anchor, i, hasSend = false;
    for (i = 0; i < 10 && row && row.parentElement; i++) {
      row = row.parentElement;
      try { hasSend = !!row.querySelector('[class*="ds-button--circle"]'); } catch (e) { hasSend = false; }
      if (hasSend) break;
    }
    if (!row) return composeContainer(anchor, 3);
    var p = row.parentElement;
    if (p) {
      var hasAttach = false;
      try {
        hasAttach = !!p.querySelector('img[src^="blob:"]') ||
                    /\.(md|txt|pdf|docx|xlsx|png|jpe?g|gif|webp)\b/i.test(ownText(p, 400));
      } catch (e) {}
      if (hasAttach) return p;
    }
    return row;
  }

  // ---------- 按钮候选收集 ----------
  var BTN_SEL = 'button,[role="button"],a[href],div[class*="button" i],div[class*="btn" i],span[class*="button" i],span[class*="btn" i],label,svg';
  function collectButtons(host) {
    var root = host || document.body || document.documentElement;
    var list = [];
    try { list = root.querySelectorAll(BTN_SEL); } catch (e) { return []; }
    var anchor = findComposer();
    var aRect = anchor ? anchor.getBoundingClientRect() : null;
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      if (el.tagName === 'SVG') {
        // svg 只在它自己没有可点父按钮时才作为候选，避免同一个按钮算两次
        var pb = el.closest ? el.closest('button,[role="button"],a[href],label') : null;
        if (pb) continue;
      }
      // ⚠️ 只保留"最外层"的候选。
      // 真实页面是 div[role=button] > div.ds-button__icon > div.ds-icon > svg，
      // 而 BTN_SEL 里 div[class*=button] 会把内层两个 div 也算成候选 ——
      // 上一版发送键就选中了内层 ds-button__icon（点它没有任何反应），
      // 表现就是"程序不会自己点发送"。
      // 只按"可点宿主"判断祖先，避免把 toolbar 这类容器也算进去而误杀所有按钮。
      //
      // ⚠️⚠️ 注意：本文件整体被包在一个模板字符串里（见文件开头的 return (() => { ），
      // 所以注释和字符串里都不能出现反引号或美元大括号，否则会把模板提前闭合、
      // 整个文件语法报错（这个坑刚踩过一次）。
      try {
        var clickableHost = el.parentElement && el.parentElement.closest
          ? el.parentElement.closest('button,[role="button"],a[href],label') : null;
        if (clickableHost) continue;
      } catch (e) {}
      if (!visible(el)) continue;
      var r = el.getBoundingClientRect();
      var label = '';
      try { label = norm(el.getAttribute('aria-label') || el.getAttribute('data-testid') || el.getAttribute('title') || ''); } catch (e) {}
      var t = norm(txt(el));
      var cls = clsOf(el);
      var dist = null;
      if (aRect && r.width) {
        var dx = Math.max(0, Math.max(aRect.left - r.right, r.left - aRect.right));
        var dy = Math.max(0, Math.max(aRect.top - r.bottom, r.top - aRect.bottom));
        dist = Math.round(Math.sqrt(dx * dx + dy * dy));
      }
      var hasRect = false, hasPath = false;
      // ⚠️ 必须查**后代**，不能用 :scope。
      // 真实页面的结构是 div[role=button] > div.ds-button__icon > div.ds-icon > svg > rect，
      // rect 从来不是按钮的直接子元素，用 :scope rect 永远查不到 ——
      // 结果"停止键=方块图标"这条判定在真实页面上从来没生效过（实测踩到）。
      try { hasRect = !!el.querySelector('rect'); } catch (e) { hasRect = false; }
      try { hasPath = !!el.querySelector('path'); } catch (e) { hasPath = false; }
      var disabled = false;
      try {
        disabled = el.hasAttribute('disabled') ||
                   el.getAttribute('aria-disabled') === 'true' ||
                   // 真实页面用 CSS 类表示禁用（ds-button--disabled），既没有 disabled
                   // 属性也没有 aria-disabled —— 只认属性会把禁用按钮当可用
                   /(^|[\s])(ds-button--disabled|is-disabled|disabled)([\s]|$)/.test(clsOf(el));
      } catch (e) {}
      var rec = {
        idx: tagCandidate(el),
        tag: el.tagName.toLowerCase(),
        role: (function () { try { return el.getAttribute('role') || ''; } catch (e) { return ''; } })(),
        label: label,
        text: t.slice(0, 60),
        class: cls.slice(0, 140),
        path: pathOf(el),
        rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
        hasRect: hasRect,
        hasPath: hasPath,
        disabled: disabled,
        distToComposer: dist
      };
      rec.__el = el;
      out.push(rec);
    }
    // 与输入框的邻近度是主排序键：聊天输入区那一排按钮永远离输入框最近。
    out.sort(function (a, b) {
      var da = a.distToComposer == null ? 1e9 : a.distToComposer;
      var db = b.distToComposer == null ? 1e9 : b.distToComposer;
      return da - db;
    });
    return out;
  }
  function publicCand(c) {
    return {
      idx: c.idx, tag: c.tag, label: c.label, text: c.text, class: c.class, path: c.path,
      rect: c.rect, hasRect: c.hasRect, hasPath: c.hasPath, disabled: c.disabled,
      distToComposer: c.distToComposer
    };
  }
  function publicCands(list, n) {
    var out = [];
    var k = Math.min(list.length, n || 12);
    for (var i = 0; i < k; i++) out.push(publicCand(list[i]));
    return out;
  }
  function findByIdx(idx) {
    try { return document.querySelector('[data-dsh-idx="' + String(idx) + '"]'); } catch (e) { return null; }
  }
  // 取按钮第一个 svg 的 path 的 d 属性（把数字/空格归一化，只留下命令字母），便于判断图标形状
  function pathCmds(el) {
    try {
      var p = el.matches && el.matches('svg') ? el.querySelector('path') : el.querySelector('svg path');
      if (!p) return '';
      return String(p.getAttribute('d') || '').replace(/[0-9.,\-\s]+/g, '');
    } catch (e) { return ''; }
  }
  function pathCount(el) {
    try { return el.querySelectorAll('svg path').length; } catch (e) { return 0; }
  }
  // 发送图标特征：纸飞机/箭头 —— 多条 path，或单条 path 但只有直线/水平竖直命令且命令序列短
  function looksLikeSendIcon(el) {
    var cmds = pathCmds(el);
    if (!cmds) return 0;
    var n = pathCount(el);
    var score = 0;
    if (n >= 2) score += 45;                                  // 纸飞机常由 2 条 path 组成（机身 + 折线）
    if (/^[MLlHhVvZz]+$/.test(cmds) && cmds.length <= 40) score += 20; // 纯直线短路径 = 箭头/纸飞机
    if (/^M?[LlHhVv]/.test(cmds) && cmds.length <= 24) score += 15;
    return score;
  }
  // 附件（回形针）图标特征：单条 path、命令序列很长、含曲线命令 a/A/c/C
  function attachIconPenalty(el) {
    var cmds = pathCmds(el);
    if (!cmds) return 0;
    var n = pathCount(el);
    var penalty = 0;
    if (n === 1 && cmds.length > 60) penalty += 35;
    if (/[aA]/.test(cmds)) penalty += 25;   // 回形针的圆环用弧线命令
    if (/[cC]/.test(cmds) && cmds.length > 60) penalty += 15;
    return penalty;
  }

  // ---------- 文件输入 ----------
  function findFileInput() {
    try {
      var el = document.querySelector('input[type=file]');
      return el || null;
    } catch (e) { return null; }
  }

  // ---------- 附件按钮 ----------
  var ATTACH_STRONG = /上传|附件|添加附件|添加文件|选择文件|文件上传|upload|attach|clip|paperclip|file[-_ ]?input/i;
  var ATTACH_WEAK = /文件|图片|添加|回形针|\+/;

  // ══ 真实页面的结构指纹（实测 chat.deepseek.com，登录后）══
  //
  // 真实页面**一个 aria-label 都没有**，所有按钮都是
  //   <div role="button" class="ds-button ds-button--..."> ，
  // 所以只能靠 DeepSeek 自己的类名 + 位置来判断：
  //
  //   发送键： ds-button--primary + ds-button--filled + ds-button--circle（圆形实心，最右）
  //   附件键： ds-button--icon + ds-button--capsule（和顶部工具栏同类名！
  //            所以必须再加"在输入区那一排、且在最右边那个按钮左边"）
  //   停止键： 生成中会占据发送键同一个位置，图标是方块（<rect>）
  //
  // 下面这些判断就是为此加的；旧图标形状启发式保留作兜底。
  var COMPOSER_ROW_PX = 150;    // 离输入框多近算"输入区那一排"
  function clsHas(c, s) {
    return !!c && typeof c.class === 'string' && c.class.indexOf(s) >= 0;
  }
  function inComposerRow(c) {
    var d = c.distToComposer == null ? 1e9 : c.distToComposer;
    return d <= COMPOSER_ROW_PX;
  }
  function isSendSlot(c) {
    if (!c) return false;
    return clsHas(c, 'ds-button--circle') ||
           (clsHas(c, 'ds-button--primary') && clsHas(c, 'ds-button--filled'));
  }
  function isIconBtn(c) {
    return !!c && clsHas(c, 'ds-button--icon') && !isSendSlot(c);
  }
  function rightmost(list) {
    return list.slice().sort(function (a, b) { return b.rect.x - a.rect.x; })[0];
  }
  // 发送槽位上的图标是「纸飞机」还是「停止方块」？
  //
  // 实测真实页面（生成中抓的快照）：
  //   停止： M2 4.88C2 3.68...H11.12C12.31...  —— 圆角方块，命令全是 C/H，**没有直线 L**
  //   发送： M8.3125 0.980C8.667...L14.707 6.8347L13.293 8.24876 —— 纸飞机，**含 L**
  // 站点并不用 <rect> 画停止图标，所以"含 rect=停止"那条判定在真实页面上永远不成立。
  function pathHasLine(c) {
    var cmds = '';
    try { cmds = pathCmds(c && c.__el); } catch (e) { cmds = ''; }
    if (!cmds) return null;                  // 没有 path 可判断
    return /[Ll]/.test(cmds);
  }
  function looksLikeStopIcon(c) {
    var hasLine = pathHasLine(c);
    if (hasLine === null) return false;
    return hasLine === false;                // 全曲线 → 方块 → 停止
  }
  function pickAttach() {
    // ⚠️ 搜索根必须是**输入区容器**，不能是输入框本身。
    // 输入框是 textarea（没有子元素），拿它当根 querySelectorAll 永远返回 0 个候选 ——
    // 这个 bug 让下面所有规则都建立在空列表上（真实页面实测 nCands=0）。
    var anchor = findComposer();
    var scope = anchor ? composeContainer(anchor, 6) : null;
    var cands = collectButtons(scope || document.body);
    // ① 结构优先：输入区那一排、最右（发送键）左边的那个 ds-button--icon 胶囊键
    var icons = [];
    var iconsDisabled = [];
    for (var k = 0; k < cands.length; k++) {
      var ic = cands[k];
      if (!isIconBtn(ic)) continue;
      if (!inComposerRow(ic)) continue;
      if (ic.hasRect) continue;
      (ic.disabled ? iconsDisabled : icons).push(ic);
    }
    if (icons.length || iconsDisabled.length) {
      var pool = icons.length ? icons : iconsDisabled;
      var pickIc = rightmost(pool);
      log('pickAttach', { how: 'ds-icon-in-row', idx: pickIc.idx, class: pickIc.class,
                          n: pool.length, disabled: !!pickIc.disabled });
      return { ok: true, how: 'ds-icon-in-row', cand: pickIc };
    }
    var strong = [];
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i];
      if (c.disabled) continue;
      var hay = c.label + ' | ' + c.text + ' | ' + c.class;
      if (ATTACH_STRONG.test(hay)) strong.push(c);
    }
    if (strong.length) {
      // 标签命中多个时取离输入框最近的
      log('pickAttach', { how: 'label', idx: strong[0].idx, label: strong[0].label, class: strong[0].class });
      return { ok: true, how: 'label', cand: strong[0] };
    }
    // 退化：输入区容器里，含 path 但明显不是纸飞机的 svg 按钮中取最靠左的（附件通常在最左）
    var loose = [];
    for (var j = 0; j < cands.length; j++) {
      var d = cands[j];
      if (d.disabled) continue;
      if (d.label) continue;
      if (!/svg/i.test(d.tag) && !d.hasPath) continue;
      if ((d.rect.w || 0) > 60 || (d.rect.h || 0) > 60) continue;
      if ((d.distToComposer == null ? 1e9 : d.distToComposer) > 240) continue;
      loose.push(d);
    }
    if (loose.length) {
      loose.sort(function (a, b) { return a.rect.x - b.rect.x; });
      log('pickAttach', { how: 'loose-svg-leftmost', idx: loose[0].idx, class: loose[0].class, n: loose.length });
      return { ok: true, how: 'loose-svg-leftmost', cand: loose[0] };
    }
    log('pickAttach', { how: 'none', candidates: publicCands(cands, 8) });
    return { ok: false, how: 'none', reason: '找不到任何疑似「附件/上传」按钮', candidates: publicCands(cands, 12) };
  }

  // ---------- 发送按钮 ----------
  // 难点：发送键和附件键都没有 aria-label，都是「path 图标 + 32x32 按钮」，
  // 所以必须靠图标形状区分（纸飞机/箭头 vs 回形针），再加「离输入框的距离」定位。
  function pickDebug() {
    // 排查用：把「发送/停止键候选为什么没被选中」的每个条件摊开。
    // 真实页面没有 aria-label，选择器坏了只能靠这个定位。
    // ⚠️ 搜索根必须是输入区容器（和 pickSend/pickAttach 一致）；用 textarea 当根
    //    返回的候选恒为 0，会把人带进沟里（这个坑踩过）。
    var anchor = findComposer();
    var scope = anchor ? composeContainer(anchor, 6) : null;
    var cands = collectButtons(scope || document.body);
    var slots = [];
    for (var i = 0; i < cands.length; i++) {
      if (!isSendSlot(cands[i])) continue;
      slots.push({
        idx: cands[i].idx,
        cls: String(cands[i].class).slice(0, 60),
        dist: cands[i].distToComposer,
        disabled: cands[i].disabled,
        hasRect: cands[i].hasRect,
        inRow: inComposerRow(cands[i]),
        cmds: String(pathCmds(cands[i].__el) || '').slice(0, 24),
        hasLine: pathHasLine(cands[i]),
        stopIcon: looksLikeStopIcon(cands[i])
      });
    }
    var icons = [];
    for (var j = 0; j < cands.length; j++) {
      if (clsHas(cands[j], 'ds-button--icon') && !isSendSlot(cands[j])) {
        icons.push({ idx: cands[j].idx, dist: cands[j].distToComposer,
                     inRow: inComposerRow(cands[j]),
                     cls: String(cands[j].class).slice(0, 50) });
      }
    }
    var st = {};
    try { st = getState(); } catch (e) {}
    var hintText = '';
    var stopWhy = '';
    try {
      var ss = streamingState();
      hintText = ss.textHint || '';
      stopWhy = ss.stopReason || '';
    } catch (e) {}
    return { ok: true, anchorFound: !!anchor, nCands: cands.length,
             compRowPx: COMPOSER_ROW_PX, slots: slots, iconBtns: icons.slice(0, 6),
             pickSendHow: pickSend().how, pickAttachHow: pickAttach().how,
             pickStopHow: pickStop().how,
             streaming: st.streaming, textHint: st.textHint, hintText: hintText,
             stopReason: stopWhy,
             stopCandidate: st.stopCandidate, attachments: st.attachments };
  }
  function pickSend() {
    // ⚠️ 同上：搜索根必须是输入区容器，不能是 textarea 本身（否则候选恒为 0）。
    var anchor = findComposer();
    var scope = anchor ? composeContainer(anchor, 6) : null;
    var cands = collectButtons(scope || document.body);
    var i, c;
    // ① 结构优先（真实页面走这条）：输入区那一排最右边的圆形主按钮。
    //    发送中它会变成"停止键"（图标是方块），那种不选。
    //    注意：输入框为空时这个按钮带 ds-button--disabled（禁用态），挂上附件后
    //    才变可用；禁用态也留着当兜底候选，否则挂载过程中会一时找不到它。
    var slot = [];
    var slotDisabled = [];
    for (i = 0; i < cands.length; i++) {
      c = cands[i];
      if (!isSendSlot(c)) continue;
      if (!inComposerRow(c)) continue;
      if (c.hasRect) continue;            // 方块 = 停止键
      if (looksLikeStopIcon(c)) continue; // 图标全曲线（圆角方块）= 正在生成，别当发送键
      (c.disabled ? slotDisabled : slot).push(c);
    }
    if (slot.length) {
      var pickSlot = rightmost(slot);
      log('pickSend', { how: 'ds-circle-rightmost', idx: pickSlot.idx, class: pickSlot.class, n: slot.length });
      return { ok: true, how: 'ds-circle-rightmost', cand: pickSlot };
    }
    if (slotDisabled.length) {
      var pickDis = rightmost(slotDisabled);
      log('pickSend', { how: 'ds-circle-disabled', idx: pickDis.idx, class: pickDis.class });
      return { ok: true, how: 'ds-circle-disabled', cand: pickDis };
    }
    var hi = [];
    for (i = 0; i < cands.length; i++) {
      c = cands[i];
      if (c.disabled) continue;
      if (!isSendLabel(c)) continue;
      if (scope && scope.contains && scope.contains(c.__el)) c.__inScope = true;
      hi.push(c);
    }
    // 标签命中的同时也在输入区容器内 -> 最高优先级；其次离输入框最近的
    if (hi.length) {
      hi.sort(function (a, b) {
        if (!!b.__inScope !== !!a.__inScope) return (b.__inScope ? 1 : 0) - (a.__inScope ? 1 : 0);
        return (a.distToComposer == null ? 1e9 : a.distToComposer) - (b.distToComposer == null ? 1e9 : b.distToComposer);
      });
      log('pickSend', { how: 'label', idx: hi[0].idx, label: hi[0].label, text: hi[0].text });
      return { ok: true, how: 'label', cand: hi[0] };
    }
    // 无任何标签：在输入区附近按「图标形状 + 距离」打分
    var scopeCands = scope ? collectButtons(scope) : cands;
    var pool = [];
    var seen = {};
    var j;
    for (j = 0; j < scopeCands.length; j++) { if (!seen[scopeCands[j].idx]) { seen[scopeCands[j].idx] = 1; pool.push(scopeCands[j]); } }
    for (j = 0; j < cands.length; j++) { if (!seen[cands[j].idx]) { seen[cands[j].idx] = 1; pool.push(cands[j]); } }
    var scored = [];
    for (j = 0; j < pool.length; j++) {
      var d = pool[j];
      if (d.disabled) continue;
      if (isStopLabel(d)) continue;          // 停止键绝不当作发送键
      if (isAttachLabel(d)) continue;        // 明确是附件键的排除
      if (d.hasRect) continue;               // 含 <rect>（方块）的是停止类图标
      var dist = d.distToComposer == null ? 1e9 : d.distToComposer;
      if (dist > 420) continue;              // 离输入区太远，不是输入区工具条上的键
      var score = 0;
      var shape = looksLikeSendIcon(d.__el);
      score += shape;
      score -= attachIconPenalty(d.__el);     // 回形针形状：扣分
      if (d.hasPath) score += 15;
      if (d.tag === 'button' || d.tag === 'a' || d.tag === 'label') score += 20; // 优先真正的按钮，而不是它里面的 svg
      if (dist <= 60) score += 60;           // 真的在输入框那一排
      else if (dist <= 160) score += 40;
      else if (dist <= 260) score += 20;
      if (d.rect && d.rect.w <= 64 && d.rect.h <= 64) score += 10;
      score -= Math.min(dist, 420) / 20;
      if (score > 55) scored.push({ c: d, score: score, shape: shape });
    }
    if (scored.length) {
      scored.sort(function (a, b) {
        if (Math.abs(b.score - a.score) > 3) return b.score - a.score;
        // 分数接近时取更靠下的（发送键通常在工具条右侧/输入框正下方）
        if (b.c.rect.y !== a.c.rect.y) return b.c.rect.y - a.c.rect.y;
        return b.c.rect.x - a.c.rect.x;
      });
      log('pickSend', { how: 'heuristic-icon', idx: scored[0].c.idx, score: Math.round(scored[0].score), n: scored.length });
      return { ok: true, how: 'heuristic-icon', cand: scored[0].c, score: Math.round(scored[0].score) };
    }
    log('pickSend', { how: 'none', candidates: publicCands(cands, 12) });
    return {
      ok: false, how: 'none',
      reason: '找不到疑似发送按钮（无 aria-label/文本，且输入区附近没有像纸飞机的图标按钮）',
      candidates: publicCands(cands, 12)
    };
  }
  // 语义匹配只看 aria-label / 可见文本 —— 绝不能把 DOM 路径或 class 拼进来，
  // 否则 button#stopBtn 这种 id 会被当成「停止」标签，隐藏的按钮也会被误判成可见的停止键。
  function semOf(c) { return (c.label || '') + ' ' + (c.text || ''); }
  function isSendLabel(c) {
    return /发送|送信|submit|send/i.test(semOf(c));
  }
  function isStopLabel(c) {
    return /停止|停止生成|中止|stop|halt|abort/i.test(semOf(c));
  }
  function isAttachLabel(c) {
    if (!c.label && !c.text) return false;
    return /上传|附件|添加文件|选择文件|upload|attach|clip/i.test(semOf(c));
  }
  // class 里带 stop 味（不含 path/id）
  function hasStopClass(c) { return /stop|halt|interrupt|中止|停止/i.test(c.class || ''); }
  // 页面上的「正在生成」提示：只认同一个小元素里的短提示语，避免把历史消息里的
  // 「已停止生成」当成正在生成。
  var STREAM_ACTIVE = /^(正在生成|停止生成|正在思考|思考中|生成中|深度思考中|正在输出|Stop generating|Generating)/i;
  var STREAM_DONE = /已|完成|结束|interrupted|stopped|finished/i;   // 「已停止生成」这类是历史记录
  function activeStreamHint() {
    try {
      var all = document.querySelectorAll('body *');
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (el.children && el.children.length > 0) continue;
        if (!visible(el)) continue;
        var t = norm(txt(el));
        if (!t || t.length > 24) continue;
        if (STREAM_DONE.test(t)) continue;
        if (STREAM_ACTIVE.test(t)) return t;
      }
    } catch (e) {}
    return null;
  }

  // ---------- 停止按钮（最难的一点） ----------
  // 线上实测经验：停止键的图标是方块(<rect>)，发送键是纸飞机(<path>)。
  // 顺序：标签 -> 页面上出现附件名/停止提示（生成中的强证据）时认「附近唯一的方块图标」
  //      -> 输入区容器内含 <rect> 的 svg 按钮 -> class 含 stop 的按钮 -> Escape（主进程兜底）
  function pickStop() {
    var anchor = findComposer();
    var scoped = [];
    var scope = anchor ? composeContainer(anchor, 6) : null;
    if (scope) {
      try { scoped = collectButtons(scope); } catch (e) { scoped = []; }
    }
    var cands = collectButtons(document.body);
    var pool = [];
    var seen = {};
    var i;
    for (i = 0; i < scoped.length; i++) { if (!seen[scoped[i].idx]) { seen[scoped[i].idx] = 1; pool.push(scoped[i]); } }
    for (i = 0; i < cands.length; i++) { if (!seen[cands[i].idx]) { seen[cands[i].idx] = 1; pool.push(cands[i]); } }

    var byLabel = [];
    for (i = 0; i < pool.length; i++) {
      if (!pool[i].disabled && isStopLabel(pool[i])) byLabel.push(pool[i]);
    }
    if (byLabel.length) {
      log('pickStop', { how: 'label', idx: byLabel[0].idx, label: byLabel[0].label });
      return { ok: true, how: 'label', cand: byLabel[0] };
    }
    // ① 结构优先（真实页面）：生成中，发送键那个"圆形主按钮"的槽位里换成了停止图标
    //    （圆角方块：命令全是 C/H，没有直线 L；站点不用 <rect>）
    var inSlot = [];
    for (i = 0; i < pool.length; i++) {
      var s = pool[i];
      if (!isSendSlot(s)) continue;
      if (!inComposerRow(s)) continue;
      if (!s.hasRect && !looksLikeStopIcon(s)) continue;
      inSlot.push(s);
    }
    if (inSlot.length) {
      var pickSlot = rightmost(inSlot);
      log('pickStop', { how: 'ds-stop-slot', idx: pickSlot.idx, class: pickSlot.class, n: inSlot.length });
      return { ok: true, how: 'ds-stop-slot', cand: pickSlot };
    }
    // 生成中的证据：
    //   1) 页面上出现「停止生成/正在生成」类短提示
    //   2) 刚登记过的附件已从页面消失 —— 消息已提交
    //   3) 输入框被站点设成只读，且【本次会话确实发过消息】—— 典型的生成中锁输入
    //      第 3 条必须加「本次会话发过」这个门控：否则只读/分享会话下输入框长期禁用，
    //      会被误判成一直在生成。
    var box = countAttachments([]);
    var a = findComposer();
    var ce = null;
    try { ce = a ? a.getAttribute('contenteditable') : null; } catch (e) {}
    var readonlyComposer = !!(a && a.tagName !== 'TEXTAREA' && a.tagName !== 'INPUT' && ce === 'false');
    var submittedAny = ATT.submittedCount > 0;
    var submitted = ATT.expected > 0 && box.count < ATT.expected;
    var hintText = activeStreamHint();
    var textHint = !!hintText;
    var streamingHint = textHint || submitted || (readonlyComposer && submittedAny);
    if (streamingHint) {
      var rects = [];
      for (i = 0; i < pool.length; i++) {
        var c = pool[i];
        if (c.disabled || !c.hasRect) continue;
        // 「深度思考 / 智能搜索」这类功能开关的图标里也有 <rect>（Lottie 动画），
        // 绝不能当成停止键 —— 挂上图片时它曾导致 streaming 误报 true。
        if (clsHas(c, 'ds-toggle-button') || clsHas(c, 'toggle-button')) continue;
        var dist = c.distToComposer == null ? 1e9 : c.distToComposer;
        if (dist > 260) continue;
        rects.push(c);
      }
      if (rects.length) {
        rects.sort(function (a, b) {
          var da = a.distToComposer == null ? 1e9 : a.distToComposer;
          var db = b.distToComposer == null ? 1e9 : b.distToComposer;
          if (Math.abs(da - db) > 3) return da - db;
          // 距离接近时优先真正的按钮，而不是它里面的 svg
          var ab = (a.tag === 'button' || a.tag === 'a' || a.tag === 'label') ? 0 : 1;
          var bb = (b.tag === 'button' || b.tag === 'a' || b.tag === 'label') ? 0 : 1;
          return ab - bb;
        });
        log('pickStop', { how: 'svg-rect', idx: rects[0].idx, class: rects[0].class, n: rects.length });
        return { ok: true, how: 'svg-rect', cand: rects[0] };
      }
    }
    // 没有强证据时，只有 label/class 明确带 stop 味的 <rect> 才算数
    var byRect = [];
    for (i = 0; i < pool.length; i++) {
      var d = pool[i];
      if (d.disabled || !d.hasRect) continue;
      if (!hasStopClass(d) && !/stop|halt|中止|停止/i.test(d.label)) continue;
      if ((d.distToComposer == null ? 1e9 : d.distToComposer) > 260) continue;
      byRect.push(d);
    }
    if (byRect.length) {
      log('pickStop', { how: 'svg-rect-labeled', idx: byRect[0].idx, class: byRect[0].class });
      return { ok: true, how: 'svg-rect-labeled', cand: byRect[0] };
    }
    var byClass = [];
    for (i = 0; i < pool.length; i++) {
      var e = pool[i];
      if (e.disabled) continue;
      if (!hasStopClass(e)) continue;
      byClass.push(e);
    }
    if (byClass.length) {
      log('pickStop', { how: 'class', idx: byClass[0].idx, class: byClass[0].class });
      return { ok: true, how: 'class', cand: byClass[0] };
    }
    log('pickStop', { how: 'none', scoped: publicCands(scoped, 10), near: publicCands(cands, 10) });
    return {
      ok: false, how: 'none', reason: '找不到疑似停止按钮',
      candidates: { inScope: publicCands(scoped, 10), byProximity: publicCands(cands, 12) }
    };
  }

  // ---------- 附件计数 ----------
  var FILE_EXT = /\.(txt|md|markdown|csv|json|jsonl|xml|html|htm|log|py|js|ts|tsx|jsx|c|cpp|h|hpp|java|go|rs|rb|php|sql|yaml|yml|ini|toml|conf|sh|bat|ps1|pdf|doc|docx|xls|xlsx|ppt|pptx|png|jpg|jpeg|gif|webp|bmp|svg|zip|rar|7z|tar|gz|mp3|mp4|wav|m4a|eml|msg)$/i;
  var ATTACH_HOST_SEL = '[data-ds-attachment],[class*="file-item" i],[class*="fileItem" i],[class*="file_item" i],[class*="attach" i],[class*="upload" i],[class*="file-card" i],[class*="fileCard" i]';
  function ownText(el, maxLen) {
    // 只统计元素自己的直接文本，避免把父容器的整段文字算进来
    var out = '';
    try {
      var kids = el.childNodes;
      for (var i = 0; i < kids.length; i++) {
        if (kids[i].nodeType === 3) out += kids[i].nodeValue || '';
      }
    } catch (e) {}
    out = norm(out);
    if (!out) out = norm(txt(el)).slice(0, maxLen || 140);
    return out;
  }
  function countAttachments(filenames) {
    var names = (filenames && filenames.length) ? filenames : ATT.names;
    var want = {};
    var i;
    for (i = 0; i < (names || []).length; i++) want[String(names[i]).toLowerCase()] = 1;
    var matched = {};
    // ⚠️ 一切计数都只在**输入卡片**范围内做（见 inputCard 的说明）：
    // 已经发出去的消息里，附件卡片和图片缩略图与输入区里长得一样，
    // 扫整页会把它们算成"还留在输入区的附件"。
    var card = inputCard();
    var root = card || document.body;
    var fileNameHits = 0;
    if (names && names.length) {
      try {
        var all = root.querySelectorAll('*');
        for (i = 0; i < all.length; i++) {
          var el = all[i];
          if (el.children && el.children.length > 0) continue; // 只看叶子元素，避免重复计数
          var t = norm(txt(el));
          if (!t || t.length > 200) continue;
          var low = t.toLowerCase();
          for (var key in want) {
            if (!Object.prototype.hasOwnProperty.call(want, key)) continue;
            if (matched[key]) continue;
            if (low.indexOf(key) < 0) continue;
            matched[key] = 1;
            fileNameHits++;
          }
        }
      } catch (e) {}
    }
    // 行启发式：页面只画图标不显示文件名时的兜底
    var rowHits = 0;
    var rows = [];
    try {
      var hosts = root.querySelectorAll(ATTACH_HOST_SEL);
      for (i = 0; i < hosts.length; i++) {
        var h = hosts[i];
        var hasNested = false;
        try { hasNested = h.querySelector(ATTACH_HOST_SEL) !== null; } catch (e) { hasNested = false; }
        if (hasNested) continue;
        if (!visible(h)) continue;
        var own = ownText(h, 140);
        if (!own) continue;
        if (/正在上传|上传中|uploading/i.test(own)) continue;
        var clsLooksFile = /上传|附件|文件|attach|upload|file/i.test(clsOf(h));
        var textLooksName = FILE_EXT.test(own) || ((names || []).length > 0 && tooManyTextHits(own));
        if (!clsLooksFile && !textLooksName) continue;
        rowHits++;
        if (rows.length < 12) rows.push({ class: clsOf(h).slice(0, 90), text: own.slice(0, 70), visible: true });
      }
    } catch (e) {}
    // 图片附件在页面上是 **blob: 缩略图，没有任何文件名文本** —— 必须单独数。
    // 不数它的后果（实测踩到）：挂了图片被判成"一个都没挂上"，而且会连带把
    // "附件消失 = 消息已提交" 那条启发式触发，让 streaming 误报 true。
    var imageHits = 0;
    var uploading = 0;
    try {
      var imgs = root.querySelectorAll('img');
      var seenSrc = {};
      for (i = 0; i < imgs.length; i++) {
        var im = imgs[i];
        var src = String(im.currentSrc || im.src || '');
        if (src.indexOf('blob:') !== 0) continue;
        if (seenSrc[src]) continue;
        var ir = im.getBoundingClientRect();
        if (ir.width < 12 || ir.height < 12) continue;
        seenSrc[src] = 1;
        imageHits++;
      }
      // "还在上传"的标记：附件条上出现进度/上传中文案时，说明文件还没传完，
      // 这时候点发送会发出去一条不完整的消息（用户明确提到过这个现象）。
      var marks = root.querySelectorAll('[class*="progress" i],[role="progressbar"],[class*="uploading" i]');
      for (i = 0; i < marks.length; i++) {
        if (visible(marks[i])) uploading++;
      }
      if (/正在上传|上传中|uploading/i.test(ownText(root, 400))) uploading++;
    } catch (e) {}
    // 两种口径取大值：文件名命中更准，行数启发式兜底（页面只显示图标时用得上）
    var count = Math.max(fileNameHits, rowHits) + imageHits;
    return { count: count, fileNameHits: fileNameHits, rowHits: rowHits,
             imageHits: imageHits, uploading: uploading,
             scoped: !!card, expected: ATT.expected, rows: rows };
  }
  function tooManyTextHits(t) {
    // 文本里像是「文件名」的短串（带扩展名、或以点号分隔的短串）
    if (FILE_EXT.test(t)) return true;
    return /^[\w\u4e00-\u9fa5 .\-()（）]{1,80}\.[A-Za-z0-9]{1,6}$/.test(t);
  }


  // ---------- 页面状态 ----------
  function pageText() {
    try { return norm((document.body && document.body.innerText) || ''); } catch (e) { return ''; }
  }
  function loginState() {
    var href = location.href || '';
    if (/sign_in|sign-in|login/i.test(href)) return false;
    var t = pageText();
    if (t.length < 5) return null; // 页面没渲染完，判断不出来
    if (/登录|注册|发送验证码|密码登录|扫码登录/.test(t) && !findComposer()) return false;
    if (/退出登录|新建对话|新对话|开启新对话|我的对话|历史对话|深度思考|联网搜索/.test(t)) return true;
    if (findComposer() || findFileInput()) return true;
    return null;
  }
  function streamingState() {
    var stop = pickStop();
    var hintText = activeStreamHint();
    return {
      streaming: !!(stop.ok || hintText),
      stopCandidate: stop.ok ? { idx: stop.cand.idx, how: stop.how, label: stop.cand.label, class: stop.cand.class, hasRect: stop.cand.hasRect } : null,
      textHint: hintText || null,
      stopReason: stop.ok ? undefined : stop.reason
    };
  }
  // 提交后附件实体已消失（生成本身可能还没开始）—— 用于给「已发送」补一个确认信号
  function attachmentsCleared() {
    var box = countAttachments([]);
    return { ok: true, cleared: ATT.expected > 0 && box.count === 0, count: box.count, expected: ATT.expected };
  }
  function getState() {
    var fi = findFileInput();
    var att = countAttachments(null);
    // 附件数：用 countAttachments 的合并口径（文件名命中 / 行启发式 / blob 图片缩略图）。
    // 不要再从 fileNameHits 单独推 —— 那样"只挂了图片"会被算成 0 个。
    var count = att.count;
    if (att.expected > 0 && count > att.expected) count = att.expected;
    var login = null;
    try { login = loginState(); } catch (e) {}
    var st = { streaming: false, stopCandidate: null };
    try { st = streamingState(); } catch (e) {}
    var hasComposer = !!findComposer();
    return {
      ok: true,
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      loggedIn: login,
      streaming: !!st.streaming,
      attachments: count,
      fileInput: !!fi,
      fileInputInfo: fi ? { accept: fi.getAttribute('accept'), multiple: !!fi.multiple, name: fi.getAttribute('name'), class: clsOf(fi).slice(0, 90), path: pathOf(fi) } : null,
      hasComposer: hasComposer,
      stopCandidate: st.stopCandidate || null,
      textHint: !!st.textHint,
      attachCount: { count: count, fileNameHits: att.fileNameHits, rowHits: att.rowHits, expected: att.expected },
      attachedNames: ATT.names.length
    };
  }

  // ---------- 动作 ----------
  function attachInfo() {
    var fi = findFileInput();
    var out = {
      ok: !!fi,
      hasFileInput: !!fi,
      fileInputInfo: fi ? { accept: fi.getAttribute('accept'), multiple: !!fi.multiple, path: pathOf(fi) } : null,
      pickerOpen: false,
      picker: null
    };
    if (fi) { try { scrollTo(fi); } catch (e) {} return out; }
    var pick = pickAttach();
    out.picker = { how: pick.how, reason: pick.reason || null, candidates: pick.candidates || null };
    if (!pick.ok) return out;
    try { scrollTo(pick.cand.__el); } catch (e) {}
    out.pickerOpen = clickable(pick.cand.__el);
    out.pickerIdx = pick.cand.idx;
    out.pickerLabel = pick.cand.label;
    out.pickerClass = pick.cand.class;
    return out;
  }
  function pickFileInputAfterClick() {
    var fi = findFileInput();
    if (fi) return { ok: true, fileInputInfo: { accept: fi.getAttribute('accept'), multiple: !!fi.multiple, path: pathOf(fi) } };
    return { ok: false, reason: '点击附件按钮后页面上仍没有 input[type=file]' };
  }
  function countWithNames(names) {
    var box = countAttachments(names || []);
    var st = getState();
    return { count: box.count, fileNameHits: box.fileNameHits, rowHits: box.rowHits,
             imageHits: box.imageHits, uploading: box.uploading, scoped: box.scoped,
             streaming: st.streaming, samples: box.rows };
  }
  // 发送键当前"能不能发"——挂完附件后站点要处理/上传一会儿，这期间键是禁用态。
  // 用户实测：必须等文件加载完才点得动。主进程据此轮询等待，而不是瞎点。
  function sendReadyInfo() {
    var p = pickSend();
    return {
      ok: !!p.ok,
      how: p.how,
      disabled: p.cand ? !!p.cand.disabled : null,
      class: p.cand ? String(p.cand.class).slice(0, 80) : null,
      reason: p.reason || ''
    };
  }
  function clickSend() {
    var p = pickSend();
    if (!p.ok) return { ok: false, how: 'failed', reason: p.reason, candidates: p.candidates };
    // ⚠️ 发送键是**禁用态**时不能点：挂了附件之后站点还要处理/上传一会儿，
    // 这期间按钮带 ds-button--disabled，点了等于没点 —— 实测表现为"程序没发出去"。
    // 这里如实返回 disabled，让主进程去等（而不是假装点过了）。
    if (p.cand && p.cand.disabled) {
      log('clickSend', { how: 'disabled', cls: p.cand.class });
      return { ok: false, how: 'disabled', reason: '发送键还是禁用状态（附件还在处理）',
               class: p.cand.class, idx: p.cand.idx };
    }
    var el = findByIdx(p.cand.idx) || p.cand.__el;
    try { scrollTo(el); } catch (e) {}
    var clicked = clickable(el);
    log('clickSend', { how: p.how, idx: p.cand.idx, clicked: clicked, class: p.cand.class });
    return { ok: clicked, how: p.how, idx: p.cand.idx, class: p.cand.class, label: p.cand.label, rect: p.cand.rect, clicked: clicked };
  }
  function clickStop() {
    var p = pickStop();
    if (!p.ok) return { ok: false, how: 'none', reason: p.reason, candidates: p.candidates };
    var el = findByIdx(p.cand.idx) || p.cand.__el;
    try { scrollTo(el); } catch (e) {}
    var clicked = clickable(el);
    log('clickStop', { how: p.how, idx: p.cand.idx, clicked: clicked, class: p.cand.class });
    return { ok: clicked, how: p.how, idx: p.cand.idx, class: p.cand.class, label: p.cand.label, rect: p.cand.rect, clicked: clicked };
  }
  function focusComposer() {
    var a = findComposer();
    if (!a) return { ok: false, reason: '找不到输入框（textarea / contenteditable）' };
    try { a.focus(); } catch (e) {}
    try {
      if (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT') {
        var n = a.value == null ? 0 : a.value.length;
        try { a.setSelectionRange(n, n); } catch (e) {}
      } else {
        var sel = window.getSelection();
        var rg = document.createRange();
        rg.selectNodeContents(a);
        rg.collapse(false);
        sel.removeAllRanges();
        sel.addRange(rg);
      }
    } catch (e) {}
    return { ok: true, hasFocus: document.hasFocus(), active: (document.activeElement && document.activeElement.tagName) || null, tag: a.tagName.toLowerCase() };
  }
  function composerEmpty() {
    var a = findComposer();
    if (!a) return { ok: false, empty: null, reason: '找不到输入框' };
    // 生成中输入框会被设成只读，此时「空」不代表消息发出去过，不能当发送成功的证据
    var ce = null;
    try { ce = a.getAttribute('contenteditable'); } catch (e) {}
    if (a.tagName !== 'TEXTAREA' && a.tagName !== 'INPUT' && ce === 'false') {
      return { ok: false, empty: null, editable: false, reason: '输入框当前只读（生成中），不能据此判断已发送' };
    }
    var v = a.tagName === 'TEXTAREA' || a.tagName === 'INPUT' ? (a.value || '') : norm(txt(a));
    return { ok: true, empty: norm(v).length === 0, len: norm(v).length, editable: true };
  }

  // ---------- 诊断快照 ----------
  function snapRect(el) { return el ? rectOf(el) : null; }
  function diag() {
    var fi = findFileInput();
    var anchor = findComposer();
    var all = collectButtons(document.body);
    var scope = anchor ? composeContainer(anchor, 6) : null;
    var scoped = scope ? collectButtons(scope) : [];
    var stop = pickStop();
    var send = pickSend();
    var att = pickAttach();
    var box = countAttachments(null);
    var fullText = pageText();
    return {
      at: new Date().toISOString(),
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      ua: navigator.userAgent,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      envWarn: /使用环境异常/.test(fullText),
      pageTextHead: fullText.slice(0, 600),
      fileInput: fi ? {
        accept: fi.getAttribute('accept'), multiple: !!fi.multiple, name: fi.getAttribute('name'),
        class: clsOf(fi).slice(0, 120), path: pathOf(fi), rect: snapRect(fi), visible: visible(fi)
      } : null,
      composer: anchor ? { tag: anchor.tagName.toLowerCase(), class: clsOf(anchor).slice(0, 120), path: pathOf(anchor), rect: snapRect(anchor), placeholder: (anchor.getAttribute && (anchor.getAttribute('placeholder') || anchor.getAttribute('data-placeholder'))) || null } : null,
      composerContainer: scope ? { tag: scope.tagName.toLowerCase(), class: clsOf(scope).slice(0, 160), path: pathOf(scope), rect: snapRect(scope) } : null,
      attachPick: { ok: att.ok, how: att.how, reason: att.reason || null, idx: att.ok ? att.cand.idx : null, label: att.ok ? att.cand.label : null, class: att.ok ? att.cand.class : null },
      sendPick: { ok: send.ok, how: send.how, reason: send.reason || null, idx: send.ok ? send.cand.idx : null, label: send.ok ? send.cand.label : null, class: send.ok ? send.cand.class : null, candidates: send.candidates || null },
      stopPick: { ok: stop.ok, how: stop.how, reason: stop.reason || null, idx: stop.ok ? stop.cand.idx : null, label: stop.ok ? stop.cand.label : null, class: stop.ok ? stop.cand.class : null, hasRect: stop.ok ? stop.cand.hasRect : null, candidates: stop.candidates || null },
      streaming: streamingState(),
      attachmentsHeuristic: box,
      buttonCandidatesInComposerContainer: publicCands(scoped, 25),
      buttonCandidatesByProximity: publicCands(all, 40),
      // 每个候选按钮的 outerHTML 截断 300 字符，便于登录后人工比对真实 DOM
      outerHtmlSamples: (function () {
        var out = [];
        for (var i = 0; i < all.length && out.length < 30; i++) {
          var h = '';
          try { h = String(all[i].__el.outerHTML || '').slice(0, 300); } catch (e) {}
          out.push({ idx: all[i].idx, outerHtml: h });
        }
        return out;
      })(),
      inputTypeFileCount: document.querySelectorAll('input[type=file]').length,
      log: LOG.slice(-40)
    };
  }

  window.${HELPER_NAME} = {
    __v: 1,
    getState: getState,
    pickDebug: pickDebug,
    diag: diag,
    findFileInput: findFileInput,
    fileInputSummary: function () {
      var fi = findFileInput();
      return fi ? { exists: true, accept: fi.getAttribute('accept'), multiple: !!fi.multiple, path: pathOf(fi) } : { exists: false };
    },
    attachInfo: attachInfo,
    pickFileInputAfterClick: pickFileInputAfterClick,
    pickSend: pickSend,
    pickStop: pickStop,
    clickSend: clickSend,
    sendReadyInfo: sendReadyInfo,
    clickStop: clickStop,
    countWithNames: countWithNames,
    setAttached: setAttached,
    clearAttached: clearAttached,
    attachedInfo: attachedInfo,
    attachmentsCleared: attachmentsCleared,
    focusComposer: focusComposer,
    composerEmpty: composerEmpty,
    log: LOG
  };
  return true;
})()`;
}

/** 主进程里用来读页面状态的一行表达式。 */
function exprGetState() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.getState() : {ok:false,reason:'helper 未注入'})`;
}
function exprDiag() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.diag() : {ok:false,reason:'helper 未注入'})`;
}
function exprAttachInfo() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.attachInfo() : {ok:false,reason:'helper 未注入'})`;
}
function exprFileInputSummary() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.fileInputSummary() : {ok:false,reason:'helper 未注入'})`;
}
function exprPickFileInput() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.pickFileInputAfterClick() : {ok:false,reason:'helper 未注入'})`;
}
function exprClickSend() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.clickSend() : {ok:false,how:'failed',reason:'helper 未注入'})`;
}
function exprSendReady() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.sendReadyInfo() : {ok:false,how:'none',reason:'helper 未注入'})`;
}
function exprClickStop() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.clickStop() : {ok:false,how:'none',reason:'helper 未注入'})`;
}
function exprCountWithNames(names) {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.countWithNames(${JSON.stringify(names || [])}) : {ok:false,reason:'helper 未注入'})`;
}
function exprSetAttached(names) {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.setAttached(${JSON.stringify(names || [])}) : {ok:false,reason:'helper 未注入'})`;
}
function exprClearAttached() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.clearAttached() : {ok:false,reason:'helper 未注入'})`;
}
function exprFocusComposer() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.focusComposer() : {ok:false,reason:'helper 未注入'})`;
}
function exprComposerEmpty() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.composerEmpty() : {ok:false,reason:'helper 未注入'})`;
}
function exprAttachmentsCleared() {
  return `(window.${HELPER_NAME} ? window.${HELPER_NAME}.attachmentsCleared() : {ok:false,reason:'helper 未注入'})`;
}

module.exports = {
  HELPER_NAME,
  buildHelper,
  exprGetState,
  exprDiag,
  exprAttachInfo,
  exprFileInputSummary,
  exprPickFileInput,
  exprClickSend,
  exprSendReady,
  exprClickStop,
  exprCountWithNames,
  exprSetAttached,
  exprClearAttached,
  exprFocusComposer,
  exprComposerEmpty,
  exprAttachmentsCleared
};
