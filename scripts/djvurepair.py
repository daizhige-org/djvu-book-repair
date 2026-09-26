#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修復內容損壞的 bundled DjVu：壞頁、缺頁換成「頁面損壞」佔位頁

    python3 tools/djvurepair.py 壞檔.djvu 修好.djvu --vol 1537 [--work 目錄] [--jobs 4]

做法：
1. 讀 DIRM 目錄（offset 直讀，size/id 在 BZZ 壓縮段，用 djvulibre 的 bzz 解）。
2. 每頁按 `FORM+長度+DJVU` 這 12 位元組的簽名定位：先看 DIRM 記的 offset，
   不對就全檔搜尋（下載時混入垃圾會把後面整段位移）。
3. 找到的頁單獨存成一頁 djvu，用 ddjvu 真的解碼一次；解不出的算壞頁。
4. 壞頁、找不到的頁（截斷）用同尺寸的 PBM 畫「頁面損壞」，cjb2 壓成一頁。
5. djvm -c 依原順序、原 id 重新裝訂；最後 djvused 數頁數、整本再解碼一遍驗證。

輸出 JSON（修好.djvu.repair.json）記錄每頁狀態，方便寫上傳說明。
需要 djvulibre-bin（bzz、ddjvu、cjb2、djvm、djvused）與 Pillow。
"""
import argparse, json, os, re, struct, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

FONT = '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'
FONT_LATIN = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
DEFAULT_WH = (2144, 3024, 300)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def read_dirm(path):
    """→ (nfiles, [(id, offset, size, kind)])，kind: 'page'|'include'|'thumb'"""
    with open(path, 'rb') as h:
        head = h.read(16)
        if head[:4] != b'AT&T' or head[4:8] != b'FORM' or head[12:16] != b'DJVM':
            raise SystemExit('%s 不是 bundled DjVu（DJVM）' % path)
        tag = h.read(4); ln = struct.unpack('>I', h.read(4))[0]
        if tag != b'DIRM':
            raise SystemExit('第一個 chunk 不是 DIRM：%r' % tag)
        dirm = h.read(ln)
    flags = dirm[0]; n = struct.unpack('>H', dirm[1:3])[0]
    if not flags & 0x80:
        raise SystemExit('不是 bundled 檔')
    offs = struct.unpack('>%dI' % n, dirm[3:3 + 4 * n])
    with tempfile.TemporaryDirectory() as td:
        bz = os.path.join(td, 'd.bzz'); out = os.path.join(td, 'd.raw')
        open(bz, 'wb').write(dirm[3 + 4 * n:])
        r = run(['bzz', '-d', bz, out])
        if r.returncode:
            raise SystemExit('bzz 解 DIRM 失敗：%s' % r.stderr.decode(errors='replace'))
        raw = open(out, 'rb').read()
    sizes = [int.from_bytes(raw[3 * i:3 * i + 3], 'big') for i in range(n)]
    fl = raw[3 * n:4 * n]
    p = 4 * n; ids = []
    for i in range(n):
        e = raw.index(b'\0', p); ids.append(raw[p:e].decode('utf-8', 'replace')); p = e + 1
        if fl[i] & 0x80:
            p = raw.index(b'\0', p) + 1
        if fl[i] & 0x40:
            p = raw.index(b'\0', p) + 1
    kinds = {0: 'include', 1: 'page', 2: 'thumb'}
    return n, [(ids[i], offs[i], sizes[i], kinds.get(fl[i] & 0x3f, '?')) for i in range(n)]


def page_info(blob):
    """從一頁的 FORM:DJVU 找 INFO → (w, h, dpi)；沒有就 None"""
    m = re.search(rb'INFO\x00\x00\x00[\x0a-\x14]', blob[:64])
    if not m:
        return None
    d = blob[m.end():m.end() + 10]
    # INFO：寬(2) 高(2) 次版本(1) 主版本(1) dpi(2, little-endian) gamma(1) flags(1)
    w, h = struct.unpack('>HH', d[:4]); dpi = struct.unpack('<H', d[6:8])[0]
    if not (100 < w < 20000 and 100 < h < 20000):
        return None
    if not 25 <= dpi <= 6000:   # 與 djvulibre 同：dpi 不合理就當 300
        dpi = 300
    return w, h, dpi


def signature(entry):
    _, off, size, kind = entry
    sub = b'DJVU' if kind == 'page' else b'DJVI'
    return b'FORM' + struct.pack('>I', size - 8) + sub


def locate(data, entry, prev_end, upper=None):
    """→ (pos, blob) 或 (None, None)。先看 DIRM 的 offset；不對就只在
    [prev_end, upper) 之間搜簽名：頁的先後順序不會變，跑出這個範圍的都是
    恰好同大小的別頁（每冊都有幾對）。"""
    _, off, size, kind = entry
    sig = signature(entry)
    if data[off:off + 12] == sig:
        return off, data[off:off + size]
    cands = []
    p = data.find(sig, prev_end)
    while p != -1 and (upper is None or p + size <= upper):
        cands.append(p); p = data.find(sig, p + 1)
    if not cands:
        return None, None
    best = min(cands, key=lambda c: abs(c - off))
    return best, data[best:best + size]


def decodes(blob, td, name):
    fn = os.path.join(td, name)
    with open(fn, 'wb') as f:
        f.write(b'AT&T' + blob)
    r = run(['ddjvu', '-format=pbm', '-subsample=12', fn, os.devnull])
    ok = r.returncode == 0 and not r.stderr.strip()
    return ok, fn, r.stderr.decode(errors='replace').strip()


def _runs(text):
    """把一行拆成 (是否 CJK, 片段)：Droid Fallback 只有漢字，數字和拉丁字母要換 DejaVu"""
    out = []
    for ch in text:
        cjk = ord(ch) > 0x2E7F
        if out and out[-1][0] == cjk:
            out[-1][1] += ch
        else:
            out.append([cjk, ch])
    return out


def draw_line(dr, text, size, y, w):
    from PIL import ImageFont
    fcjk = ImageFont.truetype(FONT, size)
    flat = ImageFont.truetype(FONT_LATIN, int(size * 0.9))
    parts = [(fcjk if cjk else flat, s) for cjk, s in _runs(text)]
    widths = [dr.textlength(s, font=f) for f, s in parts]
    x = (w - sum(widths)) / 2
    for (f, s), tw in zip(parts, widths):
        dr.text((x, y), s, font=f, fill=0, anchor='ls')
        x += tw
    return size


def make_placeholder(td, name, wh, vol, pageno, reason):
    from PIL import Image, ImageDraw
    w, h, dpi = wh
    im = Image.new('1', (w, h), 1)
    dr = ImageDraw.Draw(im)
    big, small = int(w * 0.16), int(w * 0.035)
    lines = [('頁面損壞', big),
             ('《續修四庫全書》第%04d冊　第 %d 頁' % (vol, pageno), small),
             ('原始掃描檔此頁%s，無法恢復' % reason, small),
             ('This page is damaged in the source scan', small)]
    y = int(h * 0.42)
    for text, size in lines:
        draw_line(dr, text, size, y, w)
        y += int(size * 1.8)
    # 四邊畫框，掃描頁翻到這裡一眼看得出是佔位
    m = int(w * 0.04)
    dr.rectangle([m, m, w - m, h - m], outline=0, width=max(2, w // 400))
    pbm = os.path.join(td, name + '.pbm'); out = os.path.join(td, name)
    im.save(pbm)
    r = run(['cjb2', '-dpi', str(dpi), pbm, out])
    if r.returncode:
        raise SystemExit('cjb2 失敗：%s' % r.stderr.decode(errors='replace'))
    os.unlink(pbm)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('dst')
    ap.add_argument('--vol', type=int, required=True, help='冊號，寫在佔位頁上')
    ap.add_argument('--work', default=None, help='工作目錄（預設 dst 旁的 .work）')
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()

    data = open(a.src, 'rb').read()
    n, entries = read_dirm(a.src)
    if any(k != 'page' for _, _, _, k in entries):
        raise SystemExit('有非頁面元件（共享字典/縮圖），本工具不處理：%r' %
                         [e for e in entries if e[3] != 'page'])
    print('%s：%d 位元組，DIRM %d 頁' % (os.path.basename(a.src), len(data), n))

    td = a.work or (a.dst + '.work')
    os.makedirs(td, exist_ok=True)
    # 先標出仍在 DIRM offset 上的頁（沒被位移的），其他頁只准在相鄰兩個這種頁之間找
    direct = [data[e[1]:e[1] + 12] == signature(e) for e in entries]
    next_direct = [None] * n
    nd = None
    for i in range(n - 1, -1, -1):
        next_direct[i] = nd
        if direct[i]:
            nd = entries[i][1]
    found = []; prev_end = 16
    for i, e in enumerate(entries):
        pos, blob = locate(data, e, prev_end, next_direct[i])
        if pos is not None:
            prev_end = pos + len(blob)
            if len(blob) < e[2]:
                blob = None; pos = 'truncated'
        found.append((pos, blob))

    def test(i):
        pos, blob = found[i]
        if blob is None:
            return i, False, None, 'missing' if pos is None else 'truncated', None
        ok, fn, err = decodes(blob, td, entries[i][0])
        return i, ok, fn, err, page_info(blob)

    results = [None] * n
    with ThreadPoolExecutor(a.jobs) as ex:
        for i, ok, fn, err, wh in ex.map(test, range(n)):
            results[i] = (ok, fn, err, wh)
            if not ok:
                print('  第 %d 頁 %s：%s' % (i + 1, entries[i][0], err.splitlines()[0] if err else '?'))

    # 佔位頁尺寸：壞頁自己的 INFO 讀得到就用，否則用前後最近的好頁
    whs = [r[3] for r in results]
    last = next((w for w in whs if w), DEFAULT_WH)
    report = []; files = []
    for i, (ok, fn, err, wh) in enumerate(results):
        if wh:
            last = wh
        if ok:
            files.append(fn); report.append({'page': i + 1, 'status': 'ok'}); continue
        reason = {'missing': '缺失', 'truncated': '截斷'}.get(err, '損壞')
        size = wh or last
        fn = make_placeholder(td, entries[i][0], size, a.vol, i + 1, reason)
        files.append(fn)
        report.append({'page': i + 1, 'status': 'placeholder', 'reason': reason, 'error': err})
    bad = [r for r in report if r['status'] != 'ok']
    print('壞頁／缺頁 %d 頁：%s' % (len(bad), ','.join(str(r['page']) for r in bad)))

    if os.path.exists(a.dst):
        os.unlink(a.dst)
    r = run(['djvm', '-c', a.dst] + files)
    if r.returncode:
        raise SystemExit('djvm 失敗：%s' % r.stderr.decode(errors='replace'))
    cnt = run(['djvused', a.dst, '-e', 'n']).stdout.decode().strip()
    r = run(['ddjvu', '-format=pbm', '-subsample=12', a.dst, os.devnull])
    verr = r.stderr.decode(errors='replace').strip()
    print('重裝訂 %s：%s 頁（應為 %d），整本解碼 %s' %
          (a.dst, cnt, n, '無錯' if r.returncode == 0 and not verr else '有錯：' + verr[:200]))
    json.dump({'src': a.src, 'pages': n, 'bad': bad}, open(a.dst + '.repair.json', 'w'),
              ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
