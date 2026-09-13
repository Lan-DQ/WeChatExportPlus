# -*- coding: utf-8 -*-
r"""批量导出编排：多个会话 → 一个带时间戳的总目录。

产出结构（与用户确认过的契约，见 tasks/spec.md 第 8 节）：

    <用户选择的总目录>\
    └── 导出_20260213_1430\
        ├── 导出清单.html          ← 总索引，可点击跳转
        ├── 群聊A\                 ← 会话名，同名直接覆盖
        │   ├── index.html         （HTML 格式）
        │   ├── 对话.jsonl / 对话.txt / 群聊A.xlsx …（按所选格式）
        │   └── 图片\<md5>.jpg
        └── 私聊B\
            └── ...

设计原则：
  - 与 GUI 解耦：本模块不认识 tkinter，只通过 progress/should_cancel 回调交互，
    因此可以直接在命令行/测试里调用。
  - 单个会话失败不影响其余会话，错误汇总在结果里返回。
  - 取消是「软取消」：处理完当前会话后停下，不留半个文件。
"""
import datetime
import os
import re
import shutil

import message_content

# 需要把图片解密落盘的格式：
#   html 用 <img src="图片/...">，ai 用 image 字段给出相对路径（供多模态模型取图）。
#   pdf 把图片内嵌进单个文件，不需要也不应该再留一份 图片/ 目录。
# 需要把图片解密落盘的格式：
#   html 用 <img src="图片/...">，md 用序号文件（01.jpg）供网页版一起拖进去，
#   ai 用 image 字段给出相对路径（供多模态模型取图）。
#   pdf 把图片内嵌进单个文件，不需要也不应该再留一份 图片/ 目录。
IMAGE_FORMATS = {'html', 'ai', 'md'}

# 支持的导出格式：key 与 GUI 下拉框的值一致
FORMATS = ['md', 'ai', 'html', 'pdf', 'txt', 'json', 'csv', 'excel']

# 非 HTML 格式导出的主文件名（HTML 固定用 index.html）
_MAIN_FILENAME = {
    'md': None,            # md 由 md_exporter 写 <会话名>.md，直接放根目录
    'ai': None,            # ai 由 ai_exporter 自己写 对话.jsonl / 对话.txt
    'txt': '对话.txt',
    'json': '对话.json',
    'csv': '对话.csv',
    'excel': None,         # <会话名>.xlsx
    'pdf': None,           # <会话名>.pdf
}

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class Cancelled(Exception):
    """已请求取消。"""


def timestamp_dir_name(now=None):
    now = now or datetime.datetime.now()
    return f'导出_{now.strftime("%Y%m%d_%H%M")}'


# 每次导出产生的东西，用于「覆盖上次导出」时精确清理。
# 只认这两种形态，别的文件一律不碰 —— 用户可能把导出目录选在桌面或某个有用目录，
# 清空整个目录是不可接受的行为。
_EXPORT_DIR_RE = re.compile(r'^导出_\d{8}_\d{4}(_\d+)?$')
_EXPORT_FILES = {'导出清单.html', '给AI的指令.txt'}


def scan_previous_exports(out_root):
    """列出会被 clear_previous_exports 删掉的内容（覆盖前给用户看，便于确认）。"""
    empty = {'dirs': [], 'files': [], 'bytes': 0}
    if not out_root or not os.path.isdir(out_root):
        return empty
    dirs, files, total = [], [], 0
    try:
        names = os.listdir(out_root)
    except OSError:
        return empty
    for name in names:
        p = os.path.join(out_root, name)
        try:
            if os.path.isdir(p) and _EXPORT_DIR_RE.match(name):
                dirs.append(name)
                for dp, _dn, fns in os.walk(p):
                    for f in fns:
                        try:
                            total += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
            elif os.path.isfile(p) and name in _EXPORT_FILES:
                files.append(name)
                total += os.path.getsize(p)
        except OSError:
            pass
    return {'dirs': dirs, 'files': files, 'bytes': total}


