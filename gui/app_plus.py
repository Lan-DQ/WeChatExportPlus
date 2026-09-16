# -*- coding: utf-8 -*-
"""微信聊天记录批量导出工具 v2.2.0 —— 自绘界面版。

功能与 v2.0.0 一致（获取密钥 / 连接数据库 / 勾选会话 / 批量导出 /
进度取消 / 预览 / 编辑给 AI 的指令），界面层是重写的：
  · 背景：渐变 + 柔和光晕（也支持放自己的图，见 ui_theme.find_bg_image）
  · 面板：玻璃拟态（背景与面板色混合后切圆角）
  · 控件：全部 Canvas 自绘，圆角走 PIL 超采样抗锯齿，悬停/按下带缓动
  · 深浅主题可切换，选择会记住
  · 导出目录、工作目录都会记住，下次开程序不用重选

v2.2.0 相对 v2.1.0 修的问题（都影响可用性）：
  · 所有按钮都点不动 —— tkinter 的 event.type 是 EventType 枚举而不是 int，
    查表永远查不到，事件被静默丢弃
  · 背景在正常启动路径下从未被画出来
  · 重绘时旧行图元不清理，反复重绘会叠加（"窗口一闪就多一个控件"）
  · 页面重建后旧控件仍留在输入名单里，用旧状态操作新画布
  · 滚轮事件没登记进映射表，只能拖滚动条
  · 复选框图用了单槽缓存，导致整个列表的勾选状态显示串味
  · 关窗口被数据库清理阻塞（现在先关窗，清理转后台）

界面层全部在 gui/ui_theme.py 与 gui/ui_widgets.py，本文件只负责业务编排。
"""
import datetime
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk

