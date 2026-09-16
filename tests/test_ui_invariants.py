# -*- coding: utf-8 -*-
"""界面不变式测试：防止"背景丢失"和"按钮点不动"这两类回归。

每个问题都是真实发生过的，用断言把正确状态固定下来。
"""
import itertools
import os
import subprocess
import sys
import time
import tkinter as tk

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'gui'))
sys.path.insert(0, os.path.join(ROOT, 'exporters'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import app_plus as _A       # noqa: E402  （布局表是纯函数，测试直接调它）


def test_renderer_js_template_intact():
    """`dsview/renderer.js` 必须语法正确、且模板没被反引号截断。

    事故（接管文档 5.1，2026-09-15 一天踩了三次）：renderer.js 整个是一个
    ``String.raw`` 模板字符串，helper 源码被塞在模板里当字符串注入。往**注释**里
    写一个反引号就会提前结束模板 → 语法错误。而且这个错误 `node --check` 有时
    也能看出来、有时只会静默地把模板截短（后面还有别的反引号能让文件继续"看起来合法"），
    所以必须用 dsview/check_renderer.py 同时检查语法和模板边界。
    """
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'dsview', 'check_renderer.py')],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert r.returncode == 0, ('renderer.js 检查未通过：\n%s\n%s'
                               % (r.stdout or '', r.stderr or ''))


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


def test_search_then_click_opens_the_right_session(app):
    """★ 搜索后点击，必须打开**那一行显示的**会话，而不是别的。

    事故（用户实测）："输入关键词后，鼠标光标停留在输入框，然后我点击会话会跳出
    随机会话"。根因：`_row_at()` 返回的是 `self.view` 里的**可见行号**，
    而 `self.items` 是**完整列表**；搜索过滤后 `view[row] != row`，
    `_on_click` 却直接写 `self.items[i]` —— 于是点第一行打开的是 items[0]。
    修法是所有"点击 → 定位数据"都过 `item_index()` 换算。这条测试就是钉住它。
    """
    app = _fresh_sessions(app)
    lst = app.list
    opened = []
    lst.on_open = lambda it: opened.append(it['wxid'])

    # 造一个只有部分命中关键词的列表：只有 wxid_3 的标题含"命中"
    lst.items[3]['title'] = '命中目标'
    lst.items[7]['title'] = '命中目标二'
    lst.refresh_view('命中')
    app.root.update()
    assert lst.view == [3, 7], '前置条件：过滤后应只剩这两项，实际 %s' % (lst.view,)

    # 点"可见的第 0 行"（= items[3]）
    y0 = lst.y + lst.HEADER_H + 0 * lst.ROW_H + lst.ROW_H // 2
    assert lst._row_at(y0) == 0, '坐标算错，应落在可见第 0 行'
    app.sf.dispatch_input(_mk('press', lst.x + 200, y0))
    app.sf.dispatch_input(_mk('release', lst.x + 200, y0))
    app.root.update()
    assert opened == ['wxid_3'], (
        '★ 搜索后点第 0 行应打开 wxid_3（该行显示的会话），实际打开 %s' % opened)

    # 再点"可见的第 1 行"（= items[7]）
    opened.clear()
    y1 = lst.y + lst.HEADER_H + 1 * lst.ROW_H + lst.ROW_H // 2
    app.sf.dispatch_input(_mk('press', lst.x + 200, y1))
    app.sf.dispatch_input(_mk('release', lst.x + 200, y1))
    app.root.update()
    assert opened == ['wxid_7'], (
        '★ 搜索后点第 1 行应打开 wxid_7，实际打开 %s' % opened)


