# -*- coding: utf-8 -*-
"""发送计划（ds_bridge/plan.py）的单元测试。

用临时目录造一棵和真实导出目录同构的文件树，断言扫描结果、展开顺序与批次切法。
纯逻辑，不碰 tkinter、不联网。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ds_bridge import plan as P  # noqa: E402


def _mk_export(root, chats=(('群聊A', 3), ('群聊B', 60)), docs=True):
    if docs:
        with open(os.path.join(root, '给AI的指令.txt'), 'w', encoding='utf-8') as f:
            f.write('instruction')
        with open(os.path.join(root, '导出清单.html'), 'w', encoding='utf-8') as f:
            f.write('<html></html>')
    for name, n in chats:
        with open(os.path.join(root, f'{name}.md'), 'w', encoding='utf-8') as f:
            f.write('# chat')
        d = os.path.join(root, f'{name}_图片')
        os.makedirs(d, exist_ok=True)
        for i in range(1, n + 1):
            with open(os.path.join(d, f'{i:04d}.jpg'), 'wb') as f:
                f.write(b'x')


def test_scan_finds_docs_and_image_dirs(tmp_path):
    _mk_export(str(tmp_path))
    units = P.scan_export_dir(str(tmp_path))
    docs = [u for u in units if u['kind'] == 'doc']
    imgs = [u for u in units if u['kind'] == 'image']
    # 文档顺序：给AI的指令 → .md → 导出清单
    assert [u['label'] for u in docs] == ['给AI的指令.txt', '群聊A.md', '群聊B.md', '导出清单.html']
    assert [u['label'] for u in imgs] == ['群聊A_图片', '群聊B_图片']
    assert [u['count'] for u in imgs] == [3, 60]
    assert imgs[1]['group'] == '群聊B'
    assert imgs[1]['detail'] == '60 张图片'
    assert all(u['paths'] and os.path.isabs(u['paths'][0]) for u in units)


def test_scan_ignores_unknown_and_missing_dir(tmp_path):
    assert P.scan_export_dir(str(tmp_path / '不存在')) == []
    d = tmp_path / '空目录'
    d.mkdir()
    assert P.scan_export_dir(str(d)) == []
    (d / '笔记.docx').write_bytes(b'x')
    (d / '截图.png').write_bytes(b'x')
    units = P.scan_export_dir(str(d))
    # .docx 不在白名单里 → 忽略；根目录图片合成一项
    assert [u['kind'] for u in units] == ['image']
    assert units[0]['count'] == 1


def test_expand_puts_docs_first_then_images_in_chat_order(tmp_path):
    _mk_export(str(tmp_path))
    units = P.scan_export_dir(str(tmp_path))
    paths = P.expand_units(units)
    assert [os.path.basename(p) for p in paths[:4]] == [
        '给AI的指令.txt', '群聊A.md', '群聊B.md', '导出清单.html']
    assert os.path.basename(paths[4]) == '0001.jpg'
    assert '群聊A_图片' in paths[4]
    assert '群聊B_图片' in paths[-1]
    assert len(paths) == 4 + 3 + 60


def test_plan_batches_fills_across_chats(tmp_path):
    _mk_export(str(tmp_path))
    units = P.scan_export_dir(str(tmp_path))
    batches = P.plan_from_units(units)
    # 4 个文档 + 63 张图 = 67 → 50 + 17
    assert [len(b) for b in batches] == [50, 17]
    # 第一批 = 4 个文档 + 群聊A 的 3 张 + 群聊B 的前 43 张
    first = batches[0]
    assert os.path.basename(first[0]) == '给AI的指令.txt'
    assert sum(1 for p in first if p.endswith('.md')) == 2
    assert sum(1 for p in first if p.endswith('.jpg')) == 46
    # 第二批就是群聊B 剩下的 17 张（60 - 43）
    assert len(batches[1]) == 17
    assert all(p.endswith('.jpg') for p in batches[1])
    # 不丢不重
    flat = [p for b in batches for p in b]
    assert flat == P.expand_units(units)


def test_plan_batches_edge_cases():
    assert P.plan_batches([]) == []
    assert P.plan_batches(['a']) == [['a']]
    assert [len(b) for b in P.plan_batches([str(i) for i in range(100)])] == [50, 50]
    assert [len(b) for b in P.plan_batches([str(i) for i in range(101)])] == [50, 50, 1]
    # limit 非法值要有确定行为：0/None 当作"没传"用默认 50；负数至少切成 1 个一批
    assert [len(b) for b in P.plan_batches(['a', 'b'], limit=0)] == [2]
    assert [len(b) for b in P.plan_batches(['a', 'b'], limit=None)] == [2]
    assert [len(b) for b in P.plan_batches(['a', 'b'], limit=-5)] == [1, 1]


def test_summarize(tmp_path):
    _mk_export(str(tmp_path))
    s = P.summarize(P.scan_export_dir(str(tmp_path)))
    assert s['files'] == 67
    assert s['docs'] == 4
    assert s['images'] == 63
    assert s['batches'] == 2
    assert s['batch_sizes'] == [50, 17]


def test_oversize_and_missing(tmp_path):
    d = str(tmp_path)
    small = os.path.join(d, 'a.jpg')
    with open(small, 'wb') as f:
        f.write(b'x' * 10)
    big = os.path.join(d, 'b.jpg')
    with open(big, 'wb') as f:
        f.write(b'x' * 2048)
    assert P.find_oversize([small, big], limit_bytes=1024) == [(big, 2048)]
    assert P.missing_files([small, os.path.join(d, '没有这个.jpg')]) == [
        os.path.join(d, '没有这个.jpg')]


def test_scan_handles_other_format_layout(tmp_path):
    """非 md 格式的导出（一个会话一个文件夹）也要能扫出来。"""
    root = str(tmp_path)
    chat = os.path.join(root, '群聊C')
    os.makedirs(os.path.join(chat, '图片'))
    with open(os.path.join(chat, 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<html>')
    for i in range(2):
        with open(os.path.join(chat, '图片', f'img_{i}.jpg'), 'wb') as f:
            f.write(b'x')
    units = P.scan_export_dir(root)
    kinds = sorted(u['kind'] for u in units)
    assert kinds == ['doc', 'image']
    img = [u for u in units if u['kind'] == 'image'][0]
    assert img['count'] == 2 and img['group'] == '群聊C'
    doc_labels = [u['label'] for u in units if u['kind'] == 'doc']
    assert doc_labels == [os.path.join('群聊C', 'index.html')]
