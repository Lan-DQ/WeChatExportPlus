# -*- coding: utf-8 -*-
"""md_exporter 与 batch_export 的 md 布局测试。"""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

import batch_export as be    # noqa: E402
import md_exporter as mdx    # noqa: E402
import message_content as mc  # noqa: E402


class FakeWCDB:
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


def prep(rows, my='wxid_me'):
    return mc.prepare(rows, {r['sender_username']: f'N-{r["sender_username"]}'
                             for r in rows if r['sender_username']}, my, '我')


# ─────────────── build_document ───────────────

def test_document_has_header_and_people():
    rows = prep([
        msg(1, '你好', sender='wxid_a', lid='1'),
        msg(1, '你也好', sender='wxid_b', lid='2'),
        msg(1, '你好', sender='wxid_a', lid='3'),
        msg(1, '我是我', sender='wxid_me', lid='4'),
    ])
    doc, recs = mdx.build_document(rows, {'wxid': 'g@chatroom', 'title': '测试群',
                                          'is_group': True})
    assert doc.startswith('# 测试群')
    assert '群聊' in doc
    # 参与者按发言次数排序：wxid_a 说了 2 次，排最前
    assert doc.index('N-wxid_a') < doc.index('N-wxid_b')
    assert '消息数：4 条' in doc
    assert '我' in doc


def test_document_one_line_per_message_with_speaker():
    rows = prep([msg(1, '第一条', sender='wxid_a', lid='1'),
                 msg(1, '第二条', sender='wxid_b', lid='2')])
    doc, _ = mdx.build_document(rows, {'wxid': 'w', 'title': 'T', 'is_group': False})
    # 每条消息一行：[时间] 发言人: 内容
    lines = [l for l in doc.splitlines() if l.startswith('[')]
    assert len(lines) == 2
    assert 'N-wxid_a: 第一条' in lines[0]
    assert 'N-wxid_b: 第二条' in lines[1]
    # 时间在最前
    assert lines[0].startswith('[20')


def test_document_multiline_is_indented():
    """多行消息（群公告等）的续行要缩进 4 空格，不能和下一行 [时间] 混淆。"""
    rows = prep([
        msg(49, '<msg><appmsg><title>公告</title><type>87</type>'
                '<datadesc>公告第一行&#10;公告第二行</datadesc></appmsg></msg>',
            ts='1700000000', sender='wxid_a', lid='1'),
        msg(1, '下一条普通消息', ts='1700000060', sender='wxid_b', lid='2'),
    ])
    doc, _ = mdx.build_document(rows, {'wxid': 'w', 'title': 'T', 'is_group': False})
    lines = doc.splitlines()
    i = next(i for i, l in enumerate(lines) if '公告第一行' in l)
    assert lines[i].startswith('[20'), '首行应有 [时间] 前缀'
    assert lines[i + 1].startswith('    '), '续行应缩进 4 空格'
    # 下一条消息仍能被正确识别为独立一行
    assert any(l.startswith('[20') and '下一条普通消息' in l for l in lines)


def test_document_image_index_matches_filename():
    """图片文件名带会话名前缀，但正文里必须仍然只写 4 位编号。"""
    rows = prep([msg(3, '<msg><img aeskey="x"/></msg>', sender='wxid_a', lid='77')])
    images = {'77': '群聊A_0001.jpg'}
    doc, recs = mdx.build_document(rows, {'wxid': 'w', 'title': '群聊A', 'is_group': False},
                                   images)
    assert '[图片0001]' in doc
    assert '图片群聊A' not in doc    # 正文不能把前缀吃进 [图片...] 里
    assert '_图片' in doc            # 头部说明图片位置
    assert '群聊A_图片\\群聊A_NNNN.jpg' in doc   # 头部说明真实文件名形态
    assert recs[0]['image_index'] == '群聊A_0001.jpg'   # 语义不变（头部统计用）


def test_document_without_images_has_no_image_dir_note():
    rows = prep([msg(1, '纯文字', sender='wxid_a', lid='1')])
    doc, _ = mdx.build_document(rows, {'wxid': 'w', 'title': 'T', 'is_group': False})
    assert '_图片' not in doc


def test_document_keeps_system_and_rich_types_readable():
    rows = prep([
        msg(10000, '语音通话已经结束', sender='', lid='1'),
        msg(34, '<msg><voicemsg voicelength="4601"/></msg>', sender='wxid_a', lid='2'),
        msg(48, '<msg><location poiname="某机场"/></msg>', sender='wxid_a', lid='3'),
    ])
    doc, _ = mdx.build_document(rows, {'wxid': 'w', 'title': 'T', 'is_group': False})
    assert '语音通话已经结束' in doc
    assert '[语音 4.6秒]' in doc
    assert '[位置] 某机场' in doc
    assert '[类型' not in doc


