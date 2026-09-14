# -*- coding: utf-8 -*-
"""界面不变式测试：防止"背景丢失"和"按钮点不动"这两类回归。

每个问题都是真实发生过的，用断言把正确状态固定下来。
"""
import sys
import time
import tkinter as tk

import pytest

sys.path.insert(0, r'C:\My_GongJu\grab\WeChatExportPlus\gui')
sys.path.insert(0, r'C:\My_GongJu\grab\WeChatExportPlus\exporters')
sys.path.insert(0, r'C:\My_GongJu\grab\WeChatExportPlus\scripts')


class Ev:
    """模拟 tk 事件。

    ⚠️ type 必须用真实的 tkinter.EventType 枚举，不能用裸 int。
    事故：tkinter 8.6 的 event.type 是 EventType 枚举成员，而
    `EventType.ButtonPress == 4` 是 **False**，用它查以 int 为键的字典
    永远查不到 —— 所有事件被静默丢弃，表现就是"所有按钮都点不了"。
    早期测试里传的是 int，所以测试全绿而真实运行全坏。
    """

    def __init__(self, t, x, y, delta=0):
        self.type = t
        self.x, self.y = x, y
        self.delta = delta


def _mk(kind, x, y, delta=0):
    """用真实的 EventType 构造事件。kind: 'press'|'release'|'motion'|'leave'"""
    from tkinter import EventType
    t = {'press': EventType.ButtonPress,
         'release': EventType.ButtonRelease,
         'motion': EventType.Motion,
         'leave': EventType.Leave}[kind]
    return Ev(t, x, y, delta)


def _wheel(x, y, delta):
    """构造真实的滚轮事件（EventType.MouseWheel = 38）。"""
    from tkinter import EventType
    return Ev(EventType.MouseWheel, x, y, delta)


@pytest.fixture
def sessions(app):
    """进入会话页，并保证每次都是干净状态。

    ⚠️ 只共享一个 Tk 根窗口（Tk 每进程只能有一个），但页面状态必须每个
    用例重置 —— 否则前一个用例改了 scroll / hover_row / 选中状态，会让
    后一个用例莫名其妙地失败（"单独跑都过、一起跑就挂"）。
    """
    app.build_sessions()
    app.root.update()
    lst = app.list
    lst.scroll = 0
    lst.hover_row = -1
    lst._drag_off = None
    for it in lst.items:
        it['sel'] = False
    app._selected = set()
    lst.draw()
    app.root.update()
    return app


@pytest.fixture(scope='module')
def app():
    """整个模块共用一个 Tk 根窗口。

    坑：Tk 每个进程只能有一个根窗口，逐个测试 destroy() 会让后面的测试
    报 "Tcl wasn't installed properly"。所以这里建一次、最后统一销毁。
    """
    import app_plus as A
    A.App.do_getkey = lambda self: None
    A.App.do_connect = lambda self: None
    a = A.App()
    a.root.update_idletasks()
    a.root.update()
    yield a
    try:
        a.root.destroy()
    except Exception:
        pass
    # 允许后续模块再建根窗口
    import tkinter
    tkinter._default_root = None


def _reset(a, n=40):
    """把 App 恢复到"已连库、在首页"的状态。"""
    a.sessions = [{'username': f'wxid_{i}', 'summary': f'消息{i}',
                   'last_timestamp': '1789000000'} for i in range(n)]
    a.nick_map = {f'wxid_{i}': f'会话{i}' for i in range(n)}
    a.data_dir = 'x'
    a._selected = set()
    a.build_home()
    a.root.update()


@pytest.fixture(autouse=True)
def _clean(app):
    _reset(app)
    yield


# ── 回归：背景必须真的画在画布上 ──

def test_home_has_background_item(app):
    """首页必须有 __bg 图元，且覆盖整个画布。

    事故：redraw_bg() 早期只生成图片、不往画布上画，画的动作散落在
    set_theme/_relayout 两处，导致正常启动路径下从来没画过背景，
    用户看到的是一块纯白底。
    """
    ids = app.sf.canvas.find_withtag('__bg')
    assert ids, '首页没有 __bg 图元 —— 背景没被画上'
    bbox = app.sf.canvas.bbox(ids[0])
    assert bbox is not None
    w, h = app.sf.size
    assert bbox[2] - bbox[0] == w and bbox[3] - bbox[1] == h, \
        f'背景尺寸 {bbox} 和画布 {w}x{h} 不一致'


def test_background_does_not_cover_content(app):
    """背景不能盖在控件上面。

    验证方式：取一个控件所在的位置，看该处最顶层的图元不是背景。
    （不能比较图元 id 大小 —— Tk 会回收被删除的 id，顺序不单调。）
    """
    cv = app.sf.canvas
    bg = set(cv.find_withtag('__bg'))
    assert bg, '没有背景'
    btn = app.btn_key
    hits = cv.find_overlapping(btn.x + 5, btn.y + 5, btn.x + 5, btn.y + 5)
    assert hits, '按钮位置没有任何图元？'
    assert hits[-1] not in bg, '背景盖在按钮上面了'
    # 画布大部分区域应该都被背景覆盖
    assert cv.bbox(list(bg)[0]) is not None, '背景 bbox 为空'


