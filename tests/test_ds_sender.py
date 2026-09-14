# -*- coding: utf-8 -*-
"""批次发送调度（ds_bridge/sender.py）的单元测试。

用一个假宿主（FakeHost）替掉 Electron：它忠实模拟「发出去 → 模型开始思考 →
点停止才停」的行为，并记录每一次调用的顺序和参数，因此能验证用户定的规则：
    每批 ≤50 个文件 / 批次之间要截断 / 最后一批不截断 / 全自动跑完。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ds_bridge import plan as P          # noqa: E402
from ds_bridge import sender as S        # noqa: E402


class Clock:
    """假时钟：每次读取前进 0.5 秒，保证 _wait_until 不会真的等下去。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.5
        return self.t


class FakeHost:
    LIMIT = 50

    def __init__(self):
        self.streaming = False
        self.attachments = []
        self.calls = []
        self.over_limit = []
        self.settle_seen = []
        # 让「第一个路径以某串结尾」的那一批挂载失败（用来模拟某一批挂不上）
        self.fail_if_first = ''

    # 供 sender 调用
    def state(self):
        return {'ok': True, 'streaming': self.streaming,
                'attachments': len(self.attachments), 'fileInput': True}

    def attach(self, paths, timeout_ms=0, settle_ms=None):
        self.calls.append(('attach', list(paths), self.streaming))
        if settle_ms is not None:
            self.settle_seen.append(settle_ms)
        if self.fail_if_first and paths and paths[0].endswith(self.fail_if_first):
            return {'ok': False, 'attached': 0, 'error': 'fake attach fail'}
        if len(paths) > self.LIMIT:
            self.over_limit.append(len(paths))
        self.attachments = list(paths)
        return {'ok': True, 'attached': len(paths), 'how': 'input', 'sendReady': True}

    def send(self, method='auto', text=''):
        self.calls.append(('send', list(self.attachments), text))
        self.attachments = []
        self.streaming = True          # 发出去后模型自动开始思考
        return {'ok': True, 'how': 'button'}

    def stop(self, timeout_ms=0):
        self.calls.append(('stop', self.streaming))
        was = self.streaming
        self.streaming = False
        return {'ok': True, 'stopped': was, 'how': 'button'}


def _mk_sender(host, **kw):
    kw.setdefault('log', None)
    return S.BatchSender(host, sleep=lambda _s: None, clock=Clock(), **kw)


def _files(n, tag='f'):
    return [f'C:\\tmp\\{tag}{i:04d}.jpg' for i in range(n)]


def test_three_batches_stop_between_but_not_after_last():
    host = FakeHost()
    batches = P.plan_batches(_files(117), 50)
    assert [len(b) for b in batches] == [50, 50, 17]
    s = _mk_sender(host)
    res = s.run(batches)

    assert res['ok'] is True
    assert res['batches'] == 3 and res['sent_batches'] == 3
    assert res['sent_files'] == 117
    assert res['stopped'] == 2          # 只在批次之间截断
    assert res['failed'] == [] and res['error'] == ''
    assert host.over_limit == []        # 任何一次挂附件都没超过 50

    kinds = [c[0] for c in host.calls]
    assert kinds == ['attach', 'send', 'stop', 'attach', 'send', 'stop',
                     'attach', 'send'], kinds
    # 截断必须发生在「正在生成」时
    assert [c[1] for c in host.calls if c[0] == 'stop'] == [True, True]