# ─────────────── export() 落盘 ───────────────

def test_export_writes_md_and_numbered_images(tmp_path):
    """不传前缀时的旧行为（纯编号）保留，供老调用点/老测试继续用。"""
    src = tmp_path / 'src'
    src.mkdir()
    # 造两张假图，文件名模拟 md5
    names = ['aaaa.jpg', 'bbbb.jpg']
    for n in names:
        (src / n).write_bytes(b'\xff\xd8\xff\xe0dummy')

    rows = prep([
        msg(1, '开始', sender='wxid_a', lid='1'),
        msg(3, '<msg><img aeskey="x"/></msg>', sender='wxid_a', lid='2'),
        msg(3, '<msg><img aeskey="y"/></msg>', sender='wxid_a', lid='3'),
    ])
    rows[1]['image_file'] = names[0]
    rows[2]['image_file'] = names[1]

    out = tmp_path / 'out'
    res = mdx.export(rows, str(out), {'wxid': 'w', 'title': '会话A', 'is_group': False},
                     image_map=mdx.build_image_map(rows), image_src_dir=str(src))

    assert res['images'] == 2
    md = out / '会话A.md'
    assert md.exists()
    img_dir = out / '会话A_图片'
    assert img_dir.is_dir()
    assert sorted(p.name for p in img_dir.iterdir()) == ['0001.jpg', '0002.jpg']
    text = md.read_text(encoding='utf-8')
    assert '[图片0001]' in text and '[图片0002]' in text


def test_build_image_map_numbers_by_appearance():
    rows = prep([msg(1, 'a', sender='x', lid='1'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='2'),
                 msg(1, 'b', sender='x', lid='3'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='4')])
    rows[1]['image_file'] = 'aaa.jpg'
    rows[3]['image_file'] = 'bbb.png'
    m = mdx.build_image_map(rows)
    assert m == {'2': '0001.jpg', '4': '0002.png'}


def test_build_image_map_is_stable_when_one_message_has_two_images():
    """同一个 local_id 出现两条图片消息时，编号不能重复占用两个号。"""
    rows = prep([msg(3, '<msg><img/></msg>', sender='x', lid='7'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='7'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='8')])
    for r in rows:
        r['image_file'] = 'x.jpg'
    m = mdx.build_image_map(rows)
    assert m == {'7': '0001.jpg', '8': '0002.jpg'}


# ─────────────── 图片文件名带会话名前缀 ───────────────

def test_build_image_map_prefixes_files_but_body_keeps_number():
    """① 文件名带前缀 群聊A_0001.jpg，正文里仍然是 [图片0001]。"""
    rows = prep([msg(1, 'a', sender='x', lid='1'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='2'),
                 msg(3, '<msg><img/></msg>', sender='x', lid='4')])
    rows[1]['image_file'] = 'aaa.jpg'
    rows[2]['image_file'] = 'bbb.png'

    m = mdx.build_image_map(rows, '群聊A')
    assert m == {'2': '群聊A_0001.jpg', '4': '群聊A_0002.png'}

    doc, recs = mdx.build_document(rows, {'wxid': 'g@chatroom', 'title': '群聊A',
                                          'is_group': True}, m)
    assert '[图片0001]' in doc and '[图片0002]' in doc
    assert '图片群聊A_0001' not in doc      # 前缀不能进正文编号
    assert '群聊A_图片\\群聊A_NNNN.jpg' in doc   # 头部要写明真实文件名形态
    # image_index 语义不变：仍是 map 里的值
    assert [r['image_index'] for r in recs if r.get('image_index')] == \
        ['群聊A_0001.jpg', '群聊A_0002.png']


def test_image_prefix_is_sanitized_and_bounded():
    """前缀过长/含非法字符时要能安全落盘（会话名最长 80 字符，不能撑爆路径）。"""
    rows = prep([msg(3, '<msg><img/></msg>', sender='x', lid='1')])
    rows[0]['image_file'] = 'a.jpg'

    name = mdx.build_image_map(rows, 'a/b:c*d?e' + 'x' * 40)['1']
    pfx = name[: -len('_0001.jpg')]
    assert name.endswith('_0001.jpg')
    assert pfx.startswith('a_b_c_d_ex')      # 非法字符已清洗
    assert not set(pfx) & set('\\/:*?"<>|')
    assert len(pfx) <= mdx.IMAGE_PREFIX_MAX
    assert mdx.IMAGE_PREFIX_MAX == 24
    # 幂等：export() 会把已经清洗过的 base 再交进来一次，不能越洗越短
    assert mdx._image_prefix(pfx) == pfx
    assert mdx._image_prefix('') == ''
    # 空前缀 = 旧行为
    assert mdx.build_image_map(rows) == {'1': '0001.jpg'}