def test_sessions_page_has_background(app):
    app.build_sessions()
    app.root.update()
    assert app.sf.canvas.find_withtag('__bg'), '会话页背景丢失'


def test_background_survives_theme_switch(app):
    """切主题后背景必须还在（set_theme 里 delete('all') 会把它删掉）。"""
    for name in ('dark', 'light', 'dark'):
        app.toggle_theme()
        app.root.update()
        ids = app.sf.canvas.find_withtag('__bg')
        assert ids, f'切到 {name} 后背景丢了'
        assert app.sf.canvas.bbox(ids[0]), '切主题后背景 bbox 为空'


def test_background_survives_page_switch(app):
    """首页↔会话页来回切，背景不能丢。"""
    for _ in range(3):
        app.build_sessions()
        app.root.update()
        assert app.sf.canvas.find_withtag('__bg'), '切到会话页后背景丢了'
        app.build_home()
        app.root.update()
        assert app.sf.canvas.find_withtag('__bg'), '切回首页后背景丢了'


# ── 回归：按钮必须能被点动 ──

def test_button_click_fires_command(app):
    """一次完整的按下+抬起必须触发 command。"""
    fired = []
    from ui_widgets import Button
    btn = Button(app.sf, 100, 600, 160, 40, '测试',
                 command=lambda: fired.append(1), kind='primary')
    app.root.update()
    cx, cy = btn.x + btn.w / 2, btn.y + btn.h / 2
    app.sf.dispatch_input(_mk('press', cx, cy))
    app.sf.dispatch_input(_mk('release', cx, cy))
    assert fired == [1], '按钮按下+抬起没有触发 command'


def test_button_click_works_without_animation_frame(app):
    """Press 和 Release 之间一帧动画都不跑，也必须算作一次点击。

    事故：早期用动画进度 _press_t > 0.15 判断"是否按过"。动画是异步的，
    快速点击时 _press_t 还是 0，于是被误判成没按过 —— 按钮点了没反应，
    而且点击快慢会改变行为，极难定位。
    """
    fired = []
    from ui_widgets import Button
    btn = Button(app.sf, 100, 540, 160, 40, '快点',
                 command=lambda: fired.append(1), kind='primary')
    app.root.update()
    cx, cy = btn.x + btn.w / 2, btn.y + btn.h / 2
    # 连续派发，中间不调 root.update()（模拟极快点击）
    app.sf.dispatch_input(_mk('press', cx, cy))
    app.sf.dispatch_input(_mk('release', cx, cy))
    assert fired == [1], '快速点击丢失 —— 又用动画值判断点击了'


def test_button_release_outside_does_not_fire(app):
    """在按钮上按下、移到外面再抬起来，不应触发（标准按钮语义）。"""
    fired = []
    from ui_widgets import Button
    btn = Button(app.sf, 100, 480, 160, 40, '拖出',
                 command=lambda: fired.append(1), kind='primary')
    app.root.update()
    app.sf.dispatch_input(_mk('press', btn.x + 10, btn.y + 10))
    app.sf.dispatch_input(_mk('release', btn.x - 200, btn.y - 200))
    assert fired == [], '在按钮外抬起却触发了 command'


def test_all_home_buttons_registered_for_input(app):
    """页面上每个按钮都必须注册为输入消费者。

    事故：漏注册的控件表现就是"完全点不动"，而且不会报错。
    """
    from ui_widgets import Button
    btns = [w for w in app.sf._input_owners if isinstance(w, Button)]
    assert len(btns) >= 3, f'首页按钮注册数异常: {len(btns)}'
    assert len(app.sf._input_owners) == len(set(map(id, app.sf._input_owners))), \
        '有重复注册的输入消费者'


# ── 回归：列表点击分工 ──

def test_checkbox_toggles_and_row_opens(sessions):
    """点方框=勾选；点行其它位置=打开详情。两者不能混淆。"""
    app = sessions
    lst = app.list
    toggled, opened = [], []
    lst.on_toggle = lambda it: toggled.append(it['title'])
    lst.on_open = lambda it: opened.append(it['title'])
    y = lst.y + lst.HEADER_H + 22

    app.sf.dispatch_input(_mk('press', lst.x + 20, y))
    assert toggled and not opened, '点勾选框应该只勾选'

    toggled.clear()
    app.sf.dispatch_input(_mk('press', lst.x + 400, y + lst.ROW_H))
    assert opened and not toggled, '点行中间应该只打开详情'


def test_click_outside_list_does_not_consume(sessions):
    """点在列表外不能被列表消费，否则会挡住后面的控件。

    事故：_on_click 早期无条件 return 'break'，Tk 会因此中止后续所有
    处理器，绑在它后面的下拉框永远收不到点击 —— 表现为下拉框点不动。
    """
    app = sessions
    lst = app.list
    r = app.sf.dispatch_input(_mk('press', lst.x + 100, lst.y - 200))
    assert r is None, '点在列表外却消费了事件'


def test_input_registration_no_leak(sessions):
    """反复切页不能累积输入消费者。"""
    app = sessions
    n = len(app.sf._input_owners)
    for _ in range(6):
        app.build_home()
        app.root.update()
        app.build_sessions()
        app.root.update()
    assert len(app.sf._input_owners) == n, \
        f'输入消费者泄漏: {n} -> {len(app.sf._input_owners)}'


