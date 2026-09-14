# -*- coding: utf-8 -*-
"""把「一次导出的内容」拆成一批一批发给 DeepSeek 网页版的计划。

纯逻辑、不联网、不 import GUI，方便单测。

为什么需要它
------------
网页版一次最多能挂 **50 个文件**，而一次导出可能是「几个 .md + 上千张图」。
所以发送前必须把文件排好序、切成 ≤50 的批次：

    先发文档（给AI的指令 → 各会话 .md → 导出清单.html）
    再发图片（按会话、按编号），并且**跨会话凑满 50 个**（用户明确要求：
    前一个群剩 10 张时，拿下一个群的 40 张凑满一批）

批次之间要「截断」模型的思考过程，最后一批不截断 —— 那属于发送调度（sender.py），
这里只负责「发什么、怎么切」。
"""
import os

DEFAULT_LIMIT = 50                     # 网页版单次上传上限
MAX_FILE_BYTES = 100 * 1024 * 1024     # 网页版单文件上限 100MB

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic'}
DOC_EXTS = {'.md', '.txt', '.html', '.htm', '.json', '.jsonl', '.csv', '.xlsx', '.pdf'}
IMAGE_DIR_SUFFIX = '_图片'

# 文档的发送优先级：数字越小越先发
_DOC_RANK = [
    ('给AI的指令', 0),
    ('.md', 1),
    ('导出清单', 2),
]


def _doc_rank(name):
    low = name.lower()
    for key, rank in _DOC_RANK:
        if key.lower() in low or (key == '.md' and low.endswith('.md')):
            return rank
    return 3


def _size_text(n):
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{n / 1024:.0f} KB'
    return f'{n / 1048576:.1f} MB'


def _unit(kind, label, detail, paths, group=''):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return {'id': f'{kind}:{label}', 'kind': kind, 'label': label, 'detail': detail,
            'paths': list(paths), 'count': len(paths), 'group': group,
            'bytes': total}


def scan_export_dir(root):
    """扫描一个导出目录，返回可勾选的发送项（unit）列表。

    unit 的字段：
        kind    'doc' | 'image'
        label   界面上显示的名字（文件名 / `<会话名>_图片`）
        detail  副标题（张数、大小）
        paths   该发送项展开后的绝对路径列表（图片目录会被展开）
        group   所属会话名（图片目录取 `_图片` 前面的部分），用于排序与归属显示

    识别规则（对 md 格式的导出目录最贴切，也能兼容其它格式的目录）：
        · 根目录下的 .md/.txt/.html/... → 每个文件一个 doc
        · 根目录下的图片文件 → 合成一个 image（"根目录图片"）
        · `<会话名>_图片\\` → 一个 image，group = 会话名
        · 其它子目录（非 md 格式的会话文件夹）→ 里面的图片合成一个 image，
          其余文档各自一个 doc
    """
    root = os.path.abspath(root or '')
    if not root or not os.path.isdir(root):
        return []

    docs, images = [], []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []

    root_images = []
    for name in entries:
        p = os.path.join(root, name)
        if os.path.isdir(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTS:
            root_images.append(p)
        elif ext in DOC_EXTS:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            docs.append(_unit('doc', name, _size_text(size), [p]))

    for name in entries:
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        group = name[:-len(IMAGE_DIR_SUFFIX)] if name.endswith(IMAGE_DIR_SUFFIX) else name
        imgs, sub_docs = [], []
        for dp, _dn, fns in os.walk(p):
            for fn in sorted(fns):
                fp = os.path.join(dp, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXTS:
                    imgs.append(fp)
                elif ext in DOC_EXTS:
                    sub_docs.append(fp)
        if imgs:
            unit = _unit('image', name, f'{len(imgs)} 张图片', imgs, group=group)
            images.append(unit)
        for fp in sub_docs:
            rel = os.path.relpath(fp, root)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            docs.append(_unit('doc', rel, _size_text(size), [fp], group=group))

    if root_images:
        images.append(_unit('image', '（根目录图片）',
                            f'{len(root_images)} 张图片', root_images))

    # 文档排序：给AI的指令 → .md → 导出清单 → 其它；同档按名字
    docs.sort(key=lambda u: (_doc_rank(u['label']), u['label']))
    # 图片排序：跟着同名 .md 的顺序走（会话名相同就挨着），其余按名字
    md_order = {os.path.splitext(u['label'])[0]: i
                for i, u in enumerate(docs) if u['label'].lower().endswith('.md')}
    images.sort(key=lambda u: (md_order.get(u['group'], len(md_order)), u['label']))
    return docs + images


def expand_units(units):
    """把勾选的发送项展开成**有序的文件路径列表**：先所有文档，再所有图片。

    图片在这里是"按会话、按文件名"排好的；切批次时会跨会话凑满 limit 个，
    这正是用户要的「前一个群剩 10 张 + 下一个群 40 张」。
    """
    docs, imgs = [], []
    for u in units or []:
        (imgs if u.get('kind') == 'image' else docs).extend(u.get('paths') or [])
    return docs + imgs


def plan_batches(paths, limit=DEFAULT_LIMIT):
    """把文件列表切成每批 ≤limit 个。空输入返回 []，绝不产生空批次。"""
    limit = max(1, int(limit or DEFAULT_LIMIT))
    out = []
    seq = list(paths or [])
    for i in range(0, len(seq), limit):
        batch = seq[i:i + limit]
        if batch:
            out.append(batch)
    return out


def plan_from_units(units, limit=DEFAULT_LIMIT):
    """便捷入口：勾选项 → 批次列表。"""
    return plan_batches(expand_units(units), limit)


def find_oversize(paths, limit_bytes=MAX_FILE_BYTES):
    """挑出超过网页版单文件上限的文件，返回 [(路径, 字节数)]。"""
    bad = []
    for p in paths or []:
        try:
            n = os.path.getsize(p)
        except OSError:
            continue
        if n > limit_bytes:
            bad.append((p, n))
    return bad


def missing_files(paths):
    """挑出已经不在磁盘上的文件（导出目录被移走/改名时用）。"""
    return [p for p in (paths or []) if not os.path.isfile(p)]


def summarize(units, limit=DEFAULT_LIMIT):
    """给界面用的一句话摘要 + 批次预览（不读文件内容，只数数）。"""
    paths = expand_units(units)
    batches = plan_batches(paths, limit)
    docs = sum(u['count'] for u in (units or []) if u.get('kind') != 'image')
    imgs = sum(u['count'] for u in (units or []) if u.get('kind') == 'image')
    return {
        'files': len(paths),
        'batches': len(batches),
        'batch_sizes': [len(b) for b in batches],
        'images': imgs,
        'docs': docs,
    }
