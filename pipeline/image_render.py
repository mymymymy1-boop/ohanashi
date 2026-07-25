# -*- coding: utf-8 -*-
"""
プログラム描画エンジン — AIに数えさせないための機械合成。

- render_dots(n):   数字カード(kazu_N) → 試験標準のドットカード(黒丸N個)
- compose_count(base_png, n, pair): 物×N のカード。1個だけのAI生成画像を
  N個(ペア指定時は2個1組×N組)並べて合成する。個数は構造上100%正確。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

CANVAS = 1024
MARGIN = 90


def _rows_for(n: int) -> list[int]:
    """幼児が数えやすい段組(1段5個まで)。"""
    if n <= 4:
        return [n]
    if n <= 8:
        top = (n + 1) // 2
        return [top, n - top]
    return [5, 5, n - 10] if n > 10 else [5, n - 5]


def render_dots(n: int, out_path: Path):
    """黒丸N個のドットカード(小学校受験の数量表示の標準形)。"""
    img = Image.new("RGB", (CANVAS, CANVAS), "white")
    d = ImageDraw.Draw(img)
    rows = _rows_for(n)
    max_cols = max(rows)
    cell = min((CANVAS - 2 * MARGIN) // max_cols, (CANVAS - 2 * MARGIN) // len(rows), 220)
    r = int(cell * 0.32)
    total_h = len(rows) * cell
    y0 = (CANVAS - total_h) // 2 + cell // 2
    for ri, cols in enumerate(rows):
        total_w = cols * cell
        x0 = (CANVAS - total_w) // 2 + cell // 2
        for ci in range(cols):
            cx, cy = x0 + ci * cell, y0 + ri * cell
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#333333")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")


def strip_frame(png_path: Path) -> bool:
    """生成AIが描いた「額縁」(白余白+枠線)を機械的に切り落とす。
    非白領域のbboxの内側へ2%食い込んでクロップ→正方形に戻す。"""
    img = Image.open(png_path).convert("RGB")
    gray = ImageOps.invert(img.convert("L"))
    bbox = gray.point(lambda p: 255 if p > 12 else 0).getbbox()
    if not bbox:
        return False
    l, t, r, b = bbox
    inset = max(8, int(min(r - l, b - t) * 0.02))
    l, t, r, b = l + inset, t + inset, r - inset, b - inset
    if r - l < 200 or b - t < 200:
        return False
    img.crop((l, t, r, b)).resize((CANVAS, CANVAS), Image.LANCZOS).save(png_path, "PNG")
    return True


def _trim(img: Image.Image) -> Image.Image:
    """白背景の余白を切り落とす。"""
    gray = ImageOps.invert(img.convert("L"))
    bbox = gray.point(lambda p: 255 if p > 12 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def compose_count(base_png: Path, n: int, out_path: Path, pair: bool = False):
    """1個絵をN個(pair時は2個1組×N組)並べる。"""
    base = _trim(Image.open(base_png).convert("RGB"))
    unit_count = n  # 描画ユニット数(ペアなら1組=1ユニット)
    rows = _rows_for(unit_count)
    max_cols = max(rows)
    cell_w = (CANVAS - 2 * MARGIN) // max_cols
    cell_h = (CANVAS - 2 * MARGIN) // len(rows)

    # ペアは「2個を少し重ねた1組」をユニット化
    if pair:
        w, h = base.size
        overlap = int(w * 0.35)
        pair_img = Image.new("RGB", (w * 2 - overlap, h), "white")
        pair_img.paste(base, (0, 0))
        pair_img.paste(base, (w - overlap, 0))
        unit = pair_img
    else:
        unit = base

    # ユニットをセルに収める(拡大はしない)
    uw, uh = unit.size
    scale = min(cell_w * 0.9 / uw, cell_h * 0.9 / uh, 1.0)
    unit = unit.resize((max(1, int(uw * scale)), max(1, int(uh * scale))), Image.LANCZOS)
    uw, uh = unit.size

    img = Image.new("RGB", (CANVAS, CANVAS), "white")
    total_h = len(rows) * cell_h
    y0 = (CANVAS - total_h) // 2
    for ri, cols in enumerate(rows):
        total_w = cols * cell_w
        x0 = (CANVAS - total_w) // 2
        for ci in range(cols):
            px = x0 + ci * cell_w + (cell_w - uw) // 2
            py = y0 + ri * cell_h + (cell_h - uh) // 2
            img.paste(unit, (px, py))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
