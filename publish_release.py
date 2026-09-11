# -*- coding: utf-8 -*-
"""把发布包上传到 GitHub Releases，得到一个可供朋友直接下载的链接。

为什么用 Release 而不是把文件提交进仓库
----------------------------------------
* GitHub 单个文件超过 100 MB 会被拒绝；本发布包解压前 224 MB、解压后 516 MB，
  **不可能**放进 Git 仓库（用 Git LFS 也会让克隆极慢且容易超配额）。
* Release 附件（asset）单文件上限 2 GB，正是为分发二进制准备的，
  而且有稳定的下载直链，不占仓库体积。

用法
----
1. 生成一个 GitHub Personal Access Token（classic，勾选 `repo` 或 `public_repo`）：
   https://github.com/settings/tokens
2. 把 token 存到本目录下的 `.token` 文件（该文件已在 .gitignore 中，不会被提交），
   或设置环境变量 `GH_TOKEN` / `GITHUB_TOKEN`。
3. 运行：
       python publish_release.py --repo 用户名/WeChatExportPlus --tag v2.0.0
   常用参数：
       --zip 指定压缩包路径（默认自动从发布目录打包）
       --notes 指定发布说明文件（默认用 RELEASE_NOTES.md，没有就自动生成）
       --no-build 跳过重新打包（直接用已有 zip）
       --create-repo 仓库不存在时自动创建

脚本做的事：初始化 git → 提交源码 → 推送到 GitHub → 创建 Release → 上传 zip 附件。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
API = 'https://api.github.com'
DEFAULT_APP_NAME = 'WeChatExportPlus'
DEFAULT_RELEASE_DIR = os.path.join(os.path.dirname(ROOT), 'WeChatExportPlus_发布版')


def log(msg):
    print(msg, flush=True)


def run(cmd, cwd=None, check=True, capture=True):
    kw = {'cwd': cwd or ROOT, 'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    r = subprocess.run(cmd, **kw)
    if check and r.returncode != 0:
        raise SystemExit(f'命令失败: {" ".join(cmd)}\n{r.stdout or ""}')
    return r


def read_token():
    """按优先级找 token：环境变量 → 本目录任意 *.token / .token 文件。

    允许任意 `*.token` 命名（例如 ME.token、GH.token），
    免得因为文件名差一个点就卡住。
    """
    for env in ('GH_TOKEN', 'GITHUB_TOKEN'):
        v = os.environ.get(env, '').strip()
        if v:
            return v, env
    cands = ['.token']
    for p in sorted(os.listdir(ROOT)):
        if p.endswith('.token') and p not in cands:
            cands.append(p)
    for name in cands:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            with open(p, encoding='utf-8-sig', errors='replace') as f:
                v = f.read().strip()
            if v:
                return v, name
    raise SystemExit(
        '找不到 GitHub token。请任选一种方式：\n'
        '  1) 在本目录放一个 token 文件，文件名以 .token 结尾\n'
        '     （例如 ME.token），内容只有一行 token\n'
        '  2) 设置环境变量 GH_TOKEN\n'
        'token 生成地址：https://github.com/settings/tokens\n'
        '  选 classic，勾选 repo（只发公开仓库勾 public_repo 也可）')


def api(method, path, token, data=None, content_type='application/json', timeout=120):
    url = path if path.startswith('http') else API + path
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    req.add_header('User-Agent', 'WeChatExportPlus-publisher')
    if body is not None:
        req.add_header('Content-Type', content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode('utf-8')) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:500]
        raise SystemExit(f'GitHub API {method} {url} 失败: HTTP {e.code}\n{detail}')
    except urllib.error.URLError as e:
        raise SystemExit(f'连不上 GitHub（{e.reason}）。\n'
                         '若本机网络不稳定，可稍后重试；上传大文件请确保网络通畅。')


def upload_asset(upload_url, token, filepath, name=None, retries=3):
    """上传 Release 附件。大文件用流式读取，避免一次性占满内存。"""
    name = name or os.path.basename(filepath)
    size = os.path.getsize(filepath)
    base = upload_url.split('{')[0]
    url = f'{base}?name={urllib.parse.quote(name)}'

    for attempt in range(1, retries + 1):
        try:
            with open(filepath, 'rb') as f:
                req = urllib.request.Request(url, data=f, method='POST')
                req.add_header('Authorization', f'Bearer {token}')
                req.add_header('Content-Type', 'application/zip')
                req.add_header('Content-Length', str(size))
                req.add_header('User-Agent', 'WeChatExportPlus-publisher')
                log(f'  上传中… {size / 1048576:.0f} MB（第 {attempt} 次尝试）')
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=1800) as r:
                    info = json.loads(r.read().decode('utf-8'))
                log(f'  上传完成，用时 {time.time() - t0:.0f} 秒')
                return info
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:400]
            if e.code == 422 and 'already_exists' in detail:
                log('  同名附件已存在，先删除再重传')
                return None
            log(f'  上传失败 HTTP {e.code}: {detail}')
        except Exception as e:
            log(f'  上传异常: {e}')
        time.sleep(5 * attempt)
    raise SystemExit('附件上传多次失败。可稍后重跑本脚本（加 --skip-git 跳过 git 步骤）。')


def build_zip(release_dir, out_zip):
    """用最小依赖的方式打包（调用 PowerShell 的 Compress-Archive，条目名 UTF-8 正确）。"""
    if os.path.exists(out_zip):
        os.remove(out_zip)
    if not os.path.isdir(release_dir):
        raise SystemExit(
            f'找不到发布目录: {release_dir}\n'
            '请先运行 python build_dist_plus.py，或用 --release-dir 指定。')
    log(f'  打包 {release_dir} -> {out_zip}')
    ps = ('Compress-Archive -Path (Join-Path $args[0] "*") '
          '-DestinationPath $args[1] -CompressionLevel Optimal -ErrorAction Stop')
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps,
                        release_dir, out_zip],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_zip):
        raise SystemExit(f'打包失败:\n{r.stderr[:800]}')
    return out_zip


def git_publish(owner, rname, token, branch='main', commit_msg=''):
    """提交并把源码推到 owner/rname。

    注意：remote URL 里带 token，属于敏感信息。推送结束后（无论成败）
    都会把它换回不带凭据的地址，避免 token 长期留在 .git/config 里。
    """
    log('\n[1] 提交源码到 git')
    if not os.path.isdir(os.path.join(ROOT, '.git')):
        run(['git', 'init', '-b', branch])
    run(['git', 'add', '-A'])
    st = run(['git', 'status', '--porcelain'])
    if st.stdout.strip():
        msg = commit_msg or 'Update WeChatExportPlus'
        run(['git', '-c', 'user.name=WeChatExportPlus',
             '-c', 'user.email=noreply@localhost', 'commit', '-m', msg])
        log('  已提交改动')
    else:
        log('  没有需要提交的改动')

    clean_url = f'https://github.com/{owner}/{rname}.git'
    if 'origin' in run(['git', 'remote'], check=False).stdout.split():
        run(['git', 'remote', 'set-url', 'origin', clean_url])
    else:
        run(['git', 'remote', 'add', 'origin', clean_url])

    # token 通过环境变量交给 git，不写进 remote URL、不落到 .git/config、
    # 也不出现在任何错误输出里（曾经因为把 token 拼进 URL，失败时被打印出来）。
    helper = ('!f(){ test "$1" = get && printf "username=%s\\npassword=%s\\n" '
              '"x-access-token" "$WE_GH_TOKEN"; }; f')
    env = dict(os.environ, WE_GH_TOKEN=token)
    log(f'  推送到 {owner}/{rname} ({branch})')
    try:
        r = subprocess.run(
            ['git', '-c', f'credential.helper={helper}', 'push', '-u', 'origin', branch],
            cwd=ROOT, text=True, encoding='utf-8', errors='replace',
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        ok = r.returncode == 0
        out = r.stdout or ''
    except Exception as e:
        ok, out = False, str(e)

    if not ok:
        # 万一 git 把凭据回显出来，这里再做一次兜底打码
        safe = out.replace(token, '***')[:600]
        raise SystemExit(
            f'推送失败:\n{safe}\n\n'
            '常见原因：网络中断（本机到 GitHub 偶发不通，重试即可）、'
            'token 权限不足（需 repo）、或仓库地址不对。')
    log('  推送成功')


def main():
    global urllib
    import urllib.parse  # noqa: F401  （上传时用到）

    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default='WeChatExportPlus',
                    help='仓库名，或 用户名/仓库名；只给仓库名时用户名由 token 自动识别')
    ap.add_argument('--tag', default='v2.0.0', help='版本 tag，如 v2.0.0')
    ap.add_argument('--name', default='', help='Release 标题')
    ap.add_argument('--release-dir', default=DEFAULT_RELEASE_DIR)
    ap.add_argument('--zip', default='', help='已有 zip 路径；不传则重新打包')
    ap.add_argument('--notes', default='', help='发布说明文件（markdown）')
    ap.add_argument('--skip-git', action='store_true', help='跳过提交/推送')
    ap.add_argument('--create-repo', action='store_true', help='仓库不存在时自动创建')
    ap.add_argument('--private', action='store_true', help='配合 --create-repo：创建私有仓库')
    args = ap.parse_args()

    token, src = read_token()
    log(f'已读取 token（来源：{src}）')

    # 先用 token 确认身份，再决定 owner —— 免去用户手打用户名的麻烦与出错
    me = api('GET', '/user', token)
    my_login = me.get('login') or ''
    log(f'登录身份: {my_login}')

    owner, _, rname = args.repo.partition('/')
    if not rname:
        # 只给了仓库名：owner 一律用 token 对应的账号，避免推错地方
        rname, owner = owner, my_login
    if owner.lower() != my_login.lower():
        log(f'注意：owner({owner}) 与 token 账号({my_login}) 不一致，'
            f'需要该组织的写权限才能继续。')

    # 仓库不存在时按需创建
    exists = True
    try:
        api('GET', f'/repos/{owner}/{rname}', token)
    except SystemExit:
        exists = False
    if not exists:
        if not args.create_repo:
            raise SystemExit(
                f'仓库 {owner}/{rname} 不存在或无权访问。\n'
                '加 --create-repo 让脚本自动创建。')
        log(f'创建仓库 {owner}/{rname}（{"私有" if args.private else "公开"}）…')
        api('POST', '/user/repos', token, {
            'name': rname, 'private': bool(args.private),
            'description': '微信聊天记录批量导出工具（批量勾选 + 单文件 Markdown 导出，适合喂给 AI）',
            'has_issues': True, 'has_wiki': False,
        })
        log('  已创建')

    if not args.skip_git:
        git_publish(owner, rname, token)

    log('\n[2] 准备发布包')
    zip_path = args.zip
    if not zip_path:
        zip_path = os.path.join(os.path.dirname(ROOT),
                                f'{DEFAULT_APP_NAME}_{args.tag}_win64.zip')
        build_zip(args.release_dir, zip_path)
    if not os.path.exists(zip_path):
        raise SystemExit(f'找不到 zip: {zip_path}')
    size_mb = os.path.getsize(zip_path) / 1048576
    log(f'  发布包: {os.path.basename(zip_path)} ({size_mb:.0f} MB)')

    log('\n[3] 创建 Release')
    notes = ''
    if args.notes and os.path.exists(args.notes):
        with open(args.notes, encoding='utf-8') as f:
            notes = f.read()
    if not notes:
        notes = (f'## 下载\n\n'
                 f'下载下面的 `{os.path.basename(zip_path)}`，解压后双击 '
                 f'`WeChatExportPlus.exe` 即可。\n\n'
                 f'无需安装 Python / Node.js。需要微信 4.x Windows 版，'
                 f'获取密钥时需管理员权限。\n\n'
                 f'详见压缩包内的 `使用说明.md`。\n')

    tag = args.tag
    # tag 已存在则复用该 release
    rel = None
    try:
        rel = api('GET', f'/repos/{owner}/{rname}/releases/tags/{tag}', token)
        log(f'  tag {tag} 已存在，复用该 Release')
    except SystemExit:
        pass
    if rel is None:
        rel = api('POST', f'/repos/{owner}/{rname}/releases', token, {
            'tag_name': tag,
            'name': args.name or f'WeChatExportPlus {tag}',
            'body': notes,
            'draft': False,
            'prerelease': False,
        })
        log(f"  已创建: {rel.get('html_url')}")

    log('\n[4] 上传附件')
    existing = {a['name']: a for a in rel.get('assets', [])}
    asset_name = os.path.basename(zip_path)
    if asset_name in existing:
        log('  删除同名旧附件')
        api('DELETE', f"/repos/{owner}/{rname}/releases/assets/{existing[asset_name]['id']}",
            token)
    upload_asset(rel['upload_url'], token, zip_path, asset_name)

    dl = f"https://github.com/{owner}/{rname}/releases/download/{tag}/{asset_name}"
    log('\n' + '=' * 62)
    log('完成！')
    log(f'  Release 页面 : {rel.get("html_url")}')
    log(f'  直接下载链接 : {dl}')
    log(f'  最新版固定页 : https://github.com/{owner}/{rname}/releases/latest')
    log('=' * 62)
    log('\n把这个下载链接发给朋友即可。')


if __name__ == '__main__':
    import urllib.parse  # noqa: F401
    main()
