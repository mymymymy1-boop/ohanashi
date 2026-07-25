# -*- coding: utf-8 -*-
"""
選択肢ラベルのひらがな化 — manifestの全choice idをひらがな表示名に変換し
choice_labels.json を書き出す(検品UI・本番UIの表示用)。

生成モデルが id をローマ字スラッグで出すため(例: mado, tamagoyaki)、
そのまま表示すると読めない。romaji_to_hiragana は先頭トークンのみ変換する
照合用のため、ここでは全トークンを変換して結合する。

使い方:
    python -m pipeline.choice_labels          # 生成
    python -m pipeline.choice_labels --show   # 生成して全件表示(目視点検用)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import CONTENT_DIR, romaji_to_hiragana

import re

# 変換器の best-effort では不自然になる語の上書き(2026-07-23 全件点検で整備)
LABEL_OVERRIDES = {
    # 英語トークン
    "box": "はこ", "hat": "ぼうし", "bag": "かばん", "towel": "たおる",
    "socks": "くつした", "apron": "えぷろん", "backpack": "りゅっく",
    "ribbon": "りぼん", "smile": "えがお", "correct": "ただしい",
    "wrong": "まちがい", "scattered": "ちらかった", "extra": "よぶんな",
    "found": "みつけた", "arrival": "とうちゃく", "request": "おねがい",
    "season": "きせつ", "sheet": "しいと", "tshirt": "しゃつ",
    # 変換器が崩す/綴りゆれの日本語
    "taro": "たろう", "katazuke": "かたづけ", "katazukezuni": "かたづけずに",
    "konnichiwa": "こんにちは", "mafura": "まふらー", "mafuraa": "まふらー",
    "booru": "ぼーる", "roosoku": "ろうそく", "obento": "おべんとう",
    "mizutou": "すいとう", "hank": "はんかち", "home": "ほめる",
    "dammatte": "だまって", "teoarai": "てあらい",
    # 表示不要トークン
    "default": "", "icon": "", "scene": "", "num": "", "emotion": "",
}

# 数+単位トークン(2pai, 3mai 等)の読み
_UNIT_READINGS = {
    "pai": {1: "いっぱい", 2: "にはい", 3: "さんばい", 4: "よんはい", 5: "ごはい"},
    "mai": {1: "いちまい", 2: "にまい", 3: "さんまい", 4: "よんまい",
            5: "ごまい", 6: "ろくまい"},
}


def _token_label(tok: str) -> str:
    if tok in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[tok]
    if re.fullmatch(r"scene\d+", tok):      # scene1, scene4 等
        return ""
    if tok.isdigit():                        # 数字はそのまま表示(検品者向け)
        return tok
    m = re.fullmatch(r"(\d+)(pai|mai)", tok)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return _UNIT_READINGS.get(unit, {}).get(n, f"{n}{unit}")
    return romaji_to_hiragana(tok)


def id_to_label(cid: str) -> str:
    parts = [t for t in (_token_label(tok) for tok in cid.split("_")) if t]
    return " ".join(parts) or cid


def main():
    ap = argparse.ArgumentParser(description="選択肢ラベルのひらがな化")
    ap.add_argument("--show", action="store_true", help="全件を表示(目視点検)")
    args = ap.parse_args()

    manifest = json.loads((CONTENT_DIR / "review_manifest.json").read_text(encoding="utf-8"))
    labels = {}
    for it in manifest["items"]:
        for q in it["questions"]:
            for c in q["choices"]:
                cid = c["id"]
                if cid not in labels:
                    labels[cid] = id_to_label(cid)

    out = CONTENT_DIR / "choice_labels.json"
    out.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"choice_labels: {len(labels)}件 → {out}", flush=True)
    if args.show:
        for k, v in sorted(labels.items()):
            print(f"  {k} => {v}")


if __name__ == "__main__":
    main()