def test_search_then_check_toggles_the_right_row(app):
    """★ 搜索后点复选框，必须勾选**那一行显示的**会话（同一个根因的另一面）。"""
    app = _fresh_sessions(app)
    lst = app.list
    lst.items[5]['title'] = '独一无二'
    lst.refresh_view('独一无二')
    app.root.update()
    assert lst.view == [5], '前置条件：过滤后应只剩 items[5]'

    y = lst.y + lst.HEADER_H + 0 * lst.ROW_H + lst.ROW_H // 2
    app.sf.dispatch_input(_mk('press', lst.x + 20, y))       # x+20 = 勾选框区
    app.sf.dispatch_input(_mk('release', lst.x + 20, y))
    app.root.update()
    assert lst.items[5]['sel'] is True, '应勾选 items[5]（该行显示的会话）'
    others = [it['wxid'] for it in lst.items if it.get('sel')]
    assert others == ['wxid_5'], '★ 只应勾选那一行，实际勾了 %s' % others


def test_item_index_maps_view_to_items(app):
    """item_index() 的语义：可见行号 → items 下标（搜索 bug 修的就是这个换算）。"""
    app = _fresh_sessions(app)
    lst = app.list
    # 不过滤时两者一致
    assert lst.item_index(0) == 0
    # 过滤后必须走 view
    lst.refresh_view('会话11')
    app.root.update()
    if lst.view:
        assert lst.item_index(0) == lst.view[0], 'item_index 必须按 view 换算'
    # 越界要返回 -1（调用方据此放弃，而不是索引到错的项）
    assert lst.item_index(len(lst.items) + 100) == -1
    assert lst.item_index(-1) == -1


def test_click_on_canvas_takes_focus_away_from_search_box(app):
    """★ 搜索之后点别处，键盘焦点必须从搜索框交出去。

    用户原话（本轮遗留待查项）："输入关键词后鼠标光标会停留在输入框"。
    根因：搜索框是一个**叠在画布上的真 tk.Entry**，而列表/按钮都是画在图元上
    走的 `dispatch_input` —— 点画布不会自动把 Entry 的键盘焦点拿走。于是搜索完
    再点列表，用户接着敲键盘还是打进搜索框（后面那个"跳出随机会话"的 bug 就是
    在这条路上被发现的；那个已修，这条是另一个现象）。
    修法：`Surface._install_input_pump` 在画布上按下鼠标时 `canvas.focus_set()`。
    """
    app = _fresh_sessions(app)
    ent = app._search_entry.entry
    assert ent.winfo_exists(), '会话页应该有搜索框'

    # 用户点进搜索框并输入
    ent.focus_force()
    app.root.update()
    app.search_var.set('会话1')
    app.root.update()
    assert app.root.focus_get() is ent, '前置条件：焦点应该在搜索框里'

    # ⚠️ 这里必须用 `event_generate` 走**真实的 tk 绑定**，
    #    不能像别的用例那样直接 `dispatch_input(...)`：焦点交接是装在
    #    Surface 的 `<Button-1>` 绑定处理里的，绕过绑定就测不到它
    #    （第一版就是这么写的，于是"修好了还失败"）。
    lst = app.list
    x = lst.x + 200
    y = lst.y + lst.HEADER_H + lst.ROW_H // 2
    app.sf.canvas.event_generate('<Button-1>', x=x, y=y, when='now')
    app.sf.canvas.event_generate('<ButtonRelease-1>', x=x, y=y, when='now')
    app.root.update()

    focus = app.root.focus_get()
    assert focus is not ent, (
        '★ 点了列表之后焦点还留在搜索框上 —— 接着敲键盘会打进搜索框')
    assert focus is app.sf.canvas, '焦点应交给画布，实际是 %r' % (focus,)

    # 点回搜索框照样能拿到焦点（别把输入框本身弄坏了）
    ent.focus_force()
    app.root.update()
    assert app.root.focus_get() is ent


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


