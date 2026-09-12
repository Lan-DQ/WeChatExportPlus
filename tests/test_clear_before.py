# -*- coding: utf-8 -*-
"""「导出并清除之前的导出内容」的测试。

重点是**安全边界**：只能删本工具自己生成的东西，绝不能动用户目录里的其它文件。
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

import batch_export as be   # noqa: E402


class FakeWCDB:
    def __init__(self, data=None):
        self.data = data or {}

    def get_messages(self, wxid, limit=0, offset=0):
        rows = self.data.get(wxid, [])
        return rows[:limit] if limit else rows

    def get_display_names(self, wxids):
        return {w: f'N-{w}' for w in wxids}

    def get_count(self, wxid):
        return len(self.data.get(wxid, []))


def msg(lt, c, ts='1700000000', sender='', lid='1'):
    return {'local_type': lt, 'message_content': c, 'create_time': ts,
            'sender_username': sender, 'local_id': lid}


@pytest.fixture
def fake():
    return FakeWCDB({'wxid_a': [msg(1, 'hello', sender='wxid_a', lid='1')]})


def make_export(root, name='导出_20260213_1430', extra_dir=None, extra_file=None):
    """造一个"上次导出"的目录，外加可选的无关联文件。"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'A.md'), 'w', encoding='utf-8') as f:
        f.write('x' * 100)
    sub = os.path.join(d, 'A_图片')
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, '0001.jpg'), 'wb') as f:
        f.write(b'y' * 200)
    with open(os.path.join(root, '导出清单.html'), 'w', encoding='utf-8') as f:
        f.write('<html></html>')
    with open(os.path.join(root, '给AI的指令.txt'), 'w', encoding='utf-8') as f:
        f.write('prompt')
    if extra_dir:
        os.makedirs(os.path.join(root, extra_dir), exist_ok=True)
        with open(os.path.join(root, extra_dir, '重要.txt'), 'w', encoding='utf-8') as f:
            f.write('DO NOT DELETE')
    if extra_file:
        with open(os.path.join(root, extra_file), 'w', encoding='utf-8') as f:
            f.write('DO NOT DELETE')
    return d


# ─────────────── scan ───────────────

def test_scan_finds_exports_and_sizes(tmp_path):
    make_export(str(tmp_path))
    info = be.scan_previous_exports(str(tmp_path))
    assert info['dirs'] == ['导出_20260213_1430']
    assert sorted(info['files']) == sorted(['给AI的指令.txt', '导出清单.html'])
    assert info['bytes'] >= 300


def test_scan_ignores_unrelated(tmp_path):
    make_export(str(tmp_path), extra_dir='我的资料', extra_file='笔记.txt')
    info = be.scan_previous_exports(str(tmp_path))
    assert '我的资料' not in info['dirs']
    assert '笔记.txt' not in info['files']


def test_scan_on_missing_dir(tmp_path):
    info = be.scan_previous_exports(str(tmp_path / 'nope'))
    assert info == {'dirs': [], 'files': [], 'bytes': 0}


# ─────────────── clear ───────────────

def test_clear_removes_previous_exports(tmp_path):
    make_export(str(tmp_path))
    nd, nf = be.clear_previous_exports(str(tmp_path))
    assert (nd, nf) == (1, 2)
    assert not os.path.exists(os.path.join(tmp_path, '导出_20260213_1430'))
    assert not os.path.exists(os.path.join(tmp_path, '导出清单.html'))
    assert not os.path.exists(os.path.join(tmp_path, '给AI的指令.txt'))


def test_clear_handles_same_minute_suffix(tmp_path):
    make_export(str(tmp_path), '导出_20260213_1430_2')
    nd, _ = be.clear_previous_exports(str(tmp_path))
    assert nd == 1


def test_clear_KEEPS_unrelated_files_and_dirs(tmp_path):
    """最关键的安全测试：用户目录里的其它东西一个都不能动。"""
    make_export(str(tmp_path), extra_dir='我的资料', extra_file='笔记.txt')
    be.clear_previous_exports(str(tmp_path))
    assert os.path.isdir(os.path.join(tmp_path, '我的资料'))
    assert open(os.path.join(tmp_path, '我的资料', '重要.txt'), encoding='utf-8').read() \
        == 'DO NOT DELETE'
    assert os.path.isfile(os.path.join(tmp_path, '笔记.txt'))


def test_clear_keeps_similarly_named_things(tmp_path):
    """名字像但不是本工具生成的东西也要保留。"""
    for name in ('导出_备份', '导出_2026', '导出清单.html.bak', 'my导出_20260213_1430'):
        os.makedirs(os.path.join(tmp_path, name), exist_ok=True)
    be.clear_previous_exports(str(tmp_path))
    for name in ('导出_备份', '导出_2026', '导出清单.html.bak', 'my导出_20260213_1430'):
        assert os.path.isdir(os.path.join(tmp_path, name)), name


def test_clear_on_empty_or_missing_dir(tmp_path):
    assert be.clear_previous_exports(str(tmp_path)) == (0, 0)
    assert be.clear_previous_exports(str(tmp_path / 'nope')) == (0, 0)
    assert be.clear_previous_exports('') == (0, 0)


# ─────────────── 集成到 export_sessions ───────────────

def test_export_with_clear_before_removes_old_then_writes_new(fake, tmp_path):
    make_export(str(tmp_path), extra_dir='我的资料')
    res = be.export_sessions(fake, '', [{'wxid': 'wxid_a', 'title': '会话A'}],
                             'md', str(tmp_path), resolve_images=False,
                             prompt='P', clear_before=True)
    assert res['cleared'] == (1, 2)
    # 旧的不在了
    assert not os.path.exists(os.path.join(tmp_path, '导出_20260213_1430'))
    # 新的在
    assert os.path.exists(os.path.join(res['root'], '会话A.md'))
    # 无关内容仍在
    assert os.path.isdir(os.path.join(tmp_path, '我的资料'))


def test_export_without_clear_keeps_old(fake, tmp_path):
    old = make_export(str(tmp_path))
    res = be.export_sessions(fake, '', [{'wxid': 'wxid_a', 'title': '会话A'}],
                             'md', str(tmp_path), resolve_images=False,
                             prompt='P', clear_before=False)
    assert res['cleared'] == (0, 0)
    assert os.path.isdir(old), '不开覆盖时旧导出必须保留'
    assert res['root'] != old


def test_export_clear_before_on_empty_dir_is_noop(fake, tmp_path):
    res = be.export_sessions(fake, '', [{'wxid': 'wxid_a', 'title': '会话A'}],
                             'md', str(tmp_path), resolve_images=False,
                             prompt='P', clear_before=True)
    assert res['cleared'] == (0, 0)
    assert len(res['ok']) == 1
