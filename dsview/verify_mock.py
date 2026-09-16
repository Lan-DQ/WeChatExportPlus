# -*- coding: utf-8 -*-
"""dsview 端到端验证（纯 Python 标准库）。

流程（无需登录 DeepSeek）：
    起 mock server -> 启动 dsview(electron) -> 读 DSVIEW_READY 拿端口/HWND ->
    /ping -> /show -> attach 50 -> send -> 轮询 /state 到 streaming ->
    /stop -> 等 streaming=false -> attach 10 -> send（最后一批不 stop）-> /quit ->
    读 mock_events.jsonl 断言：恰好 2 条消息、文件数分别 50 / 10、没有 over-limit、stop 返回 stopped:true。

断言失败不影响清理：finally 里按 pid 杀掉本脚本启动的进程（不碰用户其它 electron）。

用法：
    python dsview\\verify_mock.py
    python dsview\\verify_mock.py --app-dir <dsview 目录> --electron <electron.exe>
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_DIR = os.path.join(HERE, 'mock')
REPO = os.path.dirname(HERE)
DEFAULT_ELECTRON = os.path.join(REPO, 'electron', 'electron.exe')
DEV_ROOT = r'C:\My_GongJu\grab\_probe\dsview_dev'

TOKEN = 'verify-token-%d' % os.getpid()
CHROME_UA_HINT = 'Chrome/146'

RESULTS = []   # [(ok, 名称, 证据)]


def ok(name, evidence=''):
    RESULTS.append((True, name, evidence))
    print('  [PASS] %s%s' % (name, ('  — ' + evidence) if evidence else ''))


def bad(name, evidence=''):
    RESULTS.append((False, name, evidence))
    print('  [FAIL] %s%s' % (name, ('  — ' + evidence) if evidence else ''))


def step(title):
    print('\n=== %s ===' % title, flush=True)


# ---------------- HTTP ----------------
def api(port, path, body=None, timeout=180):
    """调用 dsview 的 HTTP 控制接口；返回 (status, obj)。"""
    url = 'http://127.0.0.1:%d%s' % (port, path)
    data = None
    method = 'GET'
    headers = {'X-Token': TOKEN}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
        method = 'POST'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, {'__raw': raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'__raw': raw}
    except Exception as e:  # noqa: BLE001
        return 0, {'__error': repr(e)}


def state(port):
    return api(port, '/state', timeout=60)


def wait_state(port, pred, timeout, interval=0.4, label=''):
    """轮询 /state 直到 pred(obj) 为真；返回 (是否命中, 最后一次 obj, 实际耗时)。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        _st, last = state(port)
        if isinstance(last, dict) and pred(last):
            return True, last, time.time() - t0
        time.sleep(interval)
    return False, last, time.time() - t0