def clear_previous_exports(out_root, log=None):
    """删除 out_root 里**上一次导出产生的内容**，只删本工具自己生成的。

    返回 (删除的目录数, 删除的文件数)。
    """
    log = log or (lambda s: None)
    if not out_root or not os.path.isdir(out_root):
        return 0, 0
    nd = nf = 0
    try:
        names = os.listdir(out_root)
    except OSError as e:
        log(f'覆盖前清理失败（读目录）：{e}')
        return 0, 0
    for name in names:
        p = os.path.join(out_root, name)
        try:
            if os.path.isdir(p) and _EXPORT_DIR_RE.match(name):
                shutil.rmtree(p, ignore_errors=True)
                if not os.path.exists(p):
                    nd += 1
                    log(f'  已删除旧导出目录：{name}')
            elif os.path.isfile(p) and name in _EXPORT_FILES:
                os.remove(p)
                nf += 1
                log(f'  已删除旧文件：{name}')
        except OSError as e:
            log(f'  删除失败 {name}：{e}')
    return nd, nf


def safe_name(name, fallback='未命名'):
    """清洗成合法的 Windows 文件夹/文件名。"""
    s = _ILLEGAL.sub('_', str(name or '')).strip()
    s = s.rstrip('. ')
    s = re.sub(r'\s+', ' ', s)
    if not s:
        s = str(fallback) or '未命名'
    # 保留名规避
    if s.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'COM\d|LPT\d', s.upper()):
        s = '_' + s
    return s[:80]


def unique_dir(parent, name):
    """在 parent 下取一个不冲突的目录名（name、name_2、name_3…）。"""
    if not os.path.exists(os.path.join(parent, name)):
        return name
    i = 2
    while os.path.exists(os.path.join(parent, f'{name}_{i}')):
        i += 1
    return f'{name}_{i}'


def unique_base(parent, name, ext='.md'):
    """给「直接放在 parent 下的目标文件」取一个不冲突的主名。

    与 unique_dir 的区别：这里要同时避开**已存在的同名文件**和已存在的同名目录
    （md 格式的图片目录叫 `<名>_图片`，所以主名冲突会连带图片目录冲突）。
    """
    if not os.path.exists(os.path.join(parent, name + ext)) \
            and not os.path.exists(os.path.join(parent, name)):
        return name
    i = 2
    while (os.path.exists(os.path.join(parent, f'{name}_{i}{ext}'))
           or os.path.exists(os.path.join(parent, f'{name}_{i}'))):
        i += 1
    return f'{name}_{i}'


def resolve_my_wxid(data_dir):
    """从微信数据目录里推断当前登录账号的 wxid。"""
    if not data_dir or not os.path.isdir(data_dir):
        return ''
    try:
        for entry in os.listdir(data_dir):
            if entry.startswith('wxid_') and os.path.isdir(os.path.join(data_dir, entry)):
                return message_content.clean_wxid(entry)
    except OSError:
        pass
    return ''


def _prompt_header(prompt, comment=False):
    """把「给 AI 的指令」变成文件头。comment=True 时用 # 注释（给 .txt/.jsonl 这类）。"""
    text = (prompt or '').strip()
    if not text:
        return ''
    lines = ['===== 给 AI 的指令（以下内容是对你的要求，不是聊天记录） =====', '']
    lines.extend(text.split('\n'))
    lines.append('')
    lines.append('===== 以下是聊天记录正文 =====')
    lines.append('')
    body = '\n'.join(lines)
    if comment:
        return '\n'.join(('# ' + l).rstrip() for l in body.split('\n'))
    return body


def _prompt_block_md(prompt):
    import ai_prompt
    return ai_prompt.as_markdown(prompt)


def _write_txt(rows, path, prompt=None):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        head = _prompt_header(prompt, comment=False)
        if head:
            f.write(head + '\n')
        for m in rows:
            text = (m.get('text') or '').strip()
            if not text:
                continue
            f.write(f"[{m.get('time_str', '')}] {m.get('sender_display', '')}\n{text}\n\n")
    return path


def _write_json(rows, path, prompt=None):
    import json
    clean = []
    for m in rows:
        d = {k: v for k, v in m.items() if not callable(v)}
        d.pop('image_data', None)      # base64 会把 json 撑爆，图片走文件
        clean.append(d)
    payload = clean
    if prompt:
        # 结构化格式用一个独立字段承载指令，不污染消息数组
        payload = {'_ai_instruction': prompt.strip(), 'messages': clean}
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


