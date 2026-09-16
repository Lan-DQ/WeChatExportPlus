"""WCDB 服务客户端 - 自动检测路径"""
import subprocess, json, os, socket, time, http.client, shutil

# ⚠️ 起**控制台程序**（taskkill 这类）必须带这个标志，否则每次调用都闪一个黑框。
# 用户实测反馈"关闭后跳出来一堆黑框（可能是 cmd）"—— 退出时这里会 taskkill 两次
# （一次按树杀、一次补刀），每次一个黑框。`capture_output=True` 只管管道，
# **不阻止控制台窗口出现**。
_NO_WINDOW = {'creationflags': 0x08000000} if os.name == 'nt' else {}

def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))

class WCDBClient:
    def __init__(self):
        self.proc = None
        self.port = None

    def _find_runtime(self, base):
        candidates = [
            os.path.join(base, 'electron', 'electron.exe'),
            os.path.join(base, 'runtime', 'node.exe'),
            os.path.join(base, 'APP', 'WeChatExport', 'electron', 'electron.exe'),
            os.path.join(base, 'APP', 'WeChatExport', 'runtime', 'node.exe'),
        ]
        for c in candidates:
            if os.path.exists(c): return c
        return shutil.which('node') or shutil.which('node.exe') or ''

    def start(self, key, data_dir='', timeout=45, account_dir=''):
        """启动内核服务。

        ⚠️ `account_dir`（2026-09-16，对应 issue #2）：**具体账号目录**（`wxid_xxx`）。
        给了它就只在那里面找 `session.db` —— 因为服务端默认是"递归找到第一个就用"，
        用户在同一台机器登录过多个微信时，会拿别的账号的库去开 → 开不了 → 连接超时。
        不给则退回原来的行为（在 `data_dir` 下递归找）。
        """
        base = os.path.dirname(_script_dir())
        server = os.path.join(_script_dir(), 'wcdb_server.js')
        runtime = self._find_runtime(base)
        if not runtime:
            raise RuntimeError('Electron/Node.js 运行时未找到')

        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        self.port = s.getsockname()[1]
        s.close()

        search_root = account_dir or data_dir
        args = [runtime, server, key, str(self.port)]
        if search_root:
            args.append(search_root)

        self.proc = subprocess.Popen(
            args, cwd=_script_dir(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        for i in range(timeout):
            if self.proc.poll() is not None:
                out = ''
                try: out = self.proc.stdout.read(1000).decode('utf-8', errors='replace').strip() if self.proc.stdout else '(无输出)'
                except: out = '(读取失败)'
                raise RuntimeError(f'WCDB 服务异常退出:\n{out[:500]}')
            try:
                c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
                c.request('GET', '/ping')
                r = c.getresponse()
                if r.read().decode() == 'pong':
                    return True
            except: pass
            time.sleep(1)
        out = ''
        try: out = '\n' + self.proc.stdout.read(1000).decode('utf-8', errors='replace').strip() if self.proc.stdout else ''
        except: pass
        raise RuntimeError(f'WCDB 启动超时{out}')

    def _get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        c.request('GET', '/' + path)
        r = c.getresponse()
        d = r.read().decode('utf-8')
        c.close()
        return d

    def get_sessions(self):
        return json.loads(self._get('sessions'))
    def get_messages(self, wxid, limit=500, offset=0):
        return json.loads(self._get(f'messages/{wxid}/{limit}/{offset}'))
    def get_count(self, wxid):
        return int(self._get(f'count/{wxid}'))
    def get_display_names(self, wxids):
        import http.client as hc
        c = hc.HTTPConnection('127.0.0.1', self.port, timeout=30)
        c.request('POST', '/displaynames', json.dumps(wxids), {'Content-Type': 'application/json'})
        r = c.getresponse()
        d = r.read().decode('utf-8')
        c.close()
        return json.loads(d)

    # v1.2: 媒体 API
    def scan_media(self, session_id, media_type=1, begin=0, end=4102444800, limit=200, offset=0):
        return json.loads(self._get(f'scan_media/{session_id}/{media_type}/{begin}/{end}/{limit}/{offset}'))

    def resolve_image(self, md5):
        return json.loads(self._get(f'resolve_image/{md5}'))

    def resolve_image_batch(self, requests):
        import http.client as hc
        c = hc.HTTPConnection('127.0.0.1', self.port, timeout=60)
        c.request('POST', '/resolve_image_batch', json.dumps(requests), {'Content-Type': 'application/json'})
        r = c.getresponse(); d = r.read().decode('utf-8'); c.close()
        return json.loads(d)

    def stop(self):
        """关闭 WCDB 服务。

        [WeChatExportPlus 改动] 原实现只 terminate 直接子进程。当 runtime 是
        electron.exe 时，它会再派生子进程，terminate 父进程后子进程会变成孤儿，
        持续占用发布包目录（表现为「文件夹被占用、无法删除/重命名/压缩」）。

        ⚠️ 2026-09-15 修：原先这里**顺序错了** —— 先 terminate 再 taskkill /T。
        主进程一死，进程树就断了，`taskkill /T` 只能看到一个已经不存在的父进程，
        子进程照样活下来（用户报的"退出后 node/electron 还在"就是这个）。
        正确顺序是**趁主进程还活着时按树杀**（与 ds_bridge/host.py 里浏览器的做法一致）。
        """
        if not self.proc:
            return
        pid = self.proc.pid
        alive = self.proc.poll() is None
        # ★ 第一步必须是"趁活着按树杀"：这一步才能连 electron 派生的子进程一起收掉
        if os.name == 'nt' and alive:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, timeout=10, **_NO_WINDOW)
            except Exception:
                pass
        # 第二步：兜底，确保主进程自己也没了
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        # 第三步：再补一次树杀（万一第一步漏了、或进程在两步之间又派生了子进程）
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, timeout=10, **_NO_WINDOW)
            except Exception:
                pass
        # 第四步：确认没有残留，有就记下来（用户报过"退出后 node 还在"）
        try:
            self._verify_no_residue(pid)
        except Exception:
            pass
        self.proc = None

    @staticmethod
    def _verify_no_residue(pid):
        """确认该 pid 及其子进程都没了；有残留就打日志（不抛异常）。"""
        import subprocess as _sp
        try:
            r = _sp.run(['tasklist', '/FI', 'PID eq %d' % pid],
                        capture_output=True, timeout=10)
            txt = (r.stdout or b'').decode('utf-8', errors='replace')
            if str(pid) in txt:
                print('[wcdb] 警告：关闭后仍能看到 PID %d 残留' % pid)
        except Exception:
            pass