def _hint_text(a):
    """提示行的完整文字。

    ⚠️ 提示行现在**按像素宽度折行**（左栏只有 330px 宽，一句话放不下），
    所以 `itemcget('dshint')` 只给得到第一行；要看全就得把整块拼起来。
    """
    cv = a.sf.canvas
    ids = sorted(cv.find_withtag('dshintline'),
                 key=lambda i: cv.coords(i)[1] if cv.coords(i) else 0)
    return ''.join(str(cv.itemcget(i, 'text')) for i in ids)


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
    txt = _hint_text(a)
    # 默认「只发文档（建议）」：图片被跳过 → 只剩 4 个文档、1 批
    assert '只发文档' in txt, txt
    assert '4 个文件' in txt and '1 批' in txt, txt

    # 关掉「只发文档」后：67 个文件；每批默认 20（实测 40 个附件就会被官网拒收）
    a._ds_docs_only = False
    a._ds_limit = 20
    a._ds_update_hint()
    txt = _hint_text(a)
    assert '67 个文件' in txt, txt
    assert '分 4 批' in txt, txt
    a._ds_docs_only = True

    img_ids = {u['id'] for u in units if u['kind'] == 'image'}
    for it in a.list.items:
        if it['wxid'] in img_ids:
            it['sel'] = False
    a.ds_selected -= img_ids
    a._ds_update_hint()
    txt = _hint_text(a)
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


# ── 回归：官网页签的文字重叠 ──
#
# 事故（用户截图圈了 6 处）：那一页的 Y 坐标是**一个个手调出来的**
# （副标题 70 / 工具栏 96 / 提示 132 / 警示 148 / 清单 164 / 选项 H-58 /
#  路径 H-116 / 日志 H-24），谁也不认识谁，于是
#   · 勾选统计那行被「发送清单」标题盖住；
#   · 副标题和页签按钮挤在同一行；
#   · 警示和工具栏同一水平线；
#   · 「只发文档」复选框压在清单框下沿；
#   · 「导出目录」和底部提示叠成一团。
# 现在坐标只有**一个来源**：app_plus.ds_page_layout()（文件顶部有说明）。
# 下面两条把"任意两行矩形不相交"固定成不变式：
#   ① 布局表（纯函数，可以随便造窗口尺寸）—— 主检查；
#   ② 真画一遍之后用 tk 的 bbox 量（字体高度、折行数都是真的）—— 兜底检查。

import itertools


def _rows_of(lay):
    """布局表里所有**行/控件**矩形：(键, 中文名, 矩形)。

    不包含 `tool_rect`（工具栏整行）：它本来就是"这一带的容器"，
    和里面每个按钮天然相交。工具栏占的高度用 `tool_h` 另外断言。
    """
    out = [(k, n, r) for k, n, r, _, _ in lay['header']]
    out += [(k, n, r) for k, n, r, _, _ in lay['text_rows']]
    out += [(f'btn:{n}', f'工具栏按钮「{n}」', r) for n, r in lay['buttons']]
    out += [('list', '发送清单', lay['list_rect']),
            ('path', '导出目录', lay['path_rect']),
            ('options', '选项行（只发文档/每批N个）', lay['opt_rect']),
            ('log', '底部日志', lay['log_rect'])]
    return out


# 一个够用的按钮定义（和 App.DS_TOOLBAR_* 一致），纯粹为了把按钮矩形算出来
_BTNS = dict(left_buttons=_A.App.DS_TOOLBAR_LEFT,
             right_buttons=_A.App.DS_TOOLBAR_RIGHT)


@pytest.mark.parametrize('W,H', [(1060, 700), (940, 700), (1060, 640),
                                 (940, 640), (1280, 800)])
def test_ds_layout_rows_never_overlap(W, H):
    """官网页签：布局表里任意两行矩形不相交（含用户圈出的那 6 处重叠）。"""
    lay = _A.ds_page_layout(W, H, **_BTNS)
    rows = _rows_of(lay)
    for (k1, n1, r1), (k2, n2, r2) in itertools.combinations(rows, 2):
        assert not _A._rect_hit(r1, r2), (
            f'{W}x{H}：{n1}{r1} 与 {n2}{r2} 重叠')


@pytest.mark.parametrize('W,H', [(1060, 700), (940, 700), (1060, 640),
                                 (940, 640), (1280, 800)])
