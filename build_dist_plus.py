# -*- coding: utf-8 -*-
"""构建发布包：dist/WeChatExportPlus/（双击 WeChatExportPlus.exe 即可运行）。

与上游 build_dist.py 的区别：
  - 入口换成 gui/app_plus.py；
  - 运行时（node.exe / dll / electron / resources / node_modules）从本机已有的
    原版 WeChatExport 目录复制，避免依赖作者机器上的绝对路径；
  - 复制前会校验必需文件都在，缺一个就直接报错退出，不产出半成品发布包。

用法：
    python build_dist_plus.py                # 自动探测内核目录
    python build_dist_plus.py --kernel <原版WeChatExport目录>
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, 'dist', 'WeChatExportPlus')
BUILD = os.path.join(ROOT, 'build')
APP_NAME = 'WeChatExportPlus'

# 本机原版安装位置的候选（用于取运行时文件）
KERNEL_CANDIDATES = [
    r'C:\My_GongJu\grab\grab\WeChatExport',
    os.path.join(ROOT, '..', 'grab', 'WeChatExport'),
]

# 发布包里必须存在的运行时文件（相对发布目录）
REQUIRED = [
    'runtime/node.exe',
    'dll/WCDB.dll',
    'dll/wcdb_api.dll',
    'dll/SDL2.dll',
    'dll/wx_key.dll',
    'scripts/wcdb_server.js',
    'scripts/wcdb_server.py',
    'scripts/get_key.js',
    'scripts/decrypt_image.js',
    'scripts/node_modules/koffi',
    'scripts/node_modules/fzstd',
    'resources/native/weflow-image-native-win32-x64.node',
]

HIDDEN_IMPORTS = [
    'wcdb_server', 'media_resolver', 'image_decoder', 'packed_info_parser',
    'logger', 'html_exporter', 'pdf_exporter', 'csv_exporter', 'excel_exporter',
    'message_content', 'batch_export', 'index_exporter', 'ai_exporter', 'md_exporter',
    'ai_prompt',
    'fpdf', 'fpdf.fonts', 'openpyxl', 'PIL', 'Crypto.Cipher.AES',
]


def log(msg):
    print(msg, flush=True)


def find_kernel(explicit):
    if explicit:
        if not os.path.isdir(explicit):
            raise SystemExit(f'指定的内核目录不存在: {explicit}')
        return explicit
    for c in KERNEL_CANDIDATES:
        c = os.path.abspath(c)
        if os.path.isdir(os.path.join(c, 'runtime')) or os.path.isdir(os.path.join(c, 'dll')):
            return c
    raise SystemExit(
        '找不到原版 WeChatExport 目录（需要它的 runtime/dll/resources）。\n'
        '请用 --kernel 指定，例如:\n'
        r'  python build_dist_plus.py --kernel "C:\My_GongJu\grab\grab\WeChatExport"')


def copy_runtime(kernel):
    log('\n[2] 复制运行时（node / dll / electron / resources）...')
    os.makedirs(DIST, exist_ok=True)

    # node.exe
    src = os.path.join(kernel, 'runtime', 'node.exe')
    if not os.path.exists(src):
        raise SystemExit(f'缺少 node.exe: {src}')
    dst_dir = os.path.join(DIST, 'runtime')
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy2(src, dst_dir)
    log('  [OK] runtime/node.exe')
    for f in ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        p = os.path.join(kernel, 'runtime', f)
        if os.path.exists(p):
            shutil.copy2(p, dst_dir)

    # dll
    dll_dst = os.path.join(DIST, 'dll')
    os.makedirs(dll_dst, exist_ok=True)
    for f in ('WCDB.dll', 'wcdb_api.dll', 'SDL2.dll', 'wx_key.dll'):
        p = os.path.join(kernel, 'dll', f)
        if not os.path.exists(p):
            raise SystemExit(f'缺少内核 DLL: {p}')
        shutil.copy2(p, dll_dst)
    log('  [OK] dll/ (WCDB, wcdb_api, SDL2, wx_key)')

    # resources（原生图片解密模块 + ffmpeg）
    res_src = os.path.join(kernel, 'resources')
    res_dst = os.path.join(DIST, 'resources')
    if os.path.isdir(res_src):
        if os.path.isdir(res_dst):
            shutil.rmtree(res_dst)
        shutil.copytree(res_src, res_dst)
        log('  [OK] resources/')
    else:
        raise SystemExit(f'缺少 resources 目录: {res_src}')

    # electron（WCDB 运行时的备选，原版发布包里带着）
    el_src = os.path.join(kernel, 'electron')
    el_dst = os.path.join(DIST, 'electron')
    if os.path.isdir(el_src):
        if os.path.isdir(el_dst):
            shutil.rmtree(el_dst)
        shutil.copytree(el_src, el_dst)
        log('  [OK] electron/')
    else:
        log('  [跳过] 未找到 electron/（若 runtime/node.exe 可用则不影响）')


def copy_scripts():
    log('\n[3] 复制内核脚本与 node 依赖...')
    dst = os.path.join(DIST, 'scripts')
    os.makedirs(dst, exist_ok=True)
    for f in ('wcdb_server.js', 'wcdb_server.py', 'get_key.js', 'decrypt_image.js'):
        src = os.path.join(ROOT, 'scripts', f)
        if os.path.exists(src):
            shutil.copy2(src, dst)
    # node_modules（koffi / fzstd / @koromix）
    nm_src = os.path.join(ROOT, 'scripts', 'node_modules')
    nm_dst = os.path.join(dst, 'node_modules')
    if os.path.isdir(nm_src):
        if os.path.isdir(nm_dst):
            shutil.rmtree(nm_dst)
        shutil.copytree(nm_src, nm_dst)
        log('  [OK] scripts/ + node_modules/')
    else:
        raise SystemExit(
            f'缺少 node_modules: {nm_src}\n'
            '请先从原版发布包复制：\n'
            r'  xcopy /E /I "C:\My_GongJu\grab\grab\WeChatExport\scripts\node_modules" '
            r'"scripts\node_modules"')


def copy_exporters():
    log('\n[4] 复制导出器源码（便于排查问题/二次修改）...')
    src = os.path.join(ROOT, 'exporters')
    dst = os.path.join(DIST, 'exporters')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    log('  [OK] exporters/')


def copy_icon():
    for name in ('icon.ico',):
        src = os.path.join(ROOT, 'gui', name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))
            log(f'  [OK] {name}')


def copy_ai_prompt():
    """把「给 AI 的指令」模板放到 exe 同目录。

    程序每次导出都会读这个文件并写进产物；用户改了立即生效，无需重新打包。
    """
    src = os.path.join(ROOT, 'AI提示词.txt')
    if not os.path.exists(src):
        log('  [跳过] 未找到 AI提示词.txt（导出时会用内置默认指令）')
        return
    shutil.copy2(src, os.path.join(DIST, 'AI提示词.txt'))
    log('  [OK] AI提示词.txt（可编辑，随每份导出一起交给 AI）')


def write_launcher():
    launcher = ('@echo off\n'
                'chcp 65001 >nul\n'
                'title 微信聊天记录批量导出工具\n'
                f'start "" "{APP_NAME}.exe"\n')
    with open(os.path.join(DIST, '启动工具.bat'), 'w', encoding='utf-8') as f:
        f.write(launcher)
    log('  [OK] 启动工具.bat')


def verify_dist():
    log('\n[6] 校验发布包完整性...')
    missing = [r for r in REQUIRED if not os.path.exists(os.path.join(DIST, r))]
    if missing:
        for m in missing:
            log(f'  [缺失] {m}')
        raise SystemExit('发布包不完整，缺少上面的文件。')
    log(f'  [OK] 必需文件齐全（{len(REQUIRED)} 项）')

    total = 0
    for dp, _dn, fns in os.walk(DIST):
        for f in fns:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    log(f'  发布包大小: {total // 1024 // 1024} MB')
    log(f'  发布包路径: {DIST}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kernel', default='', help='原版 WeChatExport 目录（取运行时用）')
    ap.add_argument('--skip-build', action='store_true', help='跳过 PyInstaller，只组装目录')
    args = ap.parse_args()

    kernel = find_kernel(args.kernel)
    log('=' * 60)
    log(f'构建 {APP_NAME}')
    log(f'内核目录: {kernel}')
    log(f'输出目录: {DIST}')
    log('=' * 60)

    # 清理
    for d in (BUILD,):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    if not args.skip_build:
        log('\n[1] PyInstaller 打包 GUI...')
        cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
               '--windowed', '--name', APP_NAME,
               '--distpath', DIST,
               '--workpath', BUILD,
               '--specpath', BUILD,
               '--paths', ROOT,
               '--paths', os.path.join(ROOT, 'scripts'),
               '--paths', os.path.join(ROOT, 'exporters'),
               '--icon', os.path.join(ROOT, 'gui', 'icon.ico')]
        # 开发期 ROOT 下可能有指向内核大目录的 junction（runtime/dll/electron/resources），
        # 明确排除掉，避免 PyInstaller 顺着链接把几百 MB 的二进制塞进 exe。
        for name in ('runtime', 'dll', 'electron', 'resources', 'gui', 'tests', 'tasks'):
            cmd += ['--exclude-module', name]
        for h in HIDDEN_IMPORTS:
            cmd += ['--hidden-import', h]
        cmd.append(os.path.join(ROOT, 'gui', 'app_plus.py'))
        subprocess.run(cmd, cwd=ROOT, check=True)
        log('  [OK] exe 打包完成')
    else:
        log('\n[1] 跳过 PyInstaller')

    copy_runtime(kernel)
    copy_scripts()
    copy_exporters()
    log('\n[5] 复制图标、提示词与启动器...')
    copy_icon()
    copy_ai_prompt()
    write_launcher()
    verify_dist()

    log('\n完成。把整个 dist\\WeChatExportPlus 文件夹拷到任意位置，')
    log(f'双击 {APP_NAME}.exe 或 启动工具.bat 即可运行。')


if __name__ == '__main__':
    main()
