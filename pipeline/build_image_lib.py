# -*- coding: utf-8 -*-
"""
画像ライブラリ一括ビルド — image_vocab.json の全語彙を安全策込みで生成する。

種別ごとの生成方法(2026-07-22 試作検品の教訓を反映):
  dots  : PIL描画(AI不使用・確実)
  count : 1個絵をAI生成+QC → PILでN個合成(個数100%保証)
  ai    : AI生成 → Gemini視覚QC(内容/文字/枠/白背景) → 不合格は再生成(2回) →
          それでも不合格なら needs_human.json に積んで人間判断に回す

使い方:
    python -m pipeline.build_image_lib                 # 全量
    python -m pipeline.build_image_lib --only-keys a,b # 指定キーのみ
    python -m pipeline.build_image_lib --limit 30      # 先頭N件(パイロット)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import CONTENT_DIR
from pipeline.gen_images import gen_one
from pipeline.image_qc import check_image
from pipeline.image_render import compose_count, render_dots

IMAGES_DIR = CONTENT_DIR / "images"
GEN_RETRIES = 2  # QC不合格時の再生成回数
WORKERS = 3


# 空・地面など背景を含む場面カード(白背景の強制もQCの白背景要求も外す)
_SCENE_PAT = __import__("re").compile(
    r"(scene|kisetsu|tenki|hanabi|yoru|niwa|kouen|hatake|ensoku|asobu|matsuri"
    r"|(^|_)(sora|yuuhi|hare|ame|kumori|yuki)(_|$))")


_SCENE_PROMPT_PAT = __import__("re").compile(r"(空|景色|風景|公園|庭|畑|の下に|背景|夜|場面|様子)")


def is_scene_key(key: str, prompt: str = "") -> bool:
    # キー名 または プロンプト内の環境表現 でscene判定(白背景強制との矛盾を避ける)
    return bool(_SCENE_PAT.search(key)) or bool(_SCENE_PROMPT_PAT.search(prompt))


def build_ai_image(key: str, prompt: str, out_path: Path) -> dict:
    """AI生成+QCゲート。戻り値 {status: ok|human, qc: {...}}"""
    scene = is_scene_key(key, prompt)
    last_qc = None
    for attempt in range(1 + GEN_RETRIES):
        hint = ""
        if last_qc:
            notes = last_qc.get("notes", "")
            hint = f" 注意: 前回の生成には問題があった({notes})。同じ問題を起こさないこと。"
        if not gen_one(prompt + hint, out_path, scene=scene):
            continue
        try:
            qc = check_image(out_path, prompt, require_white_bg=not scene)
        except RuntimeError as e:
            print(f"    [{key}] QC呼び出し失敗({e}) → 画像は保持し人間判断へ", flush=True)
            return {"status": "human", "qc": {"error": str(e)}}
        if qc["ok"]:
            return {"status": "ok", "qc": qc}
        # 枠だけが問題なら機械的に切り落として再判定(再生成ガチャより確実)
        if qc.get("has_frame") and qc.get("subject_ok") and not qc.get("has_text"):
            from pipeline.image_render import strip_frame
            if strip_frame(out_path):
                try:
                    qc2 = check_image(out_path, prompt, require_white_bg=not scene)
                except RuntimeError:
                    qc2 = None
                if qc2 and qc2["ok"]:
                    print(f"    [{key}] 枠を切除して合格", flush=True)
                    return {"status": "ok", "qc": qc2}
        last_qc = qc
        print(f"    [{key}] QC不合格({attempt + 1}回目): {qc.get('notes', '')} "
              f"(subject={qc.get('subject_ok')} text={qc.get('has_text')} "
              f"frame={qc.get('has_frame')} white={qc.get('bg_white')}) → 再生成", flush=True)
    # 不合格品を .rejected.png に隔離(既存扱いで再実行がスキップしてしまう事故の防止)
    if out_path.exists():
        import os
        os.replace(out_path, out_path.with_suffix("").with_suffix(".rejected.png"))
    return {"status": "human", "qc": last_qc or {}}


def main():
    ap = argparse.ArgumentParser(description="絵カードライブラリ一括ビルド")
    ap.add_argument("--vocab", default=str(CONTENT_DIR / "image_vocab.json"))
    ap.add_argument("--out", default=str(IMAGES_DIR))
    ap.add_argument("--only-keys", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    vocab = json.loads(Path(args.vocab).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    keys = sorted(vocab.keys())
    if args.only_keys:
        want = set(args.only_keys.split(","))
        keys = [k for k in keys if k in want]
    if args.limit:
        keys = keys[:args.limit]

    t0 = time.time()
    needs_human = {}
    done = skipped = 0

    def path_of(k):
        return out_dir / f"{k}.png"

    # ---- ① dots (即・確実) ----
    for k in keys:
        v = vocab[k]
        if v["kind"] == "dots" and not path_of(k).exists():
            render_dots(v["n"], path_of(k))
            done += 1
            print(f"[dots] {k} OK", flush=True)

    # ---- ② ai + countのbase (並列・QCゲート付き) ----
    ai_targets = []
    for k in keys:
        v = vocab[k]
        if v["kind"] == "ai":
            if path_of(k).exists():
                skipped += 1
                continue
            if not v.get("prompt"):
                needs_human[k] = {"reason": "プロンプト未起案"}
                continue
            ai_targets.append((k, v["prompt"]))
    # countのbaseがkeys範囲外でも必要なら足す
    for k in keys:
        v = vocab[k]
        if v["kind"] == "count":
            b = v["base"]
            bv = vocab.get(b)
            if bv and not path_of(b).exists() and bv.get("prompt") \
                    and all(t[0] != b for t in ai_targets):
                ai_targets.append((b, bv["prompt"]))

    print(f"AI生成対象: {len(ai_targets)}件 (並列{WORKERS})", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(build_ai_image, k, p, path_of(k)): k for k, p in ai_targets}
        for i, fut in enumerate(as_completed(futs), 1):
            k = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                needs_human[k] = {"reason": f"例外: {e}"}
                print(f"[ai {i}/{len(futs)}] {k} 例外: {e}", flush=True)
                continue
            if r["status"] == "ok":
                done += 1
                print(f"[ai {i}/{len(futs)}] {k} OK", flush=True)
            else:
                needs_human[k] = {"reason": "QC不合格", "qc": r["qc"]}
                print(f"[ai {i}/{len(futs)}] {k} 人間判断行き", flush=True)

    # ---- ③ count 合成 (baseが揃った後・決定的) ----
    for k in keys:
        v = vocab[k]
        if v["kind"] != "count" or path_of(k).exists():
            continue
        base_png = path_of(v["base"])
        if not base_png.exists():
            needs_human[k] = {"reason": f"base画像なし: {v['base']}"}
            continue
        compose_count(base_png, v["n"], path_of(k), pair=v.get("pair", False))
        done += 1
        print(f"[count] {k} = {v['base']} x{v['n']}{'(組)' if v.get('pair') else ''} OK",
              flush=True)

    (CONTENT_DIR / "images_needs_human.json").write_text(
        json.dumps(needs_human, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nビルド完了: 生成{done} / 既存スキップ{skipped} / 要人間{len(needs_human)} "
          f"/ {int(time.time() - t0)}秒", flush=True)
    if needs_human:
        print(f"要人間リスト: {CONTENT_DIR / 'images_needs_human.json'}", flush=True)


if __name__ == "__main__":
    main()
