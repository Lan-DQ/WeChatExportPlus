# -*- coding: utf-8 -*-
"""窗口形态的回归防线（纯源码断言，不启动 Electron，秒级跑完）。

守的是 2026-09-16 这几个**实测出来的**结论，防止以后被改回去：
  1. 「嵌入」必须用 **owner 关系**（`GWLP_HWNDPARENT`），**不能**用 `SetParent`
     —— 跨进程子窗口收不到真实键盘（`test_crossproc_kbd.py` 的实测表格）。
  2. `show()` 必须在 `POST /show` **之前摘掉 owner、之后挂回去** ——
     "owned + hidden → show()" 会让 Electron 主进程卡死（10s 超时）。
  3. `/bounds` 里对**有 owner** 的窗口不能再自己 show()。
  4. app_plus 的默认后端是 `embed`（用户指定的稳定基线 = v3.0.0 形态）。
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


HOST = _read(os.path.join('ds_bridge', 'host.py'))
APP = _read(os.path.join('gui', 'app_plus.py'))
MAIN = _read(os.path.join('dsview', 'main.js'))


def test_two_embed_forms_are_both_available():
    """两种嵌入形态都要在，且各按各的规矩来：

    · `embed`（**默认**）= **owner 窗口**（顶层 + owner 关系）：键盘可用，拖动要自己跟；
    · `child`（可选）  = **真子窗口**（`SetParent` + `WS_CHILD`）= **v3.0.0 形态**：
      Windows 自己搬着走、拖动零延迟，但跨进程子窗口**可能收不到真实键盘**。

    ⚠️ 这条测试的由来：早先我断言过"绝不能用 SetParent"（因为合成按键实验显示子窗口
    收不到键盘）。但用户实测 **v3.0.0 的拖动观感最好**（"就像完全就是里面自带的东西"），
    而"子窗口能不能打字"我**无法用脚本判定** —— 所以正确做法是**两种都留着**，
    默认用键盘可用的 owner，把 child 做成一行设置让用户手按选。
    """
    assert 'def set_owner' in HOST and 'GWLP_HWNDPARENT' in HOST, 'owner 形态丢了'
    assert 'def embed_child' in HOST, 'v3.0.0 的子窗口形态丢了'
    seg = HOST.split('def embed_child', 1)[1].split('def _setpos', 1)[0]
    assert 'SetParent' in seg, 'embed_child 没有 SetParent'
    assert 'WS_CHILD' in seg, 'embed_child 没有设 WS_CHILD'
    # 子窗口必须清掉 WS_EX_NOACTIVATE（否则键盘直接被挡）
    assert 'WS_EX_NOACTIVATE' in seg, 'embed_child 没有清 WS_EX_NOACTIVATE'
    # app 侧两种后端都要在
    assert "'child'" in APP, 'app_plus 没有 child 后端'
    assert "'embed', 'child', 'webview2', 'electron'" in APP, '后端白名单没更新'


def test_child_backend_uses_canvas_coords_and_no_dpi_math():
    """`child`（v3.0.0 形态）用**画布坐标**、**不做 DPI 换算**。

    v3.0.0 的 `_setpos(x, y, w, h)` 直接把布局表的矩形交给 `SetWindowPos`；
    子窗口坐标本来就是父窗口客户区坐标，加换算反而会偏。
    """
    # 坐标口径：child 走**画布坐标**那一支（不是屏幕坐标）
    head = APP.split('def _ds_place', 1)[1].split("if backend == 'embed':", 1)[0]
    child_coord = head.split("elif backend == 'child':", 1)[1].split('else:', 1)[0]
    assert '_ds_rect' in child_coord, 'child 后端应当用画布坐标（不能用屏幕坐标）'
    assert '_ds_screen_rect' not in child_coord, 'child 后端混进了屏幕坐标'
    # 摆位分支：按**内容**定位（第一个 `elif backend == 'child':` 是坐标分支，别撞上）
    place = APP.split('def _ds_place', 1)[1]
    marker = '★ v3.0.0 形态：真子窗口'
    assert marker in place, '找不到 child 的摆位分支'
    cseg = place.split(marker, 1)[1]
    assert 'embed_child' in cseg, 'child 摆位分支没有调 embed_child'
    assert '_setpos' in cseg, 'child 摆位分支没有走 _setpos（后续摆位）'
    hseg = HOST.split('def embed_child', 1)[1].split('def _setpos', 1)[0]
    assert 'SetWindowPos' in hseg or '_setpos' in hseg, 'embed_child 没有摆位'


def test_child_backend_is_excluded_from_follow_and_hide():
    """`child` 是子窗口：**不用也不该**自己去跟随/最小化隐藏（Windows 会做）。"""
    guard = APP.split('def _ds_can_follow', 1)[1].split('def _ds_realign', 1)[0]
    assert "'webview2', 'child'" in guard, '跟随守卫没有排除 child'
    um = APP.split('def _on_root_unmap', 1)[1].split('def ', 1)[0]
    assert "'webview2', 'child'" in um, '最小化隐藏没有排除 child'


def test_show_detaches_owner_before_and_reattaches_after():
    """show() 必须先摘 owner、再 POST /show、最后挂回 owner（否则卡死）。"""
    seg = HOST.split('def show(', 1)[1].split('def hide(', 1)[0]
    i_detach = seg.find('GWLP_HWNDPARENT, 0')
    i_post = seg.find("self._post('/show'")
    # 挂回去：POST 之后再设一次 owner
    i_attach = seg.find('GWLP_HWNDPARENT, int(owner)')
    assert i_detach != -1, 'show() 里没有"先摘 owner"'
    assert i_post != -1, 'show() 里没有 /show 调用'
    assert i_attach != -1, 'show() 里没有"把 owner 挂回去"'
    assert i_detach < i_post < i_attach, \
        'show() 的顺序必须是：摘 owner → /show → 挂回 owner'


def test_bounds_does_not_show_owned_window():
    """main.js 的 /bounds 必须在有 owner 时跳过 show()。"""
    seg = MAIN.split("route === '/bounds'", 1)[1].split("route === ", 1)[0]
    assert 'OWNER_SET' in seg, '/bounds 没有用 OWNER_SET 判断'
    # show 调用必须被 !OWNER_SET 保护
    m = re.search(r'if \(!OWNER_SET\)[^}]*?S\.win\.show\(\)', seg, re.S)
    assert m, '/bounds 里的 S.win.show() 没有被 !OWNER_SET 保护'
    assert "route === '/owner'" in MAIN, 'main.js 缺少 /owner 路由'


def test_owner_flag_is_reported_to_electron():
    """host 设/解 owner 时必须告知 Electron（否则它不知道能不能 show）。"""
    seg = HOST.split('def set_owner', 1)[1].split('def ', 1)[0]
    assert "'/owner'" in seg, 'set_owner 没有通知 Electron'
    dseg = HOST.split('def detach', 1)[1]
    assert "'/owner'" in dseg, 'detach 没有通知 Electron'


def test_default_backend_is_embed():
    """默认后端必须是 `embed`（owner 形态）—— 键盘已被用户手按确认可用。

    `child`（v3.0.0 真子窗口）拖动观感更好，但键盘待确认，所以只做可选项。
    """
    assert "get('ds_backend', 'embed')" in APP, '默认后端不是 embed'
    assert "'embed', 'child', 'webview2', 'electron'" in APP, '后端白名单没列全'


def test_embed_backend_uses_screen_coords():
    """`embed`（owner 顶层窗口）摆位必须用屏幕坐标 + 顶层窗口当 owner。"""
    seg = APP.split("if backend == 'embed':", 1)[1].split("elif backend == 'child':", 1)[0]
    assert 'embed(' in seg, 'embed 分支没有调 embed()'
    assert '_ds_top_hwnd' in seg, 'embed 分支没有用顶层窗口句柄当 owner'
    assert 'def _ds_top_hwnd' in APP
    # owner 必须是顶层窗口：_ds_top_hwnd 里要用 GetAncestor(GA_ROOT)
    tseg = APP.split('def _ds_top_hwnd', 1)[1].split('def ', 1)[0]
    assert 'GetAncestor' in tseg and 'GA_ROOT' in tseg
    # 坐标口径：embed 走屏幕坐标（在 _ds_place 的头部选）
    head = APP.split('def _ds_place', 1)[1].split("if backend == 'embed':", 1)[0]
    assert '_ds_screen_rect' in head, 'embed 后端没有用屏幕坐标'


# ── 2026-09-16 第四轮（用户实测反馈的三条观感问题）──

def test_follow_covers_embed_backend():
    """拖动主窗口时 `embed` 也必须跟随。

    用户实测反馈："嵌入窗口不会跟着我拖动这个窗口而动"。
    根因是 `_ds_follow` 里写着 `if self.ds_backend != 'electron': return` —— 那是
    electron 独立窗口时代的写法，把新的 embed 后端一起挡掉了。
    """
    follow = APP.split('def _ds_follow', 1)[1].split('def _ds_can_follow', 1)[0]
    guard = APP.split('def _ds_can_follow', 1)[1].split('def _ds_realign', 1)[0]
    assert "!= 'electron'" not in follow, '跟随逻辑又只认 electron 了（会漏掉 embed）'
    # 子控件/子窗口天然跟随，要排除（判断在 _ds_can_follow 里）
    assert "'webview2', 'child'" in guard, '跟随守卫应排除 webview2/child'
    # 必须走"只移动不抢焦点 + 让位期间不显示"的那条
    realign = APP.split('def _ds_realign', 1)[1].split('def _ds_settle', 1)[0]
    assert 'restore_silent' in realign, '对齐没有用 restore_silent（会抢焦点）'
    assert 'yielding' in realign, '对齐没有区分"让位中"（会盖住提示）'


def test_settle_recheck_after_move_stops():
    """拖动/缩放**停下之后**必须复查并强制对齐。

    用户实测："每次窗口移动之后不动了（位置就不对）"。
    `<Configure>` 是防抖触发的，拖动最后一段可能被合并掉，所以松手后要再量一次
    真实几何、不一致就再对齐。
    """
    follow = APP.split('def _ds_follow', 1)[1].split('def _ds_can_follow', 1)[0]
    assert '_ds_settle' in follow, '跟随结束后没有安排"停下复查"'
    seg = APP.split('def _ds_settle', 1)[1].split('\n    def ', 1)[0]
    assert 'GetWindowRect' in seg, '复查没有量浏览器真实位置'
    assert '_ds_realign' in seg, '复查发现错位后没有重新对齐'


def test_minimize_hide_covers_embed_backend():
    """主窗口最小化时 `embed` 也要跟着消失（否则桌面上留一块官网）。"""
    seg = APP.split('def _on_root_unmap', 1)[1].split('def ', 1)[0]
    assert "!= 'electron'" not in seg, '最小化隐藏又只认 electron 了'
    assert "'webview2', 'child'" in seg, '没有排除子控件/子窗口后端'


def test_toast_yields_for_embed_backend():
    """弹提示时 `embed` 要让位（否则提示被盖住 —— 用户实测反馈过）。"""
    seg = APP.split('def _ds_toast', 1)[1].split('def _ds_after_toast', 1)[0]
    assert "!= 'electron'" not in seg, '提示让位又只认 electron 了'
    assert "== 'webview2'" in seg
    assert 'self.ds.hide()' in seg, '提示没有隐藏浏览器'


def test_dialogs_use_dlg_helper():
    """所有 Toplevel 弹窗都要走 `_dlg()`（它负责让浏览器给弹窗让位）。"""
    assert 'def _dlg(' in APP
    # 除了 _dlg 自己内部的实现，别处不该再直接 new Toplevel
    body = APP.split('def _dlg(', 1)[1].split('\n    def ', 1)[0]
    rest = APP.replace(body, '')
    assert 'tk.Toplevel(self.root)' not in rest, \
        '还有弹窗没走 _dlg()（会被内嵌浏览器盖住）'
    seg = APP.split('def _dlg(', 1)[1].split('def ', 2)[0]
    assert 'Toplevel' in seg and 'hide()' in APP.split('def _dlg_hide_browser', 1)[1].split('def ', 1)[0]


def test_cancel_closes_progress_window_and_clears_attachments():
    """点「取消」/点「X」都要立刻关窗、放开切页、并清掉页面残留附件。

    用户实测反馈："取消发送了，然后想返回导出页面就会叫我等待发完或者点关闭，
    可是那个框我已经关了。"
    ⚠️ 两个入口（取消按钮 / 窗口 X）**必须走同一条收尾路径** —— 只修其中一个，
    用户从另一个入口走就又会撞上那个死结。
    """
    assert 'def stop_and_close(' in APP, '没有统一的收尾函数'
    assert 'def cancel_send(' in APP and 'def close_win(' in APP
    # 两个入口都走 stop_and_close
    cseg = APP.split('def cancel_send(', 1)[1].split('def ', 1)[0]
    wseg = APP.split('def close_win(', 1)[1].split('def ', 1)[0]
    assert 'stop_and_close(' in cseg, '取消按钮没有走统一收尾'
    assert 'stop_and_close(' in wseg, '窗口 X 没有走统一收尾'
    # 统一收尾里必须：关窗 + 放开切页 + 清附件
    seg = APP.split('def stop_and_close(', 1)[1].split('def close_win(', 1)[0]
    assert 'win.destroy()' in seg, '收尾没有关掉进度窗'
    assert '_ds_sending = False' in seg, '收尾没有放开切页限制'
    assert 'clear_attachments' in seg, '收尾后没有清理页面残留附件'
    # 进度窗被关掉之后，finish() 不能再碰已销毁的控件
    fseg = APP.split('def finish(res)', 1)[1].split('draw_bar(0, len(batches))', 1)[0]
    assert 'win_alive' in fseg, 'finish() 没有防"窗口已被取消关掉"'


def test_page_switch_not_blocked_after_cancel():
    """取消之后不许再用"正在发送"拦住切页（那个进度窗可能已经被关掉了）。"""
    seg = APP.split('def _rebuild_page', 1)[1].split('def _close_popups', 1)[0]
    assert '_ds_cancel_flag' in seg, '切页守卫没有考虑"已取消"'
    assert '_ds_worker_alive' in seg, '切页守卫没有考虑"工作线程是否还活着"'


def test_follow_moves_even_while_yielding():
    """让位期间跟随也必须平移；让位**结束**后必须走**完整摆位**。

    两条用户实测：
      · "拖上下左右边框改大小会跟随到正确位置，单靠拖动却错位"
        → 让位期间**不能跳过**平移；
      · "点开始发送后界面会像消失了一样"（窗口可见但一片空白）
        → 让位结束后**不能只做 Win32 摆位**：Electron 不重绘就白屏。
    """
    seg = APP.split('def _ds_follow', 1)[1].split('def _on_root_unmap', 1)[0]
    assert 'yielding' in seg, '跟随没有区分"让位中"'
    realign = APP.split('def _ds_realign', 1)[1].split('def _ds_settle', 1)[0]
    assert 'yielding' in realign, '对齐没有区分"让位中"'
    assert 'move_win32' in realign, '让位期间没有走 Win32（会慢）'
    assert 'show=True' in realign, '让位结束后没有走完整摆位（Electron 不重绘 → 空白）'
    # 不许再出现"检测到让位就 return"的写法（那会让位置停止更新 → 错位）
    import re
    assert not re.search(r'_dlg_depth[^\n]*\)\s*:\s*\n\s*return', seg), \
        '让位期间又直接 return 了（会停止平移 → 错位）'
    assert not re.search(r'_ds_hidden_for_toast[^\n]*\)\s*:\s*\n\s*return', seg), \
        '让位期间又直接 return 了（会停止平移 → 错位）'


def test_restore_after_yield_repaints():
    """让位结束的"恢复"必须触发 Electron 重绘（否则窗口可见但空白）。

    用户实测："点开始发送后界面会像消失了一样"——那片白正是浏览器窗口占的位置，
    `Win32 SetWindowPos` 把它摆对了但 Electron **没重绘**。
    """
    seg = APP.split('def _ds_restore_silent', 1)[1].split('def _ds_toast', 1)[0]
    assert 'restore_silent' in seg and 'show=True' in seg, \
        '恢复没有走 restore_silent(show=True)（不会重绘 → 空白）'


def test_restore_silent_has_show_flag():
    """host.restore_silent 必须能"只移动不显示"（供让位期间使用）。"""
    seg = HOST.split('def restore_silent', 1)[1].split('def ', 1)[0]
    assert 'show=False' in seg, 'restore_silent 没有 show 开关'
    assert "'/restore'" in seg
    # /restore 路由绝不能自己 show（owned + 隐藏的 show 会卡死主进程）
    rseg = MAIN.split("route === '/restore'", 1)[1].split('route ===', 1)[0]
    assert 'S.win.show()' not in rseg, '/restore 里又出现了 show()'


def test_no_console_window_flash_from_subprocess():
    """起**控制台程序**（taskkill 等）必须带 CREATE_NO_WINDOW，否则会闪黑框。

    ⚠️ 用户实测反馈："关闭后跳出来一堆黑框（可能是 cmd）"。
    根因：运行期的 `taskkill` 调用只写了 `capture_output=True` —— 那**只管管道重定向，
    不阻止控制台窗口出现**。退出时 WCDB 与浏览器各自都要 taskkill（有的还会重试），
    一次退出能闪好几个。
    """
    import re
    targets = {
        'ds_bridge/host.py': HOST,
        'ds_bridge/webview2_host.py': _read(os.path.join('ds_bridge', 'webview2_host.py')),
        'scripts/wcdb_server.py': _read(os.path.join('scripts', 'wcdb_server.py')),
    }
    bad = []
    for name, src in targets.items():
        lines = src.splitlines()
        for i, ln in enumerate(lines):
            if 'subprocess.' not in ln or 'taskkill' not in ln:
                continue
            # 该调用可能跨行，往后看 3 行找抑制标志
            window = '\n'.join(lines[i:i + 4])
            if ('CREATE_NO_WINDOW' not in window and 'NO_WINDOW' not in window
                    and '_NO_WINDOW' not in window and 'creationflags' not in window
                    and '**nw' not in window):
                bad.append(f'{name}:{i + 1}: {ln.strip()[:90]}')
    assert not bad, (
        '这些 taskkill 调用没有抑制控制台窗口（会闪黑框）：\n' + '\n'.join(bad))

    """内嵌浏览器的用户数据目录必须在**包外**（否则换包就丢登录态）。

    ⚠️ 踩过两次：`ds_profile` 原来放在包目录里，每次重新打包都是新目录 → 空 profile
    → 打开官网就是登录页，用户被迫重新登录（v3.1.4_embed / v3.1.6 都这样）。
    登录态在 Chromium 里是用户数据目录的 **localStorage `userToken`**，不是 cookie，
    所以只能靠"目录别换"来保住。
    """
    assert 'def _ds_profile_dir(' in APP, '没有做"包外 profile"的处理'
    seg = APP.split('def _ds_profile_dir(', 1)[1].split('DS_PROFILE_DIR =', 1)[0]
    assert 'LOCALAPPDATA' in seg, 'profile 没有放到 LOCALAPPDATA'
    assert 'WXEXPORT_PROFILE_HOME' in seg, '没有留可覆盖的环境变量（测试用）'
    assert 'copytree' in seg, '没有从包内老目录迁移登录态'
    # 最终使用的目录不能等于"包内 ds_profile"
    assert 'DS_PROFILE_DIR = _ds_profile_dir(ROOT)' in APP


def test_account_dir_is_passed_to_wcdb():
    """必须能把**具体账号目录**传给内核（GitHub issue #2 的核心）。

    `wcdb_server.js` 找 session.db 是"**递归找到第一个就用**"，不校验密钥属于哪个账号；
    用户在同一台机器登录过多个微信、旧账号记录还在时，就会拿**别的账号的库**去开
    → 打不开 → **连接超时**（issue #2 那位描述的就是这个）。
    （图片解密那条路早已按路径 wxid 精确匹配，但**数据库这条当时没修** —— 本轮补上。）
    """
    assert 'def find_account_dirs' in APP, '没有"找账号目录"的能力'
    seg = APP.split('def _connect', 1)[1].split('def ', 1)[0]
    assert 'account_dir=' in seg, '_connect 没有把账号目录传给内核'
    # 设置项要能记住选择
    assert "settings['account_dir']" in APP, '账号选择没有被记住'
    # 内核侧要能接收并使用
    wpy = _read(os.path.join('scripts', 'wcdb_server.py'))
    assert 'account_dir' in wpy, 'wcdb_server.py 没有 account_dir 参数'
    assert 'search_root = account_dir or data_dir' in wpy, \
        'wcdb_server.py 没有把账号目录当成搜索根'
    # 首页要有可点的入口
    assert 'def pick_account' in APP, '首页没有"切换账号"的入口'


def test_coord_space_is_consistent_everywhere():
    """坐标口径必须处处一致：只有 `webview2` 用画布坐标，其余都用**屏幕坐标**。

    ⚠️ 这条是踩出来的：`_ds_restore_silent` 里曾经写成
    `if self.ds_backend in ('embed', 'webview2'): x,y,w,h = self._ds_rect()` ——
    把 embed（**owner 顶层窗口**）也当成进程内子控件，用画布坐标去摆一个顶层窗口。
    表现就是"让位期间拖动主窗口，结束后浏览器跳到画布坐标那个点"，
    也就是用户报的"拖边框改大小会跟随，单纯拖动却错位"。
    """
    import re
    bad_sites = []
    for fname, src in (('app_plus.py', APP), ('host.py', HOST)):
        for m in re.finditer(r"in \('embed',\s*'webview2'\)", src):
            # 只在"挑坐标口径"的语境下才是错的（这个组合式本身可能有别的用途）
            line_no = src[:m.start()].count('\n') + 1
            line = src.splitlines()[line_no - 1]
            if '_ds_rect' in line or 'ds_rect' in line:
                bad_sites.append(f'{fname}:{line_no}: {line.strip()}')
    assert not bad_sites, (
        'embed 是顶层窗口（屏幕坐标），不能和 webview2 一起用画布坐标：\n'
        + '\n'.join(bad_sites))

    # `_ds_restore_silent` 必须用屏幕坐标
    seg = APP.split('def _ds_restore_silent', 1)[1].split('def _ds_toast', 1)[0]
    assert '_ds_screen_rect' in seg, '_ds_restore_silent 没有用屏幕坐标'
    assert "== 'webview2'" in seg, '坐标口径判断写错了'




def test_host_has_clear_attachments_and_restore():
    """host 侧要有清附件与"只移动不抢焦点"两条通道。"""
    assert 'def clear_attachments' in HOST
    assert "'/debug/clear-attachments'" in HOST
    assert 'def restore_silent' in HOST
    assert "'/restore'" in HOST
    # main.js 侧对应路由
    assert "route === '/restore'" in MAIN
    assert "route === '/debug/clear-attachments'" in MAIN
    # 清附件用的是渲染器里真实存在的表达式（曾经写成不存在的 exprCountAttachments）
    assert 'R.exprCountWithNames' in MAIN
    assert 'R.exprRemoveAttachments' in MAIN