def _export_one(fmt, rows, chat_dir, chat, prompt=None):
    """按格式把单个会话写到 chat_dir，返回 {'main': 主文件名, 'files': [...]}。

    prompt 非空时写进产物开头（Markdown 用 ## 区块，表格类用列/注释）。
    """
    title = chat.get('title') or chat.get('wxid') or 'chat'
    fname = safe_name(title, chat.get('wxid', 'chat'))

    if fmt == 'ai':
        import ai_exporter
        out = ai_exporter.export(rows, chat_dir, chat, prompt=prompt)
        return {'main': os.path.relpath(out['txt'], chat_dir).replace('\\', '/'),
                'files': [os.path.relpath(p, chat_dir).replace('\\', '/') for p in out.values()]}

    if fmt == 'html':
        import html_exporter
        img_dir = os.path.join(chat_dir, '图片')
        p = html_exporter.export(rows, os.path.join(chat_dir, 'index.html'),
                                 '我', title, img_dir=img_dir, prompt=prompt)
        return {'main': 'index.html', 'files': ['index.html']}

    if fmt == 'txt':
        p = _write_txt(rows, os.path.join(chat_dir, _MAIN_FILENAME['txt']), prompt=prompt)
        return {'main': _MAIN_FILENAME['txt'], 'files': [_MAIN_FILENAME['txt']]}

    if fmt == 'json':
        p = _write_json(rows, os.path.join(chat_dir, _MAIN_FILENAME['json']), prompt=prompt)
        return {'main': _MAIN_FILENAME['json'], 'files': [_MAIN_FILENAME['json']]}

    if fmt == 'csv':
        import csv_exporter
        name = _MAIN_FILENAME['csv']
        csv_exporter.export(rows, os.path.join(chat_dir, name), '我', title, prompt=prompt)
        return {'main': name, 'files': [name]}

    if fmt == 'excel':
        import excel_exporter
        name = f'{fname}.xlsx'
        excel_exporter.export(rows, os.path.join(chat_dir, name), '我', title, prompt=prompt)
        return {'main': name, 'files': [name]}

    if fmt == 'pdf':
        import pdf_exporter
        name = f'{fname}.pdf'
        pdf_exporter.export(rows, os.path.join(chat_dir, name), '我', title, prompt=prompt)
        return {'main': name, 'files': [name]}

    raise ValueError(f'不支持的格式: {fmt}')


def _resolve_images(wcdb, data_dir, rows, chat_dir, session_wxid, log=None, img_holder=None):
    """把会话里的图片解密到 `img_holder/图片/`（默认 chat_dir），返回 (rows, 成功张数)。

    图片文件名沿用 media_resolver 生成的 md5 名，便于去重与复用。
    """
    holder = img_holder or chat_dir
    img_dir = os.path.join(holder, '图片')

    # 先看会话里有没有图片，没有就完全跳过（省掉一次 resolver 构造）
    if not any(m.get('kind') == 'image' for m in rows):
        return rows, 0

    try:
        from media_resolver import MediaResolver
    except Exception as e:
        if log:
            log(f'  图片模块不可用: {e}')
        return rows, 0

    os.makedirs(img_dir, exist_ok=True)
    cache = os.path.join(holder, '.imgcache')
    resolver = MediaResolver(wcdb, cache, 0, '', data_dir,
                             log_func=lambda s: log('  ' + str(s)) if log else None)
    try:
        rows = resolver.resolve_images(rows, session_wxid, try_native=True)
    except Exception as e:
        if log:
            log(f'  图片解析异常: {e}')

    saved = 0
    try:
        from packed_info_parser import parse_image_info
    except Exception:
        parse_image_info = None

    for m in rows:
        data = m.get('image_data')
        if not data:
            continue
        ext = (m.get('image_ext') or 'jpg').lstrip('.')
        # resolver 落盘用的就是「图片 md5 + 后缀」，这里用同一套来源算出同名文件，
        # 直接把它从缓存搬到 图片/，避免二次解码和名字漂移。
        name = ''
        if parse_image_info:
            try:
                info = parse_image_info(m)
                base = info.get('md5') or info.get('alt_md5') or ''
                if base:
                    name = f'{base}.{ext}'
            except Exception:
                pass
        if not name:
            name = f"{m.get('local_id', saved)}.{ext}"

        src = _find_cached(cache, name)
        dst = os.path.join(img_dir, name)
        try:
            if src:
                shutil.copyfile(src, dst)
            else:
                import base64
                with open(dst, 'wb') as f:
                    f.write(base64.b64decode(data))
            m['image_file'] = name
            m.pop('image_data', None)     # 已落盘，没必要再带着 base64
            saved += 1
        except Exception as e:
            if log:
                log(f'  图片写入失败 {name}: {e}')
            m['image_file'] = ''

    # 清掉 resolver 的中间缓存目录，只留 图片/
    shutil.rmtree(cache, ignore_errors=True)
    return rows, saved