def test_scrollbar_drag_changes_scroll(sessions):
    """拖动滚动条滑块必须改变滚动位置。

    事故：_scroll_drag 曾经从未被调用（死代码），拖滑块毫无反应，
    用户感受就是"下滑卡死"。
    """
    app = sessions
    lst = app.list
    g = lst._thumb_geom()
    assert g, '滚动条几何为空（列表项不够时正常，这里 40 项应该够）'
    sx = g[0] + 2
    before = lst.scroll
    app.sf.dispatch_input(_mk('press', sx, g[3] + g[4] / 2))       # 抓住滑块
    assert lst._drag_off is not None, '按下滑块没有进入拖动状态'
    app.sf.dispatch_input(_mk('motion', sx, g[3] + g[4] / 2 + 160))  # 往下拖
    assert lst.scroll > before, '拖动滑块后滚动位置没变'
    app.sf.dispatch_input(_mk('release', sx, g[3] + g[4] / 2 + 160))  # 释放
    assert lst._drag_off is None, '释放后没有结束拖动'


# ── 回归：重绘不能堆积图元 ──

def _row_item_count(lst, idx):
    cv = lst.sf.canvas
    return len(cv.find_withtag(lst._row_tag(idx)))


def test_redraw_does_not_accumulate_items(app):
    """重复 draw() 不能让行图元堆积。

    事故：draw() 只删了 'listbody'，而行图元带的是 _row_tag（cl123_row0），
    于是每次重绘旧行都留在画布上、新的一遍叠上去。实测一次多余 draw()
    行图元就翻倍。用户可见现象："窗口一闪就多一个导出格式框"、
    "切主题后勾选显示错乱"、"某一行钩子凭空消失"。
    """
    app.build_sessions()
    app.root.update()
    lst = app.list
    n0 = _row_item_count(lst, 0)
    total0 = len(lst.sf.canvas.find_all())
    assert n0 > 0, '行图元一个都没有？'
    for _ in range(4):
        lst.draw()
        app.root.update()
        assert _row_item_count(lst, 0) == n0, \
            f'draw() 后行图元从 {n0} 变成 {_row_item_count(lst, 0)} —— 在堆积'
    # 整表图元总数也不能涨
    assert len(lst.sf.canvas.find_all()) == total0, \
        '整表重绘后 Canvas 图元总数变多了 —— 有图元没被清理'


def test_theme_switch_does_not_accumulate_items(app):
    """反复切主题不能让图元堆积。"""
    app.build_sessions()
    app.root.update()
    n0 = len(app.sf.canvas.find_all())
    for _ in range(3):
        app.toggle_theme()
        app.root.update()
        app.toggle_theme()
        app.root.update()
    n1 = len(app.sf.canvas.find_all())
    assert abs(n1 - n0) <= 4, f'切主题 6 次后图元从 {n0} 变成 {n1} —— 在堆积'


# ── 回归：复选框图必须按状态区分 ──

def test_checkbox_photos_differ_by_state(app):
    """选中/未选中的复选框必须是两张不同的图（不能共用一个槽）。

    事故：复选框图曾是整个列表只有一张的单槽缓存（self._ck_photo），
    悬停切到选中状态不同的行时缓存被覆盖，而所有已画出的行都指向同一个
    属性 —— 整个列表的复选框一起变成最后画的那张。
    现象："鼠标从已勾选行移到未勾选行，钩子全没了"（计数却是对的）。
    """
    app.build_sessions()
    app.root.update()
    lst = app.list
    off = str(lst._row_ck_photo(False))
    on = str(lst._row_ck_photo(True))
    assert off != on, '选中与未选中的复选框是同一张图 —— 状态无法区分'
    # 再取一次必须是同一张（说明是缓存，不是每次新建）
    assert str(lst._row_ck_photo(False)) == off, '复选框图没有被缓存，每次都在重建'


def test_each_row_checkbox_matches_its_own_state(app):
    """每一行画出来的复选框必须和自己的选中状态一致。"""
    app.build_sessions()
    app.root.update()
    lst = app.list
    cv = lst.sf.canvas
    # 隔行选中，画一次
    for i, it in enumerate(lst.items):
        it['sel'] = (i % 2 == 0)
    lst.draw()
    app.root.update()

    off, on = str(lst._row_ck_photo(False)), str(lst._row_ck_photo(True))
    for idx in range(lst.visible_rows):
        names = [str(cv.itemcget(i, 'image'))
                 for i in cv.find_withtag(lst._row_tag(idx))
                 if cv.type(i) == 'image']
        want = on if lst.items[idx].get('sel') else off
        assert want in names, (
            f'第 {idx} 行复选框显示不对：sel={lst.items[idx].get("sel")} '
            f'期望图={want} 该行图元={names}')