def test_real_export_tree_end_to_end(tmp_path):
    """真实目录结构（2 个会话 + 指令 + 清单）走一遍完整计划与发送。"""
    root = str(tmp_path)
    with open(os.path.join(root, '给AI的指令.txt'), 'w', encoding='utf-8') as f:
        f.write('x')
    with open(os.path.join(root, '导出清单.html'), 'w', encoding='utf-8') as f:
        f.write('x')
    for name, n in (('群聊A', 3), ('群聊B', 60)):
        with open(os.path.join(root, f'{name}.md'), 'w', encoding='utf-8') as f:
            f.write('x')
        d = os.path.join(root, f'{name}_图片')
        os.makedirs(d)
        for i in range(1, n + 1):
            with open(os.path.join(d, f'{i:04d}.jpg'), 'wb') as f:
                f.write(b'x')
    units = P.scan_export_dir(root)
    batches = P.plan_from_units(units)
    host = FakeHost()
    res = _mk_sender(host).run(batches)

    assert [len(c[1]) for c in host.calls if c[0] == 'send'] == [50, 17]
    assert res['ok'] and res['stopped'] == 1
    assert res['sent_files'] == 67
    assert host.over_limit == []


def test_first_batch_attach_failure_aborts():
    host = FakeHost()
    host.fail_if_first = 'f0000.jpg'          # 第一批永远挂不上（重试也没用）
    res = _mk_sender(host).run(P.plan_batches(_files(60), 50))
    assert res['ok'] is False
    assert '第一批' in res['error']
    assert [c[0] for c in host.calls] == ['attach', 'attach']   # 重试一次后放弃


def test_later_batch_attach_failure_is_recorded_and_continues():
    host = FakeHost()
    host.fail_if_first = 'f0050.jpg'          # 只让第 2 批挂不上
    res = _mk_sender(host).run(P.plan_batches(_files(120), 50))
    assert res['ok'] is False
    assert [f['index'] for f in res['failed']] == [2]
    assert res['sent_batches'] == 2      # 第 1、3 批还是发出去了
    assert res['sent_files'] == 70


def test_cancel_stops_early():
    host = FakeHost()
    state = {'n': 0}

    def should_cancel():
        state['n'] += 1
        return state['n'] > 3

    res = _mk_sender(host).run(P.plan_batches(_files(200), 50), should_cancel=should_cancel)
    assert res['cancelled'] is True
    assert res['ok'] is False
    assert res['sent_batches'] < 4
    assert host.over_limit == []


def test_dry_run_touches_nothing():
    host = FakeHost()
    res = _mk_sender(host, dry_run=True).run(P.plan_batches(_files(120), 50))
    assert host.calls == []
    assert res['sent_files'] == 120 and res['sent_batches'] == 3


def test_settle_ms_by_file_type():
    """等待时间按类型给：文档 0.25 秒/个、图片 0.15 秒/个（用户要求下调一半后的值）。"""
    assert S.settle_ms_for(['a.md', 'b.txt', 'c.pdf']) == 3 * 250
    assert S.settle_ms_for(['a.jpg', 'b.PNG', 'c.webp']) == 3 * 150
    assert S.settle_ms_for(['a.md', 'b.jpg']) == 250 + 150
    assert S.settle_ms_for([]) == 0
    # 上限保护：再多也不会超过 60 秒
    assert S.settle_ms_for(['x.md'] * 1000) == 60000


def test_sender_passes_settle_ms_to_host():
    host = FakeHost()
    s = _mk_sender(host)
    s.run([['C:\\tmp\\a.md', 'C:\\tmp\\b.jpg']])
    assert host.settle_seen == [400], host.settle_seen


def test_empty_plan_reports_error():
    res = _mk_sender(FakeHost()).run([])
    assert res['ok'] is False and '没有要发送' in res['error']


def test_leftover_attachments_aborts_instead_of_overfilling():
    host = FakeHost()
    host.attachments = ['stuck.jpg'] * 7      # 假装上一批的附件没清掉
    # 让 state 永远显示有残留：attach 也不改（这里直接让 wait 超时的办法是
    # 用一个额外残留源）
    orig_attach = host.attach

    def attach(paths, timeout_ms=0):
        r = orig_attach(paths, timeout_ms)
        host.attachments = list(paths) + ['stuck.jpg'] * 7
        return r

    host.attach = attach
    res = _mk_sender(host).run(P.plan_batches(_files(60), 50))
    assert res['ok'] is False
    assert '还挂着' in res['error'] and '没发出去的附件' in res['error']
