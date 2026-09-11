# -*- coding: utf-8 -*-
"""统一消息解析器：把一个微信消息 dict 转成 (kind, 可读文本, 结构化附加信息)。

设计要点（依据对真实数据库的探测结论，见 tasks/spec.md 第 9 节）：

1. **判定 appmsg 看内容，不看 local_type。**
   上游实现用 `(local_type & 0xFFFFFFFF) == 49` 判定「这是 appmsg」，
   但真实数据里有 670/692 条 base=49 的消息其实是**纯文字**（拍一拍提示等），
   结果被上游导出器整条静默丢弃。这里改为检查内容里是否真的含 `<appmsg`。

2. **`message_content` 有三种形态，必须先归一化：**
   - 纯文本
   - 明文 XML（多数）
   - 十六进制 ZSTD（`28b52ffd` 魔数）——正常由 Node 桥接层解压，
     若仍拿到 hex（超长消息被桥接层放弃解压），只能降级为占位文本，
     绝不能把裸 hex 当正文导出。

3. **群聊消息可能带 `wxid_xxx:` 发送者前缀**，需剥离后再判断内容类型。

对外只暴露 `extract(msg)`。所有导出器都消费它的结果，
避免「6 个导出器各写一套类型判断、且互相不一致」的历史问题。
"""
import re

# ── local_type（去掉高位标志后的 base 值）→ 语义 ──
TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_CARD = 42
TYPE_VIDEO = 43
TYPE_STICKER = 47
TYPE_LOCATION = 48
TYPE_APPMSG = 49
TYPE_CALL = 50
TYPE_SYSTEM = 10000

# ── appmsg <type> → 语义 ──
APP_LINK = {'4', '5'}                      # 链接 / 分享
APP_FILE = {'6'}                           # 文件
APP_QUOTE = {'57'}                         # 引用回复
APP_CHATRECORD = {'19'}                    # 合并转发的聊天记录
APP_MINIPROGRAM = {'33', '36', '44'}       # 小程序
APP_SOLITAIRE = {'53'}                     # 群接龙
APP_GROUPANNOUNCE = {'87'}                 # 群公告

# 十六进制 ZSTD 魔数 0x28 0xB5 0x2F 0xFD（小端）
_ZSTD_MAGIC = '28b52ffd'
# 群聊消息前缀。发送者标识不一定是 wxid_ 开头：真实数据里见过
# 'wxid_a1b2c3d4e5:'、'12345678@chatroom:'、'SomeNickName:' 三种形态。
# 前缀的判定规则（两选一，且标识必须是纯 ASCII）：
#   A) 标识后紧跟 '<'  → 同一条消息里直接跟 XML，是确定无疑的结构性前缀；
#   B) 标识单独占一行（后面是换行）且标识长度 >= 6 → 短英文单词如 'time:'、'note:'
#      不会被误伤，而真实昵称/wxid 都够长。
_SENDER_PREFIX_XML = re.compile(r'^([A-Za-z0-9_\-@\.]{2,64}):[ \t]*(?=<)')
_SENDER_PREFIX_LINE = re.compile(r'^([A-Za-z0-9_\-@\.]{6,64}):[ \t]*\r?\n')


def base_type(local_type) -> int:
    """去掉高位标志位，得到基础消息类型。"""
    try:
        return int(local_type) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def normalize_content(msg: dict) -> str:
    """把 message_content 归一化成可解析的字符串（剥离发送者前缀、识别 ZSTD）。"""
    raw = msg.get('message_content')
    if raw is None:
        return ''
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', 'replace')
    text = str(raw).strip()
    # 先按「标识:<XML」剥一次，再按「标识单独成行」剥一次（最多一层）
    text = _SENDER_PREFIX_XML.sub('', text, count=1).strip()
    text = _SENDER_PREFIX_LINE.sub('', text, count=1).strip()
    return text


def is_zstd_hex(text: str) -> bool:
    """内容是不是未经解压的十六进制 ZSTD 数据。

    最小长度取 8(魔数)+8 字节，避免把一段以 '28b52ffd' 开头的短 hex 误判成压缩数据。
    """
    if not text or len(text) < 24:
        return False
    head = text[:64].lower()
    if not head.startswith(_ZSTD_MAGIC):
        return False
    if len(text) % 2:
        return False
    return all(c in '0123456789abcdef' for c in head)


# ────────────────────────── 小工具 ──────────────────────────

