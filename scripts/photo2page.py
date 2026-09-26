#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""實體書跨頁照片 → 拉直的單頁二值圖，用來補 DjVu 的壞頁

    python3 tools/photo2page.py 輸出目錄 IMG_6497.jpg IMG_6498.jpg ...

輸入：手機側拍的跨頁（先用 tools/heic2jpg.py 轉 JPEG，EXIF 方向已套用）；
      預設照片要逆時針轉 90° 才是正的（書橫放、手機直拍）。
做法（v2，2026-09-25）：
1. 背景均一化、二值成墨圖；用長核開運算取橫線圖／直線圖（120 核直接開運算，
   與去掉粗塊後 50 核開運算取聯集），連通元件過濾掉太細（界欄）與太粗（陰影）的。
2. 直邊聚成候選左右邊，配對成左右兩頁：以「跨度 ≥ 0.6 倍框寬的橫邊組兩端是否落在
   左右邊上」評分；橫邊配成「上框＋間隔＋下框」的結構（同頁兩框高相近）。
3. 每個框從粗位置向外側逐行／逐列搜尋真正的框線，剔除離群點後擬合上下邊三次曲線、
   左右邊直線；某邊找不到或擬合太差就向同頁另一框借；單框頁另一框以平移合成。
4. 每頁按框內相對位置 remap 到 2144×3024（300 dpi，版框標準位置量自原 DjVu），
   兩框各自映成標準矩形，因此上下框對齊；紙張遮罩（以框為種子的低飽和連通區）與
   書溝遮罩去掉桌面、對頁；自適應二值化。
輸出：IMG_xxxx_L/R.png（灰階）、.pbm（二值，自己 packbits 寫 P4；cv2 寫 PBM 會全白）、
      _s.jpg（縮圖）、_dbg.jpg（紅＝上下邊曲線、綠＝左右邊）。
