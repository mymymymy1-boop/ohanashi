"""絵カードの軽量版（子ども画面用）を作る。

絵カードの正本は 1024x1024 PNG（1枚 約1MB・全1700枚で約1.7GB）。検品UIはこれを使うが、
こども画面の選択肢は画面上 130〜260px 程度でしか表示されないので、そのまま配ると
①スマホ回線で重い ②オフライン保存が端末の容量制限に当たる（30話で数百MB）。

そこで images_small/<slug>.webp（既定 448px・WebP）を作り、play 側だけがこれを使う。
生成はローカルのPIL（無料・数分）。元PNGは一切変更しない。

  python -m pipeline.make_small_images            # 追加分だけ作る
  python -m pipeline.make_small_images --all      # 全部作り直す
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from pipeline.common import CONTENT_DIR

SRC = CONTENT_DIR / "images"
DST = CONTENT_DIR / "images_small"
MAX_PX = 448
QUALITY = 82


def convert(src: Path, dst: Path) -> int:
    im = Image.open(src)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > MAX_PX:
        scale = MAX_PX / max(w, h)
        im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "WEBP", quality=QUALITY, method=6)
    return dst.stat().st_size


def main():
    global MAX_PX
    ap = argparse.ArgumentParser(description="絵カードの軽量版(WebP)を作る")
    ap.add_argument("--all", action="store_true", help="既存も作り直す")
    ap.add_argument("--max-px", type=int, default=MAX_PX)
    args = ap.parse_args()
    MAX_PX = args.max_px

    srcs = sorted(SRC.glob("*.png"))
    made = skipped = 0
    src_bytes = dst_bytes = 0
    broken: list[tuple[str, str]] = []
    for s in srcs:
        if s.name.endswith(".rejected.png"):
            continue
        d = DST / (s.stem + ".webp")
        src_bytes += s.stat().st_size
        if d.exists() and not args.all and d.stat().st_mtime >= s.stat().st_mtime:
            skipped += 1
            dst_bytes += d.stat().st_size
            continue
        try:
            dst_bytes += convert(s, d)
        except Exception as e:      # 生成中で書きかけのPNG等は飛ばして最後に報告
            broken.append((s.name, str(e)[:80]))
            continue
        made += 1
        if made % 100 == 0:
            print(f"  {made}枚…", flush=True)
    print(f"軽量版: 作成{made} / 既存スキップ{skipped}")
    print(f"容量: 正本 {src_bytes / 1048576:.0f}MB → 軽量版 {dst_bytes / 1048576:.0f}MB "
          f"({dst_bytes / max(src_bytes, 1) * 100:.1f}%)")
    print(f"出力: {DST}")
    if broken:
        print(f"⚠ 変換できなかったPNG {len(broken)}件（もう一度実行すれば拾えます）:")
        for name, err in broken[:10]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
