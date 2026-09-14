# -*- coding: utf-8 -*-
"""会话标签仓库（gui/session_tags.py）的单元测试。

纯逻辑，不需要 tkinter，所以不走 test_ui_invariants 那套。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gui'))

import session_tags as ST  # noqa: E402


def _store(tmp_path, name='会话标签.json'):
    return ST.TagStore(str(tmp_path / name))


def test_clean_tag_normalizes():
    assert ST.clean_tag('  常看  ') == '常看'
    assert ST.clean_tag('#常看') == '常看'
    assert ST.clean_tag('常   看') == '常 看'
    assert ST.clean_tag('') == ''
    assert ST.clean_tag(None) == ''
    assert ST.clean_tag('   ') == ''
    assert len(ST.clean_tag('长' * 50)) == ST.MAX_TAG_LEN


def test_assign_creates_tag_and_counts(tmp_path):
    s = _store(tmp_path)
    assert s.assign(['wxid_a'], '常看') == 1
    assert s.assign(['wxid_b'], '常看') == 1
    # 重复贴同一个标签不算新增
    assert s.assign(['wxid_a'], '常看') == 0
    assert s.all_tags() == ['常看']
    assert s.counts() == {'常看': 2}
    assert sorted(s.wxids_with('常看')) == ['wxid_a', 'wxid_b']
    assert s.tags_of('wxid_a') == ['常看']


def test_multi_tag_per_session_and_cap(tmp_path):
    s = _store(tmp_path)
    for i in range(ST.MAX_TAGS_PER_SESSION + 3):
        s.assign(['wxid_a'], f'标签{i}')
    assert len(s.tags_of('wxid_a')) == ST.MAX_TAGS_PER_SESSION
    # 超上限时不再新增，但已有的标签集合仍然可用
    assert s.assign(['wxid_a'], '另加') == 0


def test_untag_and_clear(tmp_path):
    s = _store(tmp_path)
    s.assign(['a', 'b'], 'x')
    s.assign(['a', 'b'], 'y')
    s.untag(['a'], 'x')
    assert s.tags_of('a') == ['y']
    assert s.tags_of('b') == ['x', 'y']
    s.clear(['a'])
    assert s.tags_of('a') == []
    assert s.counts() == {'x': 1, 'y': 1}


def test_delete_tag_removes_everywhere(tmp_path):
    s = _store(tmp_path)
    s.assign(['a', 'b'], 'x')
    s.assign(['a'], 'y')
    assert s.delete_tag('x') == 2
    assert s.all_tags() == ['y']
    assert s.tags_of('b') == []
    assert s.tags_of('a') == ['y']


def test_rename_tag_moves_and_merges(tmp_path):
    s = _store(tmp_path)
    s.assign(['a'], 'x')
    s.assign(['b'], 'y')
    assert s.rename_tag('x', 'z') is True
    assert s.all_tags() == ['z', 'y']
    assert s.tags_of('a') == ['z']
    # 改成已存在的标签 = 合并
    assert s.rename_tag('z', 'y') is True
    assert s.all_tags() == ['y']
    assert s.tags_of('a') == ['y']
    assert s.tags_of('b') == ['y']
    # 非法/同名改名不动
    assert s.rename_tag('y', 'y') is False
    assert s.rename_tag('', 'q') is False


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / '会话标签.json')
    s = ST.TagStore(path)
    s.assign(['a'], '常看')
    s.assign(['b'], '工作')
    s.assign(['a'], '工作')
    assert s.save() is True

    s2 = ST.TagStore(path)
    assert s2.load_error == ''
    assert s2.all_tags() == ['常看', '工作']
    assert s2.tags_of('a') == ['常看', '工作']
    assert s2.tags_of('b') == ['工作']
    # 原子写不能留下临时文件
    assert not os.path.exists(path + '.tmp')


def test_save_is_atomic_and_valid_json(tmp_path):
    path = str(tmp_path / '会话标签.json')
    s = ST.TagStore(path)
    s.assign(['a'], '常看')
    s.assign(['a'], '工作')
    s.untag(['a'], '常看')
    s.save()
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    assert data['version'] == ST.VERSION
    assert data['sessions'] == {'a': ['工作']}
    # 标签定义本身要留着（摘掉标签不等于删标签），所以 order 里两个都还在；
    # 真正没人用的标签由 prune() 清掉。
    assert data['order'] == ['常看', '工作']


def test_corrupt_file_does_not_crash_and_can_recover(tmp_path):
    path = str(tmp_path / '会话标签.json')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{ this is not json')
    s = ST.TagStore(path)
    assert s.load_error != ''
    assert s.is_empty()
    # 用户重新打标签后必须能覆盖坏文件
    s.assign(['a'], '新标签')
    assert s.save() is True
    s2 = ST.TagStore(path)
    assert s2.load_error == ''
    assert s2.tags_of('a') == ['新标签']


def test_order_rebuilt_from_sessions_when_missing(tmp_path):
    path = str(tmp_path / '会话标签.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'sessions': {'a': ['乙'], 'b': ['甲']}}, f,
                  ensure_ascii=False)
    s = ST.TagStore(path)
    assert s.all_tags() == ['乙', '甲']


def test_load_ignores_bad_entries(tmp_path):
    path = str(tmp_path / '会话标签.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'order': ['', None, '好'],
                   'sessions': {'a': ['好', '', None], 'b': [], 'c': 'not-a-list'}},
                  f, ensure_ascii=False)
    s = ST.TagStore(path)
    assert s.all_tags() == ['好']
    assert s.tags_of('a') == ['好']
    assert s.tags_of('b') == []
    assert s.tags_of('c') == []


def test_prune_drops_dead_sessions_and_unused_tags(tmp_path):
    s = _store(tmp_path)
    s.assign(['alive', 'dead'], '共享')
    s.assign(['dead'], '只有它')
    assert s.prune(['alive']) == 1
    assert s.all_tags() == ['共享']
    assert s.tags_of('dead') == []


def test_empty_store_has_no_file(tmp_path):
    s = _store(tmp_path)
    assert s.is_empty()
    assert s.all_tags() == []
    assert s.tags_of('anyone') == []