def test_ds_layout_keeps_browser_and_clickable_columns(W, H):
    """两列硬约束（接管文档 5.8）：
      ① 浏览器矩形（独立顶层窗口）不许压住要点击的控件；
      ② 底部要点击的行必须留在 x < DS_BROWSER_X 的左栏里，否则点不到。
    """
    lay = _A.ds_page_layout(W, H, **_BTNS)
    br = lay['browser_rect']
    for k, n, r in _rows_of(lay):
        if k == 'log':
            continue        # 日志行画在浏览器下沿之下
        assert not _A._rect_hit(r, br), f'{W}x{H}：{n}{r} 被浏览器矩形{br}压住'
    # 工具栏整行（含两行排布时的高度）也必须完全在浏览器矩形之上
    assert lay['tool_y'] + lay['tool_h'] <= br[1], (
        f'{W}x{H}：工具栏底部 {lay["tool_y"] + lay["tool_h"]} 伸进了浏览器矩形 {br}')
    # 要点击的按钮：不许和浏览器矩形相交（宽度够时「开始发送」落在浏览器
    # 右边那一小条上，也不算被盖住）。其余按钮必须全部待在左栏里。
    for n, r in lay['buttons']:
        assert not _A._rect_hit(r, br), f'{W}x{H}：按钮「{n}」{r} 被浏览器矩形{br}压住'
        if n == '🚀 开始发送':
            continue
        assert r[2] <= _A.DS_BROWSER_X, f'{W}x{H}：按钮「{n}」{r} 伸进浏览器区'
    # 而且都在窗口里
    for k, n, r in _rows_of(lay):
        assert r[0] >= 0 and r[1] >= 0, f'{W}x{H}：{n} 跑到窗口左上角外面 {r}'
        assert r[2] <= W and r[3] <= H, f'{W}x{H}：{n} 跑到窗口外面 {r}'


def test_ds_layout_matches_folded_text(tagged):
    """布局表算的折行数必须和真正画出来的行数一致。

    事故预防：提示/警示行是按像素宽度折行的，折行数直接决定下面的坐标。
    如果画的是一套、算的是另一套，清单就会盖住提示行（这正是老代码的问题）。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.ds_out_root = ''
    a.ds_units = []
    a.build_ds()
    a.root.update()
    cv = a.sf.canvas
    lay = a._ds_layout()
    assert len(cv.find_withtag('dshintline')) == len(
        a._ds_wrap(a._ds_hint_text, lay['hint_rect'][2] - lay['hint_rect'][0], 9))
    assert len(cv.find_withtag('dswarnline')) == len(
        a._ds_wrap(a._ds_warn_text, lay['warn_rect'][2] - lay['warn_rect'][0], 9))
    # 每块文字只有**一个**「第一行」图元。
    # 事故：build_ds 里原来还建了一个空的 dshint 占位图元，而 Canvas 的
    # itemcget(tag) 在多图元同 tag 时返回的是**第一个**（不是最新建的），
    # 于是 itemcget('dshint') 读回空字符串 —— 提示行看起来像没画上。
    assert len(cv.find_withtag('dshint')) == 1
    assert len(cv.find_withtag('dswarn')) == 1


def test_ds_theme_syncs_page_and_window_background(tagged):
    """切主题要把**两样**东西同步给内嵌浏览器：页面配色 + 窗口底色。

    为什么要底色：浏览器是独立顶层窗口，页面重绘的那一瞬间会露出它自己的
    backgroundColor；纯白配深色面板就会闪一个白角（用户说的"两个软件"里
    有一部分就是这种边界感）。所以 /theme 之外还有一条 /bg。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))

    class Host:
        running = True
        calls = []

        def set_theme(self, mode):
            Host.calls.append(('theme', mode))
            return {'ok': True}

        def set_background(self, color):
            Host.calls.append(('bg', color))
            return {'ok': True}

    a.ds = Host()
    try:
        a.theme = _A.T.THEMES['dark']
        a._ds_apply_theme()
        a.theme = _A.T.THEMES['light']
        a._ds_apply_theme()
    finally:
        a.ds = None
    assert ('theme', 'dark') in Host.calls, Host.calls
    assert ('theme', 'light') in Host.calls, Host.calls
    assert ('bg', a.DS_PANEL_FILL) in Host.calls, Host.calls
    assert len([c for c in Host.calls if c[0] == 'bg']) == 2, '每次切主题都要同步底色'