def test_uncheck_one_keeps_others_checked(app):
    """取消其中一个之后，其余的仍应显示为勾选。"""
    app = _fresh_sessions(app)
    lst = app.list
    cv = lst.sf.canvas
    for it in lst.items:
        it['sel'] = True
    lst.draw()
    app.root.update()

    # 用 _row_at 反推坐标，别手算（HEADER_H/ROW_H 变化会算错行）
    row = 1
    y1 = lst.y + lst.HEADER_H + row * lst.ROW_H + lst.ROW_H // 2
    assert lst._row_at(y1) == row, f'坐标算错了：期望第 {row} 行'
    app.sf.dispatch_input(_mk('press', lst.x + 20, y1))
    app.sf.dispatch_input(_mk('release', lst.x + 20, y1))
    app.root.update()

    assert not lst.items[row]['sel'], f'第 {row} 行没有被取消勾选'
    off, on = str(lst._row_ck_photo(False)), str(lst._row_ck_photo(True))
    for idx in range(min(4, lst.visible_rows)):
        names = [str(cv.itemcget(i, 'image'))
                 for i in cv.find_withtag(lst._row_tag(idx))
                 if cv.type(i) == 'image']
        want = on if lst.items[idx].get('sel') else off
        assert want in names, (
            f'第 {idx} 行显示不对：sel={lst.items[idx].get("sel")} '
            f'期望图={want} 该行图元={names}')


# ── 回归：滚轮必须能滚动 ──

def _fresh_sessions(app, n=40):
    """把 App 重置成"刚进入会话页"的干净状态并返回它。

    共享一个 Tk 根窗口是必须的（Tk 每进程只能有一个根），但**页面状态必须
    每个用例重置** —— 否则前一个用例改过的 scroll / hover_row / 选中状态会
    让后一个用例莫名其妙地失败（典型的"单独跑都过、一起跑就挂"）。
    """
    app.sessions = [{'username': f'wxid_{i}', 'summary': f'消息{i}',
                     'last_timestamp': '1789000000'} for i in range(n)]
    app.nick_map = {f'wxid_{i}': f'会话{i}' for i in range(n)}
    app.data_dir = 'x'
    app._selected = set()
    app.build_sessions()
    app.root.update()
    lst = app.list
    lst.scroll = 0
    lst.hover_row = -1
    lst._drag_off = None
    for it in lst.items:
        it['sel'] = False
    # 前面的用例可能切过主题/重建过页面，导致这个列表被注销过输入注册。
    # 重新登记，保证它确实在收事件。
    app.sf.register_input(lst)
    lst.draw()
    app.root.update()
    return app


def test_mouse_wheel_scrolls(app):
    """滚轮必须能滚动列表。

    事故：dispatch 的映射表里漏了 MouseWheel(=38)，滚轮事件被静默丢弃，
    用户只能拖滚动条。
    """
    import ui_widgets as W
    assert 38 in W._EVENT_NAMES and W._EVENT_NAMES[38] == 'MouseWheel', \
        'MouseWheel 没有登记在事件映射表里 —— 滚轮会被丢弃'
    app = _fresh_sessions(app)
    lst = app.list
    wx, wy = lst.x + 300, lst.y + lst.HEADER_H + 40
    assert lst._inside(wx, wy), '滚轮坐标没落在列表内'
    app.sf.dispatch_input(_wheel(wx, wy, -120))
    app.root.update()
    assert lst.scroll > 0, (
        f'滚轮向下滚列表没动。scroll={lst.scroll} view={len(lst.view)} '
        f'visible={lst.visible_rows} canvas={len(lst.sf.canvas.find_all())} '
        f'list_y={lst.y} list_h={lst.h} wy={wy} '
        f'page={getattr(app, "page", None)}')


def test_wheel_fast_scroll_uses_delta_multiple(app):
    """快速滚动时 delta 是 120 的整数倍，要按倍数滚。"""
    app = _fresh_sessions(app)
    lst = app.list
    wx, wy = lst.x + 300, lst.y + lst.HEADER_H + 40
    app.sf.dispatch_input(_wheel(wx, wy, -360))
    app.root.update()
    assert lst.scroll >= 3, f'快速滚动只滚了 {lst.scroll} 格，应按 delta 倍数滚'


# ══════════════════════ v3.0：会话标签 / DeepSeek 官网页 ══════════════════════
#
# 为什么放在这个文件里、而不是新开一个测试模块：
#   Tk 每个进程只能有一个根窗口。新模块再建一个 root，会在
#   test_ui_invariants 销毁 root 之后再建，实测**时好时坏**
#   （_tkinter.TclError，10 个用例随机全挂）。所以 v3 的界面用例
#   继续复用这一份 module 级 root。
#
# ⚠️ 官网页的宿主是懒启动的：测试里必须把 `App._ds_boot` 换成空函数，
#    否则 `build_ds()` 之后那个 after(120ms) 会真的去拉起 electron。

@pytest.fixture
def tagged(app, tmp_path):
    """把标签仓库指到临时文件，别污染仓库根的「会话标签.json」。"""
    import session_tags
    old = app.tag_store
    app.tag_store = session_tags.TagStore(str(tmp_path / '会话标签.json'))
    try:
        yield app
    finally:
        app.tag_store = old


def _no_ds_boot(app):
    """截图/测试用：禁止真启动内嵌浏览器。"""
    import app_plus as A
    if not hasattr(A.App, '_ds_boot_orig'):
        A.App._ds_boot_orig = A.App._ds_boot
    A.App._ds_boot = lambda self: None
    return app


