#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二遍：在拉直後的頁面上，找每個框現在的四角（邊已近直線），把該框所在橫帶
用透視變換拉成標準矩形。上框帶 y∈[0,1592)，下框帶 y∈[1592,3024)。
用法: refine.py 輸入目錄(v4) 輸出目錄 [IMG_xxxx_S ...]"""
import cv2, numpy as np, sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photo2page import binarize, write_pbm, border_segments, border_lines

W, H = 2144, 3024
X0, X1 = 280, 1950
YF = [(490, 1520), (1665, 2700)]
SPLIT = 1592
INK = 150

def long_lines(ink):
    k = lambda w, h: cv2.getStructuringElement(cv2.MORPH_RECT, (w, h))
    hl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(100, 1))
    vl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(1, 100))
    return hl, vl

def fit_edge(mask, pts_axis, lo, hi, horizontal):
    """在 mask（線圖）裡，取 [lo,hi) 範圍內的線像素擬合一條直線 → (a, b)：
    橫線 y = a*x + b；直線 x = a*y + b。回 None 表示點太少"""
    ys, xs = np.nonzero(mask[lo:hi] if horizontal else mask[:, lo:hi])
    if horizontal:
        ys = ys + lo
        if len(xs) < 200: return None
        keep = np.ones(len(xs), bool)
        for tol in (30, 12, 5):
            a, b = np.polyfit(xs[keep], ys[keep], 1)
            r = np.abs(a * xs + b - ys); nk = r < tol
            if nk.sum() < 200: break
            keep = nk
        return np.polyfit(xs[keep], ys[keep], 1)
    else:
        xs = xs + lo
        if len(ys) < 200: return None
        keep = np.ones(len(ys), bool)
        for tol in (30, 12, 5):
            a, b = np.polyfit(ys[keep], xs[keep], 1)
            r = np.abs(a * ys + b - xs); nk = r < tol
            if nk.sum() < 200: break
            keep = nk
        return np.polyfit(ys[keep], xs[keep], 1)

def groups_of(idx, gap=20):
    out=[]
    for i in idx:
        if out and i - out[-1][-1] <= gap: out[-1].append(i)
        else: out.append([i])
    return [(g[0], g[-1]) for g in out]

def pick_cc(lm, x0, x1, y0, y1, target, horizontal, fracs, full):
    """在 lm 的窗 [y0:y1, x0:x1] 內找線的連通元件：長度（橫線看寬、直線看高）≥ frac*full 的
    元件中，取中心離 target 最近者，對其像素擬合直線 → (a,b)；找不到 None。斜線也是一整個元件"""
    win = np.zeros_like(lm); win[y0:y1, x0:x1] = lm[y0:y1, x0:x1]
    n, lab, st, cen = cv2.connectedComponentsWithStats(win)
    for frac in fracs:
        cands = []
        for i in range(1, n):
            L = st[i, cv2.CC_STAT_WIDTH] if horizontal else st[i, cv2.CC_STAT_HEIGHT]
            if L >= frac * full:
                c = cen[i][1] if horizontal else cen[i][0]
                cands.append((abs(c - target), i))
        if cands:
            i = min(cands)[1]
            ys, xs = np.nonzero(lab == i)
            return np.polyfit(xs, ys, 1) if horizontal else np.polyfit(ys, xs, 1)
    return None

def frame_quad(ink, yt, yb, k):
    """第 k 個框：所屬橫帶內，離標準位置最近的長橫線當上下邊、長直線當左右邊；
    找不到的邊用標準位置。→ (四角, 找到幾邊, 缺邊)"""
    hl, vl = long_lines(ink)
    lo, hi = (0, SPLIT) if k == 0 else (SPLIT, H)
    mid = (yt + yb) // 2
    top = pick_cc(hl, X0 - 120, X1 + 120, lo, mid, yt, True, (0.8, 0.5), X1 - X0)
    bot = pick_cc(hl, X0 - 120, X1 + 120, mid, hi, yb, True, (0.8, 0.5), X1 - X0)
    miss = []
    if top is None: top = np.array([0.0, yt]); miss.append('top')
    if bot is None: bot = np.array([0.0, yb]); miss.append('bot')
    ya, yb2 = int(top[1] + top[0] * W / 2), int(bot[1] + bot[0] * W / 2)
    left = pick_cc(vl, 0, X0 + 300, ya + 30, yb2 - 30, X0, False, (0.5, 0.3), yb2 - ya)
    right = pick_cc(vl, X1 - 300, W, ya + 30, yb2 - 30, X1, False, (0.5, 0.3), yb2 - ya)
    if left is None: left = np.array([0.0, X0]); miss.append('left')
    if right is None: right = np.array([0.0, X1]); miss.append('right')
    def cross(h, v):
        a1, b1 = h; a2, b2 = v
        y = (a1 * b2 + b1) / (1 - a1 * a2); x = a2 * y + b2
        return [x, y]
    q = np.array([cross(top, left), cross(top, right), cross(bot, right), cross(bot, left)], np.float32)
    return q, 4 - len(miss), miss

def refine(path_png, out_dir):
    base = os.path.basename(path_png)[:-4]
    g = cv2.imread(path_png, 0)
    ink = (g < INK).astype(np.uint8) * 255
    res = np.full_like(g, 255)
    info = []
    for k, (yt, yb) in enumerate(YF):
        lo, hi = (0, SPLIT) if k == 0 else (SPLIT, H)
        canon = np.array([[X0, yt], [X1, yt], [X1, yb], [X0, yb]], np.float32)
        quad, found, miss = frame_quad(ink, yt, yb, k)
        d0 = np.abs(quad - canon).max()
        if found < 2 or d0 > 400:
            res[lo:hi] = g[lo:hi]; info.append('框%d 不動(找到%d邊,偏差%.0f)' % (k + 1, found, d0)); continue
        Hm = cv2.getPerspectiveTransform(quad, canon)
        warped = cv2.warpPerspective(g, Hm, (W, H), flags=cv2.INTER_CUBIC, borderValue=255)
        q2, f2, m2 = frame_quad((warped < INK).astype(np.uint8) * 255, yt, yb, k)
        d1 = np.abs(q2 - canon).max()
        if d1 > max(8, d0):
            res[lo:hi] = g[lo:hi]; info.append('框%d 修後更差(%.0f→%.0f)退回' % (k + 1, d0, d1)); continue
        res[lo:hi] = warped[lo:hi]
        info.append('框%d %.0f→%.0f%s' % (k + 1, d0, d1, ('缺' + '/'.join(miss)) if miss else ''))
    cv2.imwrite(os.path.join(out_dir, base + '.png'), res)
    bw = binarize(res)
    bw[:60] = 255; bw[-100:] = 255; bw[:, :40] = 255; bw[:, -40:] = 255
    write_pbm(os.path.join(out_dir, base + '.pbm'), bw)
    print(base, ' '.join(info))

if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]; os.makedirs(dst, exist_ok=True)
    names = sys.argv[3:] or sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(src, 'IMG_*_?.png')))
    for n in names:
        refine(os.path.join(src, n + '.png'), dst)