def _find_cached(cache_dir, name):
    for root, _dirs, files in os.walk(cache_dir):
        if name in files:
            return os.path.join(root, name)
    return ''


def export_sessions(wcdb, data_dir, sessions, fmt, out_root,
                    progress=None, should_cancel=None, log=None,
                    resolve_images=True, limit=0, base_dir=None, prompt=None,
                    clear_before=False):
    """批量导出多个会话。

    参数
    ----
    wcdb          : WCDBClient 实例（已 start）
    data_dir      : 微信数据目录（用于找图片，可为空）
    sessions      : [{'wxid':..., 'title':...}, ...]
    fmt           : FORMATS 之一
    out_root      : 用户选择的总目录；本函数会在其下新建「导出_时间戳」文件夹
    progress      : progress(done, total, title) 回调，每开始一个会话调用
    should_cancel : 返回 True 时在会话边界停下
    log           : 单行日志回调
    resolve_images: 是否为 html/pdf 解密图片
    base_dir      : 程序目录，用于读取 AI提示词.txt（不传则用内置默认提示词）
    prompt        : 直接指定提示词；None 表示从 base_dir 读取
    clear_before  : True 时先删掉 out_root 里上一次导出的内容（只删本工具生成的
                    「导出_日期_时间」目录与两个索引文件，其它文件不动）

    返回
    ----
    {'root': 总目录, 'ok': [...], 'failed': [...], 'cancelled': bool,
     'total': n, 'cleared': (目录数, 文件数)}
    """
    if fmt not in FORMATS:
        raise ValueError(f'不支持的格式: {fmt}')

    log = log or (lambda s: None)
    os.makedirs(out_root, exist_ok=True)

    cleared = (0, 0)
    if clear_before:
        log('覆盖模式：清理上一次导出…')
        cleared = clear_previous_exports(out_root, log)
        if cleared == (0, 0):
            log('  没有需要清理的旧导出')

    root = os.path.join(out_root, timestamp_dir_name())
    # 同名（同一分钟重复导出）不覆盖整个目录，追加序号
    root = os.path.join(out_root, unique_dir(out_root, os.path.basename(root)))
    os.makedirs(root, exist_ok=True)

    # 每个导出都带上「给 AI 的指令」
    if prompt is None:
        try:
            import ai_prompt
            prompt = ai_prompt.load_prompt(base_dir or os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        except Exception:
            prompt = ''

    my_wxid = resolve_my_wxid(data_dir)
    total = len(sessions)
    ok, failed = [], []
    cancelled = False
    used_names = set()

    log(f'导出目录: {root}')
    log(f'格式: {fmt}  会话数: {total}')
    if prompt:
        log('已附加「给 AI 的指令」')

    for idx, s in enumerate(sessions, start=1):
        if should_cancel and should_cancel():
            cancelled = True
            log('已取消')
            break

        wxid = s.get('wxid') or ''
        title = s.get('title') or wxid
        if progress:
            progress(idx, total, title)
        log(f'[{idx}/{total}] {title}')

        try:
            raw = wcdb.get_messages(wxid, limit, 0)
            if not raw:
                failed.append({'wxid': wxid, 'title': title, 'error': '没有消息'})
                log('  跳过：没有消息')
                continue

            # 昵称映射：群聊里每条消息都带 sender_username，逐个解析成显示名
            senders = list({m.get('sender_username', '') for m in raw
                            if m.get('sender_username')})
            if wxid not in senders:
                senders.append(wxid)
            nick = {}
            try:
                nick = wcdb.get_display_names(senders) or {}
            except Exception as e:
                log(f'  昵称解析失败（用 wxid 代替）: {e}')

            rows = message_content.prepare(raw, nick, my_wxid, '我')
            for m in rows:
                m['_chat_wxid'] = wxid

            is_group = str(wxid).endswith('@chatroom')
            chat = {'wxid': wxid, 'title': title, 'is_group': is_group}

            res = None
            if fmt == 'md':
                # 单文件可读格式：目标文件直接放在导出根目录下（不套会话子文件夹），
                # 因为它的用途是「拖进 DeepSeek 网页版」，多一层文件夹反而碍事。
                import md_exporter
                base = safe_name(title, wxid)
                # md 目标文件直接落在 root 下，必须避开同名文件和同名「_图片」目录
                base = unique_base(root, base)
                used_names.add(base)

                # 图片先解密到临时目录（md_exporter 会按出现顺序改名成 01.jpg 02.jpg）
                tmp = os.path.join(root, f'.md_tmp_{base}')
                os.makedirs(tmp, exist_ok=True)
                n_img = 0
                if resolve_images:
                    rows, n_img = _resolve_images(wcdb, data_dir, rows, root, wxid, log,
                                                  img_holder=tmp)
                out = md_exporter.export(
                    rows, root, {'wxid': wxid, 'title': base, 'is_group': is_group},
                    image_map=md_exporter.build_image_map(rows),
                    image_src_dir=os.path.join(tmp, '图片'),
                    # 故意不传 prompt：会话很多时，每个 .md 里都塞一份「给 AI 的指令」
                    # 纯属重复浪费（正文可能才几 KB，指令就占 1 KB）。
                    # 指令改为整个导出目录只留一份 给AI的指令.txt（见函数末尾）。
                    prompt=None)
                shutil.rmtree(tmp, ignore_errors=True)
                n_img = out['images']
                ok.append({'wxid': wxid, 'title': title, 'dir': base,
                           'main': os.path.basename(out['md']),
                           'count': len(rows), 'images': n_img})
                log(f'  完成 {len(rows)} 条' + (f'，图片 {n_img} 张' if n_img else ''))
                continue

            # 其余格式：一个会话一个子文件夹（用会话名），同名直接覆盖
            dir_name = safe_name(title, wxid)
            if dir_name in used_names:
                # 同一批里出现重名会话：加序号，避免互相覆盖导致丢数据
                dir_name = unique_dir(root, dir_name)
            used_names.add(dir_name)
            chat_dir = os.path.join(root, dir_name)

            if os.path.isdir(chat_dir):
                shutil.rmtree(chat_dir, ignore_errors=True)
            os.makedirs(chat_dir, exist_ok=True)

            n_img = 0
            if resolve_images and fmt in IMAGE_FORMATS:
                rows, n_img = _resolve_images(wcdb, data_dir, rows, chat_dir, wxid, log)

            res = _export_one(fmt, rows, chat_dir, chat, prompt=prompt)
            ok.append({'wxid': wxid, 'title': title, 'dir': dir_name,
                       'main': res['main'], 'count': len(rows), 'images': n_img})
            log(f'  完成 {len(rows)} 条' + (f'，图片 {n_img} 张' if n_img else ''))
        except Exception as e:
            import traceback
            failed.append({'wxid': wxid, 'title': title, 'error': str(e)})
            log(f'  失败: {e}')
            log('  ' + traceback.format_exc().splitlines()[-1])

    # 总索引页
    try:
        import index_exporter
        index_exporter.export(root, ok, fmt, cancelled=cancelled, prompt=prompt)
    except Exception as e:
        log(f'索引页生成失败: {e}')

    # 整个导出目录只放这一份「给 AI 的指令」（md 格式不再逐个文件重复夹带）。
    # 注意：这个文件必须在 index_exporter 之后写，因为索引页里也会引用它。
    if prompt:
        try:
            with open(os.path.join(root, '给AI的指令.txt'), 'w',
                      encoding='utf-8', newline='\n') as f:
                f.write(prompt.strip() + '\n')
        except OSError:
            pass

    return {'root': root, 'ok': ok, 'failed': failed,
            'cancelled': cancelled, 'total': total, 'format': fmt,
            'cleared': cleared}
