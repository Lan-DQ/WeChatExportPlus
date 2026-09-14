# -*- coding: utf-8 -*-
"""微信数据目录自动检测（gui/app_plus.py 的 find_xwechat_dirs）的单元测试。

背景（issue #1）：老版本只查固定几个位置、且只认 C~H 盘的根目录，用户一旦把
微信数据挪到自定义目录，就必须手动点「浏览」。现在优先读**微信自己记录的位置**，
并且会往盘符下一层找。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'exporters')):
    if p not in sys.path:
        sys.path.insert(0, p)

import app_plus as A  # noqa: E402


def _mkdata(root):
    """造一个"像微信数据目录"的目录（必须有 wxid_xxx 子文件夹）。"""
    os.makedirs(os.path.join(root, 'wxid_abc123_4f0c'), exist_ok=True)
    return root


def test_returns_configured_dir_itself(tmp_path):
    """微信记录的位置就是数据目录本身（3.x 风格：D:\\WeChat Files）。"""
    d = _mkdata(str(tmp_path / 'WeChat Files'))
    assert A.find_xwechat_dirs(extra_roots=[d]) == d


def test_returns_xwechat_files_under_configured_dir(tmp_path):
    """微信记录的位置是上一层（4.x 的 MyDocument: → 文档目录）。"""
    parent = tmp_path / 'MyDocs'
    d = _mkdata(str(parent / 'xwechat_files'))
    assert A.find_xwechat_dirs(extra_roots=[str(parent)]) == d


def test_custom_deep_path(tmp_path):
    """自定义深层路径：D:\\我的资料\\微信数据\\xwechat_files。"""
    parent = tmp_path / '我的资料' / '微信数据'
    d = _mkdata(str(parent / 'xwechat_files'))
    assert A.find_xwechat_dirs(extra_roots=[str(parent)]) == d


def test_ignores_dirs_without_wxid(tmp_path):
    """没有 wxid_xxx 子文件夹的目录不能当成数据目录（否则会连到空库）。"""
    empty = tmp_path / '空目录'
    empty.mkdir()
    assert A._is_wechat_data_dir(str(empty)) is False
    assert A._resolve_wechat_data(str(empty)) == ''


def test_missing_paths_are_safe(tmp_path):
    assert A._is_wechat_data_dir(str(tmp_path / '不存在')) is False
    assert A._resolve_wechat_data('') == ''
    assert A._resolve_wechat_data(str(tmp_path / '不存在')) == ''


def test_wechat_configured_dirs_never_throws():
    """读注册表/微信配置失败也不能影响主流程。"""
    got = A._wechat_configured_dirs()
    assert isinstance(got, list)
    for p in got:
        assert isinstance(p, str) and p


def test_documents_dirs_includes_user_documents():
    """「文档」候选至少包含 %USERPROFILE%\\Documents（OneDrive 的重定向另算）。"""
    dirs = A._documents_dirs()
    assert dirs, '文档目录候选不能为空'
    home = os.environ.get('USERPROFILE', '')
    if home:
        assert any(p.lower().startswith(home.lower()) for p in dirs), dirs