def _mk_export(root, chats=(('群聊A', 3), ('群聊B', 60))):
    import os
    with open(os.path.join(root, '给AI的指令.txt'), 'w', encoding='utf-8') as f:
        f.write('x')
    with open(os.path.join(root, '导出清单.html'), 'w', encoding='utf-8') as f:
        f.write('x')
    for name, n in chats:
        with open(os.path.join(root, f'{name}.md'), 'w', encoding='utf-8') as f:
            f.write('x')
        d = os.path.join(root, f'{name}_图片')
        os.makedirs(d, exist_ok=True)
        for i in range(1, n + 1):
            with open(os.path.join(d, f'{i:04d}.jpg'), 'wb') as f:
                f.write(b'x')


def test_tag_chips_are_clickable(tagged):
    """标签行必须有 chip（自绘 Button → 必须登记进输入消费者，否则点了没反应）。"""
    a = _fresh_sessions(tagged)
    a.tag_store.assign([f'wxid_{i}' for i in range(4)], '常看')
    a.tag_store.assign([f'wxid_{i}' for i in range(4, 8)], '工作')
    a.build_sessions()
    a.root.update()
    labels = [str(getattr(w, 'text', '')) for w in a._widgets]
    assert any('常看' in x for x in labels)
    assert any('工作' in x for x in labels)
    assert any('标签管理' in x for x in labels)


def test_click_tag_replaces_selection(tagged):
    """点标签 = **替换**当前勾选（用户明确要的行为）。"""
    a = _fresh_sessions(tagged)
    a.tag_store.assign(['wxid_1', 'wxid_2', 'wxid_3'], '常看')
    a.tag_store.assign(['wxid_5'], '工作')
    a.build_sessions()
    a._sel_all(True)
    assert len(a._selected) == len(a.list.items) > 3

    a._sel_by_tag('工作')
    assert a._selected == {'wxid_5'}
    assert [it['wxid'] for it in a.list.items if it.get('sel')] == ['wxid_5']

    a._sel_by_tag('常看')
    assert a._selected == {'wxid_1', 'wxid_2', 'wxid_3'}


def test_tag_selection_survives_page_switch(tagged):
    """按标签勾选的结果必须进 _selected，否则切页回来勾选会整体丢失。"""
    a = _fresh_sessions(tagged)
    a.tag_store.assign(['wxid_2', 'wxid_4'], '常看')
    a.build_sessions()
    a._sel_by_tag('常看')
    a.build_sessions()
    assert {it['wxid'] for it in a.list.items if it.get('sel')} == {'wxid_2', 'wxid_4'}


def test_apply_tag_persists_to_file(tagged, tmp_path):
    import json
    a = _fresh_sessions(tagged)
    a.build_sessions()
    a._sel_all(False)
    a.list.items[0]['sel'] = True
    a.list.items[1]['sel'] = True
    a._selected = {a.list.items[0]['wxid'], a.list.items[1]['wxid']}
    assert a._apply_tag('  常看  ', sorted(a._selected)) is True     # 名字会被清洗
    with open(str(tmp_path / '会话标签.json'), encoding='utf-8') as f:
        data = json.load(f)
    assert set(data['sessions']) == a._selected
    assert all(v == ['常看'] for v in data['sessions'].values())
    assert '常看' in a.tag_store.all_tags()


def test_row_tag_text_does_not_accumulate_items(tagged):
    """行内标签是拼进标题字符串的 —— 重绘不能因此堆图元。"""
    a = _fresh_sessions(tagged)
    a.tag_store.assign(['wxid_1', 'wxid_2'], '常看')
    a.build_sessions()
    a.root.update()
    lst = a.list
    before = len(a.sf.canvas.find_all())
    for _ in range(3):
        lst.draw()
    assert len(a.sf.canvas.find_all()) == before


def test_row_shows_tag_inline_with_title(tagged):
    a = _fresh_sessions(tagged)
    a.tag_store.assign(['wxid_1'], '常看')
    a.build_sessions()
    a.root.update()
    texts = []
    for iid in a.sf.canvas.find_all():
        try:
            if a.sf.canvas.type(iid) == 'text':
                texts.append(str(a.sf.canvas.itemcget(iid, 'text')))
        except Exception:
            pass
    # 标题行形如「会话1 #常看」；chip 上也会出现 #常看，所以按"会话"前缀区分
    assert any('会话' in t and '#常看' in t for t in texts), texts[:40]


def test_ds_page_builds_without_host(tagged):
    """官网页不能因为没启动 electron 就崩。"""
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.ds_out_root = ''          # 别让上一次用例留下的目录触发自动扫描
    a.ds_units = []
    a.build_ds()
    a.root.update()
    assert a.page == 'ds'
    assert a.list is not None and a.list.items == []
    assert a.ds is None and a._ds_visible is False
    labels = [str(getattr(w, 'text', '')) for w in a._widgets]
    assert any('返回导出页' in x for x in labels)
    assert any('开始发送' in x for x in labels)


