# -*- coding: utf-8 -*-
"""会话标签的存储与查询（纯逻辑，不 import tkinter，便于单测）。

为什么不塞进 .ui_settings
-------------------------
`.ui_settings` 是逐行 `key=value`、整份重写、**非原子写**、值里出现裸换行就会把
后面的设置项全部读错位（主题、导出目录跟着丢）。标签数据随会话数增长、还会被用户
随手编辑，风险不值得冒。所以标签单独存一份 JSON：

    <程序目录>\\会话标签.json

并且用「临时文件 + os.replace」原子写：中途崩了/被杀毒拦了，最坏是丢最后一次改动，
不会留下半截文件。

数据结构
--------
    {
      "version": 1,
      "order":    ["常看", "工作"],              # 标签显示顺序（新建时追加）
      "sessions": {"wxid_xxx": ["常看"], ...}    # 会话 → 标签列表
    }

只按 wxid 记录（显示名会变、可能重名；wxid 稳定）。
"""
import json
import os
import re

VERSION = 1
FILE_NAME = '会话标签.json'
MAX_TAG_LEN = 20          # 单个标签字数上限（界面上的 chip 一行要放得下）
MAX_TAGS_PER_SESSION = 6  # 一个会话最多挂几个标签


def clean_tag(name):
    """把用户输入的标签名规范化：去首尾空白、折叠连续空白、限制长度。

    返回 '' 表示这个标签名不可用（空、或全是空白）。
    """
    s = re.sub(r'\s+', ' ', str(name or '')).strip()
    s = s.lstrip('#').strip()          # 允许用户输 "#常看"，存的还是 "常看"
    return s[:MAX_TAG_LEN]


class TagStore:
    """会话标签仓库。所有写操作只改内存，最后调 save() 落盘。"""

    def __init__(self, path):
        self.path = path
        self.order = []          # [标签名]，决定界面显示顺序
        self.sessions = {}       # {wxid: [标签名]}
        self.load_error = ''
        self.load()

    # ── 读写 ──

    def load(self):
        self.order, self.sessions, self.load_error = [], {}, ''
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            # 读不出来就当作空仓库，但把原因留着给界面提示（不要把文件删掉，
            # 用户可能还想手工抢救）
            self.load_error = str(e)
            return
        if not isinstance(data, dict):
            self.load_error = '文件内容不是对象'
            return
        raw_sessions = data.get('sessions') or {}
        if isinstance(raw_sessions, dict):
            for wxid, tags in raw_sessions.items():
                # 只认列表/元组：手工编辑成字符串时按字符拆开会得到一堆乱标签
                if not isinstance(tags, (list, tuple)):
                    continue
                ws = [clean_tag(t) for t in tags if clean_tag(t)]
                if ws:
                    self.sessions[str(wxid)] = ws[:MAX_TAGS_PER_SESSION]
        raw_order = data.get('order') or []
        order = ([clean_tag(t) for t in raw_order if clean_tag(t)]
                 if isinstance(raw_order, (list, tuple)) else [])
        # order 里没列到、但会话里用到的标签也要补上（防手工编辑漏写 order）
        seen = set(order)
        for tags in self.sessions.values():
            for t in tags:
                if t not in seen:
                    seen.add(t)
                    order.append(t)
        self.order = order

    def save(self):
        """原子写。失败返回 False（调用方决定要不要提示用户）。"""
        data = {'version': VERSION, 'order': list(self.order),
                'sessions': {k: list(v) for k, v in self.sessions.items() if v}}
        tmp = f'{self.path}.tmp'
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    # ── 查询 ──

    def all_tags(self):
        """按显示顺序返回全部标签。"""
        return list(self.order)

    def tags_of(self, wxid):
        return list(self.sessions.get(str(wxid), []))

    def has_tag(self, wxid, tag):
        return tag in self.sessions.get(str(wxid), [])

    def counts(self):
        """标签 → 会话数。"""
        c = {t: 0 for t in self.order}
        for tags in self.sessions.values():
            for t in tags:
                c[t] = c.get(t, 0) + 1
        return c

    def wxids_with(self, tag):
        """带该标签的 wxid 列表（顺序 = sessions 里的插入顺序，调用方自己排序）。"""
        if not tag:
            return []
        return [w for w, tags in self.sessions.items() if tag in tags]

    def is_empty(self):
        return not self.order and not self.sessions

    # ── 写入 ──

    def assign(self, wxids, tag):
        """把标签贴到一批会话上（已有该标签的不重复）。返回实际新增的会话数。"""
        t = clean_tag(tag)
        if not t or not wxids:
            return 0
        if t not in self.order:
            self.order.append(t)
        added = 0
        for w in wxids:
            w = str(w)
            cur = self.sessions.setdefault(w, [])
            if t not in cur and len(cur) < MAX_TAGS_PER_SESSION:
                cur.append(t)
                added += 1
        return added

    def untag(self, wxids, tag):
        """把标签从一批会话上摘掉；某会话没标签了就删掉这个键。"""
        t = clean_tag(tag)
        if not t:
            return
        for w in wxids:
            w = str(w)
            if w in self.sessions and t in self.sessions[w]:
                self.sessions[w] = [x for x in self.sessions[w] if x != t]
                if not self.sessions[w]:
                    del self.sessions[w]

    def clear(self, wxids):
        """清掉这批会话的全部标签。"""
        for w in wxids:
            self.sessions.pop(str(w), None)

    def delete_tag(self, tag):
        """删掉一个标签（从所有会话和 order 里移除）。返回受影响的会话数。"""
        t = clean_tag(tag)
        if not t:
            return 0
        n = 0
        for w in list(self.sessions):
            if t in self.sessions[w]:
                self.sessions[w] = [x for x in self.sessions[w] if x != t]
                n += 1
                if not self.sessions[w]:
                    del self.sessions[w]
        if t in self.order:
            self.order = [x for x in self.order if x != t]
        return n

    def rename_tag(self, old, new):
        """改名。new 已存在时合并到 new。返回是否成功。"""
        o, n = clean_tag(old), clean_tag(new)
        if not o or not n or o == n:
            return False
        for w in self.sessions:
            if o in self.sessions[w]:
                rest = [x for x in self.sessions[w] if x != o]
                if n not in rest and len(rest) < MAX_TAGS_PER_SESSION:
                    rest.append(n)
                self.sessions[w] = rest
        if o in self.order:
            if n not in self.order:
                self.order[self.order.index(o)] = n
            else:
                self.order = [x for x in self.order if x != o]
        elif n not in self.order:
            self.order.append(n)
        return True

    def prune(self, alive_wxids):
        """丢掉已经不在数据库里的会话的标签，防止文件无限膨胀。返回删掉的条数。"""
        alive = {str(w) for w in alive_wxids}
        dead = [w for w in self.sessions if w not in alive]
        for w in dead:
            del self.sessions[w]
        # 顺手清掉没人用的标签
        used = {t for tags in self.sessions.values() for t in tags}
        self.order = [t for t in self.order if t in used]
        return len(dead)
