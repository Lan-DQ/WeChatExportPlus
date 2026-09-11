# -*- coding: utf-8 -*-
"""开发环境准备：把原版 WeChatExport 的运行时目录链接进本项目。

链接（junction）而不是复制，是为了避免在项目里多占 500MB；
打包时 build_dist_plus.py 会把真实文件复制进发布包。

用法：
    python dev_setup.py                       # 自动探测原版目录
    python dev_setup.py --kernel <目录>
    python dev_setup.py --remove              # 删除链接
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIRS = ['dll', 'runtime', 'electron', 'resources']

CANDIDATES = [
    r'C:\My_GongJu\grab\grab\WeChatExport',
    os.path.join(ROOT, '..', 'grab', 'WeChatExport'),
]


def find_kernel(explicit):
    if explicit:
        return os.path.abspath(explicit)
    for c in CANDIDATES:
        c = os.path.abspath(c)
        if os.path.isdir(os.path.join(c, 'runtime')):
            return c
    return ''


def link(src, dst):
    if os.path.exists(dst):
        print(f'  跳过（已存在）: {os.path.basename(dst)}')
        return
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', dst, src],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f'  链接 OK: {os.path.basename(dst)} -> {src}')
    else:
        print(f'  链接失败: {os.path.basename(dst)}: {(r.stderr or r.stdout).strip()}')


def unlink(dst):
    if not os.path.exists(dst):
        return
    item = os.path.exists(dst)
    try:
        # 只删除 junction 本身，不会删到目标内容（rmdir 对 junction 是安全的）
        subprocess.run(['cmd', '/c', 'rmdir', dst], capture_output=True, text=True)
        print(f'  已移除: {os.path.basename(dst)}')
    except Exception as e:
        print(f'  移除失败 {dst}: {e}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kernel', default='')
    ap.add_argument('--remove', action='store_true')
    args = ap.parse_args()

    if args.remove:
        print('移除开发链接...')
        for d in DIRS:
            unlink(os.path.join(ROOT, d))
        return

    kernel = find_kernel(args.kernel)
    if not kernel:
        print('找不到原版 WeChatExport 目录。请用 --kernel 指定。')
        sys.exit(1)

    print(f'原版内核目录: {kernel}')
    print('创建开发链接...')
    for d in DIRS:
        src = os.path.join(kernel, d)
        if os.path.isdir(src):
            link(src, os.path.join(ROOT, d))
        else:
            print(f'  源目录不存在，跳过: {d}')

    print('\n现在可以直接开发运行：')
    print('  python gui/app_plus.py')
    print('  python -m pytest tests/ -v')


if __name__ == '__main__':
    main()