def test_ds_scan_and_batch_summary(tagged, tmp_path):
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.build_ds()
    a.ds_out_root = str(tmp_path)
    _mk_export(str(tmp_path))
    a._ds_scan()
    a.root.update()
    units = a.ds_units
    assert [u['label'] for u in units] == ['给AI的指令.txt', '群聊A.md', '群聊B.md',
                                           '导出清单.html', '群聊A_图片', '群聊B_图片']
    assert len(a.ds_selected) == len(units)          # 默认全选
    assert len(a.list.items) == len(units)

    a._ds_update_hint()
    txt = str(a.sf.canvas.itemcget('dshint', 'text'))
    # 默认「只发文档（建议）」：图片被跳过 → 只剩 4 个文档、1 批
    assert '只发文档' in txt, txt
    assert '4 个文件' in txt and '1 批' in txt, txt

    # 关掉「只发文档」后：67 个文件；每批默认 20（实测 40 个附件就会被官网拒收）
    a._ds_docs_only = False
    a._ds_limit = 20
    a._ds_update_hint()
    txt = str(a.sf.canvas.itemcget('dshint', 'text'))
    assert '67 个文件' in txt, txt
    assert '分 4 批' in txt, txt
    a._ds_docs_only = True

    img_ids = {u['id'] for u in units if u['kind'] == 'image'}
    for it in a.list.items:
        if it['wxid'] in img_ids:
            it['sel'] = False
    a.ds_selected -= img_ids
    a._ds_update_hint()
    txt = str(a.sf.canvas.itemcget('dshint', 'text'))
    assert '4 个文件' in txt


def test_ds_leave_is_safe_without_host(tagged):
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.build_ds()
    a._ds_leave()
    assert a._ds_visible is False
    a.build_sessions()
    assert a.page == 'sessions'


def test_ds_rect_stays_inside_window(tagged):
    a = _no_ds_boot(_fresh_sessions(tagged))
    x, y, w, h = a._ds_rect()
    assert x > 38 + a.DS_LEFT_W
    assert x + w <= a.W - 30 and y + h <= a.H - 30
    assert w > 300 and h > 200


# ── 回归：标签弹窗必须真的能交互 ──
#
# 事故：弹窗创建到一半抛异常（`T.rgb2hex(theme['text'])` —— 主题里的颜色
# 有的是元组、有的本来就是 '#rrggbb' 字符串），而 Button 的 command 是在
# Surface.dispatch_input 里被调用的，那里 `except Exception: pass` 把异常吞了。
# 用户看到的就是"跳出来一个空框、点不动"，没有任何报错线索。
# 现在：rgb2hex 兼容字符串 + 异常会写 .ui_errors.log，并且有下面这组断言。

def _tag_dialog_widgets(win):
    """把一个 Toplevel 里的所有控件按类名收集起来。"""
    out = {'Button': [], 'Entry': [], 'Label': []}
    stack = [win]
    while stack:
        cur = stack.pop()
        for c in cur.winfo_children():
            cls = c.winfo_class()
            if cls in out:
                out[cls].append(c)
            stack.append(c)
    return out


