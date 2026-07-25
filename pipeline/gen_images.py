# -*- coding: utf-8 -*-
"""
絵カード画像生成ドライバ — Gemini API (gemini-2.5-flash-image) で
image_key ごとの PNG を生成する。冪等(出力が既にあればスキップ)。

使い方:
    python -m pipeline.gen_images --list content/pilot/images_trial/trial_list.json \
                                  --out content/pilot/images_trial

リスト形式(JSON): [{"key": "usagi_default", "prompt": "白いうさぎの…"}, ...]
スタイルは STYLE_PREFIX に一元定義(全カードの絵柄統一のため個別promptには書かない)。
APIキーは環境変数 GEMINI_API_KEY (ユーザー環境変数に登録済み)。
※当初は nanobanana-pro スキル(ブラウザ自動操作)だったが、Gemini WebのUI変更で
  ボタン検出が壊れたため(2026-07-22)、API直叩きに切り替えた。コスト≈$0.04/枚。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import BASE

load_dotenv(BASE / ".env")

MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
API_KEY = os.getenv("GEMINI_API_KEY", "")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

# 絵柄の正本(2026-07-22 泰介さん承認済み。トーン変更はここだけを直す)。
# 後半の禁止事項は試作検品で実際に出たエラーへの対策(枠線混入・金髪化)。
STYLE_PREFIX = (
    "幼児向け知育教材の絵カードイラスト。やさしい水彩風、シンプルで親しみやすい、"
    "やわらかい茶色の輪郭線、明るいパステルカラー、真っ白な背景、影なし、"
    "画面内に文字・数字・記号は一切入れない、対象を大きく中央に配置、正方形。"
    "画像の縁に枠線・フレーム・カードの縁取りを絶対に描かない。"
    "人間の子どもを描く場合は黒髪の日本人。"
    "同じ物を複数描く場合は全て同一デザイン・同一色にする。 "
)

# 場面カード用(空・地面など背景を含む絵)。白背景の強制が花火・雪景色と衝突するため分離。
SCENE_STYLE_PREFIX = (
    "幼児向け知育教材の場面イラストカード。やさしい水彩風、シンプルで親しみやすい、"
    "やわらかい茶色の輪郭線、明るいパステルカラー、影は最小限、"
    "画面内に文字・数字・記号は一切入れない、正方形。"
    "背景(空・地面など)は場面に合ったやさしい色で描いてよい。"
    "画像の縁に枠線・フレーム・カードの縁取りを絶対に描かない。"
    "人間の子どもを描く場合は黒髪の日本人。 "
)

MAX_ATTEMPTS = 3


def gen_one(prompt: str, out_path: Path, timeout: int = 120, scene: bool = False) -> bool:
    style = SCENE_STYLE_PREFIX if scene else STYLE_PREFIX
    body = {
        "contents": [{"parts": [{"text": style + prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "1:1"},
        },
    }
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = requests.post(URL, params={"key": API_KEY}, json=body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 529):
                raise RuntimeError(f"HTTP {r.status_code} (一過性)")
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            for p in parts:
                inline = p.get("inlineData") or p.get("inline_data")
                if inline:
                    data = base64.b64decode(inline["data"])
                    if len(data) < 5000:
                        raise RuntimeError(f"画像が小さすぎる ({len(data)}B)")
                    tmp = out_path.with_suffix(".tmp.png")
                    tmp.write_bytes(data)
                    os.replace(tmp, out_path)
                    return True
            raise RuntimeError("応答に画像が含まれない")
        except (requests.RequestException, RuntimeError, KeyError, IndexError) as e:
            last_err = e
            print(f"    {attempt + 1}回目失敗: {e}", flush=True)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(3 + attempt * 5)
    print(f"    諦め (最後のエラー: {last_err})", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser(description="絵カード画像生成 (Gemini API)")
    ap.add_argument("--list", required=True, help="key/promptのJSONリスト")
    ap.add_argument("--out", required=True, help="出力ディレクトリ")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if not API_KEY:
        print("GEMINI_API_KEY が未設定です。中止。", flush=True)
        sys.exit(1)

    items = json.loads(Path(args.list).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = skip = fail = 0
    for i, it in enumerate(items, 1):
        key, prompt = it["key"], it["prompt"]
        out_path = out_dir / f"{key}.png"
        if out_path.exists() and out_path.stat().st_size > 5000:
            print(f"[{i}/{len(items)}] {key} SKIP(既存)", flush=True)
            skip += 1
            continue
        print(f"[{i}/{len(items)}] {key} 生成中…", flush=True)
        if gen_one(prompt, out_path, args.timeout):
            ok += 1
            print(f"    OK ({out_path.stat().st_size // 1024}KB)", flush=True)
        else:
            fail += 1
            print(f"    FAIL: {key}", flush=True)

    print(f"画像生成完了: OK {ok} / SKIP {skip} / FAIL {fail}", flush=True)


if __name__ == "__main__":
    main()