# ── 路径解析（PyInstaller onefile 下 __file__ 在临时解包目录，必须以 exe 为准）──
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPTS_DIR = os.path.join(BASE, 'scripts')
EXPORTERS_DIR = os.path.join(BASE, 'exporters')
GUI_DIR = os.path.join(BASE, 'gui')
DS_BRIDGE_DIR = os.path.join(BASE, 'ds_bridge')
for _p in (SCRIPTS_DIR, EXPORTERS_DIR, GUI_DIR, DS_BRIDGE_DIR, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import ui_theme as T            # noqa: E402
import ui_widgets as W          # noqa: E402
import session_tags             # noqa: E402

ROOT = BASE
DEFAULT_OUT = os.path.join(os.environ.get('USERPROFILE', BASE), 'Desktop', 'wx_export')
OUT = DEFAULT_OUT
KEY_FILE = os.path.join(OUT, 'key.txt')
SETTINGS_FILE = os.path.join(ROOT, '.ui_settings')
# 会话标签单独一份文件：.ui_settings 是逐行 key=value、非原子写，
# 塞进会随数据量增长而变脆（详见 gui/session_tags.py 的说明）。
TAGS_FILE = os.path.join(ROOT, session_tags.FILE_NAME)
# 内嵌 DeepSeek 页签的用户数据目录（登录态存这里；**不能随发布包发出去**）
#
# ⚠️⚠️ 为什么放在**包外**（2026-09-16 踩出来的）：
#   登录态在 Chromium 里是用户数据目录里的 **localStorage（`userToken`）**，
#   不是 cookie。以前 `ds_profile` 就放在**包目录里面**，所以一换新版包（新目录）
#   登录态必然丢，用户被迫重新登录 —— 实测已因此丢过两次
#   （v3.1.4_embed / v3.1.6 两个包都是空 profile，打开就是登录页）。
#   现在统一放到 `%LOCALAPPDATA%\WeChatExportPlus\ds_profile`，
#   **换多少版包都不用再登一次**。包内老目录只在首次迁移时用作种子。
def _ds_profile_dir(root):
    """内嵌浏览器用哪个用户数据目录。包外持久目录优先；首次从包内老目录迁移。"""
    home = os.environ.get('WXEXPORT_PROFILE_HOME') or os.path.join(
        os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
        'WeChatExportPlus')
    persist = os.path.join(home, 'ds_profile')
    legacy = os.path.join(root, 'ds_profile')          # 老版本放在包内
    if not os.path.isdir(persist) and os.path.isdir(os.path.join(legacy, 'Local Storage')):
        # 首次迁移：把包内那份搬出来（搬完登录态就与包解耦了）
        try:
            shutil.copytree(legacy, persist, dirs_exist_ok=True)
        except (OSError, shutil.Error):
            return legacy                              # 迁移失败就用包内那份，别影响启动
    return persist


DS_PROFILE_DIR = _ds_profile_dir(ROOT)

APP_TITLE = '微信聊天记录批量导出工具'
APP_VERSION = 'v3.2.0'

FORMAT_CHOICES = [
    ('Markdown 单文件（推荐）', 'md'),
    ('AI 语料 JSONL（适合入库/RAG）', 'ai'),
    ('HTML（气泡页面，含图片）', 'html'),
    ('Excel（.xlsx 表格）', 'excel'),
    ('CSV（.csv 表格）', 'csv'),
    ('PDF（含内嵌图片）', 'pdf'),
    ('TXT（纯文本）', 'txt'),
    ('JSON（结构化）', 'json'),
]
FORMAT_HINTS = {
    'md': '每个会话一个 .md，直接放在导出目录下，可拖进 DeepSeek 网页版',
    'ai': '每个会话一个文件夹，含 对话.jsonl（带发言人/类型/图片路径）',
    'html': '每个会话一个文件夹，含 index.html + 图片\\',
    'excel': '每个会话一个文件夹，含 <会话名>.xlsx',
    'csv': '每个会话一个文件夹，含 对话.csv',
    'pdf': '每个会话一个文件夹，含 <会话名>.pdf（图片内嵌）',
    'txt': '每个会话一个文件夹，含 对话.txt',
    'json': '每个会话一个文件夹，含 对话.json',
}
FORMAT_DEFAULT = 'md'


def _fmt_dur(sec):
    """把秒数说成人话（进度窗显示剩余时间用）。"""
    sec = max(0, int(sec))
    if sec < 60:
        return f'{sec} 秒'
    return f'{sec // 60} 分 {sec % 60} 秒'


def load_saved_key():
    for path in (KEY_FILE, os.path.join(os.environ.get('USERPROFILE', 'C:'),
                                       'Desktop', 'wechat_export', 'WeChat', 'key.txt')):
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8', errors='ignore') as f:
                    k = f.read().strip()
                if len(k) == 64:
                    return k
            except OSError:
                pass
    return ''


def _documents_dirs():
    """可能的「文档」目录。

    不能只会拼 `%USERPROFILE%\\Documents` —— 很多人把「文档」重定向到了 OneDrive
    （`%USERPROFILE%\\OneDrive\\Documents` 或 `OneDrive - 公司名\\Documents`），
    这时老写法那个路径根本不存在。
    """
    home = os.environ.get('USERPROFILE', 'C:')
    out = [os.path.join(home, 'Documents')]
    try:
        import glob as _glob
        out += _glob.glob(os.path.join(home, 'OneDrive*', 'Documents'))
    except Exception:
        pass
    return out


def _wechat_configured_dirs():
    """微信**自己记录**的数据存储位置（用户改过位置时，只有这里是对的）。

    - 微信 3.x：注册表 `HKCU\\Software\\Tencent\\WeChat\\FileSavePath`
    - 微信 4.x：`%APPDATA%\\Tencent\\xwechat\\config\\*.ini`
      （实测内容就是 `MyDocument:`，和 3.x 同一套约定；改过位置就是自定义路径）

    `MyDocument:` / `MyDocuments:` 都表示"文档"目录。
    读注册表/文件失败一律忽略，绝不影响主流程。
    """
    raw = []
    try:
        import winreg
        for sub in (r'Software\Tencent\WeChat', r'Software\Tencent\Weixin',
                    r'Software\Tencent\WeChat4'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                    for name in ('FileSavePath', 'DataSavePath', 'SavePath'):
                        try:
                            v, _t = winreg.QueryValueEx(k, name)
                            if isinstance(v, str) and v.strip():
                                raw.append(v.strip())
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass
    try:
        cfg = os.path.join(os.environ.get('APPDATA', ''), 'Tencent', 'xwechat', 'config')
        if os.path.isdir(cfg):
            for fn in os.listdir(cfg):
                if not fn.lower().endswith('.ini'):
                    continue
                try:
                    with open(os.path.join(cfg, fn), encoding='utf-8',
                              errors='ignore') as f:
                        v = f.read().strip()
                except OSError:
                    continue
                if v:
                    raw.append(v)
    except Exception:
        pass

    out = []
    for v in raw:
        v = str(v).strip().strip('"').strip()
        if not v:
            continue
        low = v.lower().rstrip('\\/')
        if low in ('mydocument:', 'mydocument', 'mydocuments:', 'mydocuments'):
            out.extend(_documents_dirs())
        elif re.match(r'^[a-zA-Z]:', v) or v.startswith('\\\\'):
            out.append(os.path.expandvars(v))
    return out


def _is_wechat_data_dir(d):
    """判定标准：里面有 `wxid_xxx` 子文件夹（老规矩，避免误选到别的目录）。"""
    try:
        for e in os.listdir(d):
            if e.startswith('wxid_') and os.path.isdir(os.path.join(d, e)):
                return True
    except OSError:
        pass
    return False


def find_account_dirs(data_dir):
    """列出数据目录下的**账号目录**（`wxid_xxx`，且里面确实有 session.db）。

    ⚠️ 为什么需要它（对应 GitHub issue #2）：`wcdb_server.js` 找 session.db 是
    "**递归找到第一个就用**"，不校验密钥属于哪个账号。用户在这台电脑上登录过
    多个微信、旧账号的记录还在时，就会拿**别的账号的库**去开 → 开不了 → 连接超时。
    （图片解密那条路已经修成"按路径 wxid 精确匹配"，但**数据库这条没修**。）
    修法：把"具体账号目录"交给用户选（我无法判断密钥属于哪个账号，猜不如让他选），
    选中后直接把那个目录交给服务端，`find()` 就只会在它里面找。
    """
    out = []
    if not data_dir or not os.path.isdir(data_dir):
        return out
    try:
        for e in sorted(os.listdir(data_dir)):
            p = os.path.join(data_dir, e)
            if not (e.startswith('wxid_') and os.path.isdir(p)):
                continue
            # 里面要真有 session.db 才算（避免列出空壳账号目录）
            #
            # ⚠️ 这里**不能**用一个小的深度上限去 os.walk：微信 4.x 的账号目录下
            #    有 `business/emoticon/...` 这类又深又多的分支，**深度优先**遍历会
            #    先钻进去，`session.db`（在 `db_storage/session/` 里）还没轮到就被
            #    剪掉了 —— 实测就是这个坑：明明有库却判定成"没有 session.db"。
            #    所以先查已知路径，再退化成更宽松的有界遍历。
            hits = [os.path.join(p, 'db_storage', 'session', 'session.db'),
                    os.path.join(p, 'session', 'session.db'),
                    os.path.join(p, 'session.db')]
            has = any(os.path.isfile(h) for h in hits)
            if not has:
                for root, dirs, files in os.walk(p):
                    if 'session.db' in files:
                        has = True
                        break
                    if root.count(os.sep) - p.count(os.sep) >= 4:
                        dirs[:] = []
            out.append((e, p, has))
    except OSError:
        pass
    return out


def _resolve_wechat_data(root):
    """在 root 或它的下一层找微信数据目录；找不到返回 ''。"""
    if root and os.path.isdir(root):
        if _is_wechat_data_dir(root):
            return root
        for sub in ('xwechat_files', 'WeChat Files'):
            p = os.path.join(root, sub)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p
    return ''


def find_xwechat_dirs(extra_roots=()):
    """找微信数据目录（返回"含 wxid_xxx 子文件夹"的那一层），找不到返回 ''。

    查找顺序（越靠前越可信）：
      1. **微信自己记录的位置**（注册表 / `%APPDATA%\\Tencent\\xwechat\\config\\*.ini`）
         —— 用户把数据挪到自定义目录时，只有这里能对上；
      2. 「文档」目录（含 OneDrive 重定向那几种）；
      3. 各盘符根目录**及其下一层**（覆盖 `D:\\我的资料\\xwechat_files` 这类）。

    以前只查几个固定位置、且只认 C~H 盘的根目录，所以用户一改存储位置就得手动点
    「浏览」——issue #1 就是这个原因。
    """
    for r in list(extra_roots) + _wechat_configured_dirs():
        hit = _resolve_wechat_data(r)
        if hit:
            return hit

    for d in _documents_dirs():
        for sub in ('xwechat_files', 'WeChat Files'):
            p = os.path.join(d, sub)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p

    # 各盘符：根目录 + 下一层（下一层能覆盖"我自己建了个文件夹放微信数据"）
    skip = {'windows', 'program files', 'program files (x86)', 'programdata',
            '$recycle.bin', 'system volume information', 'recovery',
            'perflogs', 'users', 'appdata'}
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        drive = f'{letter}:\\'
        if not os.path.isdir(drive):
            continue
        for name in ('xwechat_files', 'WeChat Files',
                     r'wxxinxi\xwechat_files', r'储存信息\xwechat_files'):
            p = os.path.join(drive, name)
            if os.path.isdir(p) and _is_wechat_data_dir(p):
                return p
        try:
            entries = list(os.scandir(drive))
        except OSError:
            continue
        for ent in entries:
            try:
                if not ent.is_dir() or ent.name.lower() in skip:
                    continue
            except OSError:
                continue
            for name in ('xwechat_files', 'WeChat Files'):
                p = os.path.join(ent.path, name)
                if os.path.isdir(p) and _is_wechat_data_dir(p):
                    return p
    return ''


def clean_wxid(wxid):
    parts = str(wxid or '').split('_')
    if len(parts) >= 3 and parts[0] == 'wxid':
        return '_'.join(parts[:2])
    return str(wxid or '')


def load_settings():
    d = {'theme': 'light'}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                for line in f:
                    if '=' in line:
                        k, v = line.split('=', 1)
                        d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def save_settings(d):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            for k, v in d.items():
                f.write(f'{k}={v}\n')
    except OSError:
        pass


# ────────────────────── 官网页签的布局表 ──────────────────────
#
# 为什么要有这张表：官网页签以前是**一堆写死的 Y 坐标**（副标题 70 / 工具栏 96 /
# 提示 132 / 警示 148 / 清单 164 / 选项 H-58 / 路径 H-116 / 日志 H-24）。
# 它们是分别调出来的，谁也不认识谁，改一个字号或加一行就会互相压字 ——
# 用户截图里圈出的多处重叠（提示被「发送清单」标题盖住、警示撞工具栏、
# 复选框压清单下沿、导出目录与提示叠成一团）全是这么来的。
#
# 现在改成：**这里的纯函数是唯一事实来源**，build_ds / draw_header /
# _ds_update_hint / _ds_rect 都只读它，不再各自算坐标；
# 并且 tests/test_ui_invariants.py 里有两条不变式：
#   ① 表里任意两个矩形都不相交（H=700/640/600/560 都查）；
#   ② 真画出来以后，用 tk 的 bbox 再量一遍，任意两段文字也不相交。
# 以后要加一行/改字号，改这里 + 跑测试即可，不用再手怼坐标。

# 副标题的最大像素宽度。超过它就把页签按钮让到下一行 —— 不留这个判据的话，
# 窗口一窄（内容区 940 时右栏很挤）副标题和页签按钮就会在同一行撞上。
DS_SUB_W = 430
# 左栏（发送清单）宽度：清单控件、底部选项行都住在这里
DS_LEFT_W = 330
# 右栏起点。⚠️ x >= DS_BROWSER_X 的那块矩形会被**独立浏览器窗口**盖住，
# 所以任何要点击的控件都必须待在 x < DS_BROWSER_X 里（见接管文档 5.8）。
DS_BROWSER_X = 38 + DS_LEFT_W + 14


def _ds_font_linespace(family, size):
    """这个字体在 Tk 里的行高（画折行文字时的最小行距）。

    画布文字的 bbox 高度实测等于 `linespace`（9 号 = 17px），所以折行行距
    小于它就会自己压自己 —— 这是"提示行折了两行就叠在一起"的原因。
    没有 Tk 解释器时（纯函数测试环境）返回 None，由调用方给个保守值。
    """
    try:
        import tkinter.font as tkfont
        return int(tkfont.Font(family=family, size=size).metrics('linespace'))
    except Exception:                        # noqa: BLE001
        return None


def ds_page_layout(W, H, hint_lines=1, warn_lines=1, left_buttons=None,
                   right_buttons=None):
    """官网页签的显式布局表（纯函数，不碰 tk）。

    返回值里的矩形都是 canvas 坐标（= 主窗口客户区坐标），形如 (x1, y1, x2, y2)。
    `*_y` 是那一行的**文字基线（垂直居中锚点）**，`*_h` 是该行的标称高度。

    自上而下的推进顺序就是文档里写的那条链，每一步都只依赖前一步：
        品牌 → 页签按钮 →（副标题）→ 工具栏 → 提示 → 警示
        → 清单 → 导出目录 → 选项行 → 日志

    `left_buttons` / `right_buttons` 是 (label, width) 列表（可省略），
    用来把**按钮的矩形一并交给调用方和测试** —— 按钮也是会互相压的东西，
    光管文字不够。左边那一组放不下时，右边那组自动落到第二行。
    """
    W = int(W)
    H = int(H)
    lw = DS_LEFT_W
    pad = 38
    bx = DS_BROWSER_X
    bw = max(320, W - bx - pad)          # 浏览器矩形宽

    # ── 顶部：品牌（标题 + 副标题）与页签按钮 ──
    lx, rx = 38, W - 38                  # 左右栏边界
    header = []
    tabs_x1 = rx - 254                      # 两个页签按钮（142 + 8 + 112）的实际左边
    sub_w = min(DS_SUB_W, max(180, W - 560))
    # 副标题那一行右边要放得下页签按钮吗？放不下就把按钮整体下移一行，
    # 副标题独占一行 —— 这正是"副标题撞按钮行"那处重叠的根治办法。
    two_row = (lx + max(190, sub_w) + 20 > tabs_x1)
    tab_h = 32 if two_row else 36
    tab_y = 26
    if two_row:
        # 副标题独占一行（页签按钮已经在上面那一行），三行依次往下推
        title_y, sub_y = tab_y + tab_h + 18, tab_y + tab_h + 42
        rows_top = sub_y + 17
    else:
        title_y = tab_y + tab_h // 2 + 3
        sub_y = title_y + 32
        rows_top = sub_y + 26
    # ⚠️ 品牌标题的矩形只占左半 —— 它右边那一列是页签按钮。
    #    写成横跨整行的话，和按钮行就成"矩形相交"了（真实文字并不相交），
    #    不变式测试会因此误报。
    header.append(('title', '品牌标题',
                   (lx, title_y - 13, min(lx + 320, tabs_x1 - 10), title_y + 13),
                   title_y, 26))
    header.append(('subtitle', '页签副标题',
                   (lx, sub_y - 9, min(lx + sub_w, tabs_x1 - 10), sub_y + 9), sub_y, 18))
    header.append(('tabs', '页签按钮', (tabs_x1, tab_y, rx, tab_y + tab_h),
                   tab_y + tab_h // 2, tab_h))

    # ── 工具栏：左栏按钮（自动折行）+ 右栏「开始发送」──
    # ⚠️ 左栏按钮**不能越过 DS_BROWSER_X**：那一带会被独立浏览器窗口盖住，
    #    点不到。所以这里按"列宽"折行，而不是像老代码那样一口气横着排
    #    （老代码在 1060 宽下，「演练分批」和「诊断」就已经压在浏览器区里了）。
    btn_h = 34
    vgap = 8
    lbtns = list(left_buttons or [])
    rbtns = list(right_buttons or [])
    tool_y = rows_top + 2
    btn_rects = []
    row = []
    row_w = 0
    left_bottom = tool_y

    def _flush():
        nonlocal row, row_w, left_bottom
        if not row:
            return
        cur = lx
        for label, bw_ in row:
            btn_rects.append((label, (cur, left_bottom, cur + bw_, left_bottom + btn_h)))
            cur += bw_ + vgap
        left_bottom += btn_h + vgap
        row, row_w = [], 0

    for label, bw_ in lbtns:
        if row and (row_w + vgap + bw_) > (bx - lx - 8):
            _flush()
        row_w = row_w + bw_ + (vgap if row else 0)
        row.append((label, bw_))
    _flush()
    tool_bottom = max(left_bottom - vgap, tool_y + btn_h)
    cur = rx
    for label, bw_ in reversed(rbtns):
        cur -= bw_
        btn_rects.append((label, (cur, tool_y, cur + bw_, tool_y + btn_h)))
        cur -= vgap
    tool_rect = (lx, tool_y, rx, tool_bottom)

    # ── 提示行（勾选统计）──
    # ⚠️ 行距必须 ≥ 字体行高，否则折行的第二行会和第一行叠在一起
    #    （实测 9 号 Microsoft YaHei UI 的 linespace = 17px）。
    line_step = _ds_font_linespace('Microsoft YaHei UI', 9) or 18
    one_line_h = line_step - 3
    hint_h = one_line_h + line_step * (max(1, int(hint_lines)) - 1)
    hint_y = tool_bottom + 9 + hint_h // 2
    hint_rect = (lx, hint_y - hint_h // 2, lx + lw, hint_y + (hint_h - hint_h // 2))

    # ── 底部三行（都在左栏；右侧会被浏览器盖住）──
    log_y = H - 17
    opt_y = log_y - 12 - 26
    path_y = opt_y - 12 - 12
    path_h = 11
    path_rect = (lx, path_y - path_h // 2, lx + lw, path_y + (path_h - path_h // 2))
    log_rect = (lx, log_y - 7, lx + lw, log_y + 7)
    opt_rect = (lx, opt_y - 14, lx + lw, opt_y + 14)

    # ── 警示行（右栏，仍在浏览器矩形**之上**因此不会被盖住）──
    warn_h = one_line_h + line_step * (max(1, int(warn_lines)) - 1)
    warn_y = hint_rect[3] + 8 + warn_h // 2
    warn_rect = (bx, warn_y - warn_h // 2, bx + bw, warn_y + (warn_h - warn_h // 2))

    # ── 清单 ──
    # ⚠️ 清单的高度由**下面那几行**决定，不能反过来用 max() 撑高：
    #    老的写法是 `max(140, opt_y - 26 - list_y)`，窗口一矮就撑出去，
    #    正好压在「导出目录」和选项行上（用户截图里那一团糊字）。
    #    现在留 10px 呼吸，清单宁可矮一点（CheckList 自己会滚动）。
    list_head = 30
    rows_top2 = max(list_head + 2 * 44 + 6, warn_rect[3]) + 10
    bottom_top = min(path_rect[1], opt_rect[1])
    list_h = max(60, bottom_top - 10 - rows_top2)
    list_rect = (lx, rows_top2, lx + lw, rows_top2 + list_h)


    # ── 浏览器矩形（真窗口盖在这上面）──
    by = max(rows_top2 - 6, warn_rect[3] + 6)
    bh = max(180, H - by - pad)
    browser_rect = (bx, by, bx + bw, by + bh)

    return {
        'header': header,
        'title_y': title_y, 'sub_y': sub_y,
        'tab_y': tab_y, 'tab_h': tab_h,
        'sub_w': sub_w, 'sub_two_row': two_row,
        'rows_top': rows_top,
        'tool_y': tool_y, 'tool_h': tool_bottom - tool_y, 'tool_rect': tool_rect,
        'tool_two_row': tool_bottom > tool_y + btn_h,
        'buttons': btn_rects, 'line_step': line_step,
        'hint_y': hint_y, 'hint_h': hint_h, 'hint_rect': hint_rect,
        'warn_y': warn_y, 'warn_h': warn_h, 'warn_rect': warn_rect,
        'list_y': rows_top2, 'list_h': list_h, 'list_head': list_head,
        'list_rect': list_rect,
        'path_y': path_y, 'path_rect': path_rect,
        'opt_y': opt_y, 'opt_h': 26, 'opt_rect': opt_rect,
        'log_y': log_y, 'log_rect': log_rect,
        # 只有**一行**文字的画布行（左右两栏各一条，x 方向也分开了）
        'text_rows': (
            ('hint', '提示（勾选统计）', hint_rect, lx, lx + lw),
            ('warn', '警示（附件数量）', warn_rect, bx, bx + bw),
        ),
        'left_x': lx, 'right_x': rx,
        'browser_rect': browser_rect,
        'W': W, 'H': H,
    }


def _rect_hit(a, b):
    """两个 (x1,y1,x2,y2) 矩形是否相交（边贴边不算）。"""
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


class App:
    # 页面布局常量
    W, H = 1060, 700

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f'{APP_TITLE} {APP_VERSION}')
        self.root.geometry(f'{self.W}x{self.H}')
        self.root.minsize(940, 640)
        self._set_icon()

        self.settings = load_settings()
        self.theme = T.THEMES.get(self.settings.get('theme', 'light'), T.THEMES['light'])
        # 覆盖模式开关（记住用户上次的选择）
        self._clear_before = self.settings.get('clear_before', '0') == '1'

        # 会话标签：单独一份 JSON。读坏了也只是标签没了，不会连累主题/目录等设置。
        self.tag_store = session_tags.TagStore(TAGS_FILE)
        # 内嵌 DeepSeek 页签：宿主**懒启动** —— 不点那个页签就绝不拉起 electron。
        self.ds = None
        self.ds_out_root = self.settings.get('ds_root', '')
        self.ds_units = []
        self.ds_selected = set()
        # 只发文档（默认开：图片占绝大多数体积、且官网单条消息更容易被它们挤爆）
        self._ds_docs_only = self.settings.get('ds_docs_only', '1') == '1'
        # 每批文件数（默认 20，实测上限在 30~40 之间）
        try:
            self._ds_limit = int(self.settings.get('ds_batch', str(self.DS_LIMIT)))
        except ValueError:
            self._ds_limit = self.DS_LIMIT
        if self._ds_limit not in self.DS_LIMIT_CHOICES:
            self._ds_limit = self.DS_LIMIT
        self._ds_visible = False
        # 真嵌入（embed 后端）是否已经 SetParent 完成；进程重启/主机失效时必须清掉
        self._ds_embed = False
        # 上一次摆位用的 (宽,高)：用来区分"只挪位置"（走 Win32，快）和
        # "改变大小"（走完整摆位，触发重绘 —— 否则白板）。见 _ds_realign。
        self._ds_last_wh = None
        self._ds_win = None
        # 主窗口最小化时把浏览器一起藏起来（恢复时放回）—— 见 _on_root_unmap
        self._ds_hidden_for_icon = False

        self.key = load_saved_key() or None
        self.wcdb = None
        self.sessions = []
        self.nick_map = {}
        self.busy = False
        self.page = 'home'
        self._widgets = []          # 需要在重绘时重建的自绘控件
        self._entries = []          # 真实 tk.Entry（叠在 Canvas 上，必须显式销毁）
        self._sel_item = None       # 「已选 N 个」文本图元 id（只建一次，避免堆积）
        self._key_visible = False
        self._toast_job = None

        self.sf = W.Surface(self.root, self.theme,
                            T.find_bg_image(BASE))
        self.sf.font = T.pick_font(self.root)
        # 控件回调里抛异常时别再静默吞掉（写日志 + 提示一次）
        self._last_ui_err = ''
        self.sf.on_error = self._ui_error
        self.root.update_idletasks()
        self.sf.redraw_bg()

        self.root.protocol('WM_DELETE_WINDOW', self.quit_app)
        self.root.bind('<Configure>', self._on_resize)
        # 同一个 <Configure> 事件里，除了尺寸变化还有**位置变化**（拖动窗口）。
        # 浏览器是独立顶层窗口，必须跟着挪，否则拖走主窗口它就留在原地。
        self.root.bind('<Configure>', self._on_root_configure, add='+')
        # 最小化/恢复：浏览器是**独立顶层窗口**，不随主窗口一起最小化 ——
        # 用户切走时桌面上会孤零零留着一块官网页面（"完全是两个软件"的来源之一）。
        self.root.bind('<Unmap>', self._on_root_unmap, add='+')
        self.root.bind('<Map>', self._on_root_map, add='+')
        self.root.bind('<Escape>', lambda e: self._close_popups())

        self.build_home()
        # 自动化/自测用：设 WXEXPORT_START_PAGE=ds 就直接进「DeepSeek 官网」页签
        # （合成的鼠标消息进不了自绘输入层，所以留一个入口来验证官网页的生命周期）
        if os.environ.get('WXEXPORT_START_PAGE') == 'ds':
            self.root.after(250, self.build_ds)

    # ────────────────────── 基础设施 ──────────────────────

    def _set_icon(self):
        self._ico = ''
        for p in (os.path.join(BASE, 'icon.ico'), os.path.join(GUI_DIR, 'icon.ico')):
            if os.path.exists(p):
                try:
                    self.root.iconbitmap(p)
                    self._ico = p
                    break
                except Exception:
                    pass

    def _clear_page(self):
        """切换页面前彻底清场。

        三个必须都做，少一个就会出现"新页面画在旧页面上"的重叠：
          1. 删掉除背景外的所有 Canvas 图元（不能靠 tag —— 手画的图元没打标签）；
          2. 销毁真实 tk 控件（Entry 是叠在 Canvas 上的，不随图元一起消失）；
          3. 清空自绘控件的引用表与动画队列（否则旧动画还在跑，去操作已删除的图元）。

        另外：**离开官网页时必须把内嵌浏览器藏起来**。它是独立的 Win32 子窗口，
        不随 Canvas 图元一起消失。这个判断必须放在这里，因为 build_home /
        build_sessions 会被很多地方直接调用（不只是 _rebuild_page 那条路），
        只挂在 _rebuild_page 上会漏 —— 实际就漏了：退出官网页后网页还留在那儿。
        各页构建函数都是先设 self.page 再调本函数，所以这里能拿到目标页。
        """
        if getattr(self, 'page', '') != 'ds':
            self._ds_leave()
        cv = self.sf.canvas
        for iid in cv.find_all():
            if '__bg' not in cv.gettags(iid):
                cv.delete(iid)
        # 自绘控件都注册在 Surface 上当输入消费者，切页/重建时必须注销干净。
        # 只遍历 self._widgets 是不够的：CheckList 等不一定被登记进去，
        # 残留的旧实例会继续收事件（用旧的坐标和状态去操作新画布），
        # 表现为"某一行/某个控件行为诡异"。
        self.sf.unregister_all_input()
        for w in self._widgets:
            if hasattr(w, 'unbind'):
                try:
                    w.unbind()
                except Exception:
                    pass
        for w in self._entries:
            try:
                w.destroy()
            except Exception:
                pass
        self._entries.clear()
        self._widgets.clear()
        self._sel_item = None      # 旧图元已被删除，引用必须清掉
        if getattr(self, '_anim', None):
            self.sf.anim.clear()
        self.sf.canvas.configure(cursor='')

    def _on_resize(self, e):
        if e.widget is not self.root:
            return
        if (self.W, self.H) == (e.width, e.height):
            return
        self.W, self.H = e.width, e.height
        # 拖动窗口时不必每像素重绘，停顿后再重排
        if getattr(self, '_rs_job', None):
            self.root.after_cancel(self._rs_job)
        self._rs_job = self.root.after(140, self._relayout)

    def _relayout(self):
        """窗口尺寸变了，整页重排。

        ⚠️ 必须走 _clear_page()，不能只 canvas.delete('all')：
        早期这里只删图元不清控件表，结果
          · 旧控件仍留在输入消费者名单里 —— 一次点击被新旧两份控件同时响应；
          · 旧 CheckList 的 hover_row 等状态残留，新画的一遍按旧索引判悬停，
            表现为"某一行固定显示错"；
          · 展开中的下拉框、悬浮图元被删但对象还在。
        用户可见现象是"小窗正常、一全屏导出格式框变成两个"。

        ⚠️⚠️ **官网页签 + WebView2 后端要特殊对待**（用户实测："一开全屏就卡死"）：
        WebView2 是**子控件**，父窗口一变大小它自动跟着变，根本不需要重排整页。
        而重排整页会重建界面、再去动 WebView2 的窗口层级（最大化时这类事件密集
        触发）→ 卡死。所以这里只把它的矩形同步过去（一次很轻的 SetWindowPos），
        不重建、不动父子关系。
        """
        self._rs_job = None
        _t0 = time.time()
        if (self.page == 'ds' and self.ds is not None
                and getattr(self.ds, 'running', False)
                and self.ds_backend == 'webview2'):
            try:
                x, y, w, h = self._ds_rect()
                # ⚠️ 用 place_async：最大化时 Configure 密集触发，
                #    同步等应答会把界面卡住（用户实测一开全屏就卡死）
                pa = getattr(self.ds, 'place_async', None)
                (pa or self.ds.place)(x, y, w, h)
            except Exception:                  # noqa: BLE001
                pass
            self._wv2_trace(f'relayout(webview2 只摆位) {time.time() - _t0:.2f}s')
            return
        self._clear_page()          # 删图元 + 注销控件 + 清动画（幂等）
        self.sf.size = (0, 0)       # 让背景按新尺寸重算
        self.sf.redraw_bg()
        self._rebuild_page()
        # Electron 后端是**独立顶层窗口**，主窗口改大小它不会跟着动 —— 重排后补摆位。
        if self.page == 'ds' and getattr(self, '_ds_visible', False):
            self._ds_place()
        self._wv2_trace(f'relayout(整页重排) {time.time() - _t0:.2f}s page={self.page}')

    def _wv2_trace(self, msg):
        """把界面侧的时序写进 `.wv2_start.log`（排查"全屏卡死"用）。

        宿主侧的日志已经证明"嵌入"没问题，所以卡死只能在界面这条路上；
        把每次重排/摆位/显隐记下来，下次一看就知道停在哪一步。
        """
        try:
            with open(os.path.join(ROOT, '.wv2_start.log'), 'a',
                      encoding='utf-8') as f:
                f.write(f'{time.strftime("%H:%M:%S")} [ui] {msg}\n')
        except OSError:
            pass

    def _on_root_configure(self, e):
        """主窗口**移动**时跟着挪浏览器窗口（尺寸变化由 _on_resize 处理）。

        防抖从 160ms 收到 60ms：用户反馈"这完全是两个软件"，拖动主窗口时
        浏览器要等 0.16 秒才跟上，那一下错位感最明显。60ms ≈ 16 次/秒的
        本机 HTTP 摆位调用，负担可以忽略。
        """
        if e.widget is not self.root or self.page != 'ds':
            return
        if not getattr(self, '_ds_visible', False):
            return
        # 拖动过程中先不追，停下来再对齐一次（避免拖着窗口时狂发 HTTP）
        if getattr(self, '_ds_move_job', None):
            try:
                self.root.after_cancel(self._ds_move_job)
            except Exception:
                pass
        self._ds_move_job = self.root.after(60, self._ds_follow)

    def _ds_follow(self):
        """把浏览器重新对齐到主窗口右侧（**只移动，不抢焦点**）。

        ⚠️ 哪些后端需要"跟随"：`embed`（owner 窗口）和 `electron`（独立顶层窗口）
        **都是独立顶层窗口**，主窗口一动它们不会自己跟 —— 用户实测反馈过
        "嵌入窗口不会跟着我拖动这个窗口而动"，就是这里漏了 `embed`。
        （`webview2` 是进程内子控件，天然跟着走，不需要。）
        """
        self._ds_move_job = None
        if not self._ds_can_follow():
            return
        self._ds_realign('follow')
        # ⚠️⚠️ **停下之后再复查一次并强制对齐**（用户建议的做法，2026-09-16 采纳）：
        #    `<Configure>` 是"防抖 60ms"触发的，拖动过程中最后几次事件有可能被
        #    防抖吃掉/或顺序错开，于是"松手那一刻"浏览器的位置和主窗口差一点 ——
        #    用户看到的就是"拖动之后不动了，位置不对"。
        #    所以松手后 ~420ms 再量一次真实几何，不一致就再对齐一次（幂等、很轻）。
        try:
            if getattr(self, '_ds_settle_job', None):
                self.root.after_cancel(self._ds_settle_job)
            self._ds_settle_job = self.root.after(420, self._ds_settle)
        except Exception:                           # noqa: BLE001
            pass

    def _ds_can_follow(self):
        """现在能不能对浏览器做跟随（页签/可见/后端/最小化四项都满足）。

        ⚠️ `child`（真子窗口）**不需要也不该跟着摆**：Windows 会自己把它随父窗口
        搬走，再去 PostMessage 摆一次反而会抖、会偏。
        """
        if self.page != 'ds' or not getattr(self, '_ds_visible', False):
            return False
        if not self.ds or not self.ds.running:
            return False
        if self.ds_backend in ('webview2', 'child'):
            return False                        # 子控件/子窗口天然跟随
        if self.root.state() == 'iconic':
            return False                        # 主窗口最小化了，别把它拽回来
        return True

    def _ds_realign(self, why):
        """把浏览器对齐到布局表算出的那块矩形。

        ⚠️ 让位期间（提示/弹窗占着）**只平移、不显示**：位置必须跟着更新，否则
        让位那段时间里主窗口挪了、浏览器还停在旧位置，让位一结束就看到错位。
        （用户实测："拖上下左右边框改大小会跟随到正确位置，单靠拖动却错位"——
         差别就在于让位期间这条有没有继续平移。）
        """
        if not self._ds_can_follow():
            return False
        yielding = (getattr(self, '_ds_hidden_for_toast', False)
                    or bool(getattr(self, '_dlg_depth', 0)))
        sx, sy, w, h = self._ds_screen_rect()
        try:
            # ⚠️⚠️ **"移动"和"改变大小"必须分开走**（2026-09-16 定型 —— 前面反复踩）：
            #   · **只挪位置**（尺寸没变，例如拖动主窗口）：`SetWindowPos` 只改位置，
            #     内容不失效、**不需要重绘** → 走 Win32（0 往返，跟手）。
            #   · **尺寸变了**（缩放主窗口）：内容要重排，**必须**走完整摆位
            #     （`/bounds` → `setBounds`），否则会白板。
            #   · **让位期间**窗口是藏着的：只平移、不显示，同样走 Win32。
            #   我上一版把"拖动"也一起塞回 HTTP 了，于是"跟随又变回去了"——那是错的。
            keep_size = (self._ds_last_wh == (w, h))
            if yielding or keep_size:
                ok = self.ds.move_win32(sx, sy, w, h)
                if not ok:
                    self.ds.restore_silent(sx, sy, w, h,
                                           show=False if yielding else True)
                self._ds_last_wh = (w, h)
                self._wv2_trace(f'{why} → ({sx},{sy},{w},{h}) '
                                f'{"让位中" if yielding else "纯移动"}(Win32)')
                return True
            # 尺寸变了 → 完整摆位（会触发重绘）
            self.ds.restore_silent(sx, sy, w, h, show=True)
            self._ds_last_wh = (w, h)
            self._wv2_trace(f'{why} → ({sx},{sy},{w},{h}) 尺寸变化(完整摆位)')
            return True
        except Exception as e:                      # noqa: BLE001
            self._wv2_trace(f'{why} 失败：{e}')
            try:
                self.ds.move(sx, sy, w, h)
            except Exception:                       # noqa: BLE001
                pass
            return False

    def _ds_settle(self):
        """拖动/缩放**停下之后**的复查：量真实几何，不一致就再对齐一次。

        为什么要这一步：拖动的最后一段里 `<Configure>` 可能被防抖合并掉，
        松手时的位置和"主窗口实际位置 + 布局表"差一点 —— 用户描述为
        "每次窗口移动之后不动了（位置不对）"。这里主动补一次纠正。
        """
        self._ds_settle_job = None
        if not self._ds_can_follow():
            return
        try:
            want = self._ds_screen_rect()
        except Exception:                           # noqa: BLE001
            return
        # 量浏览器**现在在哪**
        try:
            import ctypes
            import ctypes.wintypes as wt
            rc = wt.RECT()
            ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(self.ds.hwnd)),
                                               ctypes.byref(rc))
            got = (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
        except Exception:                           # noqa: BLE001
            got = None
        if got and (abs(got[0] - want[0]) <= 3 and abs(got[1] - want[1]) <= 3
                    and abs(got[2] - want[2]) <= 5 and abs(got[3] - want[3]) <= 5):
            return                                  # 已经对齐，什么都不做
        self._wv2_trace(f'settle 复查发现错位：实得={got} 期望={want} → 重新对齐')
        self._ds_realign('settle')

    def _ds_child_keepalive(self):
        """★ `child` 后端专用：盯住"还是不是子窗口"，被撤销就立刻重新嵌。

        ⚠️⚠️ 为什么必须看住（2026-09-16 实测）：
        `SetParent` **成功了也会被撤销** —— 实测发现浏览器窗口过一会儿又变回
        **顶层窗口**（`GetParent` 返回桌面窗口 65548、类名 `#32769`），于是：
          · 位置偏移正好等于主窗口客户区原点（它按屏幕坐标解释了我们的画布坐标）；
          · 拖动时完全不跟着走（它已经不是子窗口了）。
        这就是用户报的"松手后位置还是一样的歪"。原因在 Electron/Chromium 那边
        （它自己会重建/重置原生窗口），我们改不了，**只能看住并补回去**。

        这段就是"补回去"的看门狗：每 600ms 检查一次，掉了就重新 `embed_child`。
        补一次成本极低（两个 Win32 调用），而且只在真掉了的时候才动。
        """
        if self.page != 'ds' or self.ds_backend != 'child':
            return
        if not self.ds or not self.ds.running or not getattr(self, '_ds_embed', False):
            return
        try:
            import ctypes
            u32 = ctypes.windll.user32
            u32.GetParent.argtypes = [ctypes.c_void_p]
            u32.GetParent.restype = ctypes.c_void_p
            want = int(self._ds_top_hwnd())
            got = int(u32.GetParent(ctypes.c_void_p(int(self.ds.hwnd))) or 0)
            # 诊断：每 N 次记一行，看它到底掉不掉、补得上补不上（DSVIEW_DIAG_POS 打开）
            self._ka_n = getattr(self, '_ka_n', 0) + 1
            if os.environ.get('DSVIEW_DIAG_POS') and self._ka_n % 8 == 1:
                self._wv2_trace(f'child ka#{self._ka_n}: hwnd={self.ds.hwnd} '
                                f'GetParent={got} want={want} 一致={got == want}')
            if want and got != want:
                x, y, w, h = self._ds_rect()
                ok = bool(self.ds.embed_child(want, x, y, w, h))
                after = int(u32.GetParent(ctypes.c_void_p(int(self.ds.hwnd))) or 0)
                self._wv2_trace(f'child 看门狗：父窗口被撤销（{got} ≠ {want}）→ '
                                f'重新嵌入={ok} 补后 GetParent={after}')
        except Exception as e:                      # noqa: BLE001
            self._wv2_trace(f'child 看门狗出错：{e}')
        try:
            self.root.after(600, self._ds_child_keepalive)
        except Exception:                           # noqa: BLE001
            pass

    def _on_root_unmap(self, e):
        """主窗口被最小化 → 把浏览器一起藏起来。

        ⚠️ `embed`（owner）**和** `electron`（独立窗口）都是独立顶层窗口，主窗口
        最小化时它们不会自己消失 —— 不藏就会在桌面上留一块官网（就是"两个软件"）。
        用户实测反馈过这里漏了 `embed`。
        （`webview2` 是子控件，天然跟着消失，不用管。）
        """
        if e.widget is not self.root or self.page != 'ds':
            return
        if self.ds_backend in ('webview2', 'child'):
            return                              # 子控件/子窗口天然跟着最小化
        if not getattr(self, '_ds_visible', False) or self.ds is None:
            return
        try:
            if self.root.state() == 'iconic':
                self.ds.hide()
                self._ds_hidden_for_icon = True
        except Exception:                    # noqa: BLE001
            pass

    def _on_root_map(self, e):
        """主窗口恢复 → 把浏览器放回原位（不抢焦点）。"""
        if e.widget is not self.root or self.page != 'ds':
            return
        if not getattr(self, '_ds_hidden_for_icon', False):
            return
        self._ds_hidden_for_icon = False
        self.root.after(80, self._ds_restore_silent)

    def _page_builder(self):
        """当前页面对应的构建函数。"""
        return {'home': self.build_home, 'sessions': self.build_sessions,
                'ds': self.build_ds}.get(self.page, self.build_home)

    def _rebuild_page(self):
        """按 self.page 重建整页。

        换页前先把内嵌浏览器藏起来 —— 它是**独立的 Win32 子窗口**，
        不随 Canvas 图元一起被删，不藏就会浮在新页面上。
        （_clear_page 里也有一道同样的保险，见那里的说明。）

        发送中禁止切页：浏览器一被隐藏，Chromium 会把后台页降频，上传/发送会卡住。
        ⚠️ 但**已经点了取消/关了进度窗**之后就不要再拦了 —— 否则用户会看到
        "那个框我已经关了，它还是一直不让我退"（用户实测反馈过的死结）。
        判据用"取消标记 + 工作线程是否还活着"，不是只看 `_ds_sending`。
        """
        if self.page != 'ds' and getattr(self, '_ds_sending', False):
            stopping = (getattr(self, '_ds_cancel_flag', False)
                        or not getattr(self, '_ds_worker_alive', False))
            if not stopping:
                self.page = 'ds'
                self.toast('正在发送到 DeepSeek，暂时不能切页（可点进度窗的「取消」）',
                           'warn', 3500)
                return
            # 正在停下：允许切页，但把状态清干净，别让下一次发送被旧状态干扰
            self._ds_sending = False
            self._ds_log('发送已停止，允许切页')
        if self.page != 'ds':
            self._ds_leave()
        self._page_builder()()

    def _close_popups(self):
        for w in self._widgets:
            if hasattr(w, 'close'):
                w.close()

    def _ui_error(self, exc):
        """界面回调里抛出的异常：写 `.ui_errors.log` + 弹一次提示。

        为什么需要：自绘控件的回调是在 Surface.dispatch_input 里被调用的，
        以前那里 `except Exception: pass`。后果是"点了没反应/弹出一个空框"
        这类问题完全没有线索（标签弹窗就是这么被坑过一次）。
        """
        import traceback
        txt = ''.join(traceback.format_exception_only(type(exc), exc)).strip()
        try:
            with open(os.path.join(ROOT, '.ui_errors.log'), 'a',
                      encoding='utf-8') as f:
                f.write(f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {txt}\n')
                f.write(traceback.format_exc() + '\n')
        except OSError:
            pass
        if txt != self._last_ui_err:
            self._last_ui_err = txt
            try:
                self.toast(f'界面出错了（已记到 .ui_errors.log）：{txt[:64]}',
                           'err', 5000)
            except Exception:
                pass

    def _toast_rect(self, w=340, h=46):
        """提示框该摆在哪儿。

        ⚠️ 官网页签上右下角会被**内嵌浏览器挡住** —— 浏览器是独立的 Win32 子窗口，
        永远画在 Tk 画布之上，画布上画的提示它盖得住。所以那里必须挪到浏览器
        没占的那一列（左边清单区）。抽成纯函数是为了能被测试直接断言。
        """
        if self.page == 'ds' and self._ds_visible:
            w = min(w, self.DS_LEFT_W + 40)
            x1 = 38
            y2 = self.H - 46                     # 让开最下面那行日志
            return x1, y2 - h, x1 + w, y2
        x2, y2 = self.W - 26, self.H - 26
        return x2 - w, y2 - h, x2, y2

    def toast(self, msg, kind='info', ms=2600):
        """右下角浮出提示，几秒后自己消失（比弹窗打断感小）。"""
        th = self.theme
        col = {'info': th['accent'], 'ok': th['ok'], 'err': th['err'],
               'warn': th['warn']}.get(kind, th['accent'])
        cv = self.sf.canvas
        cv.delete('toast')
        w, h = 340, 46
        x1, y1, x2, y2 = self._toast_rect(w, h)
        self.sf.draw_panel(x1, y1, x2, y2, 12, 0.96, border=True, tags='toast')
        T.round_rect_items(cv, x1, y1, x1 + 4, y2, 2, col, '', 0, tags=('toast',))
        cv.create_text(x1 + 20, (y1 + y2) / 2, text=msg, anchor='w',
                       fill=th['text'], font=(self.sf.font, 10), tags='toast')

        # 淡入
        start_y = y2 + 30

        def frame(v):
            cv.delete('toast')
            yy = start_y + (y1 - start_y) * v
            self.sf.draw_panel(x1, yy, x2, yy + h, 12, 0.96, border=True,
                               tags='toast')
            T.round_rect_items(cv, x1, yy, x1 + 4, yy + h, 2, col, '', 0,
                               tags=('toast',))
            cv.create_text(x1 + 20, yy + h / 2, text=msg, anchor='w',
                           fill=th['text'], font=(self.sf.font, 10), tags='toast')

        self.sf.anim.animate('toastin', 0.22, 'out_cubic', frame)
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(ms, lambda: cv.delete('toast'))

    def busy_on(self, text='处理中…'):
        self.busy = True
        cv = self.sf.canvas
        cv.delete('busy')
        w, h = 260, 76
        x1, y1 = (self.W - w) / 2, (self.H - h) / 2
        self.sf.draw_panel(x1, y1, x1 + w, y1 + h, 14, 0.97, tags='busy')
        cv.create_text(x1 + w / 2, y1 + 28, text=text, fill=self.theme['text'],
                       font=(self.sf.font, 11, 'bold'), tags='busy')
        pb = W.ProgressBar(self.sf, x1 + 30, y1 + 48, w - 60, 8)
        pb.set_max(100)
        self._busy_bar = pb
        self._busy_phase = 0

        def pulse():
            if not self.busy:
                return
            self._busy_phase = (self._busy_phase + 9) % 100
            pb.set(self._busy_phase, animate=False)
            self.root.after(90, pulse)

        pulse()

    def busy_off(self):
        self.busy = False
        self.sf.canvas.delete('busy')

    # ────────────────────── 通用绘制片段 ──────────────────────

    def draw_header(self, subtitle):
        """页头：品牌标题 + 副标题 + 两个页签按钮。

        坐标**不再写死**，全部来自布局表（官网页用 ds_page_layout；
        首页/会话页保持原来的位置，只为兼容那两个页面的既有观感）。
        `two_row` 是副标题和页签按钮抢同一行时的退让方案：按钮下移一行。
        """
        th = self.theme
        if self.page == 'ds':
            lay = self._ds_layout()
            title_y, sub_y = lay['title_y'], lay['sub_y']
            tab_y, tab_h = lay['tab_y'], lay['tab_h']
            two_row = lay['sub_two_row']
        else:
            title_y, sub_y = 44, 70
            tab_y, tab_h, two_row = 34, 38, False
        self.sf.text(38, title_y, APP_TITLE, 18, th['text'], True)
        self.sf.text(38, sub_y, subtitle, 10, th['text_dim'], tags='dssubtitle')
        # 页签入口：DeepSeek 官网页 ⇄ 导出页。自绘界面里没有真 tab 控件，
        # 用两个按钮当页签（位置和主题按钮并排，右侧留 8px 间隙）。
        if self.page == 'ds':
            tab_label, tab_cmd = '← 返回导出页', self._leave_ds
        else:
            tab_label, tab_cmd = '💬 DeepSeek 官网', self.build_ds
        if two_row:
            # 副标题独占一行时，按钮整行下移（这时候标题在上面，右边空着）
            tab_y = 26
        self._widgets.append(
            W.Button(self.sf, self.W - 300, tab_y, 142, tab_h, tab_label, kind='ghost',
                     font_size=10, command=tab_cmd, hover_dur=0.12))
        lbl = '🌙 深色' if self.theme['name'] == 'light' else '☀ 浅色'
        self._widgets.append(
            W.Button(self.sf, self.W - 150, tab_y, 112, tab_h, lbl, kind='ghost',
                     font_size=10, command=self.toggle_theme, hover_dur=0.12))

    def _leave_ds(self):
        """从官网页返回：有会话就回会话页，否则回首页。"""
        if getattr(self, '_ds_sending', False):
            self.toast('正在发送，等它发完（或点进度窗的「取消」）再切页', 'warn', 3200)
            return
        # 离开官网页签：**先**把浏览器藏起来再做别的（它不会随画布消失）
        self._ds_leave()
        if self.sessions:
            self.build_sessions()
        else:
            self.build_home()

    def toggle_theme(self):
        name = 'dark' if self.theme['name'] == 'light' else 'light'
        self.theme = T.THEMES[name]
        self.settings['theme'] = name
        save_settings(self.settings)
        # 行底色/复选框的图里烤着颜色，换主题必须让它们重画
        for w in self._widgets:
            if hasattr(w, '_clear_photo_cache'):
                try:
                    w._clear_photo_cache()
                except Exception:
                    pass
        self.sf.set_theme(self.theme)
        self.sf.font = T.pick_font(self.root)
        self._widgets.clear()
        self._rebuild_page()

    def _fit_text(self, s, max_px, size):
        """按像素宽度裁剪文本，超出时保留开头与结尾（路径的关键信息在两头）。

        直接按字符数截断会把 'C:\\Users\\someone\\Documents\\xwechat_files'
        变成 '...eone\\Documents\\xwechat_files' —— 开头最关键的部分反而没了。
        """
        s = str(s or '')
        if not s:
            return ''
        try:
            import tkinter.font as tkfont
            f = tkfont.Font(family=self.sf.font, size=size)
            if f.measure(s) <= max_px:
                return s
            ell = '…'
            lo, hi = 0, len(s)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                head = mid // 2 + mid % 2
                tail = mid - head
                cand = s[:head] + ell + (s[-tail:] if tail else '')
                if f.measure(cand) <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            head = lo // 2 + lo % 2
            tail = lo - head
            return s[:head] + ell + (s[-tail:] if tail else '')
        except Exception:
            return s if len(s) <= 26 else s[:13] + '…' + s[-12:]

    def draw_info_card(self, x, y, w, title, value, note, ok=True, on_click=None,
                       btn_text='浏览'):
        """一张信息卡。on_click 不为空时右下角出现按钮（默认「浏览」）。"""
        th = self.theme
        self.sf.draw_panel(x, y, x + w, y + 96, 14, 0.82)
        self.sf.text(x + 18, y + 24, title, 9, th['text_dim'])
        avail = w - 36 - (86 if on_click else 0)
        self.sf.text(x + 18, y + 50, self._fit_text(value, avail, 11),
                     11, th['text'], True)
        self.sf.text(x + 18, y + 74, ('✓ ' if ok else '· ') + note, 9,
                     th['ok'] if ok else th['text_dim'])
        if on_click:
            self._widgets.append(
                W.Button(self.sf, x + w - 84, y + 60, 66, 26, btn_text,
                         kind='ghost', font_size=9, radius=8,
                         command=on_click, hover_dur=0.12))

    def _account_label(self):
        """「账号」那一格显示什么。"""
        acc = getattr(self, 'account_dir', '') or ''
        if not acc:
            return '自动（第一个）', '多个账号时会连错，点右边切换'
        return os.path.basename(acc), '已指定账号目录（数据库不会再找错）'

    def pick_account(self):
        """选具体账号目录（对应 issue #2：同机多账号时会连到别人的库）。"""
        accs = find_account_dirs(getattr(self, 'data_dir', '') or '')
        usable = [(n, p) for n, p, has in accs if has]
        if not accs:
            self.toast('这个数据目录里没有找到账号文件夹（wxid_…）', 'warn', 3200)
            return
        if len(usable) <= 1:
            if usable:
                self.account_dir = usable[0][1]
                self.settings['account_dir'] = self.account_dir
                self.toast(f'只有一个可用账号，已选中：{usable[0][0]}', 'ok', 3000)
            else:
                self.toast('这些账号目录里都没有 session.db', 'warn', 3200)
            self.build_home()
            return

        win = self._dlg('选择要导出的微信账号')
        win.geometry('560x420')
        win.configure(bg=T.rgb2hex(self.theme['bg_top']))
        tk.Label(win, text='这台电脑上检测到多个微信账号的数据。\n'
                           '请选**当前登录/要导出的那个**（选错了会连不上数据库）：',
                 bg=T.rgb2hex(self.theme['bg_top']),
                 fg=T.rgb2hex(self.theme['text']), justify='left',
                 font=(self.sf.font, 10)).pack(anchor='w', padx=18, pady=(14, 8))
        box = tk.Frame(win, bg=T.rgb2hex(self.theme['bg_top']))
        box.pack(fill='both', expand=True, padx=18)

        def choose(path, name):
            self.account_dir = path
            self.settings['account_dir'] = path
            try:
                win.destroy()
            except Exception:                    # noqa: BLE001
                pass
            self.toast(f'已选中账号：{name}', 'ok', 3000)
            self.build_home()

        for name, path, has in accs:
            label = f'{name}' + ('' if has else '   （没有 session.db，不可用）')
            b = tk.Button(box, text=label, anchor='w', relief='flat',
                          font=(self.sf.font, 10),
                          state='normal' if has else 'disabled',
                          command=(lambda p=path, n=name: choose(p, n)) if has else None)
            b.pack(fill='x', pady=3)
        tk.Button(win, text='取消', relief='flat',
                  font=(self.sf.font, 10),
                  command=lambda: win.destroy()).pack(anchor='e', padx=18, pady=10)

    # ────────────────────── 首页 ──────────────────────

    def build_home(self):
        self.page = 'home'
        self._clear_page()
        th = self.theme
        cv = self.sf.canvas

        self.draw_header('批量勾选会话 · 一键导出 AI 语料')

        detected = find_xwechat_dirs()
        self.data_dir = detected or getattr(self, 'data_dir', '')
        # 账号目录记忆（issue #2）：上次选过的优先；只有一个账号时自动选中，
        # 省得用户多点一步。
        saved_acc = self.settings.get('account_dir', '')
        if saved_acc and os.path.isdir(saved_acc):
            self.account_dir = saved_acc
        elif not getattr(self, 'account_dir', ''):
            _accs = [p for _n, p, has in find_account_dirs(self.data_dir) if has]
            self.account_dir = _accs[0] if len(_accs) == 1 else ''
        # 工作目录记忆：上次选过的优先（选择的目录才好记，桌面是默认值不算）
        saved_out = self.settings.get('out_root', '')
        self.out_root = getattr(self, 'out_root',
                                saved_out if saved_out and os.path.isdir(saved_out)
                                else DEFAULT_OUT)

        gap, margin = 20, 38
        cw = (self.W - margin * 2 - gap * 2) // 3
        top = 106
        self.draw_info_card(margin, top, cw, '微信数据目录',
                            self.data_dir or '未检测到',
                            '已自动检测' if detected else '请点「浏览」选择',
                            bool(detected), self.pick_data_dir)
        self.draw_info_card(margin + cw + gap, top, cw, '导出工作目录',
                            self.out_root, '密钥与日志存放处', True,
                            self.pick_out_dir)
        has_key = len(self.key_input_value() or '') == 64
        self.draw_info_card(margin + (cw + gap) * 2, top, cw, '数据库密钥',
                            ('•' * 20) if has_key else '未设置',
                            '已加载保存的密钥' if has_key else '点下方「获取密钥」',
                            has_key)
        # ── 第二行：账号（issue #2 —— 同机多账号时会连到别人的库，必须能切换）──
        usable = [a for a in find_account_dirs(self.data_dir) if a[2]]
        acc_val, acc_note = self._account_label()
        if len(usable) == 1:
            acc_note = '已自动选中唯一账号'
        elif len(usable) > 1:
            acc_note = f'检测到 {len(usable)} 个账号，点右边选对的那个'
        self.draw_info_card(
            margin, top + 110, cw, '微信账号',
            acc_val, acc_note,
            bool(getattr(self, 'account_dir', '')), self.pick_account,
            btn_text='切换' if len(usable) > 1 else '选择')

        # 密钥输入
        ey = top + 118
        self.sf.draw_panel(margin, ey, self.W - margin, ey + 96, 14, 0.82)
        self.sf.text(margin + 18, ey + 24, '密钥', 9, th['text_dim'])
        self.key_var = tk.StringVar(value=self.key_input_value())
        self._key_entry = W.Entry(self.sf, margin + 18, ey + 34,
                                  self.W - margin * 2 - 140, 32,
                                  textvariable=self.key_var,
                                  show='' if self._key_visible else '*')
        self._entries.append(self._key_entry)
        self._key_entry.entry.bind('<KeyRelease>', lambda e: self._on_key_changed())
        self.key_var.trace_add('write', lambda *_: self._on_key_changed())
        self._widgets.append(W.Button(
            self.sf, self.W - margin - 110, ey + 34, 92, 32,
            '隐藏' if self._key_visible else '显示', kind='ghost', font_size=9,
            radius=8, command=self.toggle_key_show, hover_dur=0.12))
        self.sf.text(margin + 18, ey + 80,
                     '点「获取密钥」自动捕获，或直接粘贴 64 位密钥', 8, th['text_faint'])

        # 三个主操作
        by = ey + 124
        bw, bh = 240, 46
        bx = (self.W - bw) // 2
        self.btn_key = W.Button(self.sf, bx, by, bw, bh, '获取密钥', kind='primary',
                                icon='🔑', font_size=12, command=self.do_getkey)
        self.btn_conn = W.Button(self.sf, bx, by + bh + 12, bw, bh, '连接数据库',
                                 kind='primary', icon='🗄', font_size=12,
                                 command=self.do_connect)
        self.btn_browse = W.Button(self.sf, bx, by + (bh + 12) * 2, bw, bh,
                                   '浏览会话', kind='primary', icon='📤',
                                   font_size=12, command=self.build_sessions)
        self._widgets += [self.btn_key, self.btn_conn, self.btn_browse]

        self.status_y = by + (bh + 12) * 3 + 14
        self.sf.text(self.W / 2, self.status_y, self._status_text(), 9,
                     th['text_dim'], anchor='center', tags='status')

        if self.sessions:
            self.btn_conn.set_state('normal')
            self.btn_browse.set_state('normal')
        if len(self.key_input_value() or '') == 64:
            self.btn_conn.set_state('normal')
            self.status('已加载保存的密钥', 'ok')

        # 启动后自动填一次密钥
        if not self.key_var.get():
            saved = load_saved_key()
            if saved:
                self.key_var.set(saved)
                self.key = saved

    def _status_text(self):
        return getattr(self, '_status', '就绪')

    def status(self, msg, kind='info'):
        self._status = msg
        col = {'info': self.theme['text_dim'], 'ok': self.theme['ok'],
               'err': self.theme['err'], 'warn': self.theme['warn']}.get(
                   kind, self.theme['text_dim'])
        try:
            self.sf.canvas.itemconfigure('status', text=msg, fill=col)
        except Exception:
            pass

    def key_input_value(self):
        v = getattr(self, 'key_var', None)
        if v:
            return v.get().strip()
        return self.key or ''

    def _on_key_changed(self):
        ok = bool(re.fullmatch(r'[0-9a-fA-F]{64}', self.key_input_value()))
        if getattr(self, 'btn_conn', None):
            self.btn_conn.set_state('normal' if ok else 'disabled')

    def toggle_key_show(self):
        self._key_visible = not self._key_visible
        self._key_entry.set_show('' if self._key_visible else '*')

    def pick_data_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.data_dir or '/')
        if d:
            self.data_dir = d
            self.build_home()

    def pick_out_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.out_root or '/')
        if not d:
            return
        global OUT, KEY_FILE
        self.out_root = d
        try:
            os.makedirs(d, exist_ok=True)
            OUT = d
            KEY_FILE = os.path.join(OUT, 'key.txt')
        except OSError as e:
            self.toast(f'目录不可写：{e}', 'err')
            return
        # 记住这个目录，下次开程序直接用
        self.settings['out_root'] = d
        save_settings(self.settings)
        self.build_home()
        self.toast('导出工作目录已更新', 'ok')

    # ────────────────────── 获取密钥 / 连接 ──────────────────────

    def do_getkey(self):
        if self.busy:
            return
        from tkinter import messagebox
        if not messagebox.askokcancel('准备', (
                '1. 关闭微信电脑端（右键系统托盘 → 退出）\n'
                '2. 点确定后等待\n'
                '3. 看到「等待微信启动」后打开微信\n'
                '4. 微信启动过程中自动捕获密钥')):
            return
        threading.Thread(target=self._getkey, daemon=True).start()

    def _getkey(self):
        node = self._find_node()
        js = os.path.join(ROOT, 'scripts', 'get_key.js')
        if not node or not os.path.exists(js):
            self.root.after(0, lambda: self.toast(f'找不到运行时: {node}', 'err'))
            return
        self.root.after(0, lambda: self.busy_on('等待捕获密钥…'))
        status_file = os.path.join(OUT, 'key_status.txt')
        for p in (status_file, KEY_FILE):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', node, f'"{js}"', None, 1)
        if ret <= 32:
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self.toast('提权失败，请手动以管理员运行', 'err'))
            return

        status_map = {
            'started': '脚本已启动', 'dll_found': '找到 wx_key.dll',
            'dll_loaded': 'DLL 加载成功', 'dll_not_found': '找不到 wx_key.dll',
            'waiting_close': '等待微信关闭…', 'timeout_close': '关微信超时',
            'waiting_start': '等待微信启动…（请打开微信）', 'timeout_start': '等微信启动超时',
            'injecting': '正在注入 Hook…', 'hook_ok': 'Hook 成功，等登录捕获',
            'hook_failed': 'Hook 注入失败', 'polling': '等待登录中捕获…',
            'timeout_poll': '获取超时', 'captured': '已捕获',
        }
        for _ in range(180):
            if os.path.exists(KEY_FILE):
                try:
                    with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                        k = f.read().strip()
                except OSError:
                    k = ''
                if len(k) == 64:
                    self.root.after(0, self.busy_off)
                    self.root.after(0, lambda kk=k: self._key_captured(kk))
                    return
            if os.path.exists(status_file):
                try:
                    with open(status_file, encoding='utf-8', errors='ignore') as f:
                        st = f.read().strip()
                    msg = status_map.get(st.split(':')[0], st)
                    self.root.after(0, lambda m=msg: self.status('密钥：' + m))
                except OSError:
                    pass
            threading.Event().wait(1)
        self.root.after(0, self.busy_off)
        self.root.after(0, lambda: self.toast('密钥获取失败', 'err'))

    def _key_captured(self, k):
        self.key = k
        self.key_var.set(k)
        self.build_home()
        self.toast('密钥获取成功', 'ok')

    def _find_node(self):
        n = os.path.join(ROOT, 'runtime', 'node.exe')
        return n if os.path.exists(n) else (shutil.which('node') or '')

    def do_connect(self):
        if self.busy:
            return
        manual = self.key_input_value()
        if len(manual) == 64:
            self.key = manual
        elif not (self.key and len(self.key) == 64) and os.path.exists(KEY_FILE):
            try:
                with open(KEY_FILE, encoding='utf-8', errors='ignore') as f:
                    self.key = f.read().strip()
            except OSError:
                pass
        if not self.key or len(self.key) != 64:
            self.toast('密钥无效，请先获取或粘贴 64 位密钥', 'err')
            return
        self.busy_on('连接数据库…')
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            os.makedirs(OUT, exist_ok=True)
            with open(KEY_FILE, 'w', encoding='utf-8') as f:
                f.write(self.key)
            from wcdb_server import WCDBClient
            cli = WCDBClient()
            # ⚠️ 传**具体账号目录**（issue #2）：服务端默认"递归找到第一个 session.db 就用"，
            #    同机登录过多个微信时会拿错账号的库 → 打不开 → 连接超时。
            cli.start(self.key, self.data_dir,
                      account_dir=getattr(self, 'account_dir', '') or '')
            if getattr(self, '_closing', False):
                # 启动期间用户关窗了。quit_app 已经把手里的 wcdb 置空去关了，
                # 这个刚建好的没人管 —— 不收掉它会残留进程占着目录。
                try:
                    cli.stop()
                except Exception:
                    pass
                return
            self.wcdb = cli
            self.sessions = self.wcdb.get_sessions() or []
            users = [s.get('username', '') for s in self.sessions
                     if s.get('username') and not str(s.get('username')).startswith('brand')]
            if users:
                try:
                    self.nick_map = self.wcdb.get_display_names(users[:500]) or {}
                except Exception:
                    self.nick_map = {}
            n = len(self.sessions)
            self.last_error = ''
            if getattr(self, '_closing', False):
                return
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self._connected(n))
        except Exception as e:
            import traceback
            err = str(e)
            # 留存完整堆栈：界面上给用户看简短版，自动化测试里能取到完整原因
            self.last_error = f'{err}\n{traceback.format_exc()}'
            if getattr(self, '_closing', False):
                return          # 窗口已销毁，再 after() 会抛 TclError
            self.root.after(0, self.busy_off)
            self.root.after(0, lambda: self.toast(f'连接失败：{err[:60]}', 'err'))

    def _connected(self, n):
        self.build_home()
        self.toast(f'已连接数据库，共 {n} 个会话', 'ok')

    # ────────────────────── 会话页 ──────────────────────

    def _session_items(self):
        items = []
        for s in self.sessions:
            w = s.get('username', '') or ''
            if not w:
                continue
            name = self.nick_map.get(w) or s.get('last_sender_display_name') or w
            ts = s.get('last_timestamp', s.get('sort_timestamp', ''))
            if str(ts).isdigit():
                try:
                    ts = datetime.datetime.fromtimestamp(int(ts)).strftime('%m-%d %H:%M')
                except (ValueError, OSError):
                    pass
            items.append({'wxid': w, 'title': str(name)[:34],
                          'preview': str(s.get('summary', '') or '')[:46],
                          'time': str(ts)[:16],
                          'group': str(w).endswith('@chatroom'),
                          'tags': self.tag_store.tags_of(w),
                          'sel': w in getattr(self, '_selected', set())})
        # 有消息的排前面
        items.sort(key=lambda it: 0 if it['preview'] else 1)
        return items

    def build_sessions(self):
        # 先认领页面 + 收起内嵌网页，再做"有没有数据"的判断 ——
        # 否则在没有会话数据时会提前 return，官网页的浏览器窗口就留在屏幕上了
        # （实测踩到：切回会话页，网页还在）。
        self.page = 'sessions'
        self._ds_leave()
        if not self.sessions:
            self.toast('还没有会话数据，请先「连接数据库」', 'warn')
            return
        self._clear_page()
        th = self.theme
        if not hasattr(self, '_selected'):
            self._selected = set()

        self.draw_header(f'共 {len(self.sessions)} 个会话 · 勾选后统一导出')
        # 选中数量显示在标题栏右侧（不占列表表头，避免和勾选框挤在一起）。
        # 右边要给「官网对话」页签按钮留位置，所以结束在 W-312 而不是 W-176。
        self.sf.text(self.W - 312, 66, '尚未勾选', 10, th['accent'], bold=True,
                     anchor='e', tags='selhint')

        # 返回 + 选择操作
        top = 96
        self._widgets.append(W.Button(self.sf, 38, top, 92, 34, '← 返回', kind='ghost',
                                      font_size=10, radius=9, command=self.build_home,
                                      hover_dur=0.12))
        bx = 142
        for label, cb in (('全选', lambda: self._sel_all(True)),
                          ('全不选', lambda: self._sel_all(False)),
                          ('反选', self._sel_invert),
                          ('全选搜索结果', lambda: self._sel_all(True, True))):
            w_ = 108 if label == '全选搜索结果' else 74
            self._widgets.append(W.Button(self.sf, bx, top, w_, 34, label,
                                          kind='ghost', font_size=10, radius=9,
                                          command=cb, hover_dur=0.12))
            bx += w_ + 8
        # 「打包成标签」：把当前勾选的会话一次性打上某个标签（可选已有的，也可新建）
        self._widgets.append(W.Button(self.sf, bx, top, 118, 34, '🏷 打包成标签',
                                      kind='ghost', font_size=10, radius=9,
                                      command=self.open_tag_dialog, hover_dur=0.12))

        # 搜索框（放在列表右上）
        self.search_var = tk.StringVar()
        self._search_entry = W.Entry(self.sf, self.W - 38 - 300, top, 300, 34,
                                     textvariable=self.search_var)
        self._entries.append(self._search_entry)
        self.search_var.trace_add('write', lambda *_: self._on_search())
        self._search_ph = self.sf.text(self.W - 38 - 300 + 14, top + 17,
                                       '搜索会话…', 10, th['text_faint'])

        # 标签行：点一个标签 = 把「带这个标签的会话」全部勾上（替换当前勾选）
        row_y = top + 42
        CHIP_H = 26
        self._chip_y = row_y
        self._draw_tag_chips(row_y, CHIP_H)

        # 列表
        list_top = row_y + CHIP_H + 10
        list_h = self.H - list_top - 200
        self.list = W.CheckList(self.sf, 38, list_top, self.W - 76, list_h,
                                on_toggle=self._on_toggle, on_open=self.show_chat)
        self.list.set_items(self._session_items())

        # 底部导出栏（高度要够放：标签+输入框 / 勾选行 / 提示行，共 3 行）
        BAR_H = 132
        by = self.H - BAR_H - 18
        self.sf.draw_panel(38, by, self.W - 38, by + BAR_H, 14, 0.88)
        self.sf.text(56, by + 22, '导出格式', 9, th['text_dim'])
        self._fmt = W.Dropdown(self.sf, 56, by + 32, 300, 34,
                               [lbl for lbl, _ in FORMAT_CHOICES],
                               value=next(lbl for lbl, k in FORMAT_CHOICES
                                          if k == getattr(self, '_fmt_key',
                                                          FORMAT_DEFAULT)),
                               on_change=self._on_fmt_change)
        self.sf.text(374, by + 22, '导出到', 9, th['text_dim'])
        # 导出目录记忆：优先用上次选过的（存在设置里），否则默认桌面
        default_root = os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop')
        saved = self.settings.get('export_root', '')
        self.exp_root = getattr(self, 'exp_root',
                                saved if saved and os.path.isdir(saved) else default_root)
        self._exp_entry = W.Entry(self.sf, 374, by + 32, 300, 34)
        self._exp_entry.set(self.exp_root)
        self._entries.append(self._exp_entry)
        self._widgets.append(W.Button(self.sf, 682, by + 32, 74, 34, '浏览',
                                      kind='ghost', font_size=10, radius=9,
                                      command=self.pick_export_root, hover_dur=0.12))

        # 覆盖选项：勾上后每次导出前先清掉上一次的导出内容，省磁盘
        self._clear_cb = W.Checkbox(
            self.sf, 56, by + 78, 360,
            '导出并清除之前的导出内容（覆盖，省空间）',
            value=bool(getattr(self, '_clear_before', False)),
            on_change=self._on_clear_toggle, font_size=9)

        self.btn_export = W.Button(self.sf, self.W - 38 - 220, by + 26, 220, 44,
                                   '开始导出', kind='primary', icon='📦',
                                   font_size=13, command=self.do_export)
        self.btn_prompt = W.Button(self.sf, self.W - 38 - 220, by + 78, 220, 28,
                                   '✏ 编辑给 AI 的指令', kind='ghost', font_size=9,
                                   radius=8, command=self.edit_prompt)
        self._widgets += [self.btn_export, self.btn_prompt]

        # 提示放独立一行，靠右对齐到按钮左边界，避免与按钮重叠
        self._fmt_hint = self.sf.text(56, by + 110,
                                      FORMAT_HINTS.get(getattr(self, '_fmt_key',
                                                               FORMAT_DEFAULT), ''),
                                      8, th['text_faint'])
        # 这三个也是输入消费者，必须登记以便页面切换时注销（否则旧实例残留收事件）
        self._widgets += [self.list, self._fmt, self._clear_cb]
        self._update_sel_label()

    def _on_clear_toggle(self, value):
        self._clear_before = bool(value)
        self.settings['clear_before'] = '1' if value else '0'
        save_settings(self.settings)
        if value:
            self.toast('开启后，每次导出会先删除上一次的导出内容', 'warn', 3200)

    def _on_fmt_change(self, label):
        self._fmt_key = dict(FORMAT_CHOICES)[label]
        try:
            self.sf.canvas.itemconfigure(self._fmt_hint,
                                         text=FORMAT_HINTS.get(self._fmt_key, ''))
        except Exception:
            pass

    def _on_search(self):
        if getattr(self, '_search_entry', None):
            has = bool(self.search_var.get())
            try:
                self.sf.canvas.itemconfigure(self._search_ph, state='hidden' if has
                                             else 'normal')
            except Exception:
                pass
        if getattr(self, 'list', None):
            self.list.refresh_view(self.search_var.get())
            self._update_sel_label()

    def _on_toggle(self, item):
        if item.get('sel'):
            self._selected.add(item['wxid'])
        else:
            self._selected.discard(item['wxid'])
        self._update_sel_label()

    def _update_sel_label(self):
        """把「已选 N 个」写进顶部标题栏（而不是列表表头）。

        表头已经有「共 N 个 · 已选 M 个」（CheckList 自己画的），
        重复显示既冗余又会和勾选框挤在一起。
        """
        n = len(self._selected)
        try:
            self.sf.canvas.itemconfigure(
                'selhint', text=(f'已选 {n} 个会话' if n else '尚未勾选'))
        except Exception:
            pass

    def _sel_all(self, value, only_visible=False):
        self.list.set_all(value, only_visible)
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()

    def _sel_invert(self):
        self.list.invert()
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()

    # ────────────────────── 会话标签 ──────────────────────

    def _text_px(self, s, size):
        """文本像素宽度（用来给 chip 定宽；取不到就按每字 8px 估）。"""
        try:
            import tkinter.font as tkfont
            return tkfont.Font(family=self.sf.font, size=size).measure(str(s))
        except Exception:
            return len(str(s)) * 8

    def _draw_tag_chips(self, y, h):
        """画标签行：每个标签一个 chip，点一下就把带该标签的会话全部勾上。

        chip 就是自绘 Button（没有现成的 chip 控件），标签多的时候按可用宽度
        截断，最后一个 chip 固定是「管理」，点开标签管理窗。
        """
        th = self.theme
        tags = self.tag_store.all_tags()
        counts = self.tag_store.counts()
        x = 38
        right_limit = self.W - 38 - 120          # 给「管理」留位置
        if not tags:
            self.sf.text(38, y + h // 2,
                         '还没有标签：「勾选几个会话 → 🏷 打包成标签」，'
                         '下次点标签就能一键勾上这些会话',
                         9, th['text_faint'])
        for t in tags:
            label = f'#{t}' + (f' {counts.get(t, 0)}' if counts.get(t) else '')
            w_ = min(170, max(56, self._text_px(label, 9) + 26))
            if x + w_ > right_limit:
                self.sf.text(x + 4, y + h // 2, '…', 10, th['text_faint'])
                break
            self._widgets.append(W.Button(
                self.sf, x, y, w_, h, label, kind='ghost', font_size=9, radius=9,
                command=(lambda tag=t: self._sel_by_tag(tag)), hover_dur=0.12))
            x += w_ + 6
        # 「管理」恒在最右：新建/删除/清空标签都从这里进
        self._widgets.append(W.Button(
            self.sf, self.W - 38 - 110, y, 110, h, '🏷 标签管理', kind='ghost',
            font_size=9, radius=9, command=self.open_tag_dialog, hover_dur=0.12))

    def _sel_by_tag(self, tag):
        """按标签勾选：**替换**当前勾选（用户明确要的行为）。"""
        wxids = self.tag_store.wxids_with(tag)
        if not wxids:
            self.toast(f'没有会话带「{tag}」标签', 'warn')
            return
        self.list.set_selected_wxids(wxids)
        self._selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._update_sel_label()
        self.toast(f'已按标签「{tag}」勾选 {len(self._selected)} 个会话', 'ok')

    def _refresh_sessions(self, keep_search=True, keep_scroll=True):
        """标签改动后刷新会话页（整页重建最省心，也避免控件表/图元残留）。

        重建会丢掉搜索词与滚动位置，这里手动续上。
        """
        kw = self.search_var.get() if (keep_search and getattr(self, 'search_var', None)) else ''
        scroll = self.list.scroll if (keep_scroll and getattr(self, 'list', None)) else 0
        self.build_sessions()
        if kw:
            self.search_var.set(kw)
            self._on_search()
        if scroll and getattr(self, 'list', None):
            self.list.scroll = min(scroll, max(0, len(self.list.view) - 1))
            self.list.draw()

    def _apply_tag(self, tag, wxids):
        """把标签贴到这批会话上并落盘。"""
        tag = session_tags.clean_tag(tag)
        if not tag:
            self.toast('标签名不能为空', 'err')
            return False
        if not wxids:
            self.toast('请先勾选要打标签的会话', 'warn')
            return False
        added = self.tag_store.assign(wxids, tag)
        if not self.tag_store.save():
            self.toast('标签保存失败（文件可能被占用），本次改动没有落盘', 'err', 4000)
            return False
        self.toast(f'已给 {len(wxids)} 个会话打上「{tag}」（新增 {added} 个）', 'ok')
        self._refresh_sessions()
        return True

    def _untag_selection(self, tag):
        wxids = sorted(self._selected)
        if not wxids:
            self.toast('请先勾选会话', 'warn')
            return
        self.tag_store.untag(wxids, tag)
        self.tag_store.save()
        self.toast(f'已从 {len(wxids)} 个会话上摘掉「{tag}」', 'ok')
        self._refresh_sessions()

    def _delete_tag(self, tag):
        n = self.tag_store.delete_tag(tag)
        self.tag_store.save()
        self.toast(f'已删除标签「{tag}」（影响 {n} 个会话）', 'ok')
        self._refresh_sessions()

    def open_tag_dialog(self):
        """标签窗：给勾选的会话打标签。

        布局（自上而下）：
          1. 顶部一大行：**输入新标签名** → 「新建并贴上」（回车也行）；
          2. 已有标签列表：每个标签一行，三件事都能做 ——
             「贴到勾选的会话」/「只勾选这些会话」/「删除标签」；
          3. 底部：清除勾选会话的标签、关闭。

        用普通 tk 控件放在独立 Toplevel 里（和自绘 Canvas 无关）。
        ⚠️ 这里任何一处抛异常，用户看到的就是"跳出一个空框、点不动" ——
        因为 Button 的 command 是在 Surface 分发层被调用的（异常已改为上报日志）。
        """
        th = self.theme
        sel = sorted(self._selected)
        # ⚠️ 走 _dlg()：它会先把内嵌浏览器藏起来。浏览器是 root 的 owned 窗口，
        #    而 Tk 弹窗也是 root 的子窗口 —— 在 Windows 眼里是**兄弟**，z 序只看谁
        #    最后被激活，浏览器很容易把这个弹窗盖住（用户实测反馈过）。
        win = self._dlg('会话标签')
        win.geometry('560x460')
        win.minsize(500, 380)
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        win.lift()
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        bg = T.rgb2hex(th['bg_top'])
        fg = T.rgb2hex(th['text'])
        dim = T.rgb2hex(th['text_dim'])
        accent = T.rgb2hex(th['accent'])
        font = self.sf.font

        head = tk.Frame(win, bg=bg)
        head.pack(fill='x', padx=18, pady=(16, 4))
        tk.Label(head, text=f'已勾选 {len(sel)} 个会话', bg=bg, fg=accent,
                 font=(font, 12, 'bold')).pack(side='left')
        tk.Label(head, text='（勾选变化后重新打开这个窗口即可）', bg=bg, fg=dim,
                 font=(font, 8)).pack(side='left', padx=(8, 0))

        # ① 新建
        tk.Label(win, text='① 给这批会话新建一个标签（输入名字后回车或点右边按钮）',
                 bg=bg, fg=dim, font=(font, 9)).pack(anchor='w', padx=18, pady=(10, 4))
        line = tk.Frame(win, bg=bg)
        line.pack(fill='x', padx=18)
        var = tk.StringVar()
        ent = tk.Entry(line, textvariable=var, font=(font, 12))
        ent.pack(side='left', fill='x', expand=True, ipady=5)

        def create(_e=None):
            if not sel:
                self.toast('还没有勾选会话', 'warn')
                return
            if self._apply_tag(var.get(), sel):
                win.destroy()

        tk.Button(line, text='新建并贴上', font=(font, 10), relief='flat',
                  cursor='hand2', bg=accent, fg='#ffffff', activebackground=accent,
                  command=create).pack(side='left', padx=(8, 0))
        ent.bind('<Return>', create)
        ent.focus_set()

        # ② 已有标签
        tk.Label(win, text='② 或者用已有标签（可以直接把它的会话集合勾上）',
                 bg=bg, fg=dim, font=(font, 9)).pack(anchor='w', padx=18, pady=(14, 4))
        box = tk.Frame(win, bg=bg)
        box.pack(fill='both', expand=True, padx=18)
        tags = self.tag_store.all_tags()
        counts = self.tag_store.counts()
        if not tags:
            tk.Label(box, text='（还没有标签）', bg=bg, fg=dim,
                     font=(font, 9)).pack(anchor='w')

        def apply_existing(tag):
            if not sel:
                self.toast('还没有勾选会话', 'warn')
                return
            if self._apply_tag(tag, sel):
                win.destroy()

        def select_only(tag):
            win.destroy()
            self._sel_by_tag(tag)

        for t in tags:
            row = tk.Frame(box, bg=bg)
            row.pack(fill='x', pady=2)
            tk.Label(row, text=f'#{t}', bg=bg, fg=accent, width=10, anchor='w',
                     font=(font, 10, 'bold')).pack(side='left')
            tk.Label(row, text=f'{counts.get(t, 0)} 个会话', bg=bg, fg=dim,
                     width=9, anchor='w', font=(font, 9)).pack(side='left')
            tk.Button(row, text='贴到勾选的会话', font=(font, 9), relief='flat',
                      cursor='hand2',
                      command=(lambda tag=t: apply_existing(tag))).pack(side='left')
            tk.Button(row, text='只勾选这些会话', font=(font, 9), relief='flat',
                      cursor='hand2',
                      command=(lambda tag=t: select_only(tag))).pack(side='left',
                                                                     padx=(6, 0))
            tk.Button(row, text='删除', font=(font, 9), relief='flat', fg='#c0392b',
                      cursor='hand2',
                      command=(lambda tag=t: (self._delete_tag(tag), win.destroy()))
                      ).pack(side='right')

        # ③ 清除
        foot = tk.Frame(win, bg=bg)
        foot.pack(fill='x', padx=18, pady=(8, 14))
        tk.Button(foot, text='清除这批会话的全部标签', font=(font, 10), relief='flat',
                  cursor='hand2',
                  command=lambda: (self._clear_tags_of_selection(), win.destroy())
                  ).pack(side='left')
        tk.Button(foot, text='关闭', font=(font, 10), relief='flat',
                  cursor='hand2', command=win.destroy).pack(side='right')

    def _clear_tags_of_selection(self):
        wxids = sorted(self._selected)
        if not wxids:
            self.toast('请先勾选会话', 'warn')
            return
        self.tag_store.clear(wxids)
        self.tag_store.save()
        self.toast(f'已清除 {len(wxids)} 个会话的标签', 'ok')
        self._refresh_sessions()

    def pick_export_root(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.exp_root or '/')
        if d:
            self.exp_root = d
            self._exp_entry.set(d)
            self._remember_export_root(d)

    def _remember_export_root(self, path):
        """把导出目录记进设置，下次开程序直接用它。"""
        if not path:
            return
        self.settings['export_root'] = path
        try:
            save_settings(self.settings)
        except Exception:
            pass

    # ────────────────────── 导出 ──────────────────────

    def do_export(self):
        if self.busy:
            self.toast('正在导出中，请稍候', 'warn')
            return
        if not self.wcdb:
            self.toast('请先连接数据库', 'err')
            return
        picked = [it for it in self.list.items if it.get('sel')]
        if not picked:
            self.toast('请先勾选要导出的会话', 'warn')
            return
        self.exp_root = self._exp_entry.get().strip()
        if not self.exp_root:
            self.toast('请选择导出位置', 'err')
            return
        self._remember_export_root(self.exp_root)   # 手输的也记住
        try:
            os.makedirs(self.exp_root, exist_ok=True)
        except OSError as e:
            self.toast(f'无法创建导出目录：{e}', 'err')
            return
        fmt = getattr(self, '_fmt_key', FORMAT_DEFAULT)
        clear_before = bool(getattr(self, '_clear_before', False))

        # 覆盖前先算清楚会删什么，让用户确认 —— 导出目录可能选在桌面这种地方，
        # 闷声删除是不可接受的
        if clear_before:
            import batch_export as _be
            info = _be.scan_previous_exports(self.exp_root)
            if info['dirs'] or info['files']:
                mb = info['bytes'] / 1048576
                names = '\n'.join('  · ' + d for d in info['dirs'][:8])
                if len(info['dirs']) > 8:
                    names += f'\n  · …另外 {len(info["dirs"]) - 8} 个'
                from tkinter import messagebox
                if not messagebox.askokcancel(
                        '确认覆盖',
                        f'将删除 {self.exp_root} 里上一次导出的内容：\n\n'
                        f'{names}\n'
                        f'{("  · " + chr(10).join("  · " + f for f in info["files"])) if info["files"] else ""}\n\n'
                        f'共占用约 {mb:.1f} MB。\n\n'
                        '只删本工具生成的「导出_日期_时间」目录和索引文件，\n'
                        '其它文件一律不动。\n\n确定继续？'):
                    return
            else:
                self.toast('没有找到上一次的导出内容', 'info', 2000)

        self._open_export_window(
            [{'wxid': it['wxid'], 'title': it['title']} for it in picked],
            fmt, self.exp_root, clear_before=clear_before)

    def _open_export_window(self, picked, fmt, out_root, clear_before=False):
        """导出进度窗（独立 Toplevel，进度条与日志都在上面）。"""
        th = self.theme
        self.busy = True
        # ⚠️ 走 _dlg()：先把浏览器藏起来（理由见 _dlg 的说明）
        win = self._dlg('导出中…')
        win.geometry('620x400')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=T.rgb2hex(th['bg_top']))
        cv.pack(fill='both', expand=True)
        bg = T.make_background((620, 400), th, '')
        ph = __import__('PIL.ImageTk', fromlist=['PhotoImage']).PhotoImage(bg)
        cv.create_image(0, 0, image=ph, anchor='nw')
        win._bg = ph

        cv.create_text(30, 34, text=f'正在导出 {len(picked)} 个会话', anchor='w',
                       fill=th['text'], font=(self.sf.font, 14, 'bold'))
        cur = cv.create_text(30, 66, text='准备中…', anchor='w',
                             fill=th['accent'], font=(self.sf.font, 10, 'bold'))

        bar_bg = 30, 88, 590, 100
        T.round_rect_items(cv, *bar_bg, 6, T.mix(th['surface2'], th['border'], 0.5),
                           '', 0)

        def set_prog(frac):
            cv.delete('pbar')
            w = (bar_bg[2] - bar_bg[0] - 4) * max(0.0, min(1.0, frac))
            if w > 2:
                T.round_rect_items(cv, bar_bg[0] + 2, bar_bg[1] + 2,
                                   bar_bg[0] + 2 + w, bar_bg[3] - 2, 5,
                                   th['accent'], '', 0, tags=('pbar',))

        set_prog(0)
        cv.create_text(30, 122, text='日志', anchor='w', fill=th['text_dim'],
                       font=(self.sf.font, 9))

        log_bg = (30, 140, 590, 300)
        T.round_rect_items(cv, *log_bg, 10, th['surface'], th['border'], 1)
        log_ids = []

        def add_log(line):
            if len(log_ids) > 9:
                cv.delete(log_ids.pop(0))
                for i, iid in enumerate(log_ids):
                    cv.coords(iid, 44, 162 + i * 15)
            iid = cv.create_text(44, 162 + len(log_ids) * 15, text=str(line)[:78],
                                 anchor='w', fill=th['text_dim'],
                                 font=(self.sf.font, 8))
            log_ids.append(iid)

        cancel = {'v': False}

        # 弹窗内的按钮用 ttk：Toplevel 上叠自绘控件需要自己管重绘，
        # 而弹窗是临时界面，样式差异影响小，优先选稳妥。
        from tkinter import ttk
        bf = tk.Frame(win, bg=T.rgb2hex(th['bg_top']))
        bf.place(x=30, y=316)
        cancel_btn = ttk.Button(bf, text='取消导出', width=12)
        cancel_btn.pack(side='left', padx=(0, 8))
        open_btn = ttk.Button(bf, text='打开导出目录', width=14, state='disabled')
        open_btn.pack(side='left', padx=8)
        # 导出完顺手就能投喂：直接把这次导出目录填进官网页的发送清单
        send_btn = ttk.Button(bf, text='→ 去官网发送', width=14, state='disabled')
        send_btn.pack(side='left', padx=8)
        close_btn = ttk.Button(bf, text='关闭', width=10, state='disabled')
        close_btn.pack(side='left', padx=8)

        def go_send():
            root_dir = result.get('root', '')
            if not os.path.isdir(root_dir):
                return
            self.ds_out_root = root_dir
            self.ds_units = []
            self.settings['ds_root'] = root_dir
            try:
                save_settings(self.settings)
            except Exception:
                pass
            win.destroy()
            self.build_ds()

        def on_cancel():
            cancel['v'] = True
            cancel_btn.config(state='disabled', text='正在停止…')

        cancel_btn.config(command=on_cancel)

        def on_progress(done, total, title):
            def upd():
                set_prog((done - 1) / max(1, total))
                cv.itemconfigure(cur, text=f'第 {done}/{total} 个：{title}')
            self.root.after(0, upd)

        result = {}

        def worker():
            import batch_export
            try:
                result.update(batch_export.export_sessions(
                    self.wcdb, self.data_dir, picked, fmt, out_root,
                    progress=on_progress,
                    should_cancel=lambda: cancel['v'],
                    log=lambda s: self.root.after(0, lambda ss=s: add_log(ss)),
                    base_dir=BASE,
                    clear_before=clear_before))
            except Exception as e:
                import traceback
                result['fatal'] = f'{e}\n{traceback.format_exc()}'
            self.root.after(0, finish)

        def finish():
            self.busy = False
            set_prog(1.0)
            cancel_btn.config(state='disabled')
            close_btn.config(state='normal', command=win.destroy)
            if 'fatal' in result:
                cv.itemconfigure(cur, text='导出失败', fill=th['err'])
                add_log(result['fatal'].splitlines()[0])
                self.toast('导出失败，详见日志', 'err')
                return
            ok = result.get('ok', [])
            failed = result.get('failed', [])
            root_dir = result.get('root', '')
            cv.itemconfigure(cur,
                             text=(f'已取消（完成 {len(ok)} 个）' if result.get('cancelled')
                                   else f'完成：成功 {len(ok)} 个，失败 {len(failed)} 个'),
                             fill=th['ok'] if not failed else th['warn'])
            add_log(f'导出目录：{root_dir}')
            for f in failed:
                add_log(f'失败 {f.get("title")}：{f.get("error")}')
            open_btn.config(state='normal',
                            command=lambda: os.startfile(root_dir)
                            if os.path.isdir(root_dir) else None)
            send_btn.config(state='normal' if os.path.isdir(root_dir) else 'disabled',
                            command=go_send)
            self.toast(f'导出完成：成功 {len(ok)} 个', 'ok')

        threading.Thread(target=worker, daemon=True).start()

    # ────────────────────── 预览窗口 ──────────────────────

    def show_chat(self, item):
        """双击会话 → 预览消息（保留原功能）。"""
        if not self.wcdb:
            return
        wxid = item['wxid']
        try:
            total = self.wcdb.get_count(wxid)
        except Exception:
            total = 0
        th = self.theme
        # ⚠️ 走 _dlg()：先把浏览器藏起来（理由见 _dlg 的说明）；标题是动态的，随后覆盖
        win = self._dlg('消息预览')
        win.title(f"{item['title']}（{total} 条）")
        win.geometry('940x680')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass

        txt = tk.Text(win, wrap='word', font=(self.sf.font, 10), bd=0,
                      bg=th['surface'], fg=th['text'], insertbackground=th['accent'],
                      padx=14, pady=12, highlightthickness=0)
        sb = tk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        txt.pack(fill='both', expand=True, padx=10, pady=10)
        txt.tag_configure('who', foreground=th['accent'],
                          font=(self.sf.font, 10, 'bold'))
        txt.tag_configure('sys', foreground=th['text_faint'])
        txt.insert('end', '加载中…\n')
        txt.config(state='disabled')

        def load():
            try:
                raw = self.wcdb.get_messages(wxid, 500, 0)
            except Exception as e:
                txt.config(state='normal')
                txt.delete('1.0', 'end')
                txt.insert('end', f'读取失败：{e}\n')
                txt.config(state='disabled')
                return
            import message_content
            senders = list({m.get('sender_username', '') for m in raw
                            if m.get('sender_username')})
            if wxid not in senders:
                senders.append(wxid)
            try:
                nick = self.wcdb.get_display_names(senders) or {}
            except Exception:
                nick = {}
            my = ''
            try:
                for d in os.listdir(self.data_dir or '.'):
                    if d.startswith('wxid_'):
                        my = clean_wxid(d)
                        break
            except OSError:
                pass
            rows = message_content.prepare(raw, nick, my, '我')
            txt.config(state='normal')
            txt.delete('1.0', 'end')
            for m in rows:
                if m.get('kind') == 'system':
                    txt.insert('end', f"        {m.get('time_str','')}  "
                                      f"{m.get('text','')}\n\n", 'sys')
                else:
                    txt.insert('end', f"{m.get('time_str','')}  ")
                    txt.insert('end', f"{m.get('sender_display','')}\n", 'who')
                    txt.insert('end', f"  {m.get('text','')}\n\n")
            txt.config(state='disabled')
            win.title(f"{item['title']}（{len(rows)} 条）")

        win.after(60, load)

    # ────────────────────── 给 AI 的指令 ──────────────────────

    def edit_prompt(self):
        path = os.path.join(BASE, 'AI提示词.txt')
        try:
            if not os.path.exists(path):
                import ai_prompt
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(ai_prompt.load_prompt(BASE).strip() + '\n')
            os.startfile(path)
            self.toast('已用记事本打开，改完保存即可生效', 'ok', 4000)
        except Exception as e:
            self.toast(f'无法打开：{e}', 'err')

    # ────────────────────── DeepSeek 官网页 ──────────────────────

    DS_LEFT_W = 330          # 左栏（发送清单）宽度
    # 每批文件数：**默认 30**（用户定）。
    # 注意别再往上调：实测单条消息 40 个附件就会被官网拒收（每个附件显示
    # "服务器繁忙"、整条消息报"请删除异常文件再发送"），30 是实测的安全上限。
    DS_LIMIT = 30
    DS_LIMIT_CHOICES = [10, 20, 30]
    # 工具栏上两组按钮的 (文字, 宽度)。坐标由布局表算（见 ds_page_layout）：
    # 窄窗口一行放不下时，右边这组会自动落到第二行。
    DS_TOOLBAR_LEFT = [('选择导出文件夹', 150), ('全选', 72), ('全不选', 76),
                       ('演练分批', 92), ('诊断', 72)]
    DS_TOOLBAR_RIGHT = [('🚀 开始发送', 170)]
    # 浏览器矩形那块面板的底色：给一个中性值，避免加载中露出纯白（真窗口起来后
    # 会被完全盖住）。页面侧 /bg 用的是同一个色，两边一致才不会有"白角"。
    DS_PANEL_FILL = '#f7f8fc'
    # 注意：这里的等待时间**不是**生效值。真正生效的是
    # ds_bridge.sender.settle_ms_for()（文档 0.25 秒 / 图片 0.15 秒），由
    # BatchSender 按扩展名算好、通过 host.attach(settle_ms=...) 传给页面。
    # 这里只留 dsview/main.js 里 SEND_SETTLE_PER_FILE_MS 的对应说明，别再当参数用。

    def _ds_layout(self):
        """官网页签的布局表（布局计算的**唯一入口**，见 ds_page_layout）。

        提示行/警示行要按实际像素宽度折行，所以这里先把文字量一遍再交给纯函数；
        量不出来（没建号字体/画布已销毁）就按 1 行算，最坏情况是行数偏少。
        """
        lay = getattr(self, '_ds_lay_cache', None)
        key = (self.W, self.H, getattr(self, '_ds_hint_text', ''),
               getattr(self, '_ds_warn_text', ''))
        # ⚠️ `self.W/H` 是"_on_resize 认为的窗口大小"，画布尺寸才是真的。
        #    两者不一致时（测试里改过 geometry、或重排还没跑）必须重算，
        #    否则会拿着上一次尺寸算出来的表去摆这一屏的控件。
        cw, ch = getattr(self.sf, 'size', (0, 0))
        if lay is not None and getattr(self, '_ds_lay_key', None) == key \
                and cw == self.W and ch == self.H:
            return lay
        lw = self.DS_LEFT_W
        hint_lines = len(self._ds_wrap(getattr(self, '_ds_hint_text', ''), lw - 4, 9))
        warn_lines = len(self._ds_wrap(getattr(self, '_ds_warn_text', ''),
                                       max(120, self.W - DS_BROWSER_X - 38), 9))
        lay = ds_page_layout(self.W, self.H,
                             hint_lines=max(1, hint_lines),
                             warn_lines=max(1, warn_lines),
                             left_buttons=self.DS_TOOLBAR_LEFT,
                             right_buttons=self.DS_TOOLBAR_RIGHT)
        self._ds_lay_cache = lay
        self._ds_lay_key = key
        return lay

    def _ds_measure(self, s, size=9):
        """文本像素宽度。量不出来就按每字 9px 估（宁可高估，别漏掉折行）。"""
        try:
            import tkinter.font as tkfont
            return tkfont.Font(family=self.sf.font, size=size).measure(str(s))
        except Exception:                    # noqa: BLE001
            return len(str(s)) * 9

    def _ds_wrap(self, s, max_px, size=9):
        """按像素宽度折行（中文逐字、ASCII 尽量按空格断）。

        为什么需要：官网页签的提示/警示行挤在**左栏或警示行**这种窄条里，
        实测一句话能到 550px —— 不折行就只能和相邻那行叠在一起。
        """
        s = str(s or '')
        if not s:
            return ['']
        out = []
        for para in s.split('\n'):
            line = ''
            for ch in para:
                if not line:
                    line = ch
                    continue
                if self._ds_measure(line + ch, size) <= max_px:
                    line += ch
                else:
                    cut = line.rfind(' ') if ' ' in line[-12:] else -1
                    if cut > 0:
                        out.append(line[:cut])
                        line = line[cut + 1:] + ch
                    else:
                        out.append(line)
                        line = ch
            out.append(line)
        return out or ['']

    def _ds_rect(self):
        """内嵌浏览器在主窗口客户区里占的矩形，返回 (x, y, w, h)。

        ⚠️ 坐标来自布局表：浏览器矩形必须**正好压在警示行之下**，
        否则它会盖住那句警示。
        ⚠️ 这里返回的是 (左上角, 宽, 高)，而布局表存的是 (x1,y1,x2,y2) 矩形 ——
        别把表里的元组直接 return 出去（调用方会把它当 w/h 用，算出天大的窗口）。

        **两种后端**：
          · embed / webview2：浏览器是**子窗口/子控件**，坐标就是画布坐标（= 客户区坐标）；
          · electron：浏览器是独立顶层窗口，要的是屏幕坐标（见 _ds_screen_rect）。
        两者都从这里取"那块矩形"，保证和布局表、和左右栏避让约束一致。
        """
        x1, y1, x2, y2 = self._ds_layout()['browser_rect']
        return x1, y1, x2 - x1, y2 - y1

    @property
    def ds_backend(self):
        """官网页签用哪个后端：

        | 值 | 形态 | 拖动观感 | 键盘 |
        |---|---|---|---|
        | `embed`（**默认**） | Electron **owner 窗口**（顶层 + owner 关系） | 要自己"跟随"，慢一拍 | ✅ 用户实测可打字 |
        | **`child`** | **真子窗口**（`SetParent` + `WS_CHILD`）= **v3.0.0 形态** | ✅ **零延迟**（Windows 自己搬） | ⚠️ 待用户手按确认 |
        | `webview2` | WebView2 进程内子控件 | 零延迟 | ⚠️ 三个现象未解决，仅历史保留 |
        | `electron` | 完全独立的顶层窗口 | 不嵌进来 | ✅ |

        ⚠️ 为什么默认还是 `embed` 而不是 `child`：`child` 的观感明显更好（用户实测
        v3.0.0"拖动特别好，就像完全就是里面自带的东西"），但跨进程子窗口**可能收不到
        真实键盘**，而这一点**我无法用脚本判定**（合成按键送不进 Chromium）。
        所以把 `child` 做成一行设置，让用户手按验一次：
            `.ui_settings` 里写 `ds_backend=child`（或 `embed` 切回来）。
        """
        v = str(getattr(self, 'settings', {}).get('ds_backend', 'embed') or '').lower()
        return v if v in ('embed', 'child', 'webview2', 'electron') else 'embed'

    def _ds_log(self, msg):
        """宿主进程/发送过程的日志：写到窗口底部一行，同时留在 self._ds_lines。"""
        line = str(msg)[:120]
        self._ds_lines = (getattr(self, '_ds_lines', []) + [line])[-60:]
        try:
            self.sf.canvas.itemconfigure('dslog', text=line)
        except Exception:
            pass

    def _ds_apply_theme(self):
        """把软件当前的深浅色同步给内嵌网页（官网自己也有两套配色）。

        WebView2 后端走 `Profile.PreferredColorScheme`（官方开关，页面里的
        `prefers-color-scheme` 会跟着变）；顺带把窗口底色也同步过去，
        免得页面重绘瞬间闪白角。
        """
        if not self.ds or not self.ds.running:
            return
        mode = 'dark' if self.theme['name'] == 'dark' else 'light'
        try:
            r = self.ds.set_theme(mode)
            # ⚠️ 回读结果写进日志：主题没生效时这是唯一线索（网页自己也可能
            #    有自己的主题开关，那种情况只能靠"跟随系统"或页面内切换）。
            if isinstance(r, dict) and r.get('want') is not None:
                self._ds_log(f'主题同步 {mode}：{r}')
        except Exception as e:      # noqa: BLE001
            self._ds_log(f'主题同步失败：{e}')
        try:
            self.ds.set_background(self.DS_PANEL_FILL)
        except Exception:  # noqa: BLE001
            pass

    def build_ds(self):
        """DeepSeek 官网页：左栏选要发的东西，右边直接就是官网。

        布局**全部来自 ds_page_layout()**（见文件顶部那张表的说明）：
        这里只负责"把控件放到表说的位置上"，不再自己算 Y 坐标 ——
        以前每个 y 都是单独调的，改一个就压另一个（用户截图里圈出的那几处重叠）。
        """
        self.page = 'ds'
        # 上一次的内嵌浏览器如果已经死了（崩溃/被外部结束），先把残留状态清掉，
        # 否则下面会去 SetParent 一个已经失效的 HWND —— 那正是把整个软件一起
        # 带崩的经典路径（实测 2026-09-15：electron 崩在 0xc000041d，宿主随即消失）。
        self._ds_reset_dead_host()
        self._clear_page()
        th = self.theme

        # ⚠️ 提示行/警示行的文字必须**先定下来**：布局表要按它们折行后的行数
        #    来推进下面的坐标（这就是"不再手怼坐标"的关键 —— 文字长了就多占一行，
        #    清单自动往下让，而不是压在一起）。
        #    ⚠️ 提示文字要用**真实数据**算（_ds_hint_lines），否则会出现
        #    "按 1 行摆控件、最后画出 2 行文字"的错位 —— 那种错位正是清单压提示行
        #    的老毛病换了个马甲。
        self._ds_warn_text = ('⚠ 一批附件别超 30 个 —— 图片尤其容易触发官网「服务器繁忙」；'
                              '建议只发文档，或点左下角调小每批数量')
        self._ds_hint_text = self._ds_hint_lines()
        self._ds_lay_cache = None
        self.draw_header(f'内嵌 DeepSeek 官网 · 按批自动投喂（每批 ≤{self._ds_limit} 个文件）')
        lay = self._ds_layout()

        # ── 工具栏（按钮行）──
        # 按钮的 (文字, 宽度) 定义在 DS_TOOLBAR_LEFT/RIGHT，坐标由布局表算。
        # 左栏按钮会自动折行（窗口窄时不会横着伸进右侧浏览器区）。
        # ⚠️ 下面这两个字典的 key 必须和 DS_TOOLBAR_* 里的文字**逐字一致**，
        #    不一致会 KeyError（有 test_ds_page_builds_without_host 兜底）。
        btn_rect = {label: r for label, r in lay['buttons']}
        btn_cmd = {'选择导出文件夹': self._ds_pick_root,
                   '全选': lambda: self._ds_sel_all(True),
                   '全不选': lambda: self._ds_sel_all(False),
                   '演练分批': lambda: self._ds_send(dry_run=True),
                   '诊断': self._ds_diag}
        for label, _w in self.DS_TOOLBAR_LEFT:
            x1, y1, x2, y2 = btn_rect[label]
            self._widgets.append(W.Button(self.sf, x1, y1, x2 - x1, y2 - y1,
                                          label, kind='primary' if label == '选择导出文件夹'
                                          else 'ghost', font_size=10, radius=9,
                                          command=btn_cmd[label], hover_dur=0.12))
        for label, _w in self.DS_TOOLBAR_RIGHT:
            x1, y1, x2, y2 = btn_rect[label]
            btn = W.Button(self.sf, x1, y1, x2 - x1, y2 - y1, label,
                           kind='primary', font_size=11, radius=9,
                           command=btn_cmd.get(label, self._ds_send))
            self._widgets.append(btn)
            if label == '🚀 开始发送':
                self.btn_ds_send = btn
        # ⚠️ 这里曾经有一条「发给 AI」输入框 + 发送键（v3.0.1 加入）。
        #    已删除，原因是被它误导：它画在 canvas x 560~934，正好压在右侧内嵌
        #    浏览器的上方（浏览器从 x=382 起），用户看到它悬在官网页面头上，
        #    以为"要用这条才能发话"，反而挡住了自己去点官网自己的输入框。
        #    它当初存在的理由是"跨进程子窗口收不到键盘"—— 那是
        #    ds_bridge.host._attach_input() 的 bug（ctypes.wintypes 未 import），
        #    已修。修好之后这条路就是多余的。
        #    后端通道（host.type_text / Electron 的 /type）**保留**：它是排查
        #    "为什么发不出去"时的可靠手段，只是不再出现在界面上。

        # ── 提示行（左栏，勾选统计）+ 警示行（右栏，浏览器矩形**之上**）──
        # ⚠️ 这两行都**不在这里建图元**：文字由 _ds_update_hint() 按折行结果创建
        #    （可能不止一行）。Canvas 的 `itemcget(tag)` 在多图元同 tag 时返回的
        #    是显示列表里**第一个**（不是最新建的），留个空占位会让它读回空字符串、
        #    itemconfigure 也跟着写错对象。
        #
        # ⚠️ 官网对"单条消息里的附件数量"很敏感：实测一批 40 个附件就会被拒收
        # （每个附件显示"服务器繁忙"、整条消息报"请删除异常文件再发送"）。
        # 这句警示只能在浏览器矩形**之上**那一带 —— 落进矩形就被独立窗口盖住了。
        # 导出路径放左栏底部（浏览器盖不到那一片）
        self.sf.text(38, lay['path_y'], '', 8, th['text_faint'], tags='dspath')
        # 底部一行日志
        self.sf.text(38, lay['log_y'], '', 8, th['text_faint'], tags='dslog')

        # ── 左栏：发送清单（复用会话页那套 CheckList）──
        # 底部要留出来的行（**都必须待在 x<382 的左栏里**：右侧矩形会被独立浏览器
        # 窗口盖住，放在右边点不到）：
        #   导出目录 · 选项行（只发文档 / 每批 N 个）· 日志
        # 注：原来这里还有一行「写进网页输入框」，已按用户要求删除。
        self._ds_opt_y = lay['opt_y']
        self.list = W.CheckList(self.sf, 38, lay['list_y'], self.DS_LEFT_W,
                                lay['list_h'],
                                on_toggle=self._ds_on_toggle, on_open=None,
                                header='发送清单')
        self.list.set_items(self._ds_items())
        self._widgets.append(self.list)

        # ── 选项行（左下角，浏览器盖不到）──
        opt_y = lay['opt_y']
        self._docs_cb = W.Checkbox(
            self.sf, 38, opt_y - 10, 176, '只发文档（建议）',
            value=bool(self._ds_docs_only), on_change=self._ds_on_docs_only,
            font_size=9)
        self._widgets.append(self._docs_cb)
        # 每批数量用**点击循环**的按钮，不用下拉框：下拉展开的浮层会被内嵌浏览器挡住
        self._batch_btn = W.Button(
            self.sf, 224, opt_y - 13, 144, 26, self._batch_label(), kind='ghost',
            font_size=9, radius=8, command=self._ds_cycle_batch, hover_dur=0.12)
        self._widgets.append(self._batch_btn)

        # （这里曾有一条「写进网页输入框」输入框 + 「写入」按钮。用户要求删掉、保持干净：
        #   "还有就是你看这个写入框，删了吧，干净点"。
        #   删的理由：键盘现在能正常打字了（用户已确认），这条兜底成了纯干扰。
        #   **后端保留**：host.type_text() 与 Electron 的 /type 路由继续可用 ——
        #   排查"为什么发不出去"、以及远程桌面/输入法异常的极端场景都还靠它。
        #   要恢复 UI 只需把上面那段加回来，见接管文档 5.14。）

        # ── 右侧：浏览器区域（先画一个占位框，真窗口盖在上面）──
        # ⚠️ 占位框就是"浏览器那块矩形"，四周不留缝：真窗口盖上去之后，
        #    加载中/关掉时露出来的边角必须和网页底色一致，不然会看到白角
        #    （用户说的"两个软件"里有一部分就是这种边界感）。
        x, y, w, h = self._ds_rect()
        self.sf.draw_panel(x, y, x + w, y + h, 12, 0.92, fill=self.DS_PANEL_FILL)
        self.sf.text(x + w / 2, y + h / 2, '正在准备内嵌浏览器…', 10,
                     th['text_faint'], anchor='center', tags='dsplaceholder')

        self._ds_update_hint()
        # 上次用过的导出目录还在的话自动列出来，省得每次重新选
        if not self.ds_units and os.path.isdir(self.ds_out_root or ''):
            self._ds_scan()
        self.root.after(120, self._ds_boot)
        self.root.after(1500, self._ds_watchdog)

    # ── 内嵌浏览器死掉时的自愈 ──

    def _ds_reset_dead_host(self):
        """宿主已经死了就清掉残留状态；返回是否确实清理了。"""
        ds = self.ds
        if ds is None or ds.running:
            return False
        try:
            ds.shutdown(wait=False)          # 收掉可能的孤儿子进程
        except Exception:                    # noqa: BLE001
            pass
        self.ds = None
        self._ds_visible = False
        # ⚠️ 必须一起清掉：进程没了，但 `_ds_embed` 还是 True 的话，下次回来会去
        #    `move()` 一个已经失效的 HWND（而不是重新 embed）—— 那正是"退回页签
        #    再进来浏览器不见了"的一条成因。
        self._ds_embed = False
        # 关键：hwnd 已经失效，留着它下次 SetParent 会把整个软件一起带崩
        self._ds_revive_placeholder()
        return True

    def _ds_revive_placeholder(self):
        """把右侧占位框重新放出来（浏览器窗口没了时用）。"""
        try:
            x, y, w, h = self._ds_rect()
            self.sf.draw_panel(x, y, x + w, y + h, 12, 0.92, fill=self.DS_PANEL_FILL)
            self.sf.text(x + w / 2, y + h / 2,
                         '内嵌浏览器已退出 —— 点标题里的「DeepSeek 官网」可重新拉起',
                         10, self.theme['text_faint'], anchor='center',
                         tags='dsplaceholder')
        except Exception:                    # noqa: BLE001
            pass

    def _ds_watchdog(self):
        """盯着内嵌浏览器：它崩了要立刻告诉用户，而不是让界面莫名其妙没反应。

        为什么需要：实测（2026-09-15）electron 崩在 `0xc000041d`，宿主软件随后一起
        消失，用户看到的是"未响应"。以前既没有提示、也不会自己恢复 —— 再点页签
        还会拿着已经失效的 HWND 去 SetParent，属于踩同一个坑。
        """
        if self.page != 'ds':
            return                               # 离开页签就停，回来时 build_ds 会重启
        try:
            if self._ds_reset_dead_host():
                self._ds_log('内嵌浏览器已退出（崩溃或被外部结束）—— 已清理残留状态')
                self.toast('内嵌浏览器已退出。点标题栏的「DeepSeek 官网」可以重新拉起',
                           'warn', 5000)
                return
        except Exception as e:                   # noqa: BLE001
            self._ds_log(f'看护检查出错：{e}')
        try:
            self.root.after(2000, self._ds_watchdog)
        except Exception:                        # noqa: BLE001
            pass

    def _ds_boot(self):
        """页签画完再启动宿主：避免拉起 electron 时界面还是一片空白。"""
        if self.page != 'ds':
            return
        err = self._ds_ensure_host()
        if err:
            x, y, w, h = self._ds_rect()
            try:
                self.sf.canvas.itemconfigure('dsplaceholder',
                                             text=f'内嵌浏览器启动失败：{err}')
            except Exception:
                pass
            self.toast(f'内嵌浏览器启动失败：{err}', 'err', 5000)
            return
        try:
            self.sf.canvas.delete('dsplaceholder')
        except Exception:
            pass
        self._ds_place()

    def _ds_ensure_host(self):
        """确保浏览器宿主在跑（懒启动）。返回 '' 表示成功，否则返回错误说明。

        三个后端（见 `ds_backend`）：
          · **embed**（默认）：Electron 真嵌入 —— 启动后由 `_ds_place()` 做第一次
            `embed()`（`SetParent` 成主窗口子窗口），之后只 `move()`。
          · **webview2**：进程内子控件，启动回调里 reparent 到画布 HWND。
          · **electron**：独立顶层窗口 + 跟着主窗口摆位（老方案，保留作退路）。
        """
        if self.ds is not None and self.ds.running:
            return ''
        if getattr(self, '_ds_starting', False):
            return ''
        self._ds_starting = True
        try:
            if self.ds_backend == 'webview2':
                return self._ds_ensure_webview2()
            return self._ds_ensure_electron()
        finally:
            self._ds_starting = False

    def _ds_ensure_webview2(self):
        """启动 WebView2 真嵌入后端（**异步**）。

        ⚠️ 为什么要异步：`WebView2Host.start()` 要等环境 + CoreWebView2 建好
        （首次给一个新的 user-data 目录时可能几十秒）。在 Tk 主线程上等，界面就是
        "点了没反应"，用户会以为卡死。所以这里起一条后台线等它，好了再用
        `after` 回主线程嵌进去 —— 页面先画出来、占位框上写"正在准备"。
        """
        try:
            from ds_bridge import webview2_host as wv2
        except ImportError as e:
            return f'缺少 ds_bridge.webview2_host 模块（{e}）'
        if not wv2.RUNTIME_DIR:
            return ('没装 Microsoft Edge WebView2 Runtime。'
                    '去微软官网装一个（Win10/11 一般自带），装好重开本软件即可')
        ds_url = os.environ.get('WXEXPORT_DS_URL') or 'https://chat.deepseek.com/'
        canvas_hwnd = int(self.sf.canvas.winfo_id())
        holder = {}

        def worker():
            t0 = time.time()
            # ⚠️ 打包版没有控制台、界面 toast 也可能被忽略，所以这里**同时写文件日志**：
            #    `.wv2_start.log` 记每一步，排查"包内 WebView2 起没起来"时直接看它。
            try:
                with open(os.path.join(ROOT, '.wv2_start.log'), 'w',
                          encoding='utf-8') as f:
                    f.write(f'{time.strftime("%H:%M:%S")} 后端={self.ds_backend} '
                            f'url={ds_url}\nruntime={getattr(wv2, "RUNTIME_DIR", "")}\n')
            except OSError:
                pass

            def mark(msg):
                self._ds_log(msg)
                try:
                    with open(os.path.join(ROOT, '.wv2_start.log'), 'a',
                              encoding='utf-8') as f:
                        f.write(f'{time.strftime("%H:%M:%S")} {msg}\n')
                except OSError:
                    pass

            try:
                host = wv2.WebView2Host(canvas_hwnd, os.path.join(ROOT, 'wv2_profile'),
                                        url=ds_url, log=mark)
                mark('WebView2Host 已构造，开始 start()')
                if not host.start():
                    holder['error'] = host.start_error or '未知原因'
                    mark(f'start 失败：{holder["error"]}')
                    return
                mark(f'start 成功，用时 {time.time() - t0:.2f}s')
                holder['host'] = host
            except Exception as e:                  # noqa: BLE001
                holder['error'] = f'{type(e).__name__}: {e}'
                mark(f'异常：{holder["error"]}')

        th = threading.Thread(target=worker, daemon=True, name='wv2-start')
        th.start()
        self._ds_starting_thread = th
        self._ds_start_holder = holder
        self._ds_start_t0 = time.time()
        self.root.after(200, self._ds_finish_start)
        return ''

    def _ds_finish_start(self):
        """轮询后台启动结果；好了就嵌进来。"""
        holder = getattr(self, '_ds_start_holder', None)
        if holder is None:
            return
        if self.page != 'ds':
            self._ds_start_holder = None
            return
        if holder.get('error'):
            err = holder.pop('error')
            self._ds_start_holder = None
            try:
                self.sf.canvas.itemconfigure('dsplaceholder',
                                             text=f'内嵌浏览器启动失败：{err}')
            except Exception:
                pass
            self.toast(f'内嵌浏览器启动失败：{err}', 'err', 6000)
            return
        host = holder.get('host')
        if host is None:
            waited = int(time.time() - getattr(self, '_ds_start_t0', time.time()))
            if waited % 3 == 0:
                try:
                    self.sf.canvas.itemconfigure(
                        'dsplaceholder', text=f'正在准备内嵌浏览器…（已等 {waited}s）')
                except Exception:
                    pass
            self.root.after(400, self._ds_finish_start)
            return
        holder.pop('host')
        self._ds_start_holder = None
        self.ds = host
        x, y, w, h = self._ds_rect()
        self.sf.canvas.delete('dsplaceholder')
        r = {}
        try:
            r = host.reparent(int(self.sf.canvas.winfo_id()), x, y, w, h)
        except Exception as e:                      # noqa: BLE001
            self._ds_log(f'reparent 出错：{e}')
            r = {'error': str(e)}
        try:
            with open(os.path.join(ROOT, '.wv2_start.log'), 'a',
                      encoding='utf-8') as f:
                f.write(f'{time.strftime("%H:%M:%S")} reparent -> {r}\n')
        except OSError:
            pass
        if r.get('parent') != int(self.sf.canvas.winfo_id()):
            self._ds_log(f'嵌入未生效（仍以独立控件运行）：{r}')
        self._ds_visible = True
        self._ds_apply_theme()
        self._ds_log('真嵌入完成（浏览器是窗口内的子控件）')

    def _ds_ensure_electron(self):
        """启动 Electron 后端（owner 形态的真嵌入）。

        ⚠️ 同样读 `WXEXPORT_DS_URL`：这样自检脚本能用假官网跑整条链路
        （以前只有 webview2 分支读它，于是 electron 分支在自检里连的是**真官网**，
        测出来的东西没法归因）。产品运行时这个变量不存在，走真官网。
        """
        try:
            from ds_bridge import host as ds_host
        except ImportError as e:
            return f'缺少 ds_bridge 模块（{e}）'
        try:
            ds_url = os.environ.get('WXEXPORT_DS_URL') or ds_host.DEFAULT_URL
            self.ds = ds_host.DeepSeekHost(ROOT, url=ds_url, log=self._ds_log,
                                           profile=DS_PROFILE_DIR)
            if not self.ds.start():
                err = self.ds.start_error or '未知原因'
                self.ds = None
                return err
        except Exception as e:                      # noqa: BLE001
            self.ds = None
            return f'{type(e).__name__}: {e}'
        return ''

    def _ds_top_hwnd(self):
        """主窗口的**顶层** HWND（owner 关系必须挂在顶层窗口上）。

        ⚠️ 不能直接用 `sf.canvas.winfo_id()`：那是 Tk 内部的**子窗口**，把它当
        owner 会得到一个不合法的关系（浏览器窗口就不受主窗口约束了）。
        Windows 的 owner 必须是顶层窗口。
        """
        import ctypes
        u32 = ctypes.windll.user32
        u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u32.GetAncestor.restype = ctypes.c_void_p
        child = int(self.sf.canvas.winfo_id())
        top = int(u32.GetAncestor(ctypes.c_void_p(child), 2) or 0)   # GA_ROOT = 2
        return top or int(self.root.winfo_id())

    def _ds_screen_rect(self):
        """把画布矩形换成**屏幕坐标**（只有 Electron 后端需要）。

        画布坐标 = 客户区坐标（见 _ds_rect 的说明），所以屏幕坐标 =
        客户区原点(rootx, rooty) + 画布坐标。
        WebView2 后端是子控件，坐标直接用画布坐标，不走这里。
        """
        x, y, w, h = self._ds_rect()
        try:
            sx = self.root.winfo_rootx() + x
            sy = self.root.winfo_rooty() + y
        except Exception:
            sx, sy = x, y
        # 真机对账用：拖动/缩放时把"画布坐标 + root 原点"一起记下来。
        # 曾经出现过"算出来是画布坐标"的错位，靠这条一眼看出 rootx/rooty 是否异常。
        if os.environ.get('DSVIEW_DIAG_POS'):
            self._wv2_trace(f'screen_rect: 画布(x={x},y={y}) + root({self.root.winfo_rootx()},'
                            f'{self.root.winfo_rooty()}) = ({sx},{sy}) 几何={self.root.winfo_geometry()}')
        return sx, sy, w, h

    def _ds_place(self, focus=True):
        """把浏览器摆到右侧区域，并按需把键盘焦点交给它。

        **embed 后端（默认，真嵌入 = v3.0.0 形态）**：它是主窗口的子窗口，坐标就是
        画布坐标（= 客户区坐标）。**第一次**摆位时才 `SetParent`（`embed()`），之后
        一律只 `move()` —— 每次重排都去动父子关系会反复重建窗口层级，那是"最大化
        卡死"的老成因。子窗口被父窗口裁剪、跟着最小化，不需要置顶、不需要"跟随"。

        **webview2 后端**：同样是子控件，但由 WebView2Host 自己管（reparent 在启动
        回调里做过），这里只 show + place。

        **electron 后端（退路）**：独立顶层窗口，要屏幕坐标 + 跟着主窗口摆位
        + 不用置顶（用户实测反馈：置顶后会压住软件自己的提示框）。

        focus=False 用于"只是把窗口放回原位、别抢焦点"（例如提示消失后恢复）。
        """
        if not self.ds or not self.ds.running:
            return
        _t0 = time.time()
        backend = self.ds_backend
        # ⚠️ `embed` 后端也是**屏幕坐标**：它是 owner 窗口（顶层窗口 + owner 关系），
        #    不是 SetParent 子窗口 —— 这一点和 webview2 后端正好相反，别抄错。
        if backend == 'webview2':
            x, y, w, h = self._ds_rect()
        elif backend == 'child':
            # ★ v3.0.0 的坐标口径：子窗口用**父窗口客户区坐标**（= 画布坐标），
            #   直接给布局表的矩形，**不做 DPI 换算**（加了反而偏）。
            x, y, w, h = self._ds_rect()
        else:
            x, y, w, h = self._ds_screen_rect()

        if backend == 'embed':
            # ⚠️ 离开页签时被 hide() 过，回来**必须先显示再摆位**，否则就是
            #    "退回导出页再进来，浏览器不见了"（用户实测反馈过）。
            try:
                self.ds.show()
            except Exception:                       # noqa: BLE001
                pass
            # owner 必须挂在**顶层**窗口上：Tk canvas.winfo_id() 给的是子窗口，
            # 直接用它会得到一个不合法的 owner（表现为窗口不受主窗口约束）。
            try:
                top = int(self._ds_top_hwnd())
            except Exception:                       # noqa: BLE001
                top = 0
            ok = False
            try:
                if not getattr(self, '_ds_embed', False):
                    ok = bool(self.ds.embed(top, x, y, w, h)) if top else False
                    if ok:
                        self._ds_embed = True
                else:
                    ok = bool(self.ds.move(x, y, w, h))
            except Exception as e:                  # noqa: BLE001
                self._ds_log(f'嵌入/摆位失败：{e}')
            if not ok:
                self._ds_log('浏览器窗口摆位失败')
                return
        elif backend == 'child':
            # ★ v3.0.0 形态：真子窗口（SetParent + WS_CHILD）。
            #   这里是**画布客户区坐标**、且**不做 DPI 换算** —— 和 v3.0.0 一模一样。
            #   子窗口由 Windows 自己跟着父窗口移动，所以不需要任何跟随逻辑。
            try:
                self.ds.show()
            except Exception:                       # noqa: BLE001
                pass
            try:
                parent = int(self._ds_top_hwnd())
            except Exception:                       # noqa: BLE001
                parent = int(self.root.winfo_id())
            ok = False
            try:
                if not getattr(self, '_ds_embed', False):
                    ok = bool(self.ds.embed_child(parent, x, y, w, h)) if parent else False
                    if ok:
                        self._ds_embed = True
                        # ★ 起来看门狗：`SetParent` 会被 Electron 撤销，掉了要补回去
                        try:
                            self.root.after(600, self._ds_child_keepalive)
                        except Exception:           # noqa: BLE001
                            pass
                else:
                    # ⚠️ 子窗口摆位要用**画布坐标**（不是屏幕坐标）—— 这里曾经误用
                    #    `_setpos(x, y, w, h)` 而 x/y 是屏幕坐标那一支算出来的，
                    #    于是"切走再回官网页签"就摆错、动也动不了（用户实测报过）。
                    cx, cy, cw, ch = self._ds_rect()
                    ok = bool(self.ds._setpos(cx, cy, cw, ch))
            except Exception as e:                  # noqa: BLE001
                self._ds_log(f'子窗口嵌入/摆位失败：{e}')
            if not ok:
                self._ds_log('浏览器窗口摆位失败')
                return
        else:
            # ⚠️ 子控件离开页面时被隐藏过，回来必须**先显示**再摆位。
            if backend == 'webview2':
                try:
                    self.ds.show()
                except Exception:                   # noqa: BLE001
                    pass
            if not self.ds.place(x, y, w, h):
                self._ds_log('浏览器窗口摆位失败')
                return

        self._ds_visible = True
        # ⚠️ 只有 electron 独立窗口后端需要管置顶；用户实测反馈：置顶后它压在软件
        #    自己弹出的「快速发送」提示框上面，提示看不见了。
        #    embed（owner）后端**不要碰它** —— owner 本来就在宿主之上，再设一次
        #    alwaysOnTop 只会让它压过我们自己的提示框，反而制造问题。
        if backend == 'electron':
            try:
                self.ds.set_topmost(False)
            except Exception:
                pass
        if focus:
            try:
                # embed（owner 窗口）与 electron（独立窗口）都让 Electron 自己激活：
                # 它调 win.focus() + webContents.focus()，浏览器进程本来就是"用户刚
                # 点过的那个软件"。**刻意不用 AttachThreadInput**（会让 IME 死锁）。
                if backend == 'webview2':
                    self.ds.summon()
                else:
                    self.ds.focus()
            except Exception as e:      # noqa: BLE001
                self._ds_log(f'激活浏览器失败：{e}')
        self._ds_apply_theme()
        self._ds_last_wh = (w, h)
        self._wv2_trace(f'place({backend}) {time.time() - _t0:.2f}s '
                        f'rect=({x},{y},{w},{h})')

    def _ds_restore_silent(self):
        """把浏览器放回原位并显示出来，**不抢焦点**（提示结束/弹窗关闭后调用）。

        ⚠️⚠️ **不能在"提示/弹窗让位期间"调用**（2026-09-16 实测定型）：
        这个方法会 `show()`，而让位期间窗口是被**有意藏起来**的 —— 一 show 就又把它
        盖回提示上面了，用户看到的就是"这个窗口会遮挡提示框/弹出的词"。
        所以入口先看让位状态，正在让位就直接返回（让位的收尾方会再调一次）。
        """
        if self.page != 'ds' or not getattr(self, '_ds_visible', False):
            return
        if not self.ds or not self.ds.running:
            return
        # ⚠️ 让位期间不许显示（见上面说明）
        if getattr(self, '_ds_hidden_for_toast', False) or getattr(self, '_dlg_depth', 0):
            return
        if not self.ds.running:
            return
        # ⚠️⚠️ 坐标口径别抄错（这里曾经写成 `in ('embed', 'webview2')`，把 embed 也
        #    当成子控件、用画布坐标去摆一个**顶层窗口** —— 结果就是"让位期间拖动主窗口，
        #    结束后浏览器跳到画布坐标那个点"，也就是用户报的"单纯拖动会错位"）。
        #    只有 webview2 是真正的进程内子控件、吃画布坐标。
        if self.ds_backend == 'webview2':
            x, y, w, h = self._ds_rect()
        else:
            x, y, w, h = self._ds_screen_rect()
        try:
            if self.ds_backend == 'embed':
                # ⚠️⚠️ 走 restore_silent(show=True)：它内部是"先摘 owner → show →
                #    挂回 → **完整摆位**"，这几步缺一不可：
                #    · 少了 show()：窗口在让位时被 hide() 过，就再也不回来了
                #      （实测踩过："提示消失后浏览器再也不出现"）；
                #    · 少了摘 owner：owned + 隐藏的 show 会让 Electron 主进程卡死；
                #    · 少了**完整摆位**：只做 Win32 `SetWindowPos` 时 Electron **不会重绘**，
                #      于是窗口可见但是一片空白 —— 用户看到的就是
                #      "点开始发送后界面像消失了一样"（那片白就是他等的位置）。
                #      `restore_silent(show=True)` 里走的是 `host.show()` + `place()`，
                #      `place()` 会发 `/bounds`（`setBounds`），顺带把重绘带出来。
                _r = self.ds.restore_silent(x, y, w, h, show=True)
                self._wv2_trace(f'restore_silent(完整) → ({x},{y},{w},{h}) ok={_r}')
            else:
                self.ds.place(x, y, w, h)
        except Exception as e:                      # noqa: BLE001
            self._wv2_trace(f'restore_silent 失败：{e}')

    def _ds_toast(self, msg, kind='info', ms=2600):
        """在官网页签上弹提示。

        ⚠️ 哪些后端要让位：`embed`（owner 窗口）和 `electron`（独立顶层窗口）都会
        **盖住提示** —— 它们是独立顶层窗口，z 序上压在宿主内容之上（用户实测反馈
        "这个窗口会遮挡提示框"）。所以这两种要"弹提示 → 临时隐藏浏览器 → 提示消失
        后放回"。
        （`webview2` 是画布的子控件，天生画在画布下面，提示直接可见，不用让位。）
        """
        self.toast(msg, kind, ms)
        if self.ds_backend == 'webview2':
            return
        if not getattr(self, '_ds_visible', False) or self.ds is None:
            return
        self._dlg_depth = getattr(self, '_dlg_depth', 0)
        if self._dlg_depth:
            return          # 已经有弹窗占用着"让位"状态了，别重复隐藏/复原
        try:
            self.ds.hide()
        except Exception:
            return
        # 隐藏期间不改变 _ds_visible（它是"应该可见"的语义），只标记"为了提示暂时藏起来"
        self._ds_hidden_for_toast = True
        try:
            self.root.after(int(ms) + 150, self._ds_after_toast)
        except Exception:
            pass

    # ── 弹窗让位：Tk 的弹窗也是 root 的"兄弟"，会被浏览器盖住 ──

    def _dlg_hide_browser(self):
        """开弹窗前把浏览器藏起来（`embed`/`electron` 都需要）。

        为什么需要：浏览器是 root 的 **owned 窗口**，而 Tk 的 Toplevel 也是 root 的子
        窗口 —— 在 Windows 眼里它们是**兄弟**，z 序只看谁最后被激活。浏览器在提示/
        拖动时会被反复 show，很容易又把刚弹出的窗口盖上（用户实测反馈："这个窗口会
        遮挡包括提示框和这个弹出的词"）。
        """
        self._dlg_depth = getattr(self, '_dlg_depth', 0) + 1
        if self._dlg_depth > 1:
            return
        if self.ds_backend == 'webview2':
            return
        if not getattr(self, '_ds_visible', False) or self.ds is None:
            return
        try:
            self.ds.hide()
        except Exception:                    # noqa: BLE001
            pass

    def _dlg_done(self, win):
        """弹窗销毁时把浏览器放回来（不抢焦点）。"""
        def _restore(_e=None):
            if win is not None:
                try:
                    win.unbind('<Destroy>')
                except Exception:            # noqa: BLE001
                    pass
            self._dlg_depth = max(0, getattr(self, '_dlg_depth', 1) - 1)
            if self._dlg_depth == 0:
                try:
                    self.root.after(60, self._ds_restore_silent)
                except Exception:            # noqa: BLE001
                    pass
        return _restore

    def _dlg(self, title):
        """建一个和浏览器"抢 z 序"的弹窗（Toplevel），自动处理让位/复原。"""
        self._dlg_hide_browser()
        win = tk.Toplevel(self.root)
        win.title(title)
        try:
            win.transient(self.root)
        except Exception:                    # noqa: BLE001
            pass
        win.bind('<Destroy>', self._dlg_done(win), add='+')
        return win

    def _ds_after_toast(self):
        if not getattr(self, '_ds_hidden_for_toast', False):
            return
        self._ds_hidden_for_toast = False
        self._ds_restore_silent()

    def _ds_leave(self):
        """离开本页就把浏览器藏起来。

        ⚠️ 对 `embed`（子窗口）和 `webview2`（子控件）来说，藏起来是**必须**的：
        它们是宿主窗口的子孙，整页重排/切页时不会自己消失，会留在画布上挡住导出页。
        再回来时 `_ds_place()` 会**先 show() 再摆位**（否则就"浏览器不见了"）。
        """
        if self._ds_visible and self.ds is not None:
            try:
                self.ds.set_topmost(False)
            except Exception:
                pass
            try:
                self.ds.hide()
            except Exception:
                pass
            self._ds_visible = False
            self._wv2_trace('leave（已隐藏浏览器）')
        try:
            # 回主窗口：把焦点还给软件自己，否则键盘还留在浏览器那边
            self.root.focus_force()
        except Exception:
            pass

    # ── 清单 ──

    def _ds_write_in(self, text=''):
        """把文字写进官网页面上当前激活的输入框（**没有 UI 了，仅供代码/诊断调用**）。

        用户要求删掉那个输入框（"删了吧，干净点"），但这条通道本身有保留价值：
        · 排查"为什么发不出去"时可以直接调它验证注入是否通；
        · 远程桌面 / 输入法异常导致真实按键送不到时，这是唯一能填字的路。
        所以 UI 删了、方法留着。用法：`app._ds_write_in(text='要填的文字')`。
        """
        text = str(text or '')
        if not text.strip():
            self.toast('要写入的文字为空', 'warn')
            return
        if self._ds_ensure_host():
            self.toast('浏览器没起来，写不了', 'err')
            return
        try:
            info = self.ds.focused_info() or {}
            res = self.ds.type_text(text, submit=False)
        except Exception as e:      # noqa: BLE001
            self.toast(f'写入失败：{e}', 'err')
            return
        if not res or not res.get('ok'):
            self.toast('写入失败：' + str((res or {}).get('error') or '未知原因'), 'err', 4000)
            return
        where = ''
        if info.get('ok'):
            where = '（%s）' % (info.get('placeholder') or info.get('tag') or '输入框')
        self._ds_log('已写入网页输入框%s：%s' % (where, text[:20]))
        self.toast('已写进网页输入框%s' % where, 'ok', 2600)

    def _ds_on_docs_only(self, value):
        """只发文档：默认开。实测官网单条消息容易被大量图片挤爆（30 个以内才稳）。"""
        self._ds_docs_only = bool(value)
        self.settings['ds_docs_only'] = '1' if value else '0'
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self.list.set_items(self._ds_items())
        self._ds_update_hint()
        if value:
            self.toast('已切到「只发文档」：图片不会再被勾选发送（可以自己拖给网页版）',
                       'ok', 3600)

    def _batch_label(self):
        return f'每批 {self._ds_limit} 个 ⇄'

    def _ds_cycle_batch(self):
        """点一下换一个批量（10 → 20 → 30 → 10）。

        为什么不用下拉框：下拉展开的浮层是画在 Tk 画布上的，而内嵌浏览器是独立
        Win32 子窗口、永远盖在画布之上 —— 浮层会被它挡住，等于选不了。
        """
        try:
            i = self.DS_LIMIT_CHOICES.index(self._ds_limit)
        except ValueError:
            i = len(self.DS_LIMIT_CHOICES) - 1
        self._ds_limit = self.DS_LIMIT_CHOICES[(i + 1) % len(self.DS_LIMIT_CHOICES)]
        self.settings['ds_batch'] = str(self._ds_limit)
        try:
            save_settings(self.settings)
        except Exception:
            pass
        try:
            self._batch_btn.set_text(self._batch_label())
        except Exception:
            pass
        self._ds_update_hint()

    def _ds_on_batch(self, label):
        """兼容旧接口：万一还有地方按标签设置。"""
        try:
            self._ds_limit = int(str(label))
        except ValueError:
            self._ds_limit = self.DS_LIMIT
        self.settings['ds_batch'] = str(self._ds_limit)
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._ds_update_hint()

    def _ds_effective_units(self):
        """真正要发的勾选项（应用「只发文档」过滤）。"""
        units = self._ds_selected_units()
        if self._ds_docs_only:
            units = [u for u in units if u.get('kind') != 'image']
        return units

    def _ds_pick_root(self):
        from tkinter import filedialog
        init = self.ds_out_root if os.path.isdir(self.ds_out_root or '') else \
            os.path.join(os.environ.get('USERPROFILE', 'C:'), 'Desktop')
        d = filedialog.askdirectory(initialdir=init, title='选择要发送的导出文件夹')
        if not d:
            return
        self.ds_out_root = d
        self.settings['ds_root'] = d
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._ds_scan()

    def _ds_scan(self):
        from ds_bridge import plan as ds_plan
        self.ds_units = ds_plan.scan_export_dir(self.ds_out_root or '')
        self.ds_selected = {u['id'] for u in self.ds_units}     # 默认全选
        if not self.ds_units:
            self.toast('这个文件夹里没有可发送的内容（找 .md/.txt/.html 和 *_图片 目录）',
                       'warn', 4000)
        self.list.set_items(self._ds_items())
        self._ds_update_hint()

    def _ds_items(self):
        items = []
        for u in self.ds_units:
            skipped = self._ds_docs_only and u['kind'] == 'image'
            items.append({'wxid': u['id'], 'title': u['label'][:34],
                          'preview': (u['detail'] + '（已跳过：只发文档）') if skipped
                          else u['detail'],
                          'time': '图片' if u['kind'] == 'image' else '文档',
                          'group': u['kind'] == 'image', 'tags': [],
                          'sel': (u['id'] in self.ds_selected) and not skipped})
        return items

    def _ds_on_toggle(self, item):
        if item.get('sel'):
            self.ds_selected.add(item['wxid'])
        else:
            self.ds_selected.discard(item['wxid'])
        self._ds_update_hint()

    def _ds_sel_all(self, value):
        self.list.set_all(value)
        self.ds_selected = {it['wxid'] for it in self.list.items if it.get('sel')}
        self._ds_update_hint()

    def _ds_selected_units(self):
        return [u for u in self.ds_units if u['id'] in self.ds_selected]

    def _ds_hint_lines(self):
        """提示行（勾选统计）的文字。抽出来是为了让**布局**和**绘制**用同一份。"""
        from ds_bridge import plan as ds_plan
        units = self._ds_effective_units()
        s = ds_plan.summarize(units, self._ds_limit)
        mode = '只发文档' if self._ds_docs_only else '文档+图片'
        return (f'{mode} · 每批 {self._ds_limit} 个 ｜ '
                f'已选 {len(units)} 项 · {s["files"]} 个文件'
                f'（文档 {s["docs"]}' + (f' + 图片 {s["images"]}' if not self._ds_docs_only else '')
                + f'）｜ 分 {s["batches"]} 批')

    def _ds_update_hint(self):
        """重画提示行（勾选统计）+ 警示行，并把「导出目录」写到底部那一行。

        这两行都可能折成多行，而**折行数会改变布局表**（清单要往下让），
        所以顺序是：先算文字 → 交给布局表 → 按表里的位置画。
        ⚠️ 如果文字变了导致折行数变多、布局整体下移，**已经摆好的控件也要跟着动** ——
        那种情况下直接整页重建一次（`build_ds` 里已经用真实文字算过初始布局，
        所以正常路径不会走到这里；这里只兜底"数据在页面建好之后才到"）。
        """
        txt = self._ds_hint_lines()
        depth = int(getattr(self, '_ds_hint_rebuilds', 0))
        if (depth < 2 and getattr(self, '_ds_list_y_drawn', None) is not None
                and getattr(self, 'list', None) is not None):
            self._ds_hint_text = txt
            if self._ds_layout()['list_y'] != self._ds_list_y_drawn:
                self._ds_hint_rebuilds = depth + 1
                self._ds_hint_text = txt
                self.build_ds()
                self._ds_hint_rebuilds = 0
                return
        self._ds_hint_text = txt
        lay = self._ds_layout()
        self._ds_list_y_drawn = lay['list_y']
        root = self.ds_out_root or '未选择（点左上「选择导出文件夹」）'
        cv = self.sf.canvas
        try:
            for tag in ('dshintline', 'dswarnline'):
                cv.delete(tag)
            hint_max = lay['hint_rect'][2] - lay['hint_rect'][0]
            hint_lines = self._ds_wrap(txt, hint_max, 9)
            self._ds_draw_block(cv, lay['hint_y'], 38, hint_lines,
                                self.theme['text_dim'],
                                ('dshint', 'dshintline'), 9, lay['line_step'])
            warn_max = lay['warn_rect'][2] - lay['warn_rect'][0]
            warn_lines = self._ds_wrap(getattr(self, '_ds_warn_text', ''), warn_max, 9)
            self._ds_draw_block(cv, lay['warn_y'], DS_BROWSER_X, warn_lines,
                                self.theme['warn'],
                                ('dswarn', 'dswarnline'), 9, lay['line_step'])
            cv.itemconfigure(
                'dspath', text='导出目录：' + self._fit_text(root, self.DS_LEFT_W - 10, 8))
        except Exception:                    # noqa: BLE001
            pass

    def _ds_draw_block(self, cv, y, x, lines, color, tags, size=9, step=14):
        """在一个基线（y）上下居中地画一组折行文字。

        第一行的图元额外带 `tags[0]`（dshint/dswarn），整块带 `tags[1]`：
        外面按 tag 取文字（itemcget）拿到的是第一行 —— 别用整块 tag 去取。
        `step` 必须和布局表算行高用的那个步长一致，否则折行多了会自己压自己。
        """
        top = y - (len(lines) - 1) * step / 2
        for i, ln in enumerate(lines):
            t = (tags[0], tags[1]) if i == 0 else (tags[1],)
            cv.create_text(x, top + i * step, text=ln, anchor='w', fill=color,
                           font=(self.sf.font, size), tags=t)

    # ── 诊断 ──

    def _ds_diag(self):
        """页面结构诊断：选择器失效时用它导出候选元素，方便回来改。"""
        if self._ds_ensure_host():
            self.toast('内嵌浏览器还没起来', 'err')
            return
        r = self.ds.diag()
        if r.get('ok') and r.get('file'):
            try:
                os.startfile(r['file'])
                self.toast('已打开诊断文件', 'ok')
            except Exception:
                self.toast(f'诊断已写出：{r["file"]}', 'ok', 4000)
        else:
            self.toast(f'诊断失败：{r.get("error")}', 'err')

    # ── 发送 ──

    def _ds_preflight(self):
        """发送前的体检：页面在不在、登录了没、找得到上传入口吗。

        为什么要这一步：没登录时页面上**根本没有** input[type=file]，
        直接开跑只会得到一堆"挂附件失败"，甚至还可能让启发式去点到登录表单
        的按钮。所以这里先挡住，并明确告诉用户要做什么。
        返回 '' 表示可以发，否则返回给用户看的错误说明。
        """
        if not self.ds or not self.ds.running:
            return '内嵌浏览器没在运行，请先点「DeepSeek 官网」页签'
        st = self.ds.state()
        if not st.get('ok'):
            return f'读不到内嵌页面状态：{st.get("error") or st}'
        # 就绪判定要**两条路都认**：主进程侧的 S.pageReady 有可能和真实状态脱节
        # （历史上它被 did-start-loading 打回 false 后卡住，页面明明好了却报
        #  "还在加载，不能发送"）。页面自己报的 document.readyState 是更可靠的判据。
        ready_flag = bool(st.get('ready'))
        rs = str(st.get('readyState') or '').lower()
        if not ready_flag and rs != 'complete':
            return f'内嵌页面还在加载（页面状态 {rs or "未知"}），等它显示出来再试'
        if st.get('loggedIn') is False:
            return '还没登录 DeepSeek：请先在右侧页面里用手机号/密码登录，再点发送'
        if not st.get('fileInput'):
            return ('页面上找不到上传入口（input[type=file]）—— '
                    '可能是没登录、或官网改版了。可以点「诊断」把页面结构导出来反馈')
        return ''

    def _ds_refresh_prompt_file(self):
        """发送前把导出目录里的「给AI的指令.txt」刷新成**当前**模板。

        为什么必须做：导出目录里的那个文件是**导出当时**写进去的。用户后来改了
        `AI提示词.txt`（例如加了「分批接收协议」），旧导出目录里还是老文案 ——
        从那儿发送，喂给 AI 的就是过时指令（用户实测踩到过这个坑）。
        每次发送前重写一遍，保证协议永远是最新的。
        """
        root = self.ds_out_root or ''
        if not root or not os.path.isdir(root):
            return ''
        path = os.path.join(root, '给AI的指令.txt')
        try:
            import ai_prompt
            ai_prompt.clear_cache()                 # 用户刚改过模板也要立即生效
            text = (ai_prompt.load_prompt(BASE) or '').strip()
        except Exception:  # noqa: BLE001
            return ''
        if not text:
            return ''
        try:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text + '\n')
            return path
        except OSError:
            return ''

    def _ds_send(self, dry_run=False):
        from tkinter import messagebox
        from ds_bridge import plan as ds_plan
        if getattr(self, '_ds_sending', False):
            self.toast('正在发送中', 'warn')
            return
        units = self._ds_effective_units()
        if not units:
            self.toast('请先在左边勾选要发送的内容'
                       + ('（当前是「只发文档」模式，图片被跳过）'
                          if self._ds_docs_only else ''), 'warn', 4000)
            return
        paths = ds_plan.expand_units(units)
        miss = ds_plan.missing_files(paths)
        if miss:
            self.toast(f'有 {len(miss)} 个文件已经不在磁盘上，请重新选择目录', 'err', 4000)
            return
        # 把旧导出目录里的指令文件刷新成最新版（否则喂给 AI 的是过时协议）
        refreshed = self._ds_refresh_prompt_file()
        if refreshed:
            self._ds_log('已把「给AI的指令.txt」刷新为最新指令')
        over = ds_plan.find_oversize(paths)
        if over and not messagebox.askokcancel(
                '有文件超过 100MB',
                f'{len(over)} 个文件超过网页版单文件上限（100MB），发送可能失败：\n'
                + '\n'.join('  · ' + os.path.basename(p) for p, _ in over[:5]) +
                '\n\n仍然继续？'):
            return
        batches = ds_plan.plan_batches(paths, self._ds_limit)
        if not batches:
            self.toast('没有要发送的文件', 'warn')
            return
        if not dry_run:
            if self._ds_ensure_host():
                self.toast('内嵌浏览器没起来，无法发送', 'err')
                return
            problem = self._ds_preflight()
            if problem:
                self._ds_log('不能发送：' + problem)     # 也留一行在左下角（提示可能被浏览器挡）
                self.toast(problem, 'warn', 6000)
                return
            head = '、'.join(os.path.basename(b[0]) for b in batches[:3])
            if not messagebox.askokcancel(
                    '确认发送',
                    f'将向当前对话发送 {len(paths)} 个文件，分 {len(batches)} 批'
                    f'（每批 ≤{self._ds_limit}）：\n\n'
                    f'批次大小：{"+".join(str(len(b)) for b in batches)}\n'
                    f'从 {head} … 开始\n\n'
                    '每批发出后会等它自然答完（协议下每批只回一句「我已接收上述信息」），'
                    '只有超过 25 秒还没答完才截断。\n'
                    '最后一批会自动附带一句「我已发送完毕」，触发它开始正式分析。\n'
                    '发送期间请不要切换页面。\n\n确定开始？'):
                return
        self._ds_open_progress(len(paths), batches, dry_run)

    def _ds_open_progress(self, n_files, batches, dry_run):
        th = self.theme
        # ⚠️ 走 _dlg()：先把浏览器藏起来（理由见 _dlg 的说明）。
        #    用户实测反馈："开始发送到一半我取消发送了，然后想返回导出页面就会像图里
        #    一样叫我等待发完" —— 其中一个成因就是这个进度窗和浏览器抢 z 序。
        win = self._dlg('演练分批' if dry_run else '正在发送到 DeepSeek 网页版…')
        win.geometry('620x400')
        win.configure(bg=T.rgb2hex(th['bg_top']))
        win.transient(self.root)
        if self._ico:
            try:
                win.iconbitmap(self._ico)
            except Exception:
                pass
        bg = T.rgb2hex(th['bg_top'])
        tk.Label(win, text=(f'{"演练" if dry_run else "发送"} {n_files} 个文件 · '
                            f'{len(batches)} 批（每批 ≤{self._ds_limit}）'),
                 bg=bg, fg=T.rgb2hex(th['text']),
                 font=(self.sf.font, 13, 'bold')).pack(anchor='w', padx=18, pady=(14, 2))
        cur = tk.Label(win, text='准备中…', bg=bg, fg=T.rgb2hex(th['accent']),
                       font=(self.sf.font, 10, 'bold'), wraplength=580,
                       justify='left')
        cur.pack(anchor='w', padx=18, fill='x')
        # 剩余时间单独一行，由界面每秒自己刷新 —— 只靠批次边界上报的那一次，
        # 用户盯着看会觉得"数字不动/不准"（尤其每批要等十几秒文件处理）。
        eta_lbl = tk.Label(win, text='', bg=bg, fg=T.rgb2hex(th['text_dim']),
                           font=(self.sf.font, 9), wraplength=580, justify='left')
        eta_lbl.pack(anchor='w', padx=18, fill='x')
        bar = tk.Canvas(win, height=12, bg=bg, highlightthickness=0)
        bar.pack(fill='x', padx=18, pady=(8, 4))

        def draw_bar(done, total):
            bar.delete('all')
            w = max(1, bar.winfo_width() - 4)
            bar.create_rectangle(2, 3, 2 + w, 9, fill=T.rgb2hex(th['surface2']),
                                 outline='')
            if total:
                bar.create_rectangle(2, 3, 2 + w * done / total, 9,
                                     fill=T.rgb2hex(th['accent']), outline='')

        log = tk.Text(win, height=13, bg=T.rgb2hex(th['surface']),
                      fg=T.rgb2hex(th['text_dim']), bd=0,
                      font=(self.sf.font, 9))
        log.pack(fill='both', expand=True, padx=18, pady=(6, 8))
        log.configure(state='disabled')
        self._ds_cancel_flag = False

        def stop_and_close(confirm=False, silent=False):
            """停下发送 + 关掉进度窗。**「取消」和「点 X」都走这一条**。

            ⚠️ 为什么必须合并（用户实测反馈）：以前点 X 只置 `_ds_cancel_flag` 就关窗，
            **不放开 `_ds_sending`** —— 于是"框我已经关了"，但想回导出页仍然被
            「正在发送，等它发完（或点进度窗的「取消」）再切页」拦住，而那个进度窗
            已经不存在了，用户无路可走。

            做四件事：
              1. 置取消标记 → 工作线程在下一个检查点自己停下；
              2. `_ds_sending` 立刻放开 → 「返回导出页」不再被拦；
              3. 关掉进度窗 → 不再有"叫我去点关闭"的框；
              4. 起一条**短命后台线程**等工作线程退出，然后**把页面上残留的附件点掉**
                 —— 不点掉的话，下次发送的 `_ensure_idle()` 会判定"输入区还挂着 N 个
                 附件"而拒绝继续（这才是"取消了却还是发不了"的真因）。
            """
            if confirm and getattr(self, '_ds_sending', False):
                from tkinter import messagebox
                if not messagebox.askokcancel(
                        '还在发送', '发送还没结束。\n\n确定要停下并关闭这个窗口吗？'):
                    return
            was_sending = bool(getattr(self, '_ds_sending', False))
            self._ds_cancel_flag = True
            self._ds_sending = False          # 关键：放开切页限制
            try:
                self.btn_ds_send.set_state('normal')
            except Exception:                 # noqa: BLE001
                pass
            try:
                win.destroy()
            except Exception:                 # noqa: BLE001
                pass
            if was_sending and not silent:
                self.toast('已停止发送（正在清理页面上的附件）', 'warn', 3200)

            def cleanup():
                # 等工作线程真正退出（最多等 20 秒），它一停就不再往队列里丢东西了
                for _ in range(200):
                    if not getattr(self, '_ds_worker_alive', False):
                        break
                    time.sleep(0.1)
                try:
                    r = self.ds.clear_attachments() if self.ds else {}
                    n = int((r or {}).get('removed') or 0)
                    if was_sending and not silent:
                        if n:
                            self.root.after(0, lambda: self.toast(
                                f'已停止，并清掉页面上残留的 {n} 个附件', 'ok', 3600))
                        else:
                            self.root.after(0, lambda: self.toast(
                                '已停止发送', 'ok', 2600))
                except Exception as e:        # noqa: BLE001
                    self._ds_log(f'清附件失败（可手动在页面上删）：{e}')

            try:
                threading.Thread(target=cleanup, daemon=True,
                                 name='ds-cancel-clean').start()
            except Exception:                 # noqa: BLE001
                pass

        def close_win():
            """点 X：发送中先问一句，然后**走同一条收尾路径**（见 stop_and_close）。"""
            stop_and_close(confirm=True, silent=False)

        def cancel_send():
            """点「取消」：立刻关掉进度窗，不问第二遍（用户要求：别让我再等/再点一次）。"""
            stop_and_close(confirm=False, silent=False)

        btn = tk.Button(win, text='取消', font=(self.sf.font, 10), relief='flat',
                        cursor='hand2', command=cancel_send)
        btn.pack(anchor='e', padx=18, pady=(0, 14))
        win.protocol('WM_DELETE_WINDOW', close_win)

        ctx = {'win': win, 'cur': cur, 'log': log, 'bar': bar, 'bar_draw': draw_bar}
        self._ds_sending = True
        self._ds_done_batches = 0
        self.btn_ds_send.set_state('disabled')

        # ⚠️ 工作线程**绝对不要碰 Tk**。
        # 以前这里直接 `self.root.after(0, ...)`：那不是线程安全的（主线程不在
        # mainloop 里时会抛 "main thread is not in main loop"），更糟的是线程里
        # 一抛异常就没人调 finish()，界面会**永远卡在"正在发送中"**、连切页都被
        # 挡住。现在改成：线程只往队列里丢消息，主线程用 after 轮询消费。
        import queue
        q = queue.Queue()
        # 剩余时间由界面自己每秒算：只靠批次边界上报一次，等十几秒文件处理的时候
        # 数字一直不动，看起来就是"算错了"。
        prog = {'done': 0, 'total': len(batches), 't0': time.time(), 'marks': []}

        def ui_log(msg):
            q.put(('log', str(msg)))

        def ui_progress(done, total, text):
            q.put(('prog', (done, total, text)))

        def ui_eta():
            try:
                done = min(prog['done'], prog['total'])
                total = prog['total']
                marks = prog['marks']
                if done <= 0:
                    txt = f'共 {total} 批 · 正在准备第 1 批（每批要先给文件留处理时间）'
                else:
                    if len(marks) >= 2:
                        avg = (marks[-1] - marks[0]) / (len(marks) - 1)
                    elif marks:
                        avg = max(1.0, time.time() - marks[0])
                    else:
                        avg = 20.0
                    remain = avg * max(0, total - done)
                    txt = (f'已完成 {done}/{total} 批 · 预计还需 {_fmt_dur(remain)}'
                           f' · 每批约 {_fmt_dur(avg)}')
                eta_lbl.configure(text=txt)
            except Exception:
                pass
            if getattr(self, '_ds_sending', False):
                try:
                    win.after(1000, ui_eta)
                except Exception:
                    pass

        def drain():
            """主线程侧：把队列里的消息搬到界面上。返回是否收到结束信号。"""
            for _ in range(200):
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    return False
                if kind == 'log':
                    try:
                        log.configure(state='normal')
                        log.insert('end', payload + '\n')
                        log.see('end')
                        log.configure(state='disabled')
                        self._ds_log(payload)
                    except Exception:
                        pass
                elif kind == 'prog':
                    done, total, text = payload
                    self._ds_done_batches = done
                    if done > prog['done']:
                        prog['done'] = done
                        prog['marks'].append(time.time())
                    try:
                        cur.configure(text=text)
                        draw_bar(done, total)
                    except Exception:
                        pass
                elif kind == 'done':
                    finish(payload)
                    return True
            return False

        def poll():
            if drain():
                return
            try:
                win.after(80, poll)
            except Exception:
                pass

        def worker():
            from ds_bridge import sender as ds_sender
            # ⚠️ 这个标记给「取消」用：取消后要等**工作线程真的退出**再去清页面上的
            #    残留附件，否则会和正在进行的挂附件/发送抢页面。
            self._ds_worker_alive = True
            try:
                s = ds_sender.BatchSender(self.ds, limit=self._ds_limit,
                                          dry_run=dry_run, log=ui_log)
                res = s.run(batches, on_progress=ui_progress,
                            should_cancel=lambda: self._ds_cancel_flag)
            except Exception as e:      # noqa: BLE001
                import traceback
                q.put(('log', f'发送线程崩了：{e}'))
                q.put(('log', traceback.format_exc().splitlines()[-1]))
                res = {'ok': False, 'batches': len(batches), 'sent_batches': 0,
                       'sent_files': 0, 'failed': [], 'stopped': 0,
                       'cancelled': False, 'error': f'内部错误：{e}'}
            finally:
                self._ds_worker_alive = False
            q.put(('done', res))

        def finish(res):
            self._ds_sending = False
            try:
                self.btn_ds_send.set_state('normal')
            except Exception:
                pass
            summary = (f'完成 {res["sent_batches"]}/{res["batches"]} 批 · '
                       f'{res["sent_files"]} 个文件 · 截断 {res["stopped"]} 次'
                       f'{"（已取消）" if res["cancelled"] else ""}')
            if res.get('error'):
                summary += f'；错误：{res["error"]}'
            if res['failed']:
                summary += f'；失败 {len(res["failed"])} 批'
                hint = ('附件可能已经挂在内嵌页面上了：请切到官网页签，'
                        '手动点发送或把附件删掉，再重新「开始发送」。')
                if any(k in (res.get('error') or '')
                       for k in ('异常文件', '服务器繁忙', '请检查网络', '发送失败')):
                    hint = ('官网拒收（多半是一批文件太多）：把左下角「每批 30 个」'
                            '点一下改成更小的值，并把官网页面上残留的附件删掉，'
                            '再重新「开始发送」。')
                summary += '\n' + hint
            # ⚠️ 用户点了「取消」时进度窗**已经被销毁**：这时只更新底部日志 + 弹一句提示，
            #    绝不再去碰 win/cur/log/btn（那些控件已经没了，碰了只是白抛异常）。
            try:
                win_alive = bool(win.winfo_exists())
            except Exception:                   # noqa: BLE001
                win_alive = False
            if win_alive:
                try:
                    cur.configure(text=summary)
                except Exception:               # noqa: BLE001
                    pass
                try:
                    log.configure(state='normal')
                    log.insert('end', summary + '\n')
                    log.see('end')
                    log.configure(state='disabled')
                except Exception:               # noqa: BLE001
                    pass
            self._ds_log(summary)
            self.toast(summary, 'ok' if res['ok'] else 'warn', 5000)
            if win_alive:
                try:
                    win.title('发送结束')
                    # 结束后按钮变成「关闭」——演练跑完必须能关掉窗口
                    btn.configure(text='关闭', state='normal', command=close_win)
                except Exception:               # noqa: BLE001
                    pass

        draw_bar(0, len(batches))
        win.update_idletasks()          # 先布局一次，进度条初始宽度才是对的
        draw_bar(0, len(batches))
        bar.bind('<Configure>',
                 lambda e: draw_bar(getattr(self, '_ds_done_batches', 0),
                                    len(batches)))
        win.after(80, poll)             # 主线程轮询（after 只在主线程调用）
        win.after(300, ui_eta)          # 剩余时间每秒刷新
        threading.Thread(target=worker, daemon=True, name='ds-send').start()

    # ────────────────────── 退出 ──────────────────────

    def quit_app(self):
        """关窗。

        要点：**先把窗口关掉，再收尾**。收尾里 wcdb.stop() 会走
        terminate → wait(5s) → taskkill(/T, 10s)，全放在 destroy() 之前
        就会让"点叉号"卡住最多十几秒。现在窗口立即消失，杀进程丢到后台。
        """
        self._closing = True
        try:
            _boot_log('quit_app：窗口关闭请求（用户点了 X 或程序自己调了它）')
        except Exception:
            pass
        # ⚠️ 先把内置浏览器**藏起来**。用户实测反馈："程序退了那个嵌入的东西也不消失"
        # —— 它是独立顶层窗口，不随 Tk 窗口销毁而消失，必须在 destroy() 之前 hide()。
        try:
            self._ds_leave()
        except Exception as e:      # noqa: BLE001
            try:
                _boot_log(f'_ds_leave 出错（忽略）：{e}')
            except Exception:
                pass
        wcdb = self.wcdb
        self.wcdb = None
        ds = self.ds
        self.ds = None
        try:
            self.root.destroy()
        except Exception:
            pass

        def _cleanup():
            """收尾：停掉子进程 → 确认死透 → 自己删掉 `_MEI` 临时目录 → 立刻退出。

            ⚠️⚠️ 为什么必须"自己删"（2026-09-16 用户实测报的弹框）：
            PyInstaller 一体包退出时要去删 `_MEIxxxx`，而那个目录里放着
            `electron.exe` / `node.exe` / `*.dll` —— **只要还有子进程活着，文件就是被
            锁住的**，删不掉就弹
            「Failed to remove temporary directory: ...\\_MEI000b2202」。
            所以这里做三件事，顺序不能反：
              1. 把子进程停掉（`ds.shutdown` / `wcdb.stop` 内部都是"趁活着按树杀"）；
              2. **轮询等它们真的消失**（有上限，绝不死等）；
              3. 自己重试删 `_MEI`，删成/删不掉都照样 `os._exit(0)` ——
                 删掉了就不会再弹那个框；万一真删不掉，退出流程本身也不会卡。
            ⚠️ 不要在这里用 PowerShell 批量杀进程：既慢又容易误伤别的进程。
            """
            if ds is not None:
                try:
                    # ⚠️ owner 关系要先解除：主窗口正在销毁，留着关系会让 Electron 的
                    #    窗口跟着一起被销毁（表现为收尾时卡住/崩溃）。纯 Win32，很快。
                    ds.detach()
                except Exception:                    # noqa: BLE001
                    pass
                try:
                    ds.shutdown(wait=True)           # 等它把进程树收干净
                except Exception:                    # noqa: BLE001
                    pass
            if wcdb is not None:
                try:
                    wcdb.stop()
                except Exception:                    # noqa: BLE001
                    pass
            _wait_children_gone(deadline_s=6.0)
            _remove_mei_dir()
            os._exit(0)

        def _child_pids():
            """当前进程的直接子进程 PID（用 WMIC 太慢，这里用 tasklist 的 csv）。"""
            import subprocess
            pid = os.getpid()
            ps = ("Get-CimInstance Win32_Process -Filter \"ParentProcessId=%d\" | "
                  "Select-Object -ExpandProperty ProcessId" % pid)
            try:
                r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                   capture_output=True, text=True, timeout=6,
                                   creationflags=(0x08000000 if os.name == 'nt' else 0))
                return [int(x) for x in (r.stdout or '').split() if x.strip().isdigit()]
            except Exception:                        # noqa: BLE001
                return []

        def _wait_children_gone(deadline_s=6.0):
            """等子进程消失（有上限，绝不死等）—— 它们不释放，`_MEI` 就删不掉。"""
            t0 = time.time()
            while time.time() - t0 < deadline_s:
                if not _child_pids():
                    return True
                time.sleep(0.25)
            return False

        def _remove_mei_dir():
            """重试删除 PyInstaller 的 `_MEIxxxx` 临时目录，避免退出时弹警告框。

            PyInstaller 自己也会删，但它只试一次；我们删掉之后它就没得可抱怨了。
            删不掉也不影响退出（只是下次启动会多一个残留目录，系统会清理 Temp）。
            """
            base = getattr(sys, '_MEIPASS', '') or ''
            if not base or '_MEI' not in os.path.basename(base):
                return False
            import shutil
            for _ in range(8):
                try:
                    shutil.rmtree(base, ignore_errors=False)
                    return True
                except OSError:
                    time.sleep(0.3)
            return False

        if wcdb is not None or ds is not None:
            import threading
            threading.Thread(target=_cleanup, daemon=False,
                             name='shutdown').start()

            # ⚠️⚠️ **硬兜底**：独立守护线程到点直接 `os._exit(0)`，保证"无论谁卡住，
            #    进程一定会消失"。
            #    ⚠️ 但**时间必须比收尾工作长**（2026-09-16 实测踩到）：
            #    原来只给 10 秒，而收尾里 `ds.shutdown`（taskkill /T 最多 10s）+
            #    `wcdb.stop`（terminate 5s + taskkill 10s）+ 等子进程消失（6s）
            #    加起来能到 25 秒 —— 结果硬兜底**在收尾中途把清理线程一起掐死**，
            #    子进程没杀完就退出了（实测：点 X 后正好 10.0s 退出，残留 4 个进程）。
            #    现在给 45 秒：窗口早就消失了（用户看不到任何等待），
            #    这段时间只是让收尾把话说完、把子进程和临时目录处理干净。
            def _hard_exit_after(sec=45):
                time.sleep(sec)
                try:
                    os._exit(0)
                except Exception:                    # noqa: BLE001
                    pass

            threading.Thread(target=_hard_exit_after, daemon=True,
                             name='hard-exit').start()

    def run(self):
        self.root.mainloop()