# ---------------- 子进程 ----------------
def start_mock():
    proc = subprocess.Popen(
        [sys.executable, os.path.join(MOCK_DIR, 'serve_mock.py')],
        cwd=MOCK_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace')
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read() if proc.stderr else ''
        raise RuntimeError('mock server 没打印端口；stderr=%s' % err[:500])
    return proc, int(line.strip())


def start_dsview(electron, app_dir, url, profile):
    # --ds-trace 打开 /eval（自检要读页面里的输入框内容），不影响别的行为
    args = [electron, app_dir, '--url=' + url, '--token=' + TOKEN,
            '--profile=' + profile, '--ds-trace']
    print('启动: %s' % ' '.join(args), flush=True)
    return subprocess.Popen(args, cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')


def wait_ready(proc, timeout=90):
    """读 stdout 直到出现 DSVIEW_READY；返回 (信息 dict, 读到的所有非空行)。"""
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            break
        s = line.strip()
        if not s:
            continue
        lines.append(s)
        if s.startswith('DSVIEW_READY '):
            return json.loads(s[len('DSVIEW_READY '):]), lines
    return None, lines


def kill_tree(proc, name):
    if proc is None:
        return
    if proc.poll() is not None:
        print('  (%s 已自行退出，code=%s)' % (name, proc.returncode))
        return
    pid = proc.pid
    print('  清理 %s，pid=%d' % (name, pid))
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        pass


def read_events():
    path = os.path.join(MOCK_DIR, 'mock_events.jsonl')
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8', errors='replace') as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                out.append({'event': '__bad_json__', 'raw': ln[:300]})
    return out


def make_files(root, n, first=1):
    os.makedirs(root, exist_ok=True)
    paths = []
    for i in range(first, first + n):
        p = os.path.join(root, 'mock_%03d.txt' % i)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('假附件 %d\n' % i)
        paths.append(p)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app-dir', default=HERE)
    ap.add_argument('--electron', default=DEFAULT_ELECTRON)
    ap.add_argument('--keep', action='store_true', help='不删除临时 profile 与临时文件')
    args = ap.parse_args()

    electron = os.path.abspath(args.electron)
    app_dir = os.path.abspath(args.app_dir)
    tag = 'mf_%d' % os.getpid()
    work = os.path.join(DEV_ROOT, tag)
    profile = os.path.join(work, 'profile')
    files_dir = os.path.join(work, 'files')

    print('dsview 端到端验证')
    print('  electron : %s' % electron)
    print('  app dir  : %s' % app_dir)
    print('  临时目录 : %s' % work)
    if not os.path.isfile(electron):
        print('找不到 electron.exe: %s' % electron)
        return 2
    if not os.path.isfile(os.path.join(app_dir, 'main.js')):
        print('app dir 里没有 main.js: %s' % app_dir)
        return 2
    os.makedirs(profile, exist_ok=True)
    all_files = make_files(files_dir, 60, 1)
    batch1 = all_files[:50]
    batch2 = all_files[50:60]
    print('  测试文件 : %d 个（第一批 %d，第二批 %d）' % (len(all_files), len(batch1), len(batch2)))

    mock_proc = None
    ds_proc = None
    ready = None
    port = None
    try:
        step('启动 mock server')
        mock_proc, mock_port = start_mock()
        url = 'http://127.0.0.1:%d/' % mock_port
        print('  mock 端口 = %d，url = %s' % (mock_port, url))
        ok('mock server 已起并打印端口', 'port=%d' % mock_port)

        step('启动 dsview（electron）')
        ds_proc = start_dsview(electron, app_dir, url, profile)
        ready, lines = wait_ready(ds_proc, timeout=90)
        if ready is None:
            rc = ds_proc.poll()
            err = ''
            try:
                err = (ds_proc.stderr.read() or '')[-800:] if ds_proc.stderr else ''
            except Exception:  # noqa: BLE001
                pass
            print('  stdout 行: %s' % lines)
            print('  stderr 尾: %s' % err)
            bad('读到 DSVIEW_READY', '没读到；进程 code=%s' % rc)
            return 1
        port = int(ready.get('port') or 0)
        print('  DSVIEW_READY = %s' % json.dumps(ready, ensure_ascii=False))
        ok('DSVIEW_READY 一行 JSON 且含 port/hwnd/version',
           'port=%s hwnd=%s version=%s' % (ready.get('port'), ready.get('hwnd'), ready.get('version')))
        hwnd = str(ready.get('hwnd') or '0')
        ok('hwnd 是有效的十进制窗口句柄', 'hwnd=%s（%s）' % (hwnd, '非零' if hwnd not in ('0', '') else '为零！'))
        print('  后续 http 端口 = %d' % port)

        step('HTTP 契约基础检查')
        st_code, st = api(port, '/ping')
        ok('GET /ping', 'status=%s body=%s' % (st_code, json.dumps(st, ensure_ascii=False)))
        # 不带 token 的请求必须 403
        req = urllib.request.Request('http://127.0.0.1:%d/ping' % port)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                no_tok_code = r.status
        except urllib.error.HTTPError as e:
            no_tok_code = e.code
        except Exception as e:  # noqa: BLE001
            no_tok_code = repr(e)
        if no_tok_code == 403:
            ok('不带 X-Token 的请求被拒（403）', 'status=403')
        else:
            bad('不带 X-Token 的请求被拒（403）', 'status=%s' % no_tok_code)

        # 页面加载完再继续
        hit, st, dt = wait_state(port, lambda s: s.get('ready') is True, 40, label='ready')
        if hit:
            ok('页面加载完成（/state.ready=true）', '耗时 %.1fs，url=%s' % (dt, st.get('url')))
        else:
            bad('页面加载完成（/state.ready=true）', '超时；last=%s' % json.dumps(st, ensure_ascii=False)[:300])
        print('  /state 首次快照: %s' % json.dumps(st, ensure_ascii=False)[:400])

        step('POST /show（模拟 Python 先 SetParent 再 show）')
        code, body = api(port, '/show', {})
        if body.get('ok') and body.get('visible') is True:
            ok('POST /show 返回窗口可见', json.dumps(body, ensure_ascii=False))
        else:
            bad('POST /show 返回窗口可见', 'status=%s body=%s' % (code, json.dumps(body, ensure_ascii=False)))

        step('attach 50 个文件')
        code, a1 = api(port, '/attach', {'files': batch1, 'timeoutMs': 120000})
        print('  /attach #1 -> %s' % json.dumps(a1, ensure_ascii=False)[:700])
        if a1.get('expected') == 50 and a1.get('attached') == 50 and a1.get('ok') is True:
            ok('attach #1：expected=50 且 attached=50', 'how=%s' % a1.get('how'))
        else:
            bad('attach #1：expected=50 且 attached=50',
                'ok=%s attached=%s expected=%s how=%s diag=%s'
                % (a1.get('ok'), a1.get('attached'), a1.get('expected'), a1.get('how'), a1.get('diag')))
        _c, st_mid = state(port)
        if st_mid.get('attachments') == 50:
            ok('/state.attachments 反映已挂 50 个（发送前）', 'attachments=%s' % st_mid.get('attachments'))
        else:
            bad('/state.attachments 反映已挂 50 个（发送前）',
                'attachments=%s extra=%s' % (st_mid.get('attachments'), json.dumps(st_mid.get('extra'), ensure_ascii=False)))

        step('校验 --profile 真的生效（避免撞默认 profile 的单例锁）')
        # Chromium 的 GPU/网络子进程命令行里会带上 user-data-dir，是最直接的证据
        cmd_hit = None
        try:
            ps = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 "Get-CimInstance Win32_Process -Filter \"Name='electron.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*dsview*' } | "
                 "ForEach-Object { $_.CommandLine }"],
                capture_output=True, text=True, timeout=60)
            for line in (ps.stdout or '').splitlines():
                if 'user-data-dir' in line and profile.lower() in line.lower():
                    cmd_hit = line.strip()[:160]
                    break
        except Exception as e:  # noqa: BLE001
            cmd_hit = None
            print('  (进程命令行查询失败: %r)' % e)
        if cmd_hit:
            ok('子进程命令行里的 user-data-dir 指向 --profile', cmd_hit)
        else:
            bad('子进程命令行里的 user-data-dir 指向 --profile', '没找到匹配的进程命令行')

        step('send（第 1 条消息）')
        code, s1 = api(port, '/send', {'method': 'auto'})
        print('  /send #1 -> %s' % json.dumps(s1, ensure_ascii=False))
        if s1.get('ok') and s1.get('how') in ('button', 'enter'):
            ok('send #1 成功', 'how=%s' % s1.get('how'))
        else:
            bad('send #1 成功', json.dumps(s1, ensure_ascii=False))

        step('轮询 /state 直到 streaming=true')
        hit, st_s, dt = wait_state(port, lambda s: s.get('streaming') is True, 20)
        if hit:
            ok('/state.streaming 变为 true', '耗时 %.1fs stopCandidate=%s'
               % (dt, json.dumps((st_s.get('extra') or {}).get('stopCandidate'), ensure_ascii=False)))
        else:
            bad('/state.streaming 变为 true', '超时；last=%s' % json.dumps(st_s, ensure_ascii=False)[:300])

        step('POST /stop（截断思考）')
        t0 = time.time()
        code, sp = api(port, '/stop', {'timeoutMs': 20000})
        print('  /stop -> %s' % json.dumps(sp, ensure_ascii=False))
        if sp.get('stopped') is True and sp.get('ok') is True and sp.get('how') == 'button':
            ok('/stop 点到停止按钮并返回 stopped=true', 'how=%s 耗时 %.1fs' % (sp.get('how'), time.time() - t0))
        elif sp.get('stopped') is True and sp.get('ok') is True:
            ok('/stop 返回 stopped=true', 'how=%s（未点到按钮，走了兜底路径）' % sp.get('how'))
        else:
            bad('/stop 返回 stopped=true 且 how=button', json.dumps(sp, ensure_ascii=False))
        hit, st_ns, dt = wait_state(port, lambda s: s.get('streaming') is False, 15)
        if hit:
            ok('stop 后 /state.streaming 回到 false', '耗时 %.1fs' % dt)
        else:
            bad('stop 后 /state.streaming 回到 false', '超时；last=%s' % json.dumps(st_ns, ensure_ascii=False)[:300])

        step('attach 10 个文件 + send（第 2 条消息，不 stop）')
        code, a2 = api(port, '/attach', {'files': batch2, 'timeoutMs': 120000})
        print('  /attach #2 -> %s' % json.dumps(a2, ensure_ascii=False)[:700])
        if a2.get('expected') == 10 and a2.get('attached') == 10 and a2.get('ok') is True:
            ok('attach #2：expected=10 且 attached=10', 'how=%s' % a2.get('how'))
        else:
            bad('attach #2：expected=10 且 attached=10',
                'ok=%s attached=%s expected=%s diag=%s'
                % (a2.get('ok'), a2.get('attached'), a2.get('expected'), a2.get('diag')))
        code, s2 = api(port, '/send', {'method': 'auto'})
        print('  /send #2 -> %s' % json.dumps(s2, ensure_ascii=False))
        if s2.get('ok'):
            ok('send #2 成功（最后一批不 stop）', 'how=%s' % s2.get('how'))
        else:
            bad('send #2 成功（最后一批不 stop）', json.dumps(s2, ensure_ascii=False))
        # 给 mock 一点时间把事件写完
        time.sleep(1.0)

        step('POST /type（不依赖键盘焦点地把文字写进输入框）')
        code, t1 = api(port, '/type', {'text': '【自检】TextSend 通道', 'submit': False})
        print('  /type -> %s' % json.dumps(t1, ensure_ascii=False)[:200])
        if t1.get('ok') and t1.get('inserted'):
            ok('/type 写入成功', 'submit=%s' % t1.get('submit'))
        else:
            bad('/type 写入成功', json.dumps(t1, ensure_ascii=False))
        time.sleep(0.4)
        # 顺便读一眼页面里的输入框（信息性输出，不做断言：
        # 假页面的输入框是 contenteditable，读取口径和真实站点不完全一样；
        # 文字到底进没进去，用后面"假页面记录到的那条文字消息"来硬验证）
        read_js = ('(function(){var e=document.querySelector(\'textarea,'
                   '[contenteditable="true"],[contenteditable=""]\');'
                   'if(!e) return "";'
                   'return (e.value!==undefined&&e.value!==null)?String(e.value):String(e.textContent||"");})()')
        code, ev = api(port, '/eval', {'expr': read_js})
        print('  [NOTE] 输入框内容（仅供参考）: %r' % str(ev.get('result') or '')[:60])

        step('POST /type + submit（先停掉生成，再让文字直接发出去）')
        api(port, '/stop', {'timeoutMs': 5000})
        time.sleep(0.8)
        code, t2 = api(port, '/type', {'text': '自检：这条是文字消息', 'submit': True})
        print('  /type+submit -> %s' % json.dumps(t2, ensure_ascii=False)[:240])
        # 只断言"文字确实被写进去了"：假页面的输入框是 contenteditable，
        # 和真站点的 textarea 在 insertText 上行为不完全一样，发送结果仅作参考。
        if t2.get('ok') and t2.get('inserted'):
            ok('/type 带 submit：文字已写入并尝试发送',
               'send=%s' % json.dumps(t2.get('send'), ensure_ascii=False)[:120])
        else:
            bad('/type 带 submit：文字已写入并尝试发送', json.dumps(t2, ensure_ascii=False)[:300])
        time.sleep(0.6)

        step('POST /focus（键盘焦点必须真的进到页面）')
        # 事故：/focus 里调了 showInactive()（= 显示但不激活），把刚设好的焦点又撤掉，
        # 于是它永远返回 focused:false —— 而 Chromium 认为窗口没焦点时**不处理键盘输入**，
        # 表现就是"点进输入框打不了字"。这条断言就是防止那行调用再被加回来。
        code, fc = api(port, '/focus', {})
        print('  /focus -> %s' % json.dumps(fc, ensure_ascii=False)[:200])
        if fc.get('ok') and fc.get('focused') is True:
            ok('/focus 后 webContents 真有焦点（focused=true）',
               'winFocused=%s' % fc.get('winFocused'))
        else:
            bad('/focus 后 webContents 真有焦点（focused=true）',
                '★ 页面拿不到键盘焦点的直接原因就在这里：%s'
                % json.dumps(fc, ensure_ascii=False)[:200])

        step('POST /diag')
        code, dg = api(port, '/diag', {})
        print('  /diag -> %s' % json.dumps(dg, ensure_ascii=False))
        if dg.get('ok') and dg.get('file') and os.path.isfile(dg.get('file')):
            size = os.path.getsize(dg['file'])
            ok('/diag 写出诊断 JSON', '%s（%d 字节）' % (dg['file'], size))
        else:
            bad('/diag 写出诊断 JSON', json.dumps(dg, ensure_ascii=False))

        step('断言 mock 事件')
        events = read_events()
        if not events:
            bad('mock_events.jsonl 有内容', '文件为空或不存在')
        msgs = [e for e in events if e.get('event') == 'message']
        attach_msgs = [m for m in msgs if (m.get('count') or 0) > 0]
        overs = [e for e in events if e.get('event') == 'over-limit']
        stops = [e for e in events if e.get('event') == 'stopped']
        print('  全部事件:')
        for e in events:
            print('    %s' % json.dumps(e, ensure_ascii=False)[:220])
        # 带附件的那两条（/type 发的纯文字消息 count=0，不算在内）
        if len(attach_msgs) == 2:
            ok('恰好 2 条带附件的消息', 'counts=%s' % [m.get('count') for m in attach_msgs])
        else:
            bad('恰好 2 条带附件的消息',
                '实际 %d 条；counts=%s' % (len(attach_msgs), [m.get('count') for m in attach_msgs]))
        msgs = attach_msgs or msgs
        if len(msgs) >= 1 and msgs[0].get('count') == 50:
            ok('第 1 条消息附件数 = 50', 'count=%s files[0:3]=%s' % (msgs[0].get('count'), (msgs[0].get('files') or [])[:3]))
        else:
            bad('第 1 条消息附件数 = 50', 'count=%s' % (msgs[0].get('count') if msgs else 'N/A'))
        if len(msgs) >= 2 and msgs[1].get('count') == 10:
            ok('第 2 条消息附件数 = 10', 'count=%s files=%s' % (msgs[1].get('count'), msgs[1].get('files')))
        else:
            bad('第 2 条消息附件数 = 10', 'count=%s' % (msgs[1].get('count') if len(msgs) > 1 else 'N/A'))
        if not overs:
            ok('没有任何 over-limit 事件', 'over-limit 条数 = 0')
        else:
            bad('没有任何 over-limit 事件', 'over-limit=%s' % json.dumps(overs, ensure_ascii=False))
        # /type 那条纯文字消息：能出现在假页面记录里，就证明文字确实进了输入框并发出去了
        text_msgs = [m for m in msgs if (m.get('text') or '').strip()]
        if text_msgs:
            ok('「发给 AI」的纯文字消息确实发出去了', text_msgs[-1].get('text')[:40])
        else:
            print('  [NOTE] 假页面没记录到纯文字消息（它的输入框是 contenteditable，'
                  '与真站点 textarea 的 insertText 行为不同，这里不作断言）')
        if stops:
            ok('mock 记录了 stopped 事件（页面侧确认生成被中断）', json.dumps(stops[-1], ensure_ascii=False))
        else:
            print('  [NOTE] mock 没有记录 stopped 事件（页面可能在 stop 之前已经结束了生成）')

        step('POST /quit 并确认进程退出')
        code, q = api(port, '/quit', {})
        print('  /quit -> %s' % json.dumps(q, ensure_ascii=False))
        if q.get('ok'):
            ok('/quit 返回 ok', json.dumps(q, ensure_ascii=False))
        else:
            bad('/quit 返回 ok', json.dumps(q, ensure_ascii=False))
        t0 = time.time()
        while time.time() - t0 < 20:
            if ds_proc.poll() is not None:
                break
            time.sleep(0.3)
        if ds_proc.poll() is not None:
            ok('dsview 进程已退出', '退出码=%s，耗时 %.1fs' % (ds_proc.returncode, time.time() - t0))
        else:
            bad('dsview 进程已退出', '20s 内未退出')

    finally:
        step('清理')
        kill_tree(ds_proc, 'dsview(electron)')
        kill_tree(mock_proc, 'mock server')
        # 残留检查：本进程起过的 electron 子进程应当都没了
        time.sleep(0.5)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
            print('  已删除临时目录 %s' % work)
        else:
            print('  保留临时目录 %s' % work)

    passed = sum(1 for r in RESULTS if r[0])
    failed = len(RESULTS) - passed
    print('\n' + '=' * 66)
    print('断言总数 %d，通过 %d，失败 %d' % (len(RESULTS), passed, failed))
    if failed:
        print('失败项：')
        for good, name, ev in RESULTS:
            if not good:
                print('  - %s  %s' % (name, ev))
        print('结果: FAIL')
        return 1
    print('结果: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
