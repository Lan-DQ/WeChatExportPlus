# -*- coding: utf-8 -*-
"""batch_export 的行为测试（用假 WCDB 客户端，不需要真实数据库）。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

import batch_export as be  # noqa: E402
import message_content as mc  # noqa: E402


class FakeWCDB:
    """最小可用的假客户端：只实现 batch_export 用到的三个方法。"""

    def __init__(self, data=None):
        self.data = data or {}

    def get_messages(self, wxid, limit=0, offset=0):
        rows = self.data.get(wxid, [])
        return rows[:limit] if limit else rows

    def get_display_names(self, wxids):
        return {w: f'名字-{w}' for w in wxids}

    def get_count(self, wxid):
        return len(self.data.get(wxid, []))


def msg(local_type, content, ts='1700000000', sender='', lid='1'):
    return {'local_type': local_type, 'message_content': content,
            'create_time': ts, 'sender_username': sender, 'local_id': lid}


@pytest.fixture
def fake():
    return FakeWCDB({
        'group1@chatroom': [
            msg(1, 'wxid_a:\n大家好', '1700000000', 'wxid_a', '1'),
            msg(3, '<msg><img aeskey="x"/></msg>', '1700000060', 'wxid_b', '2'),
            msg(49, '"X" 拍了拍 "Y"', '1700000120', '', '3'),
        ],
        'wxid_friend': [
            msg(1, '你好', '1700000200', 'wxid_friend', '4'),
            msg(34, '<msg><voicemsg voicelength="4601"/></msg>', '1700000260', 'wxid_friend', '5'),
        ],
    })


# ─────────────── 纯函数：命名与清洗 ───────────────

def test_safe_name_strips_illegal_chars():
    assert be.safe_name('a/b\\c:d*e?f"g<h>i|j') == 'a_b_c_d_e_f_g_h_i_j'


def test_safe_name_trims_trailing_dots_and_spaces():
    assert be.safe_name('  群聊A.  ') == '群聊A'


def test_safe_name_falls_back_when_empty():
    assert be.safe_name('') == '未命名'
    assert be.safe_name('///') == '___'
    assert be.safe_name('', fallback='wxid_x') == 'wxid_x'


def test_safe_name_avoids_windows_reserved_names():
    assert be.safe_name('CON') == '_CON'
    assert be.safe_name('lpt1') == '_lpt1'


def test_safe_name_length_capped():
    assert len(be.safe_name('长' * 200)) <= 80


def test_timestamp_dir_name_format():
    import datetime
    n = be.timestamp_dir_name(datetime.datetime(2026, 2, 13, 14, 30))
    assert n == '导出_20260213_1430'


def test_unique_dir(tmp_path):
    (tmp_path / 'A').mkdir()
    assert be.unique_dir(str(tmp_path), 'A') == 'A_2'
    (tmp_path / 'A_2').mkdir()
    assert be.unique_dir(str(tmp_path), 'A') == 'A_3'
    assert be.unique_dir(str(tmp_path), 'B') == 'B'


# ─────────────── 端到端（假数据） ───────────────

def test_creates_timestamped_root_with_one_folder_per_session(fake, tmp_path):
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'},
                {'wxid': 'wxid_friend', 'title': '私聊B'}]
    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             resolve_images=False)
    assert os.path.isdir(res['root'])
    assert os.path.basename(res['root']).startswith('导出_')
    assert os.path.isdir(os.path.join(res['root'], '群聊A'))
    assert os.path.isdir(os.path.join(res['root'], '私聊B'))
    assert len(res['ok']) == 2
    assert res['failed'] == []
    assert not res['cancelled']


def test_index_page_lists_all_sessions(fake, tmp_path):
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'},
                {'wxid': 'wxid_friend', 'title': '私聊B'}]
    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             resolve_images=False)
    idx = os.path.join(res['root'], '导出清单.html')
    assert os.path.exists(idx)
    html = open(idx, encoding='utf-8').read()
    assert '群聊A' in html and '私聊B' in html
    assert '群聊' in html and '单聊' in html


def test_ai_format_writes_jsonl_and_txt(fake, tmp_path):
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'}]
    res = be.export_sessions(fake, '', sessions, 'ai', str(tmp_path),
                             resolve_images=False)
    d = os.path.join(res['root'], '群聊A')
    assert os.path.exists(os.path.join(d, '对话.jsonl'))
    assert os.path.exists(os.path.join(d, '对话.txt'))

    lines = open(os.path.join(d, '对话.jsonl'), encoding='utf-8').read().splitlines()
    meta = json.loads(lines[0])
    assert meta['_meta']['chat'] == '群聊A'
    assert meta['_meta']['is_group'] is True
    assert meta['_meta']['message_count'] == 3
    recs = [json.loads(l) for l in lines[1:]]
    assert [r['type'] for r in recs] == ['text', 'image', 'text']
    # 发言人区分
    assert recs[0]['sender'] and recs[0]['role'] in ('self', 'other')
    assert recs[2]['text'].endswith('拍了拍 "Y"')      # 拍一拍不再被丢弃


def test_ai_txt_has_speaker_labels(fake, tmp_path):
    sessions = [{'wxid': 'wxid_friend', 'title': '私聊B'}]
    res = be.export_sessions(fake, '', sessions, 'ai', str(tmp_path),
                             resolve_images=False)
    txt = open(os.path.join(res['root'], '私聊B', '对话.txt'), encoding='utf-8').read()
    assert '名字-wxid_friend' in txt
    assert '[语音 4.6秒]' in txt
    assert '#' in txt                                    # 头部注释


def test_non_html_formats_use_session_named_file(fake, tmp_path):
    for fmt, fname in [('excel', '群聊A.xlsx'), ('pdf', '群聊A.pdf'),
                       ('csv', '对话.csv'), ('json', '对话.json'), ('txt', '对话.txt')]:
        res = be.export_sessions(fake, '', [{'wxid': 'group1@chatroom', 'title': '群聊A'}],
                                 fmt, str(tmp_path), resolve_images=False)
        p = os.path.join(res['root'], '群聊A', fname)
        assert os.path.exists(p), f'{fmt} 期望 {fname}'


def test_html_format_creates_index_and_image_dir(fake, tmp_path):
    res = be.export_sessions(fake, '', [{'wxid': 'group1@chatroom', 'title': '群聊A'}],
                             'html', str(tmp_path), resolve_images=False)
    d = os.path.join(res['root'], '群聊A')
    assert os.path.exists(os.path.join(d, 'index.html'))


def test_same_name_sessions_do_not_overwrite_each_other(tmp_path):
    """两个不同 wxid 但是同一个显示名：不能互相覆盖。"""
    fake = FakeWCDB({
        'wxid_a': [msg(1, 'from a', sender='wxid_a', lid='1')],
        'wxid_b': [msg(1, 'from b', sender='wxid_b', lid='2')],
    })
    sessions = [{'wxid': 'wxid_a', 'title': '同名'},
                {'wxid': 'wxid_b', 'title': '同名'}]
    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             resolve_images=False)
    dirs = sorted(d for d in os.listdir(res['root'])
                  if os.path.isdir(os.path.join(res['root'], d)))
    assert dirs == ['同名', '同名_2']
    assert len(res['ok']) == 2


def test_rerun_creates_new_timestamped_root_without_touching_previous(fake, tmp_path):
    """重复导出应当新建一个大文件夹，不能动上一次的结果。"""
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'}]
    first = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                               resolve_images=False)
    marker = os.path.join(first['root'], '群聊A', '对话.txt')
    before = open(marker, encoding='utf-8').read()

    second = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                                resolve_images=False)
    assert second['root'] != first['root']            # 同一分钟也不覆盖
    assert os.path.exists(marker)                     # 旧结果仍在
    assert open(marker, encoding='utf-8').read() == before


# ─────────────── 错误隔离与取消 ───────────────

def test_one_failure_does_not_stop_others(tmp_path):
    class Boom(FakeWCDB):
        def get_messages(self, wxid, limit=0, offset=0):
            if wxid == 'bad':
                raise RuntimeError('模拟读取失败')
            return super().get_messages(wxid, limit, offset)

    fake = Boom({'good': [msg(1, 'hi', sender='wxid_g', lid='1')]})
    sessions = [{'wxid': 'bad', 'title': '坏会话'},
                {'wxid': 'good', 'title': '好会话'}]
    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             resolve_images=False)
    assert len(res['failed']) == 1
    assert res['failed'][0]['wxid'] == 'bad'
    assert len(res['ok']) == 1
    assert os.path.isdir(os.path.join(res['root'], '好会话'))


def test_empty_session_is_reported_not_crashed(fake, tmp_path):
    sessions = [{'wxid': 'wxid_empty', 'title': '空会话'}]
    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             resolve_images=False)
    assert res['ok'] == []
    assert len(res['failed']) == 1
    assert '没有消息' in res['failed'][0]['error']


def test_cancel_stops_at_session_boundary(fake, tmp_path):
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'},
                {'wxid': 'wxid_friend', 'title': '私聊B'}]
    calls = {'n': 0}
    seen = []

    def cancel():
        calls['n'] += 1
        return calls['n'] > 1        # 第一个会话开始后即取消

    res = be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                             progress=lambda d, t, ti: seen.append((d, t, ti)),
                             should_cancel=cancel, resolve_images=False)
    assert res['cancelled'] is True
    assert len(res['ok']) <= 1
    # 取消后不再有下一个会话的进度回调
    assert all(s[2] != '私聊B' for s in seen)


def test_progress_callback_reports_index_and_total(fake, tmp_path):
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'},
                {'wxid': 'wxid_friend', 'title': '私聊B'}]
    seen = []
    be.export_sessions(fake, '', sessions, 'txt', str(tmp_path),
                       progress=lambda d, t, ti: seen.append((d, t, ti)),
                       resolve_images=False)
    assert seen == [(1, 2, '群聊A'), (2, 2, '私聊B')]


def test_unknown_format_raises(fake, tmp_path):
    with pytest.raises(ValueError):
        be.export_sessions(fake, '', [], 'docx', str(tmp_path))


def test_resolve_my_wxid(tmp_path):
    (tmp_path / 'wxid_abc123_4f0c').mkdir()
    (tmp_path / 'other').mkdir()
    assert be.resolve_my_wxid(str(tmp_path)) == 'wxid_abc123'
    assert be.resolve_my_wxid('') == ''
    assert be.resolve_my_wxid(str(tmp_path / 'nope')) == ''
