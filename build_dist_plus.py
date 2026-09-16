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
    # v3.0：内嵌 DeepSeek 官网页（Electron 侧应用 + Python 桥接层）
    'electron/electron.exe',
    'dsview/main.js',
    'dsview/package.json',
    'ds_bridge/__init__.py',
    'ds_bridge/host.py',
    'ds_bridge/plan.py',
    'ds_bridge/sender.py',
]

HIDDEN_IMPORTS = [
    'wcdb_server', 'media_resolver', 'image_decoder', 'packed_info_parser',
    'logger', 'html_exporter', 'pdf_exporter', 'csv_exporter', 'excel_exporter',
    'message_content', 'batch_export', 'index_exporter', 'ai_exporter', 'md_exporter',
    'ai_prompt', 'ui_theme', 'ui_widgets', 'session_tags', 'PIL.ImageTk',
    'PIL.ImageFilter', 'fpdf', 'fpdf.fonts', 'openpyxl', 'PIL', 'Crypto.Cipher.AES',
    'ds_bridge', 'ds_bridge.host', 'ds_bridge.plan', 'ds_bridge.sender',
    # ★ WebView2 真嵌入后端（默认后端）：
    #   · webview2_host 自己是我们写的模块；
    #   · webview / clr / pythonnet 是**运行期才 import** 的（懒加载），
    #     PyInstaller 静态分析看不到，必须显式列出来；
    #   · comtypes 被 webview 的某些后端用到，一并带上免得缺依赖。
    'ds_bridge.webview2_host', 'webview', 'clr', 'pythonnet', 'comtypes',
    # ⚠️ ctypes.wintypes 是子模块，必须显式列出来：`import ctypes` 不会带出它。
    #    漏掉的后果是打包版里 ds_bridge.host._attach_input() 抛 AttributeError
    #    （被 except 吞掉），键盘修复静默失效 —— 真实发生过。
    'ctypes.wintypes',
]

# PyInstaller 额外要"整包收进来"的第三方库。
#
# ⚠️⚠️ **别把 webview / comtypes 加进来**（我加过一次，结果打包版主窗口再也不出现，
# 控制台也不报错，查了很久）：pywebview 带着微软的 .NET 程序集、comtypes 会在
# 打包期去注册表扫类型库，这两样被 --collect-all 拖进来会污染冻结环境，
# 表现是"进程活着但没有窗口"。我们**本来就不需要它们**：
#   · WebView2 的程序集由 `webview2_host._webview_sdk_dir()` 运行时从
#     `webview/lib` 目录加载（`copy_ds()` 会把 webview/lib 一起打进包里）；
#   · .NET 宿主由 pythonnet 自己的 hook（`hook-clr.py`）负责，加个
#     hidden-import 'clr' 就够了。
COLLECT_ALL = []


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


