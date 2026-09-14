# -*- coding: utf-8 -*-
"""本地假 DeepSeek 页面服务（纯 Python 标准库）。

用途：在【不登录】的情况下把宿主（dsview）的整条链路跑通 —— 挂附件、发送、停止。
只监听 127.0.0.1，端口由系统分配，起好后把端口打印到 stdout（供 verify_mock.py 读取）。

路由：
    GET  /      返回同目录的 ds_mock.html
    POST /log   把 JSON 体追加写入 mock_events.jsonl（一行一条）
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, 'ds_mock.html')
EVENTS = os.path.join(HERE, 'mock_events.jsonl')
MAX_BODY = 8 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'dsmock/1.0'

    # 默认实现会往 stderr 打日志，太吵
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html', '/ds_mock.html'):
            try:
                with open(HTML, 'rb') as f:
                    data = f.read()
            except OSError as e:
                self._send(500, 'mock html 读取失败: %s' % e, 'text/plain; charset=utf-8')
                return
            self._send(200, data, 'text/html; charset=utf-8')
            return
        if path == '/events':
            # 方便手工查看
            try:
                with open(EVENTS, 'rb') as f:
                    data = f.read()
            except OSError:
                data = b''
            self._send(200, data, 'application/x-ndjson; charset=utf-8')
            return
        self._send(404, 'not found: %s' % path, 'text/plain; charset=utf-8')

    def do_POST(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n < 0 or n > MAX_BODY:
            self._send(413, 'body too large', 'text/plain; charset=utf-8')
            return
        raw = self.rfile.read(n) if n else b''
        if path != '/log':
            self._send(404, 'not found: %s' % path, 'text/plain; charset=utf-8')
            return
        text = raw.decode('utf-8', 'replace')
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError as e:
            payload = {'event': '__parse_error__', 'raw': text[:2000], 'error': str(e)}
        # 追加写入，用锁保证并发 POST 不会串行错位
        with LOCK:
            with open(EVENTS, 'a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
        self._send(200, json.dumps({'ok': True}, ensure_ascii=False), 'application/json; charset=utf-8')


LOCK = threading.Lock()


def main():
    # 每次启动清空事件文件，避免上一轮残留污染断言
    try:
        with open(EVENTS, 'w', encoding='utf-8'):
            pass
    except OSError as e:
        print('无法写入 %s: %s' % (EVENTS, e), file=sys.stderr)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = srv.server_address[1]
    print(port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