def test_same_name_sessions_get_distinct_image_prefixes(tmp_path):
    """③ 同名会话 base 去重成 同名 / 同名_2 时，两批图片名不能冲突。"""
    assert be.unique_base(str(tmp_path), '同名') == '同名'   # 目录还空着

    rows = prep([msg(3, '<msg><img/></msg>', sender='x', lid='1')])
    rows[0]['image_file'] = 'aaa.jpg'
    m1 = mdx.build_image_map(rows, '同名')
    m2 = mdx.build_image_map(rows, '同名_2')
    assert list(m1.values()) == ['同名_0001.jpg']
    assert list(m2.values()) == ['同名_2_0001.jpg']
    assert not (set(m1.values()) & set(m2.values())), '同名会话的图片名不能撞'

    # 真落盘到同一个导出根目录，两个会话各写各的图目录
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'aaa.jpg').write_bytes(b'\xff\xd8\xff\xe0dummy')
    out = tmp_path / 'out'
    mdx.export(rows, str(out), {'wxid': 'w1', 'title': '同名', 'is_group': False},
               image_map=m1, image_src_dir=str(src))
    mdx.export(rows, str(out), {'wxid': 'w2', 'title': '同名_2', 'is_group': False},
               image_map=m2, image_src_dir=str(src))
    assert (out / '同名_图片' / '同名_0001.jpg').is_file()
    assert (out / '同名_2_图片' / '同名_2_0001.jpg').is_file()
    # 图片目录名不能被改动（batch_export.unique_base / clear_before 都靠它认领）
    for d in ('同名_图片', '同名_2_图片'):
        assert (out / d).is_dir()


def test_export_writes_prefixed_image_names(tmp_path):
    """② export() 落盘后目录里是 会话A_0001.jpg，且与正文引用一一对应。"""
    src = tmp_path / 'src'
    src.mkdir()
    for n in ('aaaa.jpg', 'bbbb.jpg', 'cccc.png'):
        (src / n).write_bytes(b'\xff\xd8\xff\xe0dummy')

    rows = prep([
        msg(1, '开始', sender='wxid_a', lid='1'),
        msg(3, '<msg><img/></msg>', sender='wxid_a', lid='2'),
        msg(3, '<msg><img/></msg>', sender='wxid_a', lid='3'),
        msg(3, '<msg><img/></msg>', sender='wxid_a', lid='4'),
    ])
    rows[1]['image_file'] = 'aaaa.jpg'
    rows[2]['image_file'] = 'bbbb.jpg'
    rows[3]['image_file'] = 'cccc.png'

    out = tmp_path / 'out'
    # 故意不传 image_map：走 export() 内部 build_image_map(rows, base) 这条路
    res = mdx.export(rows, str(out), {'wxid': 'w', 'title': '会话A', 'is_group': False},
                     image_src_dir=str(src))

    assert res['images'] == 3
    img_dir = out / '会话A_图片'
    files = sorted(p.name for p in img_dir.iterdir())
    assert files == ['会话A_0001.jpg', '会话A_0002.jpg', '会话A_0003.png']

    text = (out / '会话A.md').read_text(encoding='utf-8')
    for i, name in enumerate(files, 1):
        assert f'[图片{i:04d}]' in text, f'{name} 缺正文引用'
        assert f'[图片{name}]' not in text, '正文不能写完整文件名'
    # 正文引用条数 == 文件个数（一一对应）
    assert len(re.findall(r'\[图片\d{4}\]', text)) == len(files)
    assert '会话A_图片\\会话A_NNNN.jpg' in text   # 头部说明用的是真实前缀