def test_ds_widgets_are_placed_at_layout_coordinates(tagged):
    """控件必须画在布局表说的位置上（表算得对、控件放错，一样会压字）。

    这是"布局表成为唯一事实来源"这条约束的守门测试：以后谁把某个坐标写回
    常数，这条就会红。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.ds_out_root = ''
    a.ds_units = []
    a.build_ds()
    a.root.update()
    lay = a._ds_layout()

    assert (a.list.x, a.list.y) == (38, lay['list_y']), '发送清单没放在布局表的位置'
    assert a.list.h == lay['list_h']
    assert a.list.w == a.DS_LEFT_W
    assert a.list.HEADER_H == lay['list_head'], '表头高度变了要同步布局表'

    got = {str(getattr(w, 'text', '')): (w.x, w.y, w.w, w.h) for w in a._widgets
           if hasattr(w, 'x') and hasattr(w, 'h')}
    # 用**带按钮定义**的那一份表（不传按钮得到的是一组空按钮，别拿它比）。
    # 这里直接复算一遍表，顺便验证 App 放进表里的按钮定义没被改坏。
    lay_btn = _A.ds_page_layout(1060, 700, left_buttons=_A.App.DS_TOOLBAR_LEFT,
                                right_buttons=_A.App.DS_TOOLBAR_RIGHT)
    for label, rect in lay_btn['buttons']:
        wx, wy, ww, wh = got.get(label, (None, None, None, None))
        assert (wx, wy, wx + ww, wy + wh) == rect, (
            f'按钮「{label}」位置不对：{(wx, wy, wx + ww, wy + wh)} != {rect}')
    # 底部那两行的纵向位置
    assert a._ds_opt_y == lay['opt_y']
    assert a._batch_btn.y == lay['opt_y'] - 13
    assert a._batch_btn.text == a._batch_label()
    cb = a._docs_cb
    assert cb.y >= lay['list_rect'][3] + 6, (
        f'复选框压在清单下沿上：cb.y={cb.y} 清单底={lay["list_rect"][3]}')
    assert cb.y + cb.box <= lay['opt_rect'][3], (
        f'复选框超出选项行：{cb.y + cb.box} > {lay["opt_rect"][3]}')
    # 浏览器矩形＝画布上那块占位面板（严丝合缝，不许外扩）
    assert a._ds_rect() == (lay['browser_rect'][0], lay['browser_rect'][1],
                            lay['browser_rect'][2] - lay['browser_rect'][0],
                            lay['browser_rect'][3] - lay['browser_rect'][1])


def test_ds_follow_skips_when_minimized(tagged):
    """主窗口最小化时不要再去摆浏览器窗口（**Electron 后端的逻辑**）。

    事故预防：主窗口最小化后 `<Configure>` 还会来，如果无脑 move()，浏览器
    会被"拽"回一个屏幕外的位置或反复显示 —— 而且它本来该在最小化时藏起来
    （见 `_on_root_unmap`，那正是为了消掉"桌面上孤零零一块官网"的观感）。

    ⚠️ 这条只对 **electron** 后端成立：webview2 是画布子控件，跟随时它自动跟着走，
    `_ds_follow()` 会直接返回（这正是"真嵌入"要的效果），所以这里显式把后端切成 electron。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.settings['ds_backend'] = 'electron'

    class Host:
        running = True
        moved = []

        def move(self, x, y, w, h):
            Host.moved.append((x, y, w, h))

    a.ds = Host()
    a.page = 'ds'
    a._ds_visible = True
    saved = a.root.state
    try:
        a.root.state = lambda: 'iconic'
        a._ds_follow()
        assert Host.moved == [], '最小化时不该摆位，实际摆了 %s' % (Host.moved,)
        a.root.state = lambda: 'normal'
        a._ds_follow()
        assert len(Host.moved) == 1, '恢复后应该摆一次位，实际 %s' % (Host.moved,)
    finally:
        a.root.state = saved
        a.ds = None
        a.page = 'sessions'
        a._ds_visible = False
        a.settings.pop('ds_backend', None)


