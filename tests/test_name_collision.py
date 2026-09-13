# -*- coding: utf-8 -*-
"""回归测试：重名会话 / 残留图片目录 时，不能丢图片。"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

import batch_export as be    # noqa: E402


class FakeWCDB:
    def __init__(self, data=None):
        self.data = data or {}

    def get_messages(self, wxid, limit=0, offset=0):
        rows = self.data.get(wxid, [])
        return rows[:limit] if limit else rows

    def get_display_names(self, wxids):
        # 故意让两个会话显示成同一个名字，制造重名
        return {w: '同名会话' for w in wxids}

    def get_count(self, wxid):
        return len(self.data.get(wxid, []))


def msg(local_type, content, ts='1700000000', sender='', lid='1'):
    return {'local_type': local_type, 'message_content': content,
            'create_time': ts, 'sender_username': sender, 'local_id': lid}


@pytest.fixture
def fake():
    return FakeWCDB({
        'wxid_a': [msg(1, '甲的消息', sender='wxid_a', lid='1')],
        'wxid_b': [msg(1, '乙的消息', sender='wxid_b', lid='1')],
    })


SESSIONS = [{'wxid': 'wxid_a', 'title': '同名会话'},
            {'wxid': 'wxid_b', 'title': '同名会话'}]


def test_same_name_sessions_get_distinct_bases(fake, tmp_path):
    """同一批里的重名会话必须落到不同的目标文件，不能互相覆盖。"""
    res = be.export_sessions(fake, '', SESSIONS, 'md', str(tmp_path),
                             resolve_images=False, prompt='')
    mds = sorted(f for f in os.listdir(res['root']) if f.endswith('.md'))
    assert len(mds) == 2, f'重名会话没有分开: {mds}'
    assert mds[0] != mds[1]
    # 两条消息分别在两个文件里
    texts = [open(os.path.join(res['root'], f), encoding='utf-8').read()
             for f in mds]
    assert any('甲的消息' in t for t in texts)
    assert any('乙的消息' in t for t in texts)


def test_unique_base_avoids_existing_image_folder(tmp_path):
    """unique_base 必须避开已存在的 `<名字>_图片` 目录。

    事故：早期只检查 `<名字>.md` 和 `<名字>` 目录，漏了 `<名字>_图片`。
    于是当上一次导出残留了图片文件夹、而 md 已被移走时，新导出会复用同一个
    名字，md_exporter 里的 shutil.rmtree 会把旧图片文件夹连根删掉 ——
    用户看到的就是"图片文件夹没了/是空的"。
    """
    d = str(tmp_path)
    # 模拟残留：只有图片目录，没有 md
    os.makedirs(os.path.join(d, '群聊A_图片'))
    open(os.path.join(d, '群聊A_图片', '0001.jpg'), 'wb').write(b'x')

    got = be.unique_base(d, '群聊A')
    assert got != '群聊A', 'unique_base 复用了名字，会删掉已有的图片目录'
    assert not os.path.exists(os.path.join(d, got + '_图片')), \
        '返回的名字仍然和已有图片目录冲突'


def test_existing_image_folder_survives_reexport(fake, tmp_path):
    """残留的图片目录不能因为再次导出而被删掉。"""
    out = str(tmp_path)
    # 先造一个上一轮的残留：同名图片目录
    os.makedirs(os.path.join(out, '同名会话_图片'))
    keep = os.path.join(out, '同名会话_图片', '0001.jpg')
    open(keep, 'wb').write(b'KEEP')
    # 同时造一个同名 md，模拟正常的上轮产物
    open(os.path.join(out, '同名会话.md'), 'w', encoding='utf-8').write('old')

    res = be.export_sessions(fake, '', [SESSIONS[0]], 'md', out,
                             resolve_images=False, prompt='')
    # 旧产物要么原样保留，要么被新的一轮用不同名字新建 —— 但绝不能"删了又没补上"
    assert os.path.exists(keep), '残留的图片目录被删掉了'
    assert open(keep, 'rb').read() == b'KEEP', '残留图片内容被改了'
