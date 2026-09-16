// 图片解密助手 — 从 wx_key.dll 获取 code → 推导 AES Key → 解密 + WXGF 剥壳
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { execSync, execFileSync } = require('child_process');

const SCRIPT_DIR = __dirname;
const PROJECT_DIR = path.resolve(SCRIPT_DIR, '..');

// 资源路径: 优先本地打包资源，其次 WeFlow 安装目录
const NATIVE_CANDIDATES = [
    path.join(PROJECT_DIR, 'resources', 'native', 'weflow-image-native-win32-x64.node'),
    'C:/Users/OK/AppData/Local/Programs/WeFlow/resources/resources/wedecrypt/win32/x64/weflow-image-native-win32-x64.node',
];
const DLL_CANDIDATES = [
    path.join(PROJECT_DIR, 'dll', 'wx_key.dll'),
    path.join(PROJECT_DIR, 'APP', 'WeChatExport', 'dll', 'wx_key.dll'),
    'C:/Users/OK/AppData/Local/Programs/WeFlow/resources/resources/key/win32/x64/wx_key.dll',
];
const FFMPEG_CANDIDATES = [
    path.join(PROJECT_DIR, 'resources', 'bin', 'ffmpeg.exe'),
    'C:/Users/OK/AppData/Local/Programs/WeFlow/resources/app.asar.unpacked/node_modules/ffmpeg-static/ffmpeg.exe',
];

const filepath = process.argv[2];
const BATCH = process.argv[2] === '--batch';

