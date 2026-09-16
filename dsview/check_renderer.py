# -*- coding: utf-8 -*-
r"""dsview/renderer.js 的机械检查（接管文档 5.1 那个坑的自动化版）。

renderer.js 整个是一个 String.raw 模板字符串：helper 源码被塞在模板里当字符串注入。
所以那个模板内部**绝不能出现反引号**，否则模板提前结束 → 语法错误。
这个坑 2026-09-15 一天内踩了三次（都是往注释里写 xxx() 造成的），所以做成机械检查，
并接进 pytest（tests/test_ui_invariants.py::test_renderer_js_template_intact）。

检查项：
  1. node --check 语法通过
  2. buildHelper() 的模板没被反引号提前截断（结尾必须是反引号+semicolon）
  3. 模板里没有非 HELPER_NAME 的 ${…} 插值

注意：**不要**检查"正则的反斜杠是不是双份" —— 反斜杠在 String.raw 里是原样保留的，
单份 \s 本来就是对的（我一开始搞反了，误报了一堆）。

用法：python dsview/check_renderer.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RENDERER = os.path.join(REPO, 'dsview', 'renderer.js')

OPEN = 'return String.raw' + chr(96)     # return String.raw`
TICK = chr(96)


def check(path=None):
    """返回 (ok, [问题列表])。"""
    path = path or RENDERER
    problems = []
    src = open(path, encoding='utf-8').read()

    # 1) node --check
    r = subprocess.run(['node', '--check', path], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        problems.append('node --check 失败：%s' % (r.stderr or '').strip()[:300])

    # 2) 模板边界 = 开头的 ` 之后第一个 `（模板内不允许再有反引号）
    start = src.find(OPEN)
    if start < 0:
        problems.append('找不到 buildHelper 里的 return String.raw（结构变了？）')
        return (not problems), problems
    body_start = start + len(OPEN)
    end = src.find(TICK, body_start)
    if end < 0:
        problems.append('找不到模板结束的反引号')
        return False, problems
    body = src[body_start:end]
    print('  模板长度 %d 字符（%d 行）' % (len(body), body.count('\n') + 1))

    # 3) 模板必须以 ` 收尾，后面紧跟 `;` 之类的收尾代码
    #    （注意：反斜杠在 String.raw 里是**原样保留**的，所以模板里写正则用单份 \s
    #      是**正确**的，不要当问题报出来 —— 这条我一开始搞反了，误报了一堆。）
    tail = src[end:end + 40]
    if not (tail.startswith(TICK + ';') or tail.startswith(TICK + ')')):
        problems.append('模板结束之后不像收尾（可能被提前截断）：%r' % tail[:60])

    # 4) 只允许 window.${HELPER_NAME} 这一种插值
    for mm in re.finditer(r'\$\{', body):
        ctx = body[mm.start():mm.start() + 40].split('\n')[0]
        if 'HELPER_NAME' not in ctx:
            problems.append('模板内出现非 HELPER_NAME 的插值：%s' % ctx[:80])

    return (not problems), problems


def main():
    ok, probs = check()
    if ok:
        print('renderer.js 检查通过（语法 + 模板完整性）')
        return 0
    print('renderer.js 检查失败：')
    for p in probs:
        print('  ✗', p)
    return 1


if __name__ == '__main__':
    sys.exit(main())