def test_tag_dialog_is_interactive(tagged):
    import tkinter as tk
    a = _fresh_sessions(tagged)
    a._sel_all(False)
    a.list.items[0]['sel'] = True
    a.list.items[1]['sel'] = True
    a._selected = {a.list.items[0]['wxid'], a.list.items[1]['wxid']}
    a.tag_store.assign(['wxid_7'], '旧标签')

    a.open_tag_dialog()
    a.root.update()
    wins = [w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert len(wins) == 1, '标签弹窗没建出来'
    win = wins[0]
    try:
        w = _tag_dialog_widgets(win)
        # 必须真的有输入框（用户要输入标签名）
        assert len(w['Entry']) == 1, '弹窗里没有输入标签名的输入框'
        labels = [str(c.cget('text')) for c in w['Button']]
        assert any('新建并贴上' in t for t in labels), labels
        assert any('贴到勾选的会话' in t for t in labels), labels
        assert any('只勾选这些会话' in t for t in labels), labels
        assert any('删除' in t for t in labels), labels
        assert any('清除' in t for t in labels), labels
        # 已有标签要显示出来
        texts = [str(c.cget('text')) for c in w['Label']]
        assert any('旧标签' in t for t in texts), texts

        # 真的点一下「新建并贴上」
        w['Entry'][0].insert(0, '新标签')
        btn = [c for c in w['Button'] if '新建并贴上' in str(c.cget('text'))][0]
        btn.invoke()
        a.root.update()
        assert a.tag_store.tags_of(a.list.items[0]['wxid']) == ['新标签']
        assert a.tag_store.tags_of(a.list.items[1]['wxid']) == ['新标签']
    finally:
        try:
            win.destroy()
        except Exception:
            pass
        a.root.update()


def test_rgb2hex_accepts_strings_and_tuples():
    """主题颜色两种形态都有，rgb2hex 必须都能吃（否则弹窗建到一半就崩）。"""
    import ui_theme as T
    assert T.rgb2hex((18, 21, 32)) == '#121520'
    assert T.rgb2hex('#1e2438') == '#1e2438'
    assert T.rgb2hex('1e2438') == '#1e2438'
    for name, th in T.THEMES.items():
        for key in ('bg_top', 'surface', 'text', 'text_dim', 'accent', 'surface2',
                    'border', 'ok', 'warn', 'err'):
            if key in th:
                assert T.rgb2hex(th[key]).startswith('#'), f'{name}.{key} 不是颜色'


# ── 回归：一键发送的界面路径不能"点了没反应" ──
#
# 事故：发送进度窗里用了 `T.rgb2hex(theme['text'])`，而主题里的 text/surface
# 本来就是 '#rrggbb' 字符串 → ValueError → 异常被分发层吞掉 →
# 用户点「开始发送」**什么都不发生**。下面两个用例把这条路径钉住。

def _ds_ready_app(a, tmp_path):
    import os
    a.ds_out_root = str(tmp_path)
    # 模块共享一个 App，前面的用例可能留下"发送中"状态
    a._ds_sending = False
    a._ds_cancel_flag = False
    with open(os.path.join(str(tmp_path), '给AI的指令.txt'), 'w', encoding='utf-8') as f:
        f.write('x')
    a.build_ds()
    a._ds_scan()
    a.root.update()
    return a


def test_ds_send_dry_run_opens_progress_window(tagged, tmp_path):
    """演练分批：必须真的弹出进度窗，而不是静默失败。"""
    import tkinter as tk
    a = _no_ds_boot(_fresh_sessions(tagged))
    _ds_ready_app(a, tmp_path)
    before = [w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel)]
    a._ds_send(dry_run=True)
    a.root.update()
    wins = [w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert len(wins) == len(before) + 1, '演练分批没有弹出进度窗'
    win = wins[-1]
    try:
        # 等后台线程把结果发回来（演练很快）
        for _ in range(40):
            a.root.update()
            time.sleep(0.05)
            if not getattr(a, '_ds_sending', False):
                break
        assert getattr(a, '_ds_sending', False) is False, '演练没有正常结束'
    finally:
        try:
            win.destroy()
        except Exception:
            pass
        a.root.update()


def test_ds_send_without_host_gives_readable_error(tagged, tmp_path):
    """没有宿主时不能崩，也不能去点登录表单 —— 要给出可读原因。"""
    a = _no_ds_boot(_fresh_sessions(tagged))
    _ds_ready_app(a, tmp_path)
    msgs = []
    a.toast = lambda msg, kind='info', ms=2600: msgs.append((kind, msg))
    try:
        a._ds_ensure_host = lambda: '模拟：内嵌浏览器没起来'
        a._ds_send()                       # 非演练路径
        a.root.update()
        assert msgs and '浏览器' in msgs[0][1], msgs
        assert getattr(a, '_ds_sending', False) is False
    finally:
        # ⚠️ 必须还原：App 是模块级共享的，实例属性会污染后面的用例
        del a.toast
        del a._ds_ensure_host


def test_ds_preflight_blocks_when_not_logged_in(tagged):
    """没登录时必须挡住（页面上根本没有上传入口，硬发只会样样失败）。"""
    a = _no_ds_boot(_fresh_sessions(tagged))

    class FakeHost:
        running = True

        def state(self):
            return {'ok': True, 'ready': True, 'loggedIn': False, 'fileInput': False}

    a.ds = FakeHost()
    assert '登录' in a._ds_preflight()

    class Host2(FakeHost):
        def state(self):
            return {'ok': True, 'ready': True, 'loggedIn': True, 'fileInput': False}

    a.ds = Host2()
    assert '上传入口' in a._ds_preflight()

    class Host3(FakeHost):
        def state(self):
            return {'ok': True, 'ready': True, 'loggedIn': True, 'fileInput': True}

    a.ds = Host3()
    assert a._ds_preflight() == ''
    a.ds = None


def _descendants(w):
    out = []
    stack = [w]
    while stack:
        cur = stack.pop()
        for c in cur.winfo_children():
            out.append(c)
            stack.append(c)
    return out


def test_ds_progress_window_can_be_closed_after_finish(tagged, tmp_path):
    """演练/发送结束后，进度窗必须能关掉。

    事故：窗口只置了"取消"标记、从不销毁，而且点 X 也是静默取消 ——
    用户看到的是"演练分批的窗口关不掉"。
    """
    import tkinter as tk
    a = _no_ds_boot(_fresh_sessions(tagged))
    _ds_ready_app(a, tmp_path)
    a._ds_send(dry_run=True)
    a.root.update()
    wins = [w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert wins, '演练没有弹出进度窗'
    win = wins[-1]
    for _ in range(80):
        a.root.update()
        time.sleep(0.05)
        if not getattr(a, '_ds_sending', False):
            break
    assert getattr(a, '_ds_sending', False) is False, '演练没有正常结束'

    btns = [c for c in _descendants(win) if c.winfo_class() == 'Button']
    labels = [str(b.cget('text')) for b in btns]
    closers = [b for b in btns if '关闭' in str(b.cget('text'))]
    assert closers, f'结束后没有「关闭」按钮：{labels}'
    closers[0].invoke()
    a.root.update()
    assert not win.winfo_exists(), '点了关闭窗口还在'


def test_toast_not_hidden_behind_embedded_browser(tagged):
    """官网页签上的提示不能被内嵌浏览器挡住。

    浏览器是独立 Win32 子窗口，永远画在画布之上；右下角的提示会被它盖住，
    用户抱怨"提示看不到"。所以官网页上的提示必须落在浏览器矩形之外。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.ds_out_root = ''
    a.ds_units = []
    a.build_ds()
    a._ds_visible = True                 # 假装浏览器已经嵌进来并显示中
    try:
        x1, y1, x2, y2 = a._toast_rect()
        bx, by, bw, bh = a._ds_rect()
        overlap = not (x2 <= bx or x1 >= bx + bw or y2 <= by or y1 >= by + bh)
        assert not overlap, (
            f'提示会与浏览器区域重叠：toast={(x1, y1, x2, y2)} '
            f'browser={(bx, by, bx + bw, by + bh)}')
        assert x1 >= 0 and y1 >= 0 and x2 <= a.W and y2 <= a.H, '提示跑到窗口外了'
        # 顺便确认真的能画出来
        a.sf.canvas.delete('toast')
        a.toast('测试提示', 'warn')
        a.root.update()
        assert a.sf.canvas.bbox('toast'), '提示没画出来'
    finally:
        a._ds_visible = False
        a.sf.canvas.delete('toast')


def test_ds_preflight_accepts_page_ready_by_ready_state(tagged):
    """页面已经加载好了，就不能再报"还在加载"。

    事故：官网页加载完之后仍有子资源/接口活动，Electron 会再发 did-start-loading
    却没有对应的 did-finish-load，主进程侧的 S.pageReady 卡在 false ——
    用户看到的是"页面明明好了，程序却说还在加载，不能发送"。
    现在两条判据（主进程标志 / 页面自己的 document.readyState）任一成立即放行。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))

    def host(ready, ready_state, logged=True, fi=True):
        class H:
            running = True

            def state(self):
                return {'ok': True, 'ready': ready, 'readyState': ready_state,
                        'loggedIn': logged, 'fileInput': fi}
        return H()

    a.ds = host(False, 'complete')          # ← 脱节状态：必须放行
    assert a._ds_preflight() == ''
    a.ds = host(True, '')                   # 只有主进程标志
    assert a._ds_preflight() == ''
    a.ds = host(False, 'loading')           # 真的还在加载
    assert '还在加载' in a._ds_preflight()
    a.ds = host(False, '')                  # 两条都没有
    assert '还在加载' in a._ds_preflight()
    a.ds = None


