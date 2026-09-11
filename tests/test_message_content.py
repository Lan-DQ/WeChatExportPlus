# -*- coding: utf-8 -*-
"""message_content.extract() 的行为测试。

fixtures/real_messages.json 是从**真实微信数据库**提取的一条代表性消息
（每种 local_type / appmsg type 各一条），见 tests/fixtures/README.md。
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

import message_content as mc  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'real_messages.json')


@pytest.fixture(scope='module')
def real():
    with open(FIXTURES, encoding='utf-8') as f:
        return json.load(f)


def make(local_type, content, **kw):
    d = {'local_type': local_type, 'message_content': content}
    d.update(kw)
    return d


# ─────────────────── 基础契约 ───────────────────

def test_extract_returns_contract():
    r = mc.extract(make(1, '你好'))
    assert set(r) == {'kind', 'text', 'extra'}
    assert r['kind'] == 'text'
    assert r['text'] == '你好'


def test_extract_tolerates_garbage():
    assert mc.extract(None)['text'] == ''
    assert mc.extract({})['text'] == ''
    assert mc.extract({'local_type': 1, 'message_content': None})['text'] == ''
    assert mc.extract({'local_type': 'x', 'message_content': 'hi'})['text'] == 'hi'


def test_base_type_masks_high_bits():
    assert mc.base_type(1) == 1
    assert mc.base_type(244813135921) == 49
    assert mc.base_type('244813135921') == 49
    assert mc.base_type(None) == 0
    assert mc.base_type('nonsense') == 0


# ─────────────────── 关键回归：不要丢消息 ───────────────────

def test_plaintext_with_appmsg_local_type_is_not_dropped(real):
    """真实数据里 670 条 base=49 的消息其实是纯文字（拍一拍等）。

    上游实现按 local_type==49 判定 appmsg，把它们全部当成无法识别的
    '[类型49]' 丢掉。这里必须按文字导出。
    """
    m = real['49_plaintext']
    r = mc.extract(m)
    assert r['kind'] == 'text'
    assert '拍了拍' in r['text']
    assert '类型' not in r['text']


def test_plaintext_appmsg_like_local_type_generic():
    r = mc.extract(make(266287972401, '"某人" 拍了拍 "某人"'))
    assert r['kind'] == 'text'
    assert r['text'] == '"某人" 拍了拍 "某人"'


# ─────────────────── 发送者前缀 ───────────────────

def test_strips_wxid_sender_prefix():
    r = mc.extract(make(1, 'wxid_testuser01:\n你好呀'))
    assert r['text'] == '你好呀'


def test_strips_chatroom_sender_prefix():
    r = mc.extract(make(1, '12345678@chatroom:\nhello'))
    assert r['text'] == 'hello'


def test_strips_non_wxid_sender_prefix(real):
    """真实群聊里发送者标识不一定是 wxid_ 开头（如 'SomeNickName:'）。"""
    r = mc.extract(real['42'])
    assert r['kind'] == 'card'
    assert r['text'] == '[名片] 某单位'


def test_does_not_strip_colon_inside_text():
    r = mc.extract(make(1, 'time: 10:30'))
    assert r['text'] == 'time: 10:30'


def test_does_not_strip_short_word_with_colon():
    r = mc.extract(make(1, 'note:\nhello'))
    assert r['text'] == 'note:\nhello'


def test_does_not_strip_chinese_sender_with_colon():
    """中文昵称后跟冒号的正文不能被误当成发送者前缀剥掉。"""
    r = mc.extract(make(1, '张三：今天开会'))
    assert r['text'] == '张三：今天开会'


# ─────────────────── 文字类（真实 fixture） ───────────────────

def test_real_text_1(real):
    r = mc.extract(real['1'])
    assert r['kind'] == 'text'
    assert r['text'] == '[表情]'


# ─────────────────── 图片 / 语音 / 视频 / 表情 / 位置 / 名片 ───────────────────

def test_real_image(real):
    r = mc.extract(real['3'])
    assert r['kind'] == 'image'
    assert r['text'] == '[图片]'


def test_real_voice_has_duration(real):
    r = mc.extract(real['34'])
    assert r['kind'] == 'voice'
    assert r['text'].startswith('[语音')
    assert '秒' in r['text']


def test_real_video(real):
    r = mc.extract(real['43'])
    assert r['kind'] == 'video'
    assert r['text'].startswith('[视频')


def test_real_sticker(real):
    r = mc.extract(real['47'])
    assert r['kind'] == 'sticker'
    assert r['text'] == '[表情]'


def test_real_location_has_poi(real):
    r = mc.extract(real['48'])
    assert r['kind'] == 'location'
    assert r['text'].startswith('[位置]')
    assert '某机场航站楼' in r['text']


def test_real_card_has_nickname(real):
    r = mc.extract(real['42'])
    assert r['kind'] == 'card'
    assert r['text'].startswith('[名片]')


def test_voice_duration_formats():
    assert mc._dur_ms('4601') == '4.6秒'
    assert mc._dur_ms('1000') == '1秒'
    assert mc._dur_ms('80000') == '1分20秒'
    assert mc._dur_ms('500') == '500毫秒'
    assert mc._dur_ms('') == ''
    assert mc._dur_ms('abc') == ''


# ─────────────────── appmsg 分支 ───────────────────

def test_real_solitaire(real):
    r = mc.extract(real['49_t53'])
    assert r['kind'] == 'solitaire'
    assert r['text'].startswith('[接龙]')
    assert '接龙' in r['text']


def test_real_quote_shows_quoted_content(real):
    r = mc.extract(real['49_t57'])
    assert r['kind'] == 'quote'
    assert '↩ 引用' in r['text']
    assert '某句话' in r['text']


def test_real_group_announce(real):
    r = mc.extract(real['49_t87'])
    assert r['kind'] == 'groupannounce'
    assert r['text'].startswith('[群公告]')
    assert '某通知' in r['text']


def test_link_extracts_title_and_url():
    xml = ('<msg><appmsg><title>一篇好文章</title><des>摘要</des>'
           '<type>5</type><url>https://example.com/a?x=1&amp;y=2</url></appmsg></msg>')
    r = mc.extract(make(49, xml))
    assert r['kind'] == 'link'
    assert '一篇好文章' in r['text']
    assert 'https://example.com/a?x=1&y=2' in r['text']


def test_file_extracts_name_and_size():
    xml = ('<msg><appmsg><title>报告.pdf</title><type>6</type>'
           '<appattach><filename>报告.pdf</filename><totallen>2097152</totallen></appattach>'
           '</appmsg></msg>')
    r = mc.extract(make(49, xml))
    assert r['kind'] == 'file'
    assert '报告.pdf' in r['text']
    assert '2.0MB' in r['text']


def test_miniprogram():
    xml = '<msg><appmsg><title>某小程序</title><type>33</type></appmsg></msg>'
    r = mc.extract(make(49, xml))
    assert r['kind'] == 'miniprogram'
    assert r['text'] == '[小程序] 某小程序'


def test_chatrecord_expands_items():
    xml = ('<msg><appmsg><title>群聊的聊天记录</title><type>19</type><appattach>'
           '<datalist count="2">'
           '<dataitem datatype="1"><srcname>张三</srcname><datadesc>第一条</datadesc></dataitem>'
           '<dataitem datatype="1"><srcname>李四</srcname><datadesc>第二条</datadesc></dataitem>'
           '</datalist></appattach></appmsg></msg>')
    r = mc.extract(make(49, xml))
    assert r['kind'] == 'chatrecord'
    assert '群聊的聊天记录' in r['text']
    assert '张三: 第一条' in r['text']
    assert '李四: 第二条' in r['text']


def test_transfer_and_redpacket():
    assert mc.extract(make(8589934592049, '<![CDATA[微信转账]]>'))['kind'] == 'transfer'
    assert mc.extract(make(8594229559345, '<![CDATA[微信红包]]>'))['kind'] == 'redpacket'
    xml = '<msg><appmsg><type>2000</type><wcpayinfo>微信转账</wcpayinfo></appmsg></msg>'
    assert mc.extract(make(49, xml))['kind'] == 'transfer'


def test_empty_redpacket_placeholder_is_not_empty_text():
    """真实数据里存在空内容的红包占位 '<![CDATA[]]>'，不能产出空文本。"""
    r = mc.extract(make(8594229559345, '<![CDATA[]]>'))
    assert r['text'] == '[微信红包]'
    assert r['kind'] == 'redpacket'


# ─────────────────── 系统消息 / 通话 ───────────────────

def test_real_system_revoke(real):
    r = mc.extract(real['10000'])
    assert r['kind'] == 'system'
    assert '撤回了一条消息' in r['text']
    assert 'sysmsg' not in r['text']


def test_real_call_duration(real):
    r = mc.extract(real['50'])
    assert r['kind'] == 'call'
    assert '通话时长 26:21' in r['text']


# ─────────────────── 系统消息的四种真实形态 ───────────────────

def test_sysmsg_plain_text_without_any_xml():
    """真实数据里 6 成以上系统消息是纯文本、完全不带 XML，不能退化成 [系统消息]。"""
    r = mc.extract(make(10000, '语音通话已经结束'))
    assert r['kind'] == 'system'
    assert r['text'] == '语音通话已经结束'


def test_sysmsg_template_placeholders_are_filled():
    """sysmsgtemplate 的 $username$/$names$ 必须替换成真实昵称。"""
    content = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile">'
        '<plain><![CDATA[]]></plain>'
        '<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template>'
        '<link_list>'
        '<link name="username" type="link_profile"><memberlist><member>'
        '<username><![CDATA[wxid_a]]></username><nickname><![CDATA[张三]]></nickname>'
        '</member></memberlist></link>'
        '<link name="names" type="link_profile"><memberlist><member>'
        '<username><![CDATA[wxid_b]]></username><nickname><![CDATA[李四]]></nickname>'
        '</member></memberlist><separator><![CDATA[、]]></separator></link>'
        '</link_list></content_template></sysmsgtemplate></sysmsg>')
    r = mc.extract(make(10000, content))
    assert r['kind'] == 'system'
    assert r['text'] == '"张三"邀请"李四"加入了群聊'
    assert '$' not in r['text']


def test_sysmsg_multiple_names_joined_by_separator():
    content = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        '<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template>'
        '<link_list>'
        '<link name="username"><memberlist><member><nickname><![CDATA[A]]></nickname>'
        '</member></memberlist></link>'
        '<link name="names"><memberlist>'
        '<member><nickname><![CDATA[B]]></nickname></member>'
        '<member><nickname><![CDATA[C]]></nickname></member>'
        '</memberlist><separator><![CDATA[、]]></separator></link>'
        '</link_list></content_template></sysmsgtemplate></sysmsg>')
    r = mc.extract(make(10000, content))
    assert r['text'] == '"A"邀请"B、C"加入了群聊'


def test_sysmsg_paymsg_keeps_payload():
    content = ('<sysmsg type="paymsg"><paymsg><content><![CDATA[你已支付¥10.00]]>'
               '</content></paymsg></sysmsg>')
    r = mc.extract(make(10000, content))
    assert r['kind'] == 'system'
    assert '支付' in r['text']


def test_sysmsg_unknown_structure_falls_back_to_visible_text():
    content = '<sysmsg type="weird"><weird><foo>有点内容</foo></weird></sysmsg>'
    r = mc.extract(make(10000, content))
    assert '有点内容' in r['text']


# ─────────────────── ZSTD / 异常输入 ───────────────────

def test_zstd_hex_is_not_exported_as_body():
    hexz = '28b52ffd' + 'ab' * 200
    r = mc.extract(make(49, hexz))
    assert hexz[:16] not in r['text']
    assert r['extra'].get('compressed') is True
    assert '未解压' in r['text']


def test_is_zstd_hex():
    assert mc.is_zstd_hex('28b52ffd60e32d65' + 'a' * 40)
    assert not mc.is_zstd_hex('28b52ffd')
    assert not mc.is_zstd_hex('28b52ffd60e3')
    assert not mc.is_zstd_hex('hello world this is not zstd')
    assert not mc.is_zstd_hex('')
    assert not mc.is_zstd_hex('28b52ffdZZZZ' + 'a' * 20)
    assert not mc.is_zstd_hex('28b52ffd' + 'a' * 41)  # 奇数长度不是合法 hex


def test_no_fixture_message_falls_back_to_bare_type_placeholder(real):
    """所有真实 fixture 都不应再产出上游那种 '[类型N]' 占位符。"""
    import re
    for key, m in real.items():
        r = mc.extract(m)
        assert r['text'], f'{key} produced empty text'
        assert not re.match(r'^\[类型\d+\]$', r['text']), f'{key} -> {r["text"]}'


def test_every_real_fixture_has_known_kind(real):
    known = {'text', 'image', 'voice', 'video', 'file', 'link', 'miniprogram',
             'quote', 'chatrecord', 'location', 'card', 'call', 'sticker',
             'system', 'transfer', 'redpacket', 'solitaire', 'groupannounce',
             'app', 'unknown'}
    for key, m in real.items():
        assert mc.extract(m)['kind'] in known, key
