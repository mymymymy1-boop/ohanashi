# -*- coding: utf-8 -*-
"""
パイロット一括実行ドライバ — 1プロセスで最後まで走る(途中停止しても孤児が残らない)。

    python -m pipeline.run_pilot --new 9 --seed 20260722 --select 1:3,2:6,3:9,4:7,5:5

手順: ①既存骨格の未合格レベルを修復 → ②新規骨格を生成 → ③QC一括判定 →
      ④Lv別割当で選抜して音声付与 → ⑤検品マニフェスト → ⑥音声込み最終QC
"""
from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import generate as gen
from pipeline import audio as aud
from pipeline import qc as qcmod
import json

from pipeline.common import CONTENT_DIR, THEMES


def main():
    ap = argparse.ArgumentParser(description="お話の記憶 PRO パイロット一括実行")
    ap.add_argument("--new", type=int, default=0, help="新規に生成する骨格数")
    ap.add_argument("--group", default="C")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--select", default="1:3,2:6,3:9,4:7,5:5",
                    help="音声付与のLv別割当 (パイロット30問)")
    ap.add_argument("--skip-audio", action="store_true")
    args = ap.parse_args()
    levels = [1, 2, 3, 4, 5]

    print(f"=== ① 修復フェーズ ({datetime.datetime.now():%H:%M}) ===", flush=True)
    gen.repair_existing(levels, args.group)

    if args.new > 0:
        print(f"=== ② 新規骨格 {args.new} 本 ({datetime.datetime.now():%H:%M}) ===", flush=True)
        seed = args.seed if args.seed is not None else random.randrange(10 ** 8)
        rng = random.Random(seed)
        pool = THEMES[args.group.upper()]
        themes = rng.sample(pool, k=min(args.new, len(pool)))
        seasons = ["haru", "natsu", "aki", "fuyu"]
        for i in range(args.new):
            skeleton_id = gen.next_skeleton_id()
            theme, season = themes[i % len(themes)], seasons[i % 4]
            print(f"[{i+1}/{args.new}] {skeleton_id} 「{theme}」({season})", flush=True)
            gen.build_skeleton_unit(skeleton_id, args.group, theme, season,
                                    f"seed={seed} idx={i} theme={theme} season={season}", levels)

    print(f"=== ③ QC一括判定 ({datetime.datetime.now():%H:%M}) ===", flush=True)
    summary = qcmod.run_qc(CONTENT_DIR, with_audio=False)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    if args.skip_audio:
        print("音声フェーズはスキップ指定のため終了。", flush=True)
        return

    print(f"=== ④ 音声付与 ({datetime.datetime.now():%H:%M}) ===", flush=True)
    passed = aud.qc_passed_units(CONTENT_DIR)
    # このグループの骨格だけを選抜対象にする(全グループ共通のqc_reportから絞る)
    passed = [(d, lv) for d, lv in passed
              if json.loads((d / "skeleton.json").read_text(encoding="utf-8"))
              .get("group") == args.group.upper()]
    targets = aud.select_targets(passed, args.select)
    total_chars, done = 0, []
    for skel_dir, level in targets:
        uid = f"{skel_dir.name}_lv{level}"
        print(f"[audio] {uid}", flush=True)
        try:
            # aivisはspeedScaleが正確&再生成0円のため常に話速補正(audio.py mainと同じ判断)
            r = aud.build_audio_for_unit(skel_dir, level,
                                         rate_adjust=(aud.TTS_BACKEND == "aivis"))
        except Exception as e:
            print(f"  失敗: {e}", flush=True)
            continue
        aud._update_cost(skel_dir, level, r["tts_chars"])
        total_chars += r["tts_chars"]
        done.append(uid)

    print(f"=== ⑤ 検品マニフェスト ===", flush=True)
    aud.write_review_manifest(CONTENT_DIR, done)

    print(f"=== ⑥ 音声込み最終QC ===", flush=True)
    summary = qcmod.run_qc(CONTENT_DIR, with_audio=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"\n=== 完了 ({datetime.datetime.now():%H:%M}) 音声 {len(done)}ユニット / TTS {total_chars}字 ===", flush=True)


if __name__ == "__main__":
    main()