之後 cjb2 -dpi 300 -clean 壓成單頁 DjVu，djvm -d/-i 換掉佔位頁。
第二遍 tools/photo2page_refine.py：在拉直後的頁面上量每個框的四角（離標準位置最近的長線），
把該框所在橫帶用透視變換拉成標準矩形，修後再量一次、變差就退回。
需要 scratch venv 裡的 opencv-python-headless、numpy、scipy。
用於第 1477 冊冊內 522–662 頁（DjVu 527–667）全部 141 頁。
"""
import cv2, numpy as np, sys, os
from scipy.signal import find_peaks

W, H = 2144, 3024
X0, X1 = 280, 1950
YF = [(490, 1520), (1665, 2700)]
INK = 150

def normalize(gray, k=61):
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), 15)
    n = gray.astype(np.float32) / np.maximum(bg.astype(np.float32), 1) * 235
    return np.clip(n, 0, 255).astype(np.uint8)

def paper_mask(col, seeds=()):
    hsv = cv2.cvtColor(col, cv2.COLOR_BGR2HSV)
    m = (hsv[:, :, 1] < 70).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    labels = {int(lab[int(y), int(x)]) for x, y in seeds} - {0}
    if not labels:
        labels = {1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))}
    m = np.isin(lab, list(labels)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (61, 61)))
    ff = m.copy(); hh, ww = m.shape
    fm = np.zeros((hh + 2, ww + 2), np.uint8)
    for pt in ((0, 0), (ww - 1, 0), (0, hh - 1), (ww - 1, hh - 1)):
        cv2.floodFill(ff, fm, pt, 255)
    m[ff == 0] = 255
    return cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))

def line_maps(ink):
    """長橫線圖、長直線圖：120 核直接開運算（貼字的框線不會被切碎）與
    去掉粗塊後 50 核開運算（彎得厲害的框線）兩種結果取聯集；粗陰影、細界欄之後用粗細過濾"""
    k = lambda w, h: cv2.getStructuringElement(cv2.MORPH_RECT, (w, h))
    hl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(120, 1))
    thin_h = cv2.subtract(ink, cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(1, 40)))
    hl = cv2.bitwise_or(hl, cv2.morphologyEx(thin_h, cv2.MORPH_OPEN, k(50, 1)))
    vl = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(1, 120))
    thin_v = cv2.subtract(ink, cv2.morphologyEx(ink, cv2.MORPH_OPEN, k(40, 1)))
    vl = cv2.bitwise_or(vl, cv2.morphologyEx(thin_v, cv2.MORPH_OPEN, k(1, 50)))
    return hl, vl

def strong_peaks(profile, frac, merge, min_dist):
    p = cv2.GaussianBlur(profile.astype(np.float32).reshape(-1, 1), (0, 0), 6).ravel()
    if p.max() <= 0:
        return []
    pk, _ = find_peaks(p, height=frac * p.max(), distance=min_dist)
    out = []
    for x in pk:
        if out and x - out[-1][-1] < merge:
            out[-1].append(x)
        else:
            out.append([x])
    return [int(np.mean(g)) for g in out]

def robust_fit(xs, ys, deg, tol=6):
    xs = np.asarray(xs, float); ys = np.asarray(ys, float)
    if len(xs) < deg + 3:
        return np.poly1d([0.0] * deg + [float(np.median(ys)) if len(ys) else 0.0]), float('inf'), 0.0
    keep = np.ones(len(xs), bool)
    for t in (60, 30, 15, 8, tol):
        p = np.poly1d(np.polyfit(xs[keep], ys[keep], deg))
        nk = np.abs(p(xs) - ys) < t
        if nk.sum() >= max(deg + 3, len(xs) * 0.15):
            keep = nk
    p = np.poly1d(np.polyfit(xs[keep], ys[keep], deg))
    rms = float(np.sqrt(np.mean((p(xs[keep]) - ys[keep]) ** 2)))
    return p, rms, keep.mean()

def nearest_run(v, center, n=5):
    """一維 0/1 陣列裡長度 ≥ n 的 1 段中，起點離 center 最近者的起點；沒有回 -1"""
    best = -1; run = 0
    for i, b in enumerate(v):
        run = run + 1 if b else 0
        if run == n:
            s = i - n + 1
            if best < 0 or abs(s - center) < abs(best - center):
                best = s
    return best

def first_run(v, n=5):
    """一維 0/1 陣列裡第一段長度 ≥ n 的 1 的起點；沒有回 -1"""
    run = 0
    for i, b in enumerate(v):
        run = run + 1 if b else 0
        if run >= n:
            return i - n + 1
    return -1

class Frame:
    """top(x) bot(x) 三次曲線；left(y) right(y) 直線；xl xr yt yb 粗位置"""
    def __init__(self, ink, xl, xr, yt, yb, band=120):
        self.xl, self.xr, self.yt, self.yb = xl, xr, yt, yb
        self.cx, self.cy = (xl + xr) / 2, (yt + yb) / 2
        h, w = ink.shape
        xs, xs2, tops, bots = [], [], [], []
        for x in range(xl + 20, xr - 20, 3):
            a = max(0, yt - band); col = ink[a:yt + band, x]
            i = first_run(col)
            b0 = max(0, yb - band); col2 = ink[b0:min(h, yb + band), x][::-1]
            j = first_run(col2)
            if i >= 0:
                xs.append(x); tops.append(a + i)
            if j >= 0:
                xs2.append(x); bots.append(min(h, yb + band) - 1 - j)
        self.top, r1, k1 = robust_fit(xs, tops, 3)
        self.bot, r2, k2 = robust_fit(xs2, bots, 3)
        ys, ys2, ls, rs = [], [], [], []
        for y in range(yt + 20, yb - 20, 3):
            a = max(0, xl - band); row = ink[y, a:xl + band]
            i = nearest_run(row, xl - a)
            b0 = max(0, xr - band); row2 = ink[y, b0:min(w, xr + band)][::-1]
            j = nearest_run(row2, min(w, xr + band) - 1 - xr)
            if i >= 0:
                ys.append(y); ls.append(a + i)
            if j >= 0:
                ys2.append(y); rs.append(min(w, xr + band) - 1 - j)
        self.left, r3, k3 = robust_fit(ys, ls, 1)
        self.right, r4, k4 = robust_fit(ys2, rs, 1)
        self.r1, self.r2, self.r3, self.r4 = r1, r2, r3, r4
        self.resid = max(x for x in (r1, r2, r3, r4) if np.isfinite(x)) if any(np.isfinite(x) for x in (r1, r2, r3, r4)) else float('inf'); self.cover = min(k1, k2, k3, k4)
        self.n = (len(xs), len(xs2), len(ys), len(ys2))
        self.missing = [k for k, r in (('top', r1), ('bot', r2)) if not np.isfinite(r)] + \
                       [k for k, r in (('left', r3), ('right', r4)) if not (np.isfinite(r) and r < 10)]

    @staticmethod
    def shifted(f, dy):
        g = Frame.__new__(Frame)
        g.xl, g.xr, g.yt, g.yb = f.xl, f.xr, f.yt + dy, f.yb + dy
        g.cx, g.cy = f.cx, f.cy + dy
        g.top = f.top + dy; g.bot = f.bot + dy; g.left = f.left; g.right = f.right
        g.resid = f.resid; g.cover = f.cover; g.n = f.n
        return g

    def T(self, x): return self.top(np.clip(x, self.xl, self.xr))
    def B(self, x): return self.bot(np.clip(x, self.xl, self.xr))

def page_maps(f1, f2):
    X, Y = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    u = (X - X0) / (X1 - X0)
    (a1, b1), (a2, b2) = YF
    # 左右邊：各框在自己的 y 範圍用自己的直線，中間漸變；y 先用粗估
    wgt = np.clip((Y - b1) / (a2 - b1), 0, 1)
    # 直線是 y 的函數：用兩輪迭代
    ys = np.full_like(X, 0.0)
    for _ in range(2):
        L = f1.left(ys) * (1 - wgt) + f2.left(ys) * wgt
        R = f1.right(ys) * (1 - wgt) + f2.right(ys) * wgt
        xs = L * (1 - u) + R * u
        t1 = f1.T(xs); b1s = f1.B(xs); t2 = f2.T(xs); b2s = f2.B(xs)
        ys = np.empty_like(xs)
        seg1 = Y < b1; v = (Y - a1) / (b1 - a1); ys[seg1] = (t1 + (b1s - t1) * v)[seg1]
        seg3 = Y >= a2; v = (Y - a2) / (b2 - a2); ys[seg3] = (t2 + (b2s - t2) * v)[seg3]
        seg2 = ~seg1 & ~seg3; v = (Y - b1) / (a2 - b1); ys[seg2] = (b1s + (t2 - b1s) * v)[seg2]
    return xs.astype(np.float32), ys.astype(np.float32)

def binarize(page):
    bw = cv2.adaptiveThreshold(page, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 25)
    n, lab, st, _ = cv2.connectedComponentsWithStats(255 - bw, connectivity=8)
    small = np.isin(lab, np.where(st[:, cv2.CC_STAT_AREA] < 12)[0])
    bw[small] = 255
    return bw

def write_pbm(path, bw):
    bits = np.packbits((bw < 128).astype(np.uint8), axis=1)
    with open(path, 'wb') as f:
        f.write(b'P4\n%d %d\n' % (bw.shape[1], bw.shape[0])); f.write(bits.tobytes())

def cluster(vals, gap):
    vals = sorted(vals); out = []
    for v in vals:
        if out and v - out[-1][-1] < gap:
            out[-1].append(v)
        else:
            out.append([v])
    return [float(np.mean(g)) for g in out]

def border_segments(lm, horizontal, min_len, thick=(4, 35)):
    """線圖 → 粗而長的線段 [(位置, 起, 迄)]：橫線給 (y, x0, x1)，直線給 (x, y0, y1)"""
    n, lab, st, cen = cv2.connectedComponentsWithStats(lm)
    out = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        L = w if horizontal else h
        if L < min_len:
            continue
        t = a / L
        if not thick[0] <= t <= thick[1]:
            continue
        out.append((cen[i][1], x, x + w) if horizontal else (cen[i][0], y, y + h))
    return out

def border_lines(segs, gap, min_cover):
    """線段 [(pos, s, e)] 依 pos 聚組，合計覆蓋長度 ≥ min_cover 的組 → [(pos, s, e)]"""
    segs = sorted(segs); groups = []
    for sg in segs:
        if groups and sg[0] - np.mean([g[0] for g in groups[-1]]) < gap:
            groups[-1].append(sg)
        else:
            groups.append([sg])
    out = []
    for g in groups:
        iv = sorted((s, e) for _, s, e in g); cov = 0; cur = None
        for s, e in iv:
            if cur is None or s > cur[1]:
                if cur: cov += cur[1] - cur[0]
                cur = [s, e]
            else:
                cur[1] = max(cur[1], e)
        cov += cur[1] - cur[0]
        if cov >= min_cover:
            out.append((float(np.mean([x[0] for x in g])), min(s for _, s, _ in g), max(e for _, _, e in g)))
    return out

def locate(norm):
    """→ {'L': [(xl,xr,yt,yb),...], 'R': [...]}, gx, 訊息"""
    ink = (norm < INK).astype(np.uint8) * 255
    hl, vl = line_maps(ink)
    h, w = ink.shape
    vlines = border_lines(border_segments(vl, False, 150), 60, 0.2 * h)
    cols = [v[0] for v in vlines]
    msg = ['直邊 %s' % [int(c) for c in cols]]
    hs = border_segments(hl, True, 150)
    cand = [(i, j) for i in range(len(cols)) for j in range(i + 1, len(cols)) if 0.22 * w < cols[j] - cols[i] < 0.5 * w]
    if not cand:
        return None, None, msg + ['找不到框的左右邊']
    def pair_score(i, j):
        """候選左右邊：範圍內的橫線段聚成橫邊組，只看跨度 ≥ 0.6 倍框寬的組（真框邊），
        分數 = 兩端命中（±60）數 − 平均端點誤差/50；沒有長橫邊組就 -inf"""
        xl, xr = cols[i], cols[j]; fw = xr - xl
        segs = [s for s in hs if xl - 60 < (s[1] + s[2]) / 2 < xr + 60]
        groups = [g for g in border_lines(segs, 60, 0.35 * fw) if g[2] - g[1] >= 0.6 * fw]
        if not groups:
            return -1e9
        hits = sum((abs(s - xl) < 60) + (abs(e - xr) < 60) for _, s, e in groups)
        mis = np.mean([abs(s - xl) + abs(e - xr) for _, s, e in groups])
        return hits - mis / 50
    sc = {c: pair_score(*c) for c in cand}
    msg.append('配對分 %s' % {(int(cols[i]), int(cols[j])): int(s) for (i, j), s in sc.items() if s > -1e8})
    best = None
    for p in cand:
        for q in cand:
            if p[1] < q[0] and sc[p] > -1e8 and sc[q] > -1e8:
                d1 = cols[p[1]] - cols[p[0]]; d2 = cols[q[1]] - cols[q[0]]
                if abs(d1 - d2) > 0.25 * max(d1, d2):
                    continue
                if best is None or sc[p] + sc[q] > best[0]:
                    best = (sc[p] + sc[q], [p, q])
    if best is None:
        p = max(cand, key=lambda c: sc[c]); best = (sc[p], [p])
    pairs = [(int(cols[i]), int(cols[j])) for i, j in best[1]]
    if len(pairs) == 2:
        sides = {'L': pairs[0], 'R': pairs[1]}; gx = (pairs[0][1] + pairs[1][0]) / 2
    else:
        p = pairs[0]; side = 'L' if (p[0] + p[1]) / 2 < w / 2 else 'R'
        sides = {side: p}; gx = p[1] + 0.1 * (p[1] - p[0]) if side == 'L' else p[0] - 0.1 * (p[1] - p[0])
    msg.append('選邊 %s' % sides)
    frames = {'L': [], 'R': []}; fw_of = {}
    for side, (xl, xr) in sides.items():
        fw = xr - xl
        segs = [s for s in hs if xl - 60 < (s[1] + s[2]) / 2 < xr + 60]
        rows = [r[0] for r in border_lines(segs, 60, 0.35 * fw)]
        msg.append('%s 橫邊 %s' % (side, [int(r) for r in rows]))
        ref = None   # 另一頁已知框高（按寬度比例換算）當單框時的參考
        for o in frames.values():
            for (_, _, yt, yb) in o:
                ref = (yb - yt) / fw_of[id(o)] * fw
        best = None
        n = len(rows)
        RATIO = 0.62   # 版框高/寬（原 DjVu 量得 1030/1670）
        for i in range(n):
            for j in range(i + 1, n):
                h1 = rows[j] - rows[i]
                if not 0.45 * fw < h1 < 0.95 * fw:
                    continue
                for k in range(j + 1, n):
                    gap = rows[k] - rows[j]
                    if not 0.02 * fw < gap < 0.3 * fw:
                        continue
                    for l in range(k + 1, n):
                        h2 = rows[l] - rows[k]
                        if not 0.45 * fw < h2 < 0.95 * fw or abs(h1 - h2) > 0.15 * max(h1, h2):
                            continue
                        score = (abs(h1 - h2) / fw + abs(gap - 0.09 * fw) / fw
                                 + abs(h1 / fw - RATIO) + abs(h2 / fw - RATIO))
                        if best is None or score < best[0]:
                            best = (score, (i, j, k, l))
        if best:
            i, j, k, l = best[1]
            frames[side] += [(xl, xr, int(rows[i]), int(rows[j])), (xl, xr, int(rows[k]), int(rows[l]))]
        else:
            target = ref or RATIO * fw
            single = [(abs(rows[j] - rows[i] - target), i, j) for i in range(n) for j in range(i + 1, n)
                      if 0.45 * fw < rows[j] - rows[i] < 0.95 * fw]
            if single:
                _, i, j = min(single)
                frames[side].append((xl, xr, int(rows[i]), int(rows[j])))
        fw_of[id(frames[side])] = fw
    return frames, gx, msg

def process(path, out, rot=cv2.ROTATE_90_COUNTERCLOCKWISE):
    base = os.path.splitext(os.path.basename(path))[0]
    col = cv2.rotate(cv2.imread(path), rot)
    im = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY)
    norm = normalize(im)
    ink = (norm < INK).astype(np.uint8)
    rough, gx, msg = locate(norm)
    if rough is None:
        print(base, '失敗：', ' | '.join(msg)); return None
    frames = {'L': [], 'R': []}
    for s in rough:
        for r in rough[s]:
            f = Frame(ink, *r)
            ok = [x for x in (f.r1, f.r2) if np.isfinite(x)]
            if len(f.missing) <= 1 and ok and max(ok) < 10 and 'top' not in f.missing and 'bot' not in f.missing:
                frames[s].append(f)
            else:
                msg.append('%s 框 %s 丟掉：各邊 %.1f/%.1f/%.1f/%.1f 缺 %s n %s' % (s, r, f.r1, f.r2, f.r3, f.r4, f.missing, f.n))
    for s in frames:
        for f in frames[s]:
            if f.missing:
                sib = [g for g in frames[s] if g is not f and not g.missing]
                if sib:
                    g = sib[0]
                    if 'left' in f.missing: f.left = g.left
                    if 'right' in f.missing: f.right = g.right
                    msg.append('%s 框缺 %s，向另一框借' % (s, f.missing))
                else:
                    if 'left' in f.missing: f.left = np.poly1d([float(f.xl)])
                    if 'right' in f.missing: f.right = np.poly1d([float(f.xr)])
                    msg.append('%s 框缺 %s，用粗位置直線' % (s, f.missing))
    allf = frames['L'] + frames['R']
    paper = paper_mask(col, [(f.cx, f.cy) for f in allf])
    dbg = cv2.cvtColor(cv2.resize(norm, None, fx=0.25, fy=0.25), cv2.COLOR_GRAY2BGR)
    info = []
    for side in 'LR':
        fr = sorted(frames[side], key=lambda f: f.cy)
        if len(fr) > 2:
            fr = sorted(fr, key=lambda f: f.resid)[:2]; fr.sort(key=lambda f: f.cy)
        if not fr:
            blank = np.full((H, W), 255, np.uint8)
            cv2.imwrite(os.path.join(out, '%s_%s.png' % (base, side)), blank)
            write_pbm(os.path.join(out, '%s_%s.pbm' % (base, side)), blank)
            info.append(side + ':blank'); continue
        for f in fr:
            xs = np.arange(f.xl, f.xr, 8)
            for fn in (f.T, f.B):
                pts = np.stack([xs, fn(xs)], 1) * 0.25
                cv2.polylines(dbg, [pts.astype(np.int32)], False, (0, 0, 255), 2)
            ys = np.arange(f.yt, f.yb, 8)
            for fn in (f.left, f.right):
                pts = np.stack([fn(ys), ys], 1) * 0.25
                cv2.polylines(dbg, [pts.astype(np.int32)], False, (0, 255, 0), 2)
        if len(fr) == 1:
            f = fr[0]; (a1, b1), (a2, b2) = YF
            fh = f.yb - f.yt; shift = fh * (a2 - a1) / (b1 - a1)
            other = frames['R' if side == 'L' else 'L']
            if other:
                upper = min(abs(f.cy - o.cy) for o in other if o.cy <= min(x.cy for x in other) + 1) < fh * 0.5
            else:
                upper = f.cy < norm.shape[0] / 2
            g = Frame.shifted(f, shift if upper else -shift)
            fr = [f, g] if upper else [g, f]
            info.append('%s:%s(resid %.1f)' % (side, 'top-only' if upper else 'bottom-only', f.resid))
        else:
            info.append('%s:2(resid %.1f/%.1f)' % (side, fr[0].resid, fr[1].resid))
        f1, f2 = fr
        mx, my = page_maps(f1, f2)
        page = cv2.remap(norm, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        pm = cv2.remap(paper, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        page[pm == 0] = 255
        page[(mx > gx) if side == 'L' else (mx < gx)] = 255
        page[:60, :] = 255; page[-100:, :] = 255; page[:, :40] = 255; page[:, -40:] = 255
        cv2.imwrite(os.path.join(out, '%s_%s.png' % (base, side)), page)
        bw = binarize(page)
        write_pbm(os.path.join(out, '%s_%s.pbm' % (base, side)), bw)
        cv2.imwrite(os.path.join(out, '%s_%s_s.jpg' % (base, side)), cv2.resize(bw, (536, 756), interpolation=cv2.INTER_AREA))
    cv2.imwrite(os.path.join(out, base + '_dbg.jpg'), dbg, [cv2.IMWRITE_JPEG_QUALITY, 70])
    print(base, ' '.join(info), '|', ' | '.join(msg))
    return True

if __name__ == '__main__':
    out = sys.argv[1]; os.makedirs(out, exist_ok=True)
    for p in sys.argv[2:]:
        process(p, out)