def test_ds_follow_is_noop_for_webview2(tagged):
    """webview2 后端不需要"跟随"：子控件跟着父窗口走，`_ds_follow()` 必须直接返回。

    这条是"真嵌入"的守门断言：一旦有人把跟随逻辑又接回 webview2 后端，
    就说明我们又在"把子控件当独立窗口摆位"，那种错位正是用户抱怨的观感问题。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.settings['ds_backend'] = 'webview2'

    class Host:
        running = True
        moved = []

        def move(self, x, y, w, h):
            Host.moved.append((x, y, w, h))

    a.ds = Host()
    a.page = 'ds'
    a._ds_visible = True
    try:
        a._ds_follow()
        assert Host.moved == [], 'webview2 不该有跟随动作，实际 %s' % (Host.moved,)
    finally:
        a.ds = None
        a.page = 'sessions'
        a._ds_visible = False
        a.settings.pop('ds_backend', None)


def test_hover_highlight_follows_mouse_after_search(app):
    """★ 搜索之后，鼠标移到某一行上，那一行必须变灰（悬停高亮）。

    事故（用户实测："搜索词填完后鼠标移动到会话上会话没有变灰"）：
    `hover_row` 存的是**可见行号**，而 `_draw_row` 拿它当 **items 下标**用。
    不过滤时两者相等，所以一直没暴露；一搜索 `view[row] != row`，
    悬停可见第 0 行点亮的是 `items[0]` —— 高亮跑到别的行，用户看到"移上去没反应"。
    （这和 `_on_click` 那个"点会话跳出随机会话"是同一个根因的两面。）
    """
    app = _fresh_sessions(app)
    lst = app.list
    lst.items[3]['title'] = '命中目标'
    lst.items[7]['title'] = '命中目标二'

    # 未搜索时先悬停一行
    y3 = lst.y + lst.HEADER_H + 3 * lst.ROW_H + lst.ROW_H // 2
    app.sf.canvas.event_generate('<Motion>', x=lst.x + 200, y=y3, when='now')
    app.root.update()
    assert lst.hover_row == 3, '前置条件：未搜索时应该悬停到 items[3]'

    # 输入搜索词：剩下 items[3] 和 items[7]
    app.search_var.set('命中目标')
    app.root.update()
    assert lst.view == [3, 7], '前置条件：过滤后应只剩 items[3] 与 items[7]'

    # ⚠️ 关键：鼠标先移出列表（把高亮撤掉），再移到过滤结果的第 0 行。
    #    不能直接拿"刚搜索完"当基线 —— 那时指针还停在第 3 行上，高亮本来就在。
    app.sf.canvas.event_generate('<Motion>', x=lst.x + 200, y=lst.y - 20, when='now')
    app.root.update()
    assert lst.hover_row == -1, '移出列表后高亮应清零'
    base3 = len(app.sf.canvas.find_withtag(lst._row_tag(3)))

    y0 = lst.y + lst.HEADER_H + 0 * lst.ROW_H + lst.ROW_H // 2
    app.sf.canvas.event_generate('<Motion>', x=lst.x + 200, y=y0, when='now')
    app.root.update()
    assert lst.hover_row == 3, (
        '★ 搜索后 hover_row 应按 view 换算成 items 下标（期望 3，实际 %s）'
        % lst.hover_row)
    after3 = len(app.sf.canvas.find_withtag(lst._row_tag(3)))
    assert after3 > base3, (
        '★ 悬停的那一行没有多出高亮背景图元（移开=%d 悬停=%d）—— 就是"没有变灰"'
        % (base3, after3))
    # 别的行不许被点亮（老 bug 就是把高亮打到了 items[0] 上）
    assert len(app.sf.canvas.find_withtag(lst._row_tag(0))) == 0, \
        '高亮跑到 items[0] 上去了（view/items 下标又混用了）'

    # 移到过滤结果的第 1 行 → 高亮跟着走，前一行要撤掉
    y1 = lst.y + lst.HEADER_H + 1 * lst.ROW_H + lst.ROW_H // 2
    app.sf.canvas.event_generate('<Motion>', x=lst.x + 200, y=y1, when='now')
    app.root.update()
    assert lst.hover_row == 7, '悬停第 1 行应换成 items[7]，实际 %s' % lst.hover_row
    assert len(app.sf.canvas.find_withtag(lst._row_tag(3))) == base3, \
        '换行之后上一行的高亮要撤掉'


def test_ds_page_text_boxes_never_overlap(tagged):
    """真画一遍之后，用 tk 自己的 bbox 量：任意两段文字都不相交。

    为什么还要这条：纯函数只能保证"表里写的数"，字号/字体/折行是 tk 算的。
    这里把**真正画上去的**矩形拿回来再查一遍 —— 包括清单自己的表头
    （「发送清单」标题 + 「共 N 个 · 已选 M 个」），老代码的 6 处重叠里
    第一处就是它盖住了勾选统计那行。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    a.ds_out_root = ''
    a.ds_units = []
    a.build_ds()
    a.root.update()
    cv = a.sf.canvas

    items = []
    for tag in ('dshintline', 'dswarnline', 'dspath', 'dslog', 'dssubtitle'):
        for i in cv.find_withtag(tag):
            if str(cv.itemcget(i, 'text') or '').strip():
                items.append((tag, i))
    # 清单面板的表头一带（标题/计数）单独量：整表重绘会攒图元，
    # 这里只取**当前**这块面板上、位于表头高度内的文字
    lx, ly, lx2, ly2 = a.list.x, a.list.y, a.list.x + a.list.w, a.list.y + a.list.HEADER_H
    for i in cv.find_overlapping(lx, ly, lx2, ly2):
        if cv.type(i) == 'text' and str(cv.itemcget(i, 'text') or '').strip():
            items.append(('listheader', i))

    boxes = []
    for tag, i in items:
        bb = cv.bbox(i)
        assert bb, f'{tag} 图元 {i} 没有 bbox'
        boxes.append((tag, str(cv.itemcget(i, 'text'))[:24], bb))
    assert len(boxes) >= 5, f'官网页签的文字太少？只找到 {boxes}'
    for (t1, x1, b1), (t2, x2, b2) in itertools.combinations(boxes, 2):
        assert not _A._rect_hit(b1, b2), (
            f'文字重叠：{t1}「{x1}」{b1} 与 {t2}「{x2}」{b2}')


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


