#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把拉直好的二值頁（PBM）壓成 JB2 單頁 DjVu，換掉 bundled DjVu 裡的指定頁，整本重新解碼驗證

    python3 splice_pages.py 原.djvu 新.djvu --first-page 527 --pages 頁面目錄/IMG_6497_R.pbm 頁面目錄/IMG_6497_L.pbm ...
    python3 splice_pages.py 原.djvu 新.djvu --first-page 527 --glob '頁面目錄/IMG_*_?.pbm' --order RL

--first-page：第一張要換的 DjVu 頁序（1 起算）。--pages 按順序一頁一頁對應 first-page、first-page+1、…。
--glob + --order：按檔名排序後，每張照片先 R 再 L（跨頁照片右頁是偶數頁碼、左頁是奇數頁碼時用 RL）。
--limit N：只換前 N 頁（壞頁比照片頁少時用）。
每頁先量墨量，全白（<0.5%）會警告——通常是遮罩把頁抹掉了，不要傳。
"""
import argparse, glob, os, re, subprocess, sys, tempfile, shutil, json

def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise SystemExit('%s 失敗：%s' % (cmd[0], r.stderr.decode(errors='replace')[:300]))
    return r

def ink_fraction(pbm):
    with open(pbm, 'rb') as f:
        assert f.read(2) == b'P4'
        dims = b''
        while True:
            c = f.read(1)
            if c.isspace() and dims.count(b' ') >= 1 and dims.strip():
                break
            dims += c
        w, h = map(int, dims.split())
        data = f.read()
    black = sum(bin(b).count('1') for b in data)
    return black / (w * h)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('dst')
    ap.add_argument('--first-page', type=int, required=True)
    ap.add_argument('--pages', nargs='*', default=[])
    ap.add_argument('--glob'); ap.add_argument('--order', default='RL')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dpi', default='300')
    a = ap.parse_args()
    pages = list(a.pages)
    if a.glob:
        by_photo = {}
        for p in sorted(glob.glob(a.glob)):
            m = re.match(r'(.*)_([LR])\.pbm$', os.path.basename(p))
            if not m: continue
            by_photo.setdefault(m.group(1), {})[m.group(2)] = p
        for k in sorted(by_photo):
            for s in a.order:
                if s in by_photo[k]: pages.append(by_photo[k][s])
    if a.limit: pages = pages[:a.limit]
    if not pages: raise SystemExit('沒有頁面')
    n = int(run(['djvused', a.src, '-e', 'n']).stdout.decode().strip())
    last = a.first_page + len(pages) - 1
    if last > n: raise SystemExit('要換到第 %d 頁，但檔案只有 %d 頁' % (last, n))
    shutil.copyfile(a.src, a.dst)
    td = tempfile.mkdtemp(prefix='splice_')
    log = []
    for i, pbm in enumerate(pages):
        idx = a.first_page + i
        frac = ink_fraction(pbm)
        if frac < 0.005:
            print('警告：%s 幾乎全白（墨量 %.4f），第 %d 頁' % (os.path.basename(pbm), frac, idx))
        one = os.path.join(td, '%08d.djvu' % idx)
        run(['cjb2', '-dpi', a.dpi, '-clean', pbm, one])
        run(['djvm', '-d', a.dst, str(idx)])
        run(['djvm', '-i', a.dst, one, str(idx)])
        log.append({'page': idx, 'source': os.path.basename(pbm), 'ink': round(frac, 4)})
    shutil.rmtree(td)
    n2 = int(run(['djvused', a.dst, '-e', 'n']).stdout.decode().strip())
    r = subprocess.run(['ddjvu', '-format=pbm', '-subsample=12', a.dst, os.devnull], capture_output=True)
    err = r.stderr.decode(errors='replace').strip()
    print('換了 %d 頁（DjVu %d–%d），%s：%d 頁，整本解碼 %s' % (len(pages), a.first_page, last, a.dst, n2,
          '無錯' if r.returncode == 0 and not err else '有錯：' + err[:200]))
    json.dump(log, open(a.dst + '.splice.json', 'w'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