def test_batch_export_passes_deduped_base_as_image_prefix(tmp_path, monkeypatch):
    """最危险的坑：batch_export 必须传 unique_base 之后的 base 当前缀。"""
    seen = []
    real = mdx.build_image_map

    def spy(rows, prefix=''):
        seen.append(prefix)
        return real(rows, prefix)

    monkeypatch.setattr(mdx, 'build_image_map', spy)
    fake = FakeWCDB({'wxid_a': [msg(1, 'from a', sender='wxid_a', lid='1')],
                     'wxid_b': [msg(1, 'from b', sender='wxid_b', lid='2')]})
    sessions = [{'wxid': 'wxid_a', 'title': '同名'},
                {'wxid': 'wxid_b', 'title': '同名'}]
    be.export_sessions(fake, '', sessions, 'md', str(tmp_path), resolve_images=False)
    # 每个会话这里会被调用两次：batch_export 显式调一次，export() 内部在没有图片、
    # map 为空时还会兜底再调一次。关键是前缀必须已经去重 —— 若传的是去重前的 base，
    # 两次都会是「同名」，第二个会话的图片就会覆盖第一个会话的。
    assert seen, 'batch_export 没有调用 build_image_map'
    assert set(seen) == {'同名', '同名_2'}, '必须用去重后的 base，否则同名会话图片互相覆盖'


def test_export_cleans_illegal_filename_chars(tmp_path):
    rows = prep([msg(1, 'hi', sender='wxid_a', lid='1')])
    out = tmp_path / 'out'
    res = mdx.export(rows, str(out), {'wxid': 'w', 'title': 'a/b:c*d?e', 'is_group': False})
    assert os.path.basename(res['md']) == 'a_b_c_d_e.md'


def test_export_without_images_creates_only_md(tmp_path):
    rows = prep([msg(1, 'hi', sender='wxid_a', lid='1')])
    out = tmp_path / 'out'
    res = mdx.export(rows, str(out), {'wxid': 'w', 'title': 'T', 'is_group': False})
    assert res['images'] == 0
    assert res['img_dir'] == ''
    assert not (out / 'T_图片').exists()


# ─────────────── batch_export 的 md 布局 ───────────────

def test_md_layout_puts_target_file_directly_under_root(tmp_path):
    """用户的核心要求：导出目录下那个 .md 就是目标文件，不再套一层会话文件夹。"""
    fake = FakeWCDB({
        'group1@chatroom': [msg(1, '大家好', sender='wxid_a', lid='1')],
        'wxid_friend': [msg(1, '你好', sender='wxid_friend', lid='2')],
    })
    sessions = [{'wxid': 'group1@chatroom', 'title': '群聊A'},
                {'wxid': 'wxid_friend', 'title': '私聊B'}]
    res = be.export_sessions(fake, '', sessions, 'md', str(tmp_path), resolve_images=False)

    root = res['root']
    files = sorted(f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f)))
    assert '群聊A.md' in files
    assert '私聊B.md' in files
    assert '导出清单.html' in files
    # 不能多出一层「群聊A」文件夹
    dirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    assert '群聊A' not in dirs
    assert '私聊B' not in dirs


def test_md_same_name_sessions_do_not_overwrite(tmp_path):
    fake = FakeWCDB({
        'wxid_a': [msg(1, 'from a', sender='wxid_a', lid='1')],
        'wxid_b': [msg(1, 'from b', sender='wxid_b', lid='2')],
    })
    sessions = [{'wxid': 'wxid_a', 'title': '同名'},
                {'wxid': 'wxid_b', 'title': '同名'}]
    res = be.export_sessions(fake, '', sessions, 'md', str(tmp_path), resolve_images=False)
    root = res['root']
    files = sorted(f for f in os.listdir(root) if f.endswith('.md'))
    assert files == ['同名.md', '同名_2.md']
    assert 'from a' in (open(os.path.join(root, '同名.md'), encoding='utf-8').read())
    assert 'from b' in (open(os.path.join(root, '同名_2.md'), encoding='utf-8').read())


def test_md_index_page_links_to_root_files(tmp_path):
    fake = FakeWCDB({'wxid_a': [msg(1, 'x', sender='wxid_a', lid='1')]})
    res = be.export_sessions(fake, '', [{'wxid': 'wxid_a', 'title': '会话A'}],
                             'md', str(tmp_path), resolve_images=False)
    html = open(os.path.join(res['root'], '导出清单.html'), encoding='utf-8').read()
    assert 'href="%E4%BC%9A%E8%AF%9DA.md"' in html or '会话A.md' in html


def test_md_leaves_no_temp_dirs(tmp_path):
    fake = FakeWCDB({'wxid_a': [msg(1, 'x', sender='wxid_a', lid='1')]})
    res = be.export_sessions(fake, '', [{'wxid': 'wxid_a', 'title': 'A'}],
                             'md', str(tmp_path), resolve_images=False)
    leftovers = [d for d in os.listdir(res['root']) if d.startswith('.md_tmp')]
    assert leftovers == []