def test_ds_page_has_no_text_send_box(tagged, tmp_path):
    """官网页签**不该**再有那条「发给 AI」输入框。

    它曾经画在 canvas x 560~934 —— 右侧内嵌浏览器从 x=382 起，所以它整条压在
    官网页面头上，用户看到它悬在页面上方，以为"发话必须走这条"，反而找不到
    官网自己的输入框（用户实测反馈）。它当初存在的理由是"跨进程子窗口收不到
    键盘"，而那是 ds_bridge.host._attach_input() 里 ctypes.wintypes 没 import
    的 bug（已修）。现在内嵌页能正常收键盘，这条 UI 就是纯干扰。

    后端通道（host.type_text / Electron 的 /type）保留，继续供排查使用。
    """
    a = _no_ds_boot(_fresh_sessions(tagged))
    _ds_ready_app(a, tmp_path)
    assert not hasattr(a, '_ask_entry'), '「发给 AI」输入框应该已经删掉'
    assert not hasattr(a, 'btn_ds_ask'), '「发给 AI」发送键应该已经删掉'
    assert not hasattr(a, '_ds_ask'), '_ds_ask 方法应该一起删掉'
    # 开始发送键必须在，别把主功能删错了
    assert hasattr(a, 'btn_ds_send')
    a.ds = None


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
