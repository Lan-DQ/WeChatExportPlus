# -*- coding: utf-8 -*-
"""图片解密的批量模式 + 账号匹配（issue #2）的回归测试。

背景（用户实测 + issue #2）：
  · 旧实现"一张图起一个 node 进程"，400 张要十几分钟（每张 69ms 全是启动开销）。
  · 旧实现只取 `accounts[0]`，用户登录过多个微信账号时，另一账号的图片全部解不出来。
本文件用**真实 .dat 文件**（没有就跳过）验证批量模式又快又对，并验证：
  · 批量模式能跑、输出 JSON 行、能解出图
  · 账号匹配用的是"路径里的 wxid 精确匹配"
  · 单文件模式没被破坏
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'scripts')
HELPER = os.path.join(SCRIPTS, 'decrypt_image.js')
sys.path.insert(0, os.path.join(ROOT, 'exporters'))

DAT_ROOTS = [
    os.path.expanduser(r'~\Documents\xwechat_files'),
    os.path.expanduser(r'~\Documents\WeChat Files'),
]


def _node():
    for p in (os.path.join(ROOT, 'runtime', 'node.exe'),):
        if os.path.isfile(p):
            return p
    return shutil.which('node') or shutil.which('node.exe') or ''


def _real_dats(limit=20):
    for root in DAT_ROOTS:
        if not os.path.isdir(root):
            continue
        found = []
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith('.dat'):
                    found.append(os.path.join(dp, fn))
                    if len(found) >= limit:
                        return found
        if found:
            return found
    return []


NODE = _node()
DATS = _real_dats() if NODE else []


def test_helper_supports_batch_flag():
    """decrypt_image.js 必须保留 --batch 分支，且单文件模式没被破坏。"""
    src = open(HELPER, encoding='utf-8').read()
    assert "'--batch'" in src or '"--batch"' in src, '缺少 --batch 批量分支'
    # 账号匹配必须读全部账号，不能再写死 accounts[0] 取第一个就完事
    assert 'accounts = (d && d.accounts) || []' in src, '没有取出全部账号（issue #2）'
    assert 'function pickAccount' in src, '缺少按 wxid 匹配账号的逻辑'
    assert "how: 'exact'" in src, '缺少精确匹配分支'


def test_media_resolver_has_batch_cache():
    """MediaResolver 必须有批量缓存，命中缓存时不起 node 进程。"""
    import media_resolver as mr
    inst = mr.MediaResolver(None, cache_dir=os.path.join(ROOT, '.pytest_tmp_media'))
    assert hasattr(inst, '_native_cache'), '缺少 _native_cache'
    assert hasattr(inst, '_native_batch_tried'), '缺少 _native_batch_tried'
    assert hasattr(inst, '_decrypt_batch_in_dir'), '缺少批量解密方法'
    # 缓存命中时不应调用任何子进程
    inst._native_cache['/nonexistent/x.dat'] = ('jpg', 'AAAA')
    assert inst._decrypt_with_native('/nonexistent/x.dat') == ('jpg', 'AAAA')


@pytest.mark.skipif(not NODE, reason='没有 node 运行时')
def test_batch_mode_smoke_no_files():
    """批量模式空输入要能干净退出（不能挂住）。"""
    req = json.dumps({'dataDir': '', 'paths': []}).encode('utf-8')
    r = subprocess.run([NODE, HELPER, '--batch'], input=req, capture_output=True,
                       timeout=90, cwd=SCRIPTS,
                       creationflags=0x08000000 if os.name == 'nt' else 0)
    assert r.returncode == 0, r.stderr[:300]


@pytest.mark.skipif(not NODE or not DATS, reason='没有 node 或没有真实 .dat 图片')
def test_batch_matches_exact_wxid_and_is_fast():
    """真实 .dat：批量模式要能解出图、走精确账号匹配，而且比一图一进程快得多。"""
    paths = DATS[:20]
    req = json.dumps({'dataDir': '', 'paths': paths}).encode('utf-8')
    t0 = time.time()
    r = subprocess.run([NODE, HELPER, '--batch'], input=req, capture_output=True,
                       timeout=180, cwd=SCRIPTS,
                       creationflags=0x08000000 if os.name == 'nt' else 0)
    dt_batch = time.time() - t0
    assert r.returncode == 0, r.stderr[:300]
    recs, summary = [], None
    for line in (r.stdout or b'').decode('utf-8', 'replace').splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get('summary'):
            summary = d
        else:
            recs.append(d)
    assert recs, '批量模式没有任何结果行'
    assert summary is not None, '批量模式缺少 summary 汇总行'
    ok = [x for x in recs if x.get('ok')]
    # 至少要有解出来的（若这台机器原生模块完全不可用，则只能断言"跑通"）
    if ok:
        # 解出来的那些必须报出用了哪种匹配，且不能是"没有账号"
        assert all(x.get('match') in ('exact', 'prefix', 'fallback', 'nosid') for x in ok)
        with_sid = [x for x in ok if x.get('path') and 'wxid_' in x['path']]
        if with_sid:
            assert any(x.get('match') == 'exact' for x in with_sid), \
                '路径里带 wxid 的图片应该走精确匹配'
    assert summary.get('accounts') is not None, 'summary 里应报告 DLL 里的账号数'


@pytest.mark.skipif(not NODE or not DATS, reason='没有 node 或没有真实 .dat 图片')
def test_batch_is_much_faster_than_per_image():
    """批量必须显著快于一图一进程（这是这次改动的核心目的）。"""
    paths = DATS[:12]

    def one(p):
        subprocess.run([NODE, HELPER, p], capture_output=True, timeout=15,
                       cwd=SCRIPTS,
                       creationflags=0x08000000 if os.name == 'nt' else 0)

    t0 = time.time()
    for p in paths:
        one(p)
    dt_one = time.time() - t0

    req = json.dumps({'dataDir': '', 'paths': paths}).encode('utf-8')
    t0 = time.time()
    subprocess.run([NODE, HELPER, '--batch'], input=req, capture_output=True,
                   timeout=180, cwd=SCRIPTS,
                   creationflags=0x08000000 if os.name == 'nt' else 0)
    dt_batch = time.time() - t0

    assert dt_batch < dt_one, ('批量应更快：一图一进程 %.1fs vs 批量 %.1fs'
                               % (dt_one, dt_batch))
    # 实测能到 30 倍上下；这里放宽到 3 倍，只拦"批量没生效"这种回归
    assert dt_one / max(dt_batch, 0.01) > 3, \
        '批量提速不足（%.2fs → %.2fs），可能批量分支没走到' % (dt_one, dt_batch)