def _tag(xml: str, name: str) -> str:
    """取第一个 <name>...</name> 的文本（不处理嵌套同名标签）。"""
    m = re.search(r'<%s[^>]*>(.*?)</%s>' % (re.escape(name), re.escape(name)), xml, re.S)
    return m.group(1).strip() if m else ''


def _attr(xml: str, tag: str, attr: str) -> str:
    m = re.search(r'<%s\b[^>]*\b%s\s*=\s*"([^"]*)"' % (re.escape(tag), re.escape(attr)), xml)
    return m.group(1) if m else ''


def _unescape(text: str) -> str:
    """XML 实体反转义（只用标准库，避免 etree 对畸形 XML 抛异常）。"""
    import html as _html
    return _html.unescape(text)


def _clean(text: str) -> str:
    """清洗：反转义、去 CDATA 包裹、压缩空白行。"""
    if not text:
        return ''
    t = _unescape(text)
    t = re.sub(r'^\s*<!\[CDATA\[', '', t)
    t = re.sub(r'\]\]>\s*$', '', t)
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def _dur_ms(value: str) -> str:
    """把毫秒数格式化成 '4.6秒' / '1分20秒'。"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ''
    if ms < 0:
        return ''
    if ms < 1000:
        return f'{ms}毫秒'
    sec = ms / 1000.0
    if sec < 60:
        s = f'{sec:.1f}'.rstrip('0').rstrip('.')
        return f'{s}秒'
    m, s = divmod(int(round(sec)), 60)
    return f'{m}分{s}秒'


def _size(nbytes: str) -> str:
    try:
        n = int(nbytes)
    except (TypeError, ValueError):
        return ''
    for unit, div in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= div:
            return f'{n / div:.1f}{unit}'
    return f'{n}B'


def _result(kind, text, **extra):
    return {'kind': kind, 'text': text, 'extra': extra}


# ────────────────────────── XML 分支 ──────────────────────────

# sysmsgtemplate 的模板里带 $xxx$ 占位符，占位符内容在 <link_list> 的 <link name="xxx"> 里。
# 不替换的话导出的就是 '"$username$"邀请"$names$"加入了群聊' 这种对模型无意义的文本。
_TMPL_PLACEHOLDER = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)\$')
_LINK_BLOCK = re.compile(r'<link\b[^>]*\bname\s*=\s*"([^"]*)"[^>]*>(.*?)</link>', re.S)
_MEMBER_NICK = re.compile(r'<nickname>(.*?)</nickname>', re.S)
_MEMBER_NAME = re.compile(r'<username>(.*?)</username>', re.S)
_SEPARATOR = re.compile(r'<separator>(.*?)</separator>', re.S)


def _fill_template(tmpl: str, link_list_xml: str) -> str:
    """把模板里的 $key$ 占位符替换成 link_list 里的真实昵称。"""
    values = {}
    for name, body in _LINK_BLOCK.findall(link_list_xml):
        nicks = [_clean(x) for x in _MEMBER_NICK.findall(body)]
        nicks = [n for n in nicks if n]
        if not nicks:
            nicks = [n for n in (_clean(x) for x in _MEMBER_NAME.findall(body)) if n]
        sep_m = _SEPARATOR.search(body)
        sep = _clean(sep_m.group(1)) if sep_m else '、'
        values[name] = sep.join(nicks)

    def repl(m):
        return values.get(m.group(1), '')

    # 模板里常有连续多个换行/空白，替换后压一下
    filled = _TMPL_PLACEHOLDER.sub(repl, tmpl)
    filled = re.sub(r'[ \t]+\n', '\n', filled)
    return filled.strip()


def _parse_sysmsg(content: str):
    """系统消息（local_type 10000）。真实数据里有四种形态：

    1. 不带任何标签的纯文本，如「语音通话已经结束」——最容易漏，数量最多；
    2. <sysmsg type="revokemsg"> 撤回消息，正文在 <revokemsg><content>；
    3. <sysmsg type="sysmsgtemplate"> 入群/拍一拍等，需要替换 $xxx$ 占位符；
    4. <sysmsg type="paymsg"> 支付相关。
    """
    if not content.startswith('<'):
        text = _clean(content)
        return _result('system', text or '[系统消息]', subtype='plain')

    mtype = re.search(r'<sysmsg[^>]*\btype\s*=\s*"([^"]*)"', content)
    subtype = mtype.group(1) if mtype else ''

    # 2) sysmsgtemplate：优先用模板 + 占位符替换
    if subtype == 'sysmsgtemplate':
        tmpl_m = re.search(r'<template>(.*?)</template>', content, re.S)
        if tmpl_m:
            ll_m = re.search(r'<link_list>(.*?)</link_list>', content, re.S)
            filled = _fill_template(_clean(tmpl_m.group(1)),
                                    ll_m.group(1) if ll_m else '')
            if filled:
                return _result('system', filled, subtype=subtype)

    # 3) 撤回等：正文在 <content> 里
    inner = _tag(content, 'content')
    if inner:
        text = _clean(inner)
        if text:
            return _result('system', text, subtype=subtype)

    # 4) 其它结构兜底
    for name in ('plain', 'text', 'replacemsg', 'announcement', 'title'):
        val = _clean(_tag(content, name))
        if val:
            return _result('system', val, subtype=subtype)

    # 实在取不到正文时，保留原始 XML 的可见文本，至少让模型看到些东西
    stripped = _clean(re.sub(r'<[^>]+>', ' ', content))
    if stripped:
        return _result('system', stripped, subtype=subtype)
    return _result('system', f'[系统消息{f" {subtype}" if subtype else ""}]', subtype=subtype)


_RECORD_ITEM = re.compile(r'<dataitem\b([^>]*)>(.*?)</dataitem>', re.S)
_RECORD_DESC = re.compile(r'<datadesc>(.*?)</datadesc>', re.S)
_RECORD_TITLE = re.compile(r'<datatitle>(.*?)</datatitle>', re.S)
_RECORD_SRCNAME = re.compile(r'<srcname>(.*?)</srcname>', re.S)
_RECORD_NAME = re.compile(r'<dataname>(.*?)</dataname>', re.S)


def _parse_chatrecord(app: str, title: str) -> str:
    """合并转发的聊天记录：展开 datalist 里的每条消息。"""
    lines = [f'[聊天记录] {_clean(title)}' if title else '[聊天记录]']
    for attrs, body in _RECORD_ITEM.findall(app):
        desc = _RECORD_DESC.search(body) or _RECORD_TITLE.search(body)
        who = _RECORD_SRCNAME.search(body) or _RECORD_NAME.search(body)
        content = _clean(desc.group(1)) if desc else ''
        name = _clean(who.group(1)) if who else ''
        if not content:
            continue
        lines.append(f'  {name}: {content}' if name else f'  {content}')
    return '\n'.join(lines)


def _parse_appmsg(xml: str):
    """<appmsg> 分支，覆盖链接/文件/引用/聊天记录/小程序/接龙/群公告等。"""
    app_m = re.search(r'<appmsg\b.*?</appmsg>', xml, re.S)
    app = app_m.group(0) if app_m else xml
    title = _clean(_tag(app, 'title'))
    des = _clean(_tag(app, 'des'))
    url = _clean(_tag(app, 'url')) or _clean(_tag(app, 'lowurl'))
    atype = _tag(app, 'type')

    # 转账 / 红包：内容是 <![CDATA[微信转账]]> 这种，没有 title
    if '微信转账' in app:
        return _result('transfer', '[微信转账]')
    if '微信红包' in app:
        return _result('redpacket', '[微信红包]')

    if atype in APP_CHATRECORD:
        return _result('chatrecord', _parse_chatrecord(app, title), title=title)

    if atype in APP_QUOTE:
        ref_m = re.search(r'<refermsg>(.*?)</refermsg>', app, re.S)
        ref = ref_m.group(1) if ref_m else ''
        ref_who = _clean(_tag(ref, 'displayname')) or _clean(_tag(ref, 'chatusr'))
        ref_text = _clean(_tag(ref, 'content'))
        parts = []
        if ref_text:
            head = f'↩ 引用 {ref_who}：' if ref_who else '↩ 引用：'
            parts.append(head + ref_text.replace('\n', ' '))
        if title:
            parts.append(title)
        elif des:
            parts.append(des)
        text = '\n'.join(parts) or '[引用消息]'
        return _result('quote', text, quoted_from=ref_who, quoted_text=ref_text)

    if atype in APP_FILE:
        att = re.search(r'<appattach>(.*?)</appattach>', app, re.S)
        att = att.group(1) if att else ''
        fname = _clean(_tag(att, 'filename')) or title or '未知文件'
        size = _size(_tag(att, 'totallen'))
        head = f'[文件] {fname}'
        if size:
            head += f' ({size})'
        return _result('file', head, filename=fname, size=size)

    if atype in APP_MINIPROGRAM:
        name = title or des or '未知小程序'
        return _result('miniprogram', f'[小程序] {name}', title=name)

    if atype in APP_SOLITAIRE:
        return _result('solitaire', f'[接龙] {title}' if title else '[接龙]', title=title)

    if atype in APP_GROUPANNOUNCE:
        body = _clean(_tag(app, 'datadesc')) or des or title
        return _result('groupannounce', f'[群公告] {body}' if body else '[群公告]', title=title)

    if atype in APP_LINK:
        parts = ['[链接]']
        if title:
            parts.append(title)
        elif des:
            parts.append(des)
        if url:
            parts.append(url)
        return _result('link', '\n'.join(parts), title=title, url=url, des=des)

    # 其它 appmsg：有 title 就用，没有就退化成带类型编号的占位
    if title:
        kind = 'groupannounce' if atype in APP_GROUPANNOUNCE else 'app'
        text = title if not des or des in title else f'{title}\n{des}'
        return _result(kind, text, appmsg_type=atype)
    if des:
        return _result('app', des, appmsg_type=atype)
    return _result('app', f'[消息类型 {atype or "?"}]', appmsg_type=atype)


def _parse_image(xml, msg=None):
    return _result('image', '[图片]')


def _parse_voice(xml, msg=None):
    d = _dur_ms(_attr(xml, 'voicemsg', 'voicelength'))
    return _result('voice', f'[语音 {d}]' if d else '[语音]', duration=d)


def _parse_video(xml, msg=None):
    d = _attr(xml, 'videomsg', 'playlength')
    d = _dur_ms(str(int(d) * 1000)) if d and d.isdigit() else ''
    return _result('video', f'[视频 {d}]' if d else '[视频]', duration=d)


def _parse_card(xml, msg=None):
    """名片消息：昵称在 <msg nickname="..."> 属性里。

    注意第一个 <msg ...> 标签可能很长（含 bigheadimgurl 等），
    所以属性匹配用 [^>]* 跨整段标签，不能用非贪婪的 .*? 配 </msg>。
    """
    nick = _clean(_attr(xml, 'msg', 'nickname'))
    alias = _clean(_attr(xml, 'msg', 'alias'))
    return _result('card', f'[名片] {nick}' if nick else '[名片]', nickname=nick, alias=alias)


def _parse_location(xml, msg=None):
    poi = _clean(_attr(xml, 'location', 'poiname'))
    label = _clean(_attr(xml, 'location', 'label'))
    name = poi or label
    return _result('location', f'[位置] {name}' if name else '[位置]', poiname=poi, label=label)


def _parse_sticker(xml, msg=None):
    return _result('sticker', '[表情]')


def _parse_call(xml, msg=None):
    msg_m = re.search(r'<msg>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</msg>', xml, re.S)
    body = _clean(msg_m.group(1)) if msg_m else ''
    return _result('call', f'[通话] {body}' if body else '[通话]', detail=body)


def _parse_transfer_or_redpacket(xml, msg):
    if '微信转账' in xml:
        return _result('transfer', '[微信转账]')
    if '微信红包' in xml:
        return _result('redpacket', '[微信红包]')
    return None


def _parse_emoji_plain(xml, msg):
    return _result('sticker', '[表情]')


# ────────────────────────── 主入口 ──────────────────────────

def extract(msg: dict) -> dict:
    """把一个消息 dict 解析成 {'kind', 'text', 'extra'}。

    text 一定是可读文本（可能含换行），已剥离发送者前缀与 XML 实体，
    可以直接写进 TXT/CSV/Excel，HTML/PDF 侧只需再做一次转义。
    """
    if not isinstance(msg, dict):
        return _result('unknown', '')

    lt = base_type(msg.get('local_type'))
    content = normalize_content(msg)

    if not content:
        return _result('unknown', '')

    # 未解压的 ZSTD：绝不当正文导出
    if is_zstd_hex(content):
        return _result('unknown', f'[未解压消息 类型{lt}]', compressed=True)

    # 1) 系统消息。注意：local_type=10000 里有 6 成以上是不带任何 XML 的纯文本
    #    （如「语音通话已经结束」），必须先分流给 _parse_sysmsg，否则会被当成普通文字。
    if lt == TYPE_SYSTEM or content.startswith('<sysmsg'):
        return _parse_sysmsg(content)

    # 2) 通话（VOIP）
    if lt == TYPE_CALL or content.startswith('<voipmsg'):
        return _parse_call(content)

    # 3) 转账 / 红包（纯文本形式的 CDATA）
    plain = _clean(content)
    if plain in ('微信转账', '微信红包'):
        return _result('transfer' if plain == '微信转账' else 'redpacket', f'[{plain}]')
    # 真实数据里存在空内容的红包占位：'<![CDATA[]]>'（local_type 8594229559345）
    if not plain and 'CDATA' in content:
        return _result('redpacket', '[微信红包]', empty_payload=True)

    # 4) 图片
    if lt == TYPE_IMAGE:
        return _parse_image(content, msg)

    # 5) 其它 XML 形态的非 appmsg 类型。按「XML 特征标签」判定优先级高于 local_type，
    #    因为同一个 local_type 在不同微信版本下内容结构可能不同。
    if content.startswith('<'):
        if lt == TYPE_VOICE or '<voicemsg' in content:
            return _parse_voice(content, msg)
        if lt == TYPE_VIDEO or '<videomsg' in content:
            return _parse_video(content, msg)
        if lt == TYPE_CARD or 'bigheadimgurl' in content or 'smallheadimgurl' in content:
            return _parse_card(content, msg)
        if lt == TYPE_LOCATION or '<location' in content:
            return _parse_location(content, msg)
        if lt == TYPE_STICKER or '<emoji' in content:
            return _parse_sticker(content, msg)

    # 6) appmsg：判定看内容，不看 local_type（这是上游丢 670 条消息的根因）
    if '<appmsg' in content:
        return _parse_appmsg(content)

    # 7) 剩下的都是文字——包括 base=49 的拍一拍提示
    text = _clean(content)
    if not text:
        return _result('unknown', '')

    # 图片消息但没解析出 XML（只有占位）
    if lt == TYPE_IMAGE:
        return _result('image', '[图片]')

    kind = 'text'
    if lt not in (TYPE_TEXT, TYPE_APPMSG, 0):
        kind = 'unknown'
    return _result(kind, text, local_type=lt)


# ────────────────────────── 导出前处理 ──────────────────────────

def clean_wxid(wxid: str) -> str:
    """微信 v4 的 wxid 带设备后缀：wxid_xxx_xxxx → wxid_xxx。"""
    if not wxid:
        return ''
    parts = str(wxid).split('_')
    if len(parts) >= 3 and parts[0] == 'wxid':
        return '_'.join(parts[:2])
    return str(wxid)


def fmt_time(create_time) -> str:
    """时间戳 → 'YYYY-MM-DD HH:MM:SS'；非法值原样返回。"""
    s = str(create_time or '')
    if not s.isdigit():
        return s
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(s)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError, OverflowError):
        return s


def prepare(messages, nick_map=None, my_wxid='', my_name='我') -> list:
    """把原始消息列表加工成导出器统一消费的行列表。

    每个元素在原始 dict 基础上增加：
      time_str        'YYYY-MM-DD HH:MM:SS'
      sender_raw      原始发送者标识（wxid / 群 id），不丢原始信息
      sender_display  显示名（自己显示为 my_name）
      is_mine         1/0
      kind / text / extra   来自 extract()
      image_data/image_ext   由 media_resolver 预先注入（若有）

    顺带按时间升序排序，保证导出顺序正确。
    """
    nick_map = nick_map or {}
    me = clean_wxid(my_wxid) if my_wxid else ''
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        row = dict(m)
        sender = m.get('sender_username', '') or ''
        # 自己发的判定：直接比 wxid；群聊里发送者就是本人 wxid
        is_mine = 1 if (me and sender and clean_wxid(sender) == me) else 0
        row['is_mine'] = is_mine
        row['sender_raw'] = sender
        row['sender_display'] = my_name if is_mine else (nick_map.get(sender) or sender or '')
        row['sender_username'] = sender
        row['time_str'] = fmt_time(m.get('create_time'))
        parsed = extract(m)
        row['kind'] = parsed['kind']
        row['text'] = parsed['text']
        row['extra'] = parsed['extra']
        out.append(row)

    def sort_key(r):
        t = str(r.get('create_time') or '')
        return (0, int(t)) if t.isdigit() else (1, 0)

    out.sort(key=sort_key)
    return out


def kind_label(kind: str) -> str:
    """kind → 中文类型名，用于表格类导出的「类型」列。"""
    return {
        'text': '文字', 'image': '图片', 'voice': '语音', 'video': '视频',
        'file': '文件', 'link': '链接', 'miniprogram': '小程序', 'quote': '引用',
        'chatrecord': '聊天记录', 'location': '位置', 'card': '名片',
        'call': '通话', 'sticker': '表情', 'system': '系统消息',
        'transfer': '转账', 'redpacket': '红包', 'solitaire': '接龙',
        'groupannounce': '群公告', 'app': '分享', 'unknown': '未知',
    }.get(kind, '未知')
