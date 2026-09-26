#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HEIC → JPEG（套用 EXIF 方向）＋ 縮圖；印出尺寸、方向、拍攝時間、機型
    python3 heic2jpg.py 來源目錄 輸出目錄
需要 venv 裡的 pillow、pillow-heif"""
import sys, os, glob
from PIL import Image, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
for f in sorted(glob.glob(os.path.join(src, '*.HEIC')) + glob.glob(os.path.join(src, '*.heic'))):
    im = Image.open(f)
    ex = im.getexif()
    o = ex.get(274)
    im = ImageOps.exif_transpose(im)
    base = os.path.splitext(os.path.basename(f))[0]
    im.save(os.path.join(dst, base + '.jpg'), quality=92)
    th = im.copy(); th.thumbnail((900, 900))
    th.save(os.path.join(dst, base + '_s.jpg'), quality=80)
    dt = ex.get(306); model = ex.get(272)
    print(base, im.size, 'orient', o, dt, model)