def _boot_log(msg):
    """启动期插桩。

    打包成 --windowed 后没有控制台，卡住时无法诊断；这里把关键步骤写文件，
    卡在哪一步一目了然，也方便用户出问题时把日志发过来。
    """
    try:
        with open(os.path.join(ROOT, '.boot.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.datetime.now().strftime("%H:%M:%S")} {msg}\n')
    except Exception:
        pass


def _boot_log_done():
    """启动成功后删掉插桩日志，不给用户目录留垃圾。

    只在确认窗口已经建好之后调用 —— 失败时这份日志正是排查依据。
    """
    try:
        os.remove(os.path.join(ROOT, '.boot.log'))
    except OSError:
        pass


def main():
    """启动入口。

    打包成 --windowed 后没有控制台，任何启动期异常都会静默消失（表现为
    "双击没反应"）。所以这里统一捕获并写日志，同时弹一个尽量朴素的原生
    错误框 —— 用最基础的 tkinter，保证即使自绘界面初始化失败也能显示出来。
    """
    # 必须在创建 Tk() 之前声明 DPI 感知：否则在 125%/150% 缩放的显示器上，
    # Windows 会把整个窗口位图拉伸，圆角和文字都会发虚。
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        _boot_log('--- 启动 ---')
        _boot_log(f'frozen={getattr(sys, "frozen", False)} BASE={BASE}')
        _boot_log('创建 App')
        app = App()
        # 把窗口状态写进日志：打包后没有控制台，这是确认"窗口真的出来了"的唯一可靠办法
        try:
            app.root.update_idletasks()
            app.root.update()
            _boot_log(f'窗口 id={app.root.winfo_id()} '
                      f'mapped={bool(app.root.winfo_ismapped())} '
                      f'size={app.root.winfo_width()}x{app.root.winfo_height()} '
                      f'canvas_items={len(app.sf.canvas.find_all())} '
                      f'font={app.sf.font}')
        except Exception as e:
            _boot_log(f'窗口状态检查异常: {e}')
        _boot_log('App 创建完成，进入 mainloop')
        # 退出也必须留痕。事故（2026-09-15）：.boot.log 出现过三次却**从没被删掉**，
        # 说明进程在 _boot_log_done(1500ms) 之前就没了 —— 但因为退出路径不留日志，
        # 到底是"用户关窗"、"未捕获异常退出"还是"被外部结束"完全查不出来。
        # 下面这段让任何一种退出都留下最后一步是什么。
        import atexit

        def _exit_trace():
            try:
                _boot_log('atexit：进程即将退出（mainloop 已结束）')
            except Exception:
                pass
        atexit.register(_exit_trace)

        def _excepthook(etype, value, tb):
            import traceback as _tb
            try:
                _boot_log('未捕获异常：%s' % ''.join(_tb.format_exception_only(etype, value)).strip())
                with open(os.path.join(ROOT, '.ui_errors.log'), 'a', encoding='utf-8') as f:
                    f.write(''.join(_tb.format_exception(etype, value, tb)) + '\n')
            except Exception:
                pass
        try:
            sys.excepthook = _excepthook
        except Exception:
            pass

        app.root.after(1500, _boot_log_done)      # 窗口稳定后清掉插桩日志
        app.run()
        _boot_log('mainloop 已返回（窗口被关闭）')
    except Exception:
        import traceback
        tb = traceback.format_exc()
        log_path = os.path.join(ROOT, '启动错误.log')
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f'{APP_TITLE} {APP_VERSION}\n')
                f.write(f'frozen={getattr(sys, "frozen", False)}  '
                        f'BASE={BASE}\n\n{tb}\n')
        except OSError:
            log_path = '(日志写入失败)'
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            r = _tk.Tk()
            r.withdraw()
            _mb.showerror('启动失败',
                          f'{tb.strip().splitlines()[-1]}\n\n'
                          f'完整信息已写入：\n{log_path}')
            r.destroy()
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
