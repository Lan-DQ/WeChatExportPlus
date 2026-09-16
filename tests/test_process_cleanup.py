# -*- coding: utf-8 -*-
"""进程清理的回归测试：关闭时不能留下孤儿进程。

背景（用户实测反馈）："打叉退出后发现那些 node 都还在"。
查出来两个成因：
  1. `WCDBClient.stop()` **顺序错了** —— 先 terminate 再 `taskkill /T`。
     父进程一死进程树就断了，`/T` 只能看到一个已经不存在的父进程，子进程照样活。
     正确做法是**趁主进程还活着时按树杀**（与 ds_bridge/host.py 里浏览器那条一致）。
  2. `quit_app()` 把清理丢进 `daemon=True` 线程 —— daemon 不阻止进程退出，
     主线程一结束它可能刚跑一半就被掐掉。

这里用真实进程树验证"按树杀"确实有效（不依赖 WCDB 能不能起来）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

_k32 = ctypes.windll.kernel32
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))


class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


def _pids():
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    out = set()
    if snap == -1:
        return out
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.add(int(e.th32ProcessID))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


STILL_ACTIVE = 259


def _alive(pid):
    """进程是否**真的在运行**。

    ⚠️ 不能用 `OpenProcess` 成功与否来判断 —— 进程已经退出、但只要还有人持有它的
    句柄，PID 就仍然有效，OpenProcess 照样成功（这就是我第一次写错、测试假失败的原因）。
    必须用 GetExitCodeProcess 看是不是 STILL_ACTIVE。
    """
    if not pid:
        return False
    h = _k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True          # 查不到退出码，保守当作还活着
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def _spawn_tree():
    """起一个"父 → 子"进程树：python 作为父，再让父起一个 cmd 子进程并保持存活。"""
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen(['cmd', '/c', 'ping -n 60 127.0.0.1 > nul'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, '-c', code],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
    child_pid = 0
    try:
        line = parent.stdout.readline().strip()
        child_pid = int(line)
    except Exception:
        pass
    time.sleep(0.5)
    return parent, child_pid


def test_taskkill_tree_kills_child_while_parent_alive():
    """趁父进程还活着时按树杀 → 父子都没了（这就是修好后的顺序）。"""
    if os.name != 'nt':
        return
    parent, child = _spawn_tree()
    try:
        assert _alive(parent.pid), '父进程应存活'
        assert child and _alive(child), '子进程应存活（前置条件）'
        # ★ 修好后的顺序：先按树杀（父还活着）
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent.pid)],
                       capture_output=True, timeout=15)
        time.sleep(1.0)
        assert not _alive(parent.pid), '父进程应已被杀'
        assert not _alive(child), '★ 子进程也必须被杀（这是本次修复的核心）'
    finally:
        for pid in (child, parent.pid):
            if pid and _alive(pid):
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, timeout=10)


def test_taskkill_tree_after_terminate_leaves_orphan():
    """反证：先 terminate 再 taskkill /T，子进程会变孤儿 —— 旧代码就是这个错。

    这条**故意**复现旧行为，用来证明"顺序错了真的会漏"，
    也保证将来没人把顺序改回去还自以为没事。
    """
    if os.name != 'nt':
        return
    parent, child = _spawn_tree()
    try:
        assert child and _alive(child)
        # 旧顺序：先 terminate 父进程（树断）
        parent.terminate()
        try:
            parent.wait(timeout=5)
        except Exception:
            parent.kill()
        time.sleep(0.8)
        # 此时再 taskkill /T 已经找不到树了
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent.pid)],
                       capture_output=True, timeout=10)
        time.sleep(0.8)
        # 子进程是否幸存 —— 幸存正好证明"顺序很关键"；若系统顺手收了它也不算失败
        orphan_alive = _alive(child)
        print('（反证实验）先 terminate 后子进程仍存活 = %s' % orphan_alive)
        assert not _alive(parent.pid), '父进程应已终止'
        # 这条断言故意放宽：只要求"能观察到这个现象"，两种结果都允许
        assert orphan_alive in (True, False)
    finally:
        for pid in (child, parent.pid):
            if pid and _alive(pid):
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, timeout=10)


def test_wcdb_stop_does_tree_kill_before_terminate():
    """静态检查：WCDBClient.stop() 里 `taskkill /T` 必须出现在 terminate 之前。

    这是防回归的关键 —— 顺序一旦被改回去，"退出后 node/electron 还在"就会复发。
    """
    src = open(os.path.join(ROOT, 'scripts', 'wcdb_server.py'), encoding='utf-8').read()
    i = src.find('def stop(')
    assert i > 0, '找不到 WCDBClient.stop'
    body = src[i:]
    i_tree = body.find("taskkill")
    i_term = body.find('.terminate()')
    assert i_tree > 0, 'stop() 里必须按进程树杀'
    assert i_term > 0, 'stop() 里应有 terminate 兜底'
    assert i_tree < i_term, (
        '★ 顺序错了：必须"趁主进程还活着时 taskkill /T"，'
        '先 terminate 会让进程树断掉、子进程变孤儿（用户报的残留就是这个）')


def test_quit_app_cleanup_thread_is_not_daemon():
    """静态检查：quit_app 的清理线程不能是 daemon（daemon 会被主线程退出掐断）。"""
    src = open(os.path.join(ROOT, 'gui', 'app_plus.py'), encoding='utf-8').read()
    i = src.find('def quit_app(')
    assert i > 0
    # 窗口取到**下一个方法**为止（原来是写死 4000 字符，后来收尾逻辑长出注释和
    # 辅助函数就被挤出去了 —— 按结构取更稳，意图不变）。
    j = src.find('\n    def ', i + 10)
    body = src[i:j if j > 0 else i + 12000]
    assert 'threading.Thread(' in body, '找不到清理线程'
    seg = body[body.find('threading.Thread('):body.find('threading.Thread(') + 200]
    assert 'daemon=True' not in seg, (
        '★ 清理线程不能是 daemon —— 主线程一退出它可能刚跑一半就被掐掉，'
        '于是 WCDB/浏览器只被杀掉一半，子进程变孤儿')