// ---------- 批量模式：一个 node 进程解一批图 ----------
// 为什么要有它：旧实现是"一张图起一个 node 进程"，每次都重新 require koffi、
// 重新加载原生模块、重新 load(wx_key.dll) 并调 GetImageKey 取 code。
// 用户实测：400 张图要十几分钟，且每个进程都是一次孤儿进程风险。
// 批量模式把这些**与图片无关的一次性开销只付一次**。
//
// 输入（stdin，一行 JSON）：{"dataDir": "...", "paths": ["a.dat", ...]}
// 输出（stdout，每行一个 JSON）：{"path":"...","ok":true,"ext":"jpg","b64":"..."}
if (BATCH) {
    (function runBatch() {
    let raw = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (c) => { raw += c; });
    process.stdin.on('end', () => {
        let req = {};
        try { req = JSON.parse(raw || '{}'); } catch (e) { req = {}; }
        let native = null, koffi = null;
        try {
            const np = NATIVE_CANDIDATES.find(p => fs.existsSync(p));
            if (np) native = require(np);
        } catch (e) {}
        try {
            koffi = require(path.join(SCRIPT_DIR, 'node_modules', 'koffi'));
        } catch (e) {}
        if (!native) {
            process.stdout.write(JSON.stringify({ fatal: 'native 模块加载失败' }) + '\n');
            process.exit(2);
        }

        // ★ 取**全部**账号，而不是只用第一个。
        //   事故（issue #2）：旧代码写死 accounts[0]，用户登录过多个微信账号时，
        //   另一个账号的图片会全部解不出来。正确做法是**按图片路径里的 wxid 匹配账号**。
        let accounts = [];
        try {
            if (koffi) {
                const dllPath = DLL_CANDIDATES.find(p => fs.existsSync(p));
                if (dllPath) {
                    const lib = koffi.load(dllPath);
                    const fn = lib.func('bool GetImageKey(char* buf, int size)');
                    const buf = Buffer.alloc(8192);
                    if (fn(buf, buf.length)) {
                        const d = JSON.parse(buf.toString('utf-8').replace(/\0/g, '').trim());
                        accounts = (d && d.accounts) || [];
                    }
                }
            }
        } catch (e) {}

        // 账号查找：先按路径里的 wxid 精确匹配，再按前缀匹配，最后退回第一个
        function pickAccount(fp) {
            let want = '';
            const m = fp.match(/(wxid_[a-z0-9]+)/i);
            if (m) {
                const parts = m[1].split('_');
                want = parts.length >= 3 ? parts.slice(0, 2).join('_') : m[1];
            }
            if (want) {
                for (const a of accounts) {
                    if (a && a.wxid === want) return { acc: a, how: 'exact' };
                }
                for (const a of accounts) {
                    if (a && a.wxid && (a.wxid.startsWith(want) || want.startsWith(a.wxid))) {
                        return { acc: a, how: 'prefix' };
                    }
                }
            }
            return { acc: accounts[0] || null, how: want ? 'fallback' : 'nosid' };
        }

        const out = [];
        // 账号匹配统计：让"哪个账号没匹配上"一眼可见（issue #2 就是匹配问题）
        const stat = { exact: 0, prefix: 0, fallback: 0, nosid: 0, noaccount: 0 };
        for (const fp of (req.paths || [])) {
            const rec = { path: fp, ok: false };
            try {
                if (!fp || !fs.existsSync(fp)) { rec.err = 'not-found'; out.push(rec); continue; }
                const picked = pickAccount(fp);
                const acc = picked.acc;
                rec.match = picked.how;
                if (stat[picked.how] != null) stat[picked.how] += 1;
                if (!acc) { stat.noaccount += 1; rec.err = 'no-account'; out.push(rec); continue; }
                const key = (acc.keys && acc.keys[0]) || {};
                const code = key.code || 0;
                const sid = acc.wxid || 'unknown';
                const md5 = crypto.createHash('md5').update(String(code) + sid).digest('hex');
                const xorKey = key.xorKey != null ? key.xorKey : (code & 0xFF);
                const aesKey = key.aesKey || md5.substring(0, 16);
                const r = native.decryptDatNative(fp, xorKey, aesKey);
                if (!r || !r.data) { rec.err = 'decrypt-failed'; out.push(rec); continue; }
                let data = Buffer.isBuffer(r.data) ? r.data : Buffer.from(r.data);
                const isWxgf = r.isWxgf || r.is_wxgf || data.slice(0, 4).toString() === 'wxgf';
                if (isWxgf) {
                    let found = false;
                    for (let i = 4; i < Math.min(data.length - 12, 4096); i++) {
                        if (data[i] === 0xFF && data[i+1] === 0xD8 && data[i+2] === 0xFF) {
                            data = data.slice(i); found = true; break;
                        }
                        if (data[i] === 0x89 && data[i+1] === 0x50 && data[i+2] === 0x4E) {
                            data = data.slice(i); found = true; break;
                        }
                    }
                    if (!found) {
                        const ffmpeg = FFMPEG_CANDIDATES.find(p => fs.existsSync(p));
                        if (ffmpeg) {
                            const tmpRaw = path.join(require('os').tmpdir(), 'wx_decode_raw_' + Date.now() + '_' + out.length + '.hevc');
                            const tmpOut = path.join(require('os').tmpdir(), 'wx_decode_out_' + Date.now() + '_' + out.length + '.jpg');
                            try {
                                fs.writeFileSync(tmpRaw, data);
                                execFileSync(ffmpeg, ['-y', '-i', tmpRaw, '-update', '1', '-q:v', '2', tmpOut], {timeout: 30000, stdio: 'ignore'});
                                if (fs.existsSync(tmpOut) && fs.statSync(tmpOut).size > 1000) {
                                    data = fs.readFileSync(tmpOut);
                                }
                            } catch (e) {}
                            try { fs.unlinkSync(tmpRaw); } catch (e) {}
                            try { fs.unlinkSync(tmpOut); } catch (e) {}
                        }
                    }
                }
                let ext = (r.ext || '').replace(/^\./, '');
                if (!ext) {
                    if (data[0] === 0xFF && data[1] === 0xD8) ext = 'jpg';
                    else if (data[0] === 0x89 && data[1] === 0x50) ext = 'png';
                    else if (data[0] === 0x47 && data[1] === 0x49) ext = 'gif';
                    else if (data[0] === 0x52 && data[1] === 0x49) ext = 'webp';
                }
                if (!ext) { rec.err = 'no-ext'; out.push(rec); continue; }
                rec.ok = true;
                rec.ext = ext;
                rec.sid = sid;
                rec.b64 = Buffer.isBuffer(data) ? data.toString('base64') : Buffer.from(data).toString('base64');
            } catch (e) {
                rec.err = String((e && e.message) || e).slice(0, 120);
            }
            out.push(rec);
        }
        for (const rec of out) {
            process.stdout.write(JSON.stringify(rec) + '\n');
        }
        // 汇总行（Python 侧会忽略没有 path 的行）：账号匹配 + 账号总数
        process.stdout.write(JSON.stringify({
            summary: true, accounts: accounts.length, stat: stat,
            note: (stat.fallback > 0
                ? '有 ' + stat.fallback + ' 张图没能在路径里匹配到账号，已退回第一个账号 —— '
                  + '若是多账号环境，这些图可能解不出来'
                : '')
        }) + '\n');
        process.exit(0);
    });
    })();
    // 批量模式到此结束，不再走下面的单文件逻辑
} else {
if (!filepath || !fs.existsSync(filepath)) process.exit(1);

let native = null, koffi = null;
try {
    const np = NATIVE_CANDIDATES.find(p => fs.existsSync(p));
    if (np) native = require(np);
} catch(e) {}
try {
    koffi = require(path.join(SCRIPT_DIR, 'node_modules', 'koffi'));
} catch(e) {}

if (!native) process.exit(2);

try {
    // 1. 从 wx_key.dll 获取 code
    let code = 0, wxid = 'unknown';
    if (koffi) {
        const dllPath = DLL_CANDIDATES.find(p => fs.existsSync(p));
        if (dllPath) {
            try {
                const lib = koffi.load(dllPath);
                const fn = lib.func('bool GetImageKey(char* buf, int size)');
                const buf = Buffer.alloc(8192);
                if (fn(buf, buf.length)) {
                    const d = JSON.parse(buf.toString('utf-8').replace(/\0/g, '').trim());
                    if (d.accounts && d.accounts[0]) {
                        code = d.accounts[0].keys?.[0]?.code || 0;
                        wxid = d.accounts[0].wxid || 'unknown';
                    }
                }
            } catch(e) {}
        }
    }

    // 2. 从路径提取 wxid
    const wxidMatch = filepath.match(/(wxid_[a-z0-9]+)/i);
    if (wxidMatch) {
        const raw = wxidMatch[1];
        const parts = raw.split('_');
        wxid = parts.length >= 3 ? parts.slice(0, 2).join('_') : raw;
    }

    // 3. deriveImageKeys
    const md5 = crypto.createHash('md5').update(String(code) + wxid).digest('hex');
    const xorKey = code & 0xFF;
    const aesKey = md5.substring(0, 16);

    // 4. 解密
    const r = native.decryptDatNative(filepath, xorKey, aesKey);
    if (!r || !r.data) process.exit(3);

    let data = Buffer.isBuffer(r.data) ? r.data : Buffer.from(r.data);
    const isWxgf = r.isWxgf || r.is_wxgf || data.slice(0, 4).toString() === 'wxgf';

    // 5. WXGF 剥壳
    if (isWxgf) {
        let found = false;
        for (let i = 4; i < Math.min(data.length - 12, 4096); i++) {
            if (data[i] === 0xFF && data[i+1] === 0xD8 && data[i+2] === 0xFF) {
                data = data.slice(i); found = true; break;
            }
            if (data[i] === 0x89 && data[i+1] === 0x50 && data[i+2] === 0x4E) {
                data = data.slice(i); found = true; break;
            }
        }

        if (!found) {
            const ffmpeg = FFMPEG_CANDIDATES.find(p => fs.existsSync(p));
            if (ffmpeg) {
                const tmpRaw = path.join(require('os').tmpdir(), 'wx_decode_raw_' + Date.now() + '.hevc');
                const tmpOut = path.join(require('os').tmpdir(), 'wx_decode_out_' + Date.now() + '.jpg');
                try {
                    fs.writeFileSync(tmpRaw, data);
                    execFileSync(ffmpeg, ['-y', '-i', tmpRaw, '-update', '1', '-q:v', '2', tmpOut], {timeout: 30000, stdio: 'ignore'});
                    if (fs.existsSync(tmpOut) && fs.statSync(tmpOut).size > 1000) {
                        data = fs.readFileSync(tmpOut);
                    }
                } catch(e) {}
                try { fs.unlinkSync(tmpRaw); } catch(e) {}
                try { fs.unlinkSync(tmpOut); } catch(e) {}
            }
        }
    }

    // 6. 检测格式
    let ext = (r.ext || '').replace(/^\./, '');
    if (!ext) {
        if (data[0] === 0xFF && data[1] === 0xD8) ext = 'jpg';
        else if (data[0] === 0x89 && data[1] === 0x50) ext = 'png';
        else if (data[0] === 0x47 && data[1] === 0x49) ext = 'gif';
        else if (data[0] === 0x52 && data[1] === 0x49) ext = 'webp';
    }

    if (ext) {
        const buf = Buffer.isBuffer(data) ? data : Buffer.from(data);
        process.stdout.write(ext + '\n' + buf.toString('base64'));
        process.exit(0);
    }
} catch(e) {}

process.exit(4);
}
