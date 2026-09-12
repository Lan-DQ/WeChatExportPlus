# -*- coding: utf-8 -*-
"""界面不变式测试：防止"背景丢失"和"按钮点不动"这两类回归。

每个问题都是真实发生过的，用断言把正确状态固定下来。
"""
import sys
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

def test_checkbox_toggles_and_row_opens(app):
    """点方框=勾选；点行其它位置=打开详情。两者不能混淆。"""
    app.build_sessions()
    app.root.update()
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


def test_click_outside_list_does_not_consume(app):
    """点在列表外不能被列表消费，否则会挡住后面的控件。

    事故：_on_click 早期无条件 return 'break'，Tk 会因此中止后续所有
    处理器，绑在它后面的下拉框永远收不到点击 —— 表现为下拉框点不动。
    """
    app.build_sessions()
    app.root.update()
    lst = app.list
    r = app.sf.dispatch_input(_mk('press', lst.x + 100, lst.y - 200))
    assert r is None, '点在列表外却消费了事件'


def test_input_registration_no_leak(app):
    """反复切页不能累积输入消费者。"""
    app.build_sessions()
    app.root.update()
    n = len(app.sf._input_owners)
    for _ in range(6):
        app.build_home()
        app.root.update()
        app.build_sessions()
        app.root.update()
    assert len(app.sf._input_owners) == n, \
        f'输入消费者泄漏: {n} -> {len(app.sf._input_owners)}'


def test_scrollbar_drag_changes_scroll(app):
    """拖动滚动条滑块必须改变滚动位置。

    事故：_scroll_drag 曾经从未被调用（死代码），拖滑块毫无反应，
    用户感受就是"下滑卡死"。
    """
    app.build_sessions()
    app.root.update()
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