def copy_ds():
    """复制 v3.0 的内嵌 DeepSeek 官网页组件。

      - `ds_bridge/`：Python 侧（宿主管理 + 分批计划 + 发送调度）
      - `dsview/`   ：Electron 侧（页面宿主 + 上传/发送/截断控制 API）

    `dsview/mock/` 与自检脚本是开发期用的假页面，不进发布包。
    """
    log('\n[4.5] 复制 DeepSeek 官网页组件...')
    src = os.path.join(ROOT, 'ds_bridge')
    dst = os.path.join(DIST, 'ds_bridge')
    if not os.path.isdir(src):
        raise SystemExit(f'缺少 ds_bridge 目录: {src}')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    log('  [OK] ds_bridge/')

    src = os.path.join(ROOT, 'dsview')
    dst = os.path.join(DIST, 'dsview')
    if not os.path.isdir(src):
        raise SystemExit(f'缺少 dsview 目录: {src}')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc',
                                                  'mock', 'verify_*.py',
                                                  'node_modules'))
    log('  [OK] dsview/（不含 mock/自检脚本）')

    # ── WebView2 真嵌入后端要用的东西 ──
    # ① pywebview 里的微软程序集目录（webview/lib）：
    #    `Microsoft.Web.WebView2.Core.dll` / `.WinForms.dll` 和 x64 的
    #    `WebView2Loader.dll`。运行期由 `webview2_host._webview_sdk_dir()` 从
    #    `<包根>/webview/lib` 加载 —— 所以必须**按这个相对路径**放好。
    # ② pythonnet 的运行时 dll（clr.pyd / Python.Runtime.dll）：不在包内的话
    #    `import clr` 会失败，WebView2 后端就起不来（Electron 退路仍然可用）。
    try:
        import webview as _wv
        wv_src = os.path.join(os.path.dirname(os.path.abspath(_wv.__file__)), 'lib')
        if os.path.isdir(wv_src):
            wv_dst = os.path.join(DIST, 'webview', 'lib')
            if os.path.isdir(wv_dst):
                shutil.rmtree(wv_dst)
            shutil.copytree(wv_src, wv_dst)
            n = sum(len(f) for _, _, f in os.walk(wv_dst))
            log(f'  [OK] webview/lib（WebView2 程序集 {n} 个文件）')
        else:
            log('  [!!] 没找到 pywebview 的 lib 目录 —— WebView2 后端可能起不来')
    except ImportError:
        log('  [!!] 没装 pywebview —— WebView2 后端起不来（可把 ds_backend 设回 electron）')

    try:
        import pythonnet  # noqa: F401
        pn_src = os.path.dirname(os.path.abspath(
            __import__('pythonnet').__file__))
        # pythonnet 的运行时装在 pythonnet/runtime 下
        rt = os.path.join(pn_src, 'runtime')
        if os.path.isdir(rt):
            rt_dst = os.path.join(DIST, 'pythonnet_runtime')
            if os.path.isdir(rt_dst):
                shutil.rmtree(rt_dst)
            shutil.copytree(rt, rt_dst)
            log('  [OK] pythonnet_runtime/（.NET 宿主 dll）')
    except Exception as e:                      # noqa: BLE001
        log(f'  [!!] 复制 pythonnet 运行时失败：{e}')


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

    # 发布包里绝不能带用户私人数据（交接文档里那条规矩，v3.0 又多了两项）
    for junk, why in (('.ui_settings', '用户设置（含私人路径）'),
                      ('会话标签.json', '用户自己打的会话标签'),
                      ('ds_profile', '内嵌官网页的登录态（含 cookie）'),
                      ('.boot.log', '启动期插桩日志')):
        p = os.path.join(DIST, junk)
        if os.path.exists(p):
            log(f'  [危险] 发布包里残留 {junk} —— {why}，发版前必须删掉！')

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
    ap.add_argument('--out', default='',
                    help='输出目录（默认 dist\\WeChatExportPlus）。'
                         '旧版本的程序还开着时 exe 会被锁住，用这个打到别处')
    ap.add_argument('--console', action='store_true',
                    help='打成带控制台的 exe（排查"窗口不出现"这类问题时用，'
                         '能直接看到 Python 的报错）')
    args = ap.parse_args()

    # 输出目录可以整体搬走：旧 exe 正在运行（用户还在测）时不能原地覆盖
    global DIST
    if args.out:
        DIST = os.path.abspath(args.out)
    BUILD = os.path.join(os.path.dirname(DIST), '_build_' + os.path.basename(DIST))

    kernel = find_kernel(args.kernel)
    log('=' * 60)
    log(f'构建 {APP_NAME}')
    log(f'内核目录: {kernel}')
    log(f'输出目录: {DIST}')
    log('=' * 60)

    # 清理：build 目录和旧的 dist exe 都必须删干净。
    # 曾经因为没删旧 exe + PyInstaller 复用缓存，产出一个"能启动但界面是上一版"
    # 的包（表现为：窗口一直不出现、进程却活着），排查了很久。
    for d in (BUILD,):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    old_exe = os.path.join(DIST, f'{APP_NAME}.exe')
    if os.path.exists(old_exe):
        try:
            os.remove(old_exe)
            log(f'  [清理] 删除旧 exe')
        except OSError as e:
            raise SystemExit(
                f'无法删除旧 exe（可能程序还在运行）：{old_exe}\n{e}\n'
                '请先关闭正在运行的 WeChatExportPlus 再重新构建，'
                '或者用 --out 打到另一个目录。')

    if not args.skip_build:
        log('\n[1] PyInstaller 打包 GUI...')
        cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onefile']
        # ⚠️ --windowed 是正式形态；排查"窗口不出现"这类问题时用 --console，
        #    报错才会出现在控制台上（windowered 版会把 traceback 吞掉）。
        cmd += ['--console'] if args.console else ['--windowed']
        cmd += ['--name', APP_NAME,
                '--distpath', DIST,
                '--workpath', BUILD,
                '--specpath', BUILD,
                '--paths', ROOT,
                '--paths', os.path.join(ROOT, 'scripts'),
                '--paths', os.path.join(ROOT, 'exporters'),
                '--paths', os.path.join(ROOT, 'gui'),
                '--icon', os.path.join(ROOT, 'gui', 'icon.ico')]
        # 开发期 ROOT 下可能有指向内核大目录的 junction（runtime/dll/electron/resources），
        # 明确排除掉，避免 PyInstaller 顺着链接把几百 MB 的二进制塞进 exe。
        for name in ('runtime', 'dll', 'electron', 'resources', 'gui', 'tests', 'tasks'):
            cmd += ['--exclude-module', name]
        for h in HIDDEN_IMPORTS:
            cmd += ['--hidden-import', h]
        for pkg in COLLECT_ALL:
            cmd += ['--collect-all', pkg]
        cmd.append(os.path.join(ROOT, 'gui', 'app_plus.py'))
        subprocess.run(cmd, cwd=ROOT, check=True)
        log('  [OK] exe 打包完成')
    else:
        log('\n[1] 跳过 PyInstaller')

    copy_runtime(kernel)
    copy_scripts()
    copy_exporters()
    copy_ds()
    log('\n[5] 复制图标、提示词与启动器...')
    copy_icon()
    copy_ai_prompt()
    write_launcher()
    verify_dist()

    log('\n完成。把整个 dist\\WeChatExportPlus 文件夹拷到任意位置，')
    log(f'双击 {APP_NAME}.exe 或 启动工具.bat 即可运行。')


if __name__ == '__main__':
    main()