def test_ds_ask_sends_text_through_host(tagged, tmp_path):
    """「发给 AI」那条通道必须真的把文字交给宿主（走 insertText，不依赖键盘焦点）。

    为什么要这条通道：内嵌页是跨进程子窗口，切页/切回窗口时可能收不到真实按键
    （用户反馈"打不了字"）。这条走 Electron 的 insertText，一定可用。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    _ds_ready_app(a, tmp_path)

    class FakeDS:
        running = True
        _embedded = True

        def __init__(self):
            self.calls = []

        def type_text(self, text, submit=False):
            self.calls.append((text, submit))
            return {'ok': True, 'send': {'ok': True, 'how': 'button'}}

    a.ds = FakeDS()
    a._ask_entry.set('我已发送完毕')
    a._ds_ask()
    a.root.update()
    assert a.ds.calls == [('我已发送完毕', True)], a.ds.calls
    assert a._ask_entry.get() == '', '发完应该清空输入框'

    # 空内容不该发
    a._ds_ask()
    a.root.update()
    assert len(a.ds.calls) == 1


def test_ds_send_refreshes_stale_prompt_file(tagged, tmp_path):
    """发送前必须把导出目录里的「给AI的指令.txt」刷新成最新模板。

    事故：导出目录里那个文件是**导出当时**写的；用户后来改了 AI提示词.txt
    （加了「分批接收协议」），旧导出目录里还是老文案 —— 从那儿发送，AI 拿到的
    就是过时指令（用户实测踩到："你给ai的指令那个txt没修改啊"）。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    import os
    root = str(tmp_path)
    _mk_export(root)
    stale = os.path.join(root, '给AI的指令.txt')
    with open(stale, 'w', encoding='utf-8') as f:
        f.write('【角色】这是过期的老文案，没有分批接收协议')
    a.ds_out_root = root
    a.ds_units = []
    a.build_ds()
    a._ds_scan()

    got = a._ds_refresh_prompt_file()
    assert got == stale, got
    with open(stale, encoding='utf-8') as f:
        text = f.read()
    assert '分批接收协议' in text, text[:80]
    assert '我已发送完毕' in text


def test_ui_error_hook_reports_instead_of_swallowing(app):
    seen = []
    old = app.sf.on_error
    app.sf.on_error = seen.append

    class Boom:
        def on_input(self, e):
            raise RuntimeError('boom')

    app.sf.register_input(Boom())
    try:
        app.sf.dispatch_input(_mk('motion', 5, 5))
    finally:
        app.sf._input_owners = [o for o in app.sf._input_owners
                                if not isinstance(o, Boom)]
        app.sf.on_error = old
    assert seen and isinstance(seen[0], RuntimeError), seen
