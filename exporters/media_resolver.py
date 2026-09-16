# -*- coding: utf-8 -*-
"""媒体解析编排器"""
import os, base64
from packed_info_parser import parse_image_info
from image_decoder import decrypt_dat
from logger import info as log_info, error as log_error


class MediaResolver:
    def __init__(self, wcdb_client, cache_dir: str, xor_key: int = 0, aes_key: str = "",
                 data_dir: str = '', log_func=None):
        self.wcdb = wcdb_client
        self.cache_dir = cache_dir
        self.xor_key = xor_key
        self.aes_key = aes_key
        self.data_dir = data_dir
        self.log = log_func or (lambda msg: None)
        # 批量原生解密的缓存：{dat_path: (ext, b64)}。命中就完全不起 node 进程。
        self._native_cache = {}
        # 已经对哪些目录做过批量解密（避免同一目录反复批处理）
        self._native_batch_tried = set()

    def resolve_images(self, messages: list, session_wxid: str, try_native: bool = True) -> list:
        enriched = []
        img_count = 0
        for msg in messages:
            lt = int(msg.get('local_type', 0))
            if lt == 3:
                img_count += 1
                ext, b64 = self._resolve_one(msg, session_wxid, try_native)
                if b64:
                    msg = dict(msg)
                    msg['image_data'] = b64
                    msg['image_ext'] = ext
            enriched.append(msg)
        ok = sum(1 for m in enriched if m.get('image_data',''))
        self.log(f"图片: {img_count} 张, 成功 {ok} 张")
        log_info("图片", f"{img_count} 张, 成功 {ok} 张")
        return enriched

    def _resolve_one(self, msg: dict, session_wxid: str, try_native: bool = True) -> tuple:
        try:
            info = parse_image_info(msg)
            md5 = info.get('md5', '') or ''
            alt_md5 = info.get('alt_md5', '') or ''
            aeskey = info.get('aeskey', '') or ''
            dat_name = info.get('dat_name', '') or ''
            if not md5 and not dat_name:
                self.log(f"图片: 无 md5/dat_name")
                return ('', '')

            self.log(f"图片: packed_md5={'有' if md5 else '无'}, xml_md5={'有' if alt_md5 else '无'}, aeskey={'有' if aeskey else '无'}")

            # 获取 table_hash (用于目录搜索)
            table_name = msg.get('table_name', '') or ''
            table_hash = table_name.replace('Msg_', '') if table_name.startswith('Msg_') else ''

            dat_path = self._find_dat(md5, dat_name, table_hash, msg.get('create_time', ''))
            # 如果主 md5 没找到, 尝试 alt_md5 (来自 XML)
            if not dat_path and alt_md5:
                self.log(f"图片: 尝试备选 md5={alt_md5}")
                dat_path = self._find_dat(alt_md5, alt_md5 + '.dat', table_hash, msg.get('create_time', ''))
            if not dat_path:
                self.log(f"图片: 未找到 .dat 文件")
                return ('', '')

            self.log(f"图片: 找到 {os.path.basename(dat_path)} ({os.path.getsize(dat_path)}B)")

            use_aes = aeskey if aeskey else self.aes_key
            try:
                ext, img_bytes = decrypt_dat(dat_path, self.xor_key, use_aes)
            except Exception:
                ext, img_bytes = '', b''

            if ext and img_bytes:
                dat_base = os.path.splitext(os.path.basename(dat_path))[0].lower()
                for s in ['_t', '_h', '_b', '_c', '_w', '_l']:
                    if dat_base.endswith(s):
                        dat_base = dat_base[:-len(s)]
                        break
                cache_root = os.path.join(self.cache_dir, 'Images', session_wxid)
                os.makedirs(cache_root, exist_ok=True)
                cp = os.path.join(cache_root, f'{dat_base}.{ext}')
                with open(cp, 'wb') as fh: fh.write(img_bytes)
                b64 = base64.b64encode(img_bytes).decode('ascii')
                self.log(f"图片: 解密成功 {ext} {len(img_bytes)}B")
                return (ext, b64)

            # 解密失败: 尝试 Rust 原生模块 (PDF 导出时跳过，避免 ffmpeg 卡死)
            if try_native:
                self.log(f"图片: 尝试 Rust 原生模块解密")
                ext, b64 = self._decrypt_with_native(dat_path)
            else:
                self.log(f"图片: 跳过原生模块 (PDF 模式)")
                ext, b64 = '', ''
            if ext and b64:
                dat_base = os.path.splitext(os.path.basename(dat_path))[0].lower()
                for s in ['_t', '_h', '_b']:
                    if dat_base.endswith(s):
                        dat_base = dat_base[:-len(s)]
                        break
                cache_root = os.path.join(self.cache_dir, 'Images', session_wxid)
                os.makedirs(cache_root, exist_ok=True)
                cp = os.path.join(cache_root, f'{dat_base}.{ext}')
                with open(cp, 'wb') as fh:
                    fh.write(base64.b64decode(b64))
                self.log(f"图片: 原生模块解密成功 {ext}")
                return (ext, b64)

            # 解密失败: 尝试缓存缩略图
            self.log(f"图片: 尝试缓存缩略图匹配")
            ts = msg.get('create_time', '')
            thumb = self._find_cached_thumb(self.data_dir, ts)
            if thumb:
                self.log(f"图片: 找到缓存缩略图 {os.path.basename(thumb)} ({os.path.getsize(thumb)}B)")
                with open(thumb, 'rb') as fh:
                    img_bytes = fh.read()
                b64 = base64.b64encode(img_bytes).decode('ascii')
                return ('jpg', b64)
            return ('', '')
        except Exception as e:
            self.log(f"图片: 异常 {e}")
            log_error("图片", str(e))
            return ('', '')

    def _find_dat(self, md5: str, dat_name: str, table_hash: str, create_time: str) -> str:
        base = os.path.splitext(dat_name or md5 or '')[0].lower()

        # 1. WCDB API
        if md5:
            try:
                r = self.wcdb.resolve_image(md5)
                self.log(f"图片: WCDB resolve_image({md5[:12]}...) 返回 {type(r).__name__}: {str(r)[:200]}")
                for p in [r.get('path','') if isinstance(r,dict) else '',
                          r[0].get('path','') if isinstance(r,list) and len(r)>0 and isinstance(r[0],dict) else '']:
                    if p and os.path.exists(p):
                        self.log(f"图片: WCDB 返回的路径存在: {p}")
                        return p
            except Exception as e:
                self.log(f"图片: WCDB resolve_image 异常: {e}")

        # 2. 文件系统: 微信 v4 路径 msg/attach/{hash}/{date}/Img/{md5}.dat
        if self.data_dir and base:
            path = self._search_v4(self.data_dir, base, table_hash, create_time)
            if path: return path

            # 3. 兜底: 递归搜索整个 account 目录
            path = self._search_bruteforce(self.data_dir, base)
            if path: return path

        return ''

    def _search_v4(self, data_dir: str, name_base: str, table_hash: str, create_time: str) -> str:
        """按微信 v4 目录结构搜索"""
        account_dirs = []
        if os.path.basename(data_dir).startswith('wxid_'):
            account_dirs = [data_dir]
        elif os.path.isdir(data_dir):
            try:
                for e in os.listdir(data_dir):
                    if e.startswith('wxid_') and os.path.isdir(os.path.join(data_dir, e)):
                        account_dirs.append(os.path.join(data_dir, e))
            except: pass

        for ad in account_dirs:
            attach_dir = os.path.join(ad, 'msg', 'attach')
            if not os.path.isdir(attach_dir):
                self.log(f"图片: {attach_dir} 不存在")
                continue

            self.log(f"图片: 搜索目录 {attach_dir}")

            if table_hash:
                hash_dir = os.path.join(attach_dir, table_hash)
                self.log(f"图片: 搜索 table_hash={table_hash} -> {hash_dir}")
                if os.path.isdir(hash_dir):
                    p = self._find_in_attach(hash_dir, name_base)
                    if p: return p
                else:
                    self.log(f"图片: {hash_dir} 不存在")

            # 兜底: 搜所有子目录
            try:
                for h in os.listdir(attach_dir):
                    if len(h) == 32:
                        self.log(f"图片: 尝试 hash={h}")
                        p = self._find_in_attach(os.path.join(attach_dir, h), name_base)
                        if p: return p
            except: pass

        return ''

    def _find_in_attach(self, hash_dir: str, name_base: str) -> str:
        try:
            for month_dir in os.listdir(hash_dir):
                img_dir = os.path.join(hash_dir, month_dir, 'Img')
                if not os.path.isdir(img_dir):
                    continue

                # 只匹配 name_base 的文件，按优先级: HD > 完整文件 > 缩略图
                hd_path = full_path = thumb_path = None
                for f in os.listdir(img_dir):
                    fl = f.lower()
                    if not fl.endswith('.dat') or name_base not in fl[:-4]:
                        continue
                    f_base = fl[:-4]
                    if any(f_base.endswith(s) for s in ['_b', '_w', '_l', '_c']):
                        continue
                    fp = os.path.join(img_dir, f)
                    # 按后缀判断，兼容 _h.dat 和 .dat (无后缀=完整文件)
                    if f_base.endswith('_h'):
                        hd_path = fp
                    elif f_base.endswith('_t'):
                        thumb_path = fp
                    elif len(f_base) == 32 or not any(f_base.endswith(s) for s in ['_h','_t']):
                        full_path = fp
                    else:
                        full_path = fp  # 兜底

                best = hd_path or full_path or thumb_path
                if best:
                    self.log(f"图片: 匹配 {os.path.basename(best)} ({os.path.getsize(best)}B)")
                    return best
        except: pass
        return ''

    def _decrypt_with_native(self, filepath: str) -> tuple:
        """解密 .dat 文件（Rust 原生模块，经 node）。

        ⚠️ 2026-09-15 改成**批量**：旧实现是"一张图起一个 node 进程"，每次都重新
        require koffi、重新加载原生模块、重新 load(wx_key.dll) 取 code —— 用户实测
        400 张图要十几分钟。现在首次调用会把这个目录（及其子目录）里的 .dat **一次
        批处理掉**，之后全部命中缓存；一次 node 调用只付一次启动开销。
        另一个好处：不再一图一进程，孤儿进程风险一起消掉。
        """
        # 1) 先查批量缓存
        try:
            hit = self._native_cache.get(filepath)
        except Exception:
            hit = None
        if hit is not None:
            return hit
        # 2) 没命中 → 对这个目录做一次批量解密，再查
        if not getattr(self, '_native_batch_tried', None):
            self._native_batch_tried = set()
        try:
            folder = os.path.dirname(filepath)
            if folder and folder not in self._native_batch_tried:
                self._native_batch_tried.add(folder)
                self._decrypt_batch_in_dir(folder)
                hit = self._native_cache.get(filepath)
                if hit is not None:
                    return hit
        except Exception:
            pass
        # 3) 批量没覆盖到（例如文件不在那个目录）→ 退回单文件一次
        try:
            return self._decrypt_one_with_native(filepath)
        except Exception:
            return ('', '')

    def _decrypt_batch_in_dir(self, folder: str, limit: int = 300) -> int:
        """把这个目录（含子目录，最多往下 3 层）里的 .dat 一次性交给 node 批处理。

        为什么限制范围/数量：微信数据目录里 .dat 可能上万，全量批处理会拖慢首次导出。
        实测同目录的图片通常在同一批里，取 300 张做"预热"收益最大、代价可控；
        超出部分会走后面的单文件兜底。
        返回成功解出的张数。
        """
        import subprocess, json as _json
        node = self._find_node()
        if not node:
            return 0
        scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        helper = os.path.join(scripts_dir, 'decrypt_image.js')
        if not os.path.exists(helper):
            return 0
        paths = []
        base_depth = folder.rstrip('\\/').count(os.sep)
        try:
            for dp, dns, fns in os.walk(folder):
                if dp.count(os.sep) - base_depth >= 3:
                    dns[:] = []          # 不再往下钻
                for fn in fns:
                    if fn.lower().endswith('.dat'):
                        paths.append(os.path.join(dp, fn))
                        if len(paths) >= limit:
                            break
                if len(paths) >= limit:
                    break
        except OSError:
            return 0
        if not paths:
            return 0
        self.log(f"图片: 批量原生解密 {len(paths)} 个 .dat（1 个 node 进程）")
        try:
            req = _json.dumps({'dataDir': self.data_dir, 'paths': paths}).encode('utf-8')
            startupinfo = None
            if hasattr(subprocess, 'STARTUPINFO'):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0      # SW_HIDE
            # 超时给足：每张 2 秒 + 底 180 秒（个别图要走 ffmpeg 抽帧，会慢）
            timeout = max(180, len(paths) * 2)
            r = subprocess.run([node, helper, '--batch'],
                               input=req, capture_output=True, timeout=timeout,
                               cwd=scripts_dir, startupinfo=startupinfo,
                               creationflags=0x08000000 if os.name == 'nt' else 0)
            ok_n = 0
            for line in (r.stdout or b'').decode('utf-8', 'replace').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                if rec.get('summary'):
                    # 汇总行：账号匹配统计（issue #2 排查用）
                    st = rec.get('stat') or {}
                    self.log("图片: 账号匹配 精确=%s 前缀=%s 回退=%s 无账号=%s（DLL 里 %s 个账号）%s"
                             % (st.get('exact', 0), st.get('prefix', 0),
                                st.get('fallback', 0), st.get('noaccount', 0),
                                rec.get('accounts'), rec.get('note') or ''))
                    continue
                p = rec.get('path') or ''
                if not p:
                    continue
                if rec.get('ok') and rec.get('ext') and rec.get('b64'):
                    self._native_cache[p] = (rec['ext'], rec['b64'])
                    ok_n += 1
                else:
                    # 记成空，避免同一张图反复重试
                    self._native_cache.setdefault(p, ('', ''))
            self.log(f"图片: 批量原生解密完成，成功 {ok_n}/{len(paths)}")
            return ok_n
        except Exception as e:  # noqa: BLE001
            self.log(f"图片: 批量原生解密失败（{type(e).__name__}: {e}）")
            return 0

    def _find_node(self) -> str:
        """找 node/runtime。包内优先自带的 runtime\\node.exe。"""
        import shutil
        base = os.path.join(os.path.dirname(__file__), '..')
        for p in (os.path.join(base, 'runtime', 'node.exe'),
                  os.path.join(base, 'APP', 'WeChatExport', 'runtime', 'node.exe')):
            if os.path.exists(p):
                return p
        return shutil.which('node') or shutil.which('node.exe') or ''

    def _decrypt_one_with_native(self, filepath: str) -> tuple:
        """单文件兜底（批量用不上时才走这里）。"""
        import subprocess
        node = self._find_node()
        if not node:
            return ('', '')
        scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        helper = os.path.join(scripts_dir, 'decrypt_image.js')
        if not os.path.exists(helper):
            return ('', '')
        try:
            startupinfo = None
            if hasattr(subprocess, 'STARTUPINFO'):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
            result = subprocess.run([node, helper, filepath],
                capture_output=True, timeout=5, cwd=scripts_dir,
                startupinfo=startupinfo, creationflags=0x08000000 if os.name == 'nt' else 0)
            if result.returncode == 0 and result.stdout:
                lines = result.stdout.decode('utf-8', errors='replace').strip().split('\n', 1)
                if len(lines) == 2:
                    ext, b64 = lines[0].strip(), lines[1].strip()
                    if ext and b64:
                        return (ext, b64)
        except Exception:
            pass
        return ('', '')

    def _find_cached_thumb(self, data_dir: str, create_time: str) -> str:
        """搜索所有可能的缓存缩略图位置"""
        if not data_dir or not create_time:
            return ''
        try:
            msg_ts = int(create_time)
            import datetime
            msg_dt = datetime.datetime.fromtimestamp(msg_ts)

            # 搜索范围: 前后2个月
            months = []
            for offset in [-1, 0, 1]:
                m = msg_dt.month + offset
                y = msg_dt.year
                if m < 1: m, y = 12, y-1
                if m > 12: m, y = 1, y+1
                months.append(f'{y}-{m:02d}')

            best = ''
            best_diff = 86400  # 24小时

            for month in months:
                # cache/{month}/Message/{hash}/Thumb/
                cache_root = os.path.join(data_dir, 'cache', month, 'Message')
                if not os.path.isdir(cache_root):
                    continue
                for h in os.listdir(cache_root):
                    thumb_dir = os.path.join(cache_root, h, 'Thumb')
                    if not os.path.isdir(thumb_dir):
                        continue
                    for f in os.listdir(thumb_dir):
                        if not (f.endswith('_thumb.jpg') or f.endswith('.jpg') or f.endswith('.png')):
                            continue
                        parts = f.split('_')
                        if len(parts) >= 2:
                            try:
                                thumb_ts = int(parts[1])
                                diff = abs(thumb_ts - msg_ts)
                                if diff < best_diff:
                                    best_diff = diff
                                    best = os.path.join(thumb_dir, f)
                            except: pass

            # 同时搜索 cache/{month}/Sns/Video/ 下的 jpg
            for month in months:
                sns_root = os.path.join(data_dir, 'cache', month, 'Sns', 'Video')
                if os.path.isdir(sns_root):
                    for sub in os.listdir(sns_root):
                        sub_dir = os.path.join(sns_root, sub)
                        if os.path.isdir(sub_dir):
                            for f in os.listdir(sub_dir):
                                if f.endswith('.jpg') or f.endswith('.png'):
                                    fp = os.path.join(sub_dir, f)
                                    mtime = os.path.getmtime(fp)
                                    diff = abs(mtime - msg_ts)
                                    if diff < best_diff:
                                        best_diff = diff
                                        best = fp

            return best if best_diff < 43200 else ''  # 12小时内
        except: pass
        return ''

    def _search_bruteforce(self, data_dir: str, name_base: str) -> str:
        """兜底: 递归搜索整个 account 目录"""
        account_dirs = []
        if os.path.basename(data_dir).startswith('wxid_'):
            account_dirs = [data_dir]
        elif os.path.isdir(data_dir):
            try:
                for e in os.listdir(data_dir):
                    if e.startswith('wxid_'):
                        account_dirs.append(os.path.join(data_dir, e))
            except: pass

        for ad in account_dirs:
            for root, dirs, files in os.walk(ad):
                # 限制搜索深度
                if root.replace(ad, '').count(os.sep) > 6:
                    dirs.clear(); continue
                for f in files:
                    fl = f.lower()
                    if fl.endswith('.dat') and name_base in fl[:-4]:
                        return os.path.join(root, f)
        return ''
