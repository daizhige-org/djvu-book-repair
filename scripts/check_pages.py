#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核對拉直後的頁面：墨量（抓全白頁）、總覽縮圖、頁碼角落拼圖（核對連號）、指定頁大圖

    python3 check_pages.py 頁面目錄 輸出目錄 [--order RL] [--pages IMG_6499_R IMG_6507_R ...]

輸出：overview_*.png（每頁 120×169，一列 12 頁，按照片序、每張先 R 後 L）、
      numbers.png（每頁左下與右下 300×400 的角落，看書眉頁碼是否連號）、
      sheet.png（--pages 指定的頁 400×564 並排，帶檔名）。
低於 3% 墨量的頁列印出來。需要 venv 的 opencv、numpy。"""
import cv2, numpy as np, sys, os, glob, re, argparse

def load_pages(d, order):
    by = {}
    for p in sorted(glob.glob(os.path.join(d, 'IMG_*_?.pbm'))):
        m = re.match(r'(.*)_([LR])\.pbm$', os.path.basename(p))
        by.setdefault(m.group(1), {})[m.group(2)] = p
    out = []
    for k in sorted(by):
        for s in order:
            if s in by[k]: out.append((k + '_' + s, by[k][s]))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('out'); ap.add_argument('--order', default='RL')
    ap.add_argument('--pages', nargs='*', default=[])
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    pages = load_pages(a.src, a.order)
    tiles, corners = [], []
    print('墨量 < 3% 的頁：')
    for name, p in pages:
        im = cv2.imread(p, 0)
        frac = (im < 128).mean()
        if frac < 0.03: print('  %s %.3f' % (name, frac))
        tiles.append(cv2.resize(im, (120, 169), interpolation=cv2.INTER_AREA))
        h, w = im.shape
        c = np.hstack([im[h - 400:, :300], np.full((400, 8), 128, np.uint8), im[h - 400:, w - 300:]])
        c = cv2.resize(c, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        cv2.putText(c, name[-6:], (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 0, 2)
        corners.append(c)
    while len(tiles) % 12: tiles.append(np.full((169, 120), 255, np.uint8))
    rows = [np.hstack(tiles[i:i + 12]) for i in range(0, len(tiles), 12)]
    for k in range(0, len(rows), 6):
        cv2.imwrite(os.path.join(a.out, 'overview_%d.png' % (k // 6)), np.vstack(rows[k:k + 6]))
    while len(corners) % 8: corners.append(np.full(corners[0].shape, 255, np.uint8))
    rows = [np.hstack(corners[i:i + 8]) for i in range(0, len(corners), 8)]
    for k in range(0, len(rows), 4):
        cv2.imwrite(os.path.join(a.out, 'numbers_%d.png' % (k // 4)), np.vstack(rows[k:k + 4]))
    if a.pages:
        t = []
        for n in a.pages:
            im = cv2.resize(cv2.imread(os.path.join(a.src, n + '.pbm'), 0), (400, 564), interpolation=cv2.INTER_AREA)
            cv2.putText(im, n, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2); t.append(im)
        cv2.imwrite(os.path.join(a.out, 'sheet.png'), np.hstack(t))
    print('共 %d 頁，輸出在 %s' % (len(pages), a.out))

if __name__ == '__main__':
    main()
