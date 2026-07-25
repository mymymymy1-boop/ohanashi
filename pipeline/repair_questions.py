# -*- coding: utf-8 -*-
"""
設問だけの作り直し — 検品で確定した欠陥ユニットを、本文を保全したまま修復する。

検品(inspect/verify)で確定した欠陥46件のうち40件は「設問が本文に根拠を持たない」ことが原因で、
本文そのものは正常。よって**本文と本文音声はそのまま**にし、設問だけを作り直す。
強化済みの 03_question_gen.md（根拠は本文の一文／同時行動の前後を問うの禁止）で再生成する。

処理: 設問再生成 → 機械QC → 設問音声のみ再生成(Aivis・無料) → マニフェスト/ラベル更新
     → 意味検品を再実行して欠陥が消えたか確認（--verify）

使い方:
    python -m pipeline.repair_questions --units sk_x_lv5,sk_y_lv3     # 指定ユニット
    python -m pipeline.repair_questions --from-defects --limit 3      # 欠陥リストから3件(試行)
    python -m pipeline.repair_questions --from-defects                # 全件
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.content import QuestionSet, StorySkeleton, StoryText
from pipeline.common import CONTENT_DIR
from pipeline import generate as gen
from pipeline import qc as qcmod


def load_unit_files(cdir: Path, unit_id: str):
    skel_name, lv = unit_id.rsplit("_lv", 1)
    d = cdir / skel_name
    skeleton = StorySkeleton.model_validate_json((d / "skeleton.json").read_text(encoding="utf-8"))
    text = StoryText.model_validate_json((d / f"lv{lv}_text.json").read_text(encoding="utf-8"))
    return d, int(lv), skeleton, text


def repair_one(cdir: Path, unit_id: str, cost) -> dict:
    """1ユニットの設問を作り直し、QCを通ったら書き込む。"""
    d, lv, skeleton, text = load_unit_files(cdir, unit_id)
    qfile = d / f"lv{lv}_questions.json"
    backup = d / f"lv{lv}_questions.pre_repair.json"
    if qfile.exists() and not backup.exists():
        backup.write_text(qfile.read_text(encoding="utf-8"), encoding="utf-8")   # 元に戻せるよう保全

    last_fail = ""
    for attempt in range(3):
        try:
            qset = gen.gen_questions(skeleton, text, lv, text.group, cost, feedback=last_fail)
        except Exception as e:
            last_fail = f"生成失敗: {e}"
            continue
        # API再呼び出し不要の違反(ダミー種別の貼り間違い R406/R407・正解位置の3連続 R308)は
        # 先に機械修正する。これを挟まないとリトライを浪費して失敗扱いになる。
        gen._autofix_qset(skeleton, text, qset)
        res = qcmod.qc_unit(skeleton, text, qset, audio_dir=None)   # 音声はこの後で作るので除外
        if res.passed:
            data = {"questions": [q.model_dump(exclude_none=True) for q in qset.questions]}
            qfile.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"unit_id": unit_id, "status": "repaired", "attempts": attempt + 1,
                    "questions": len(qset.questions)}
        last_fail = "; ".join(f"{f['rule_id']}: {f['detail']}" for f in res.failures[:6])
    return {"unit_id": unit_id, "status": "failed", "reason": last_fail[:300]}


def main():
    ap = argparse.ArgumentParser(description="設問だけの作り直し（本文は保全）")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--units", default="", help="ユニットID(カンマ区切り)")
    ap.add_argument("--from-defects", action="store_true", help="defect_units.json から読む")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3, help="並列数(Lv5生成は数分かかるため)")
    ap.add_argument("--skip", default="", help="除外するユニットID(カンマ区切り・修復済みの再実行防止)")
    ap.add_argument("--no-audio", action="store_true", help="設問音声の再生成をしない")
    args = ap.parse_args()

    cdir = Path(args.content_dir)
    units: list[str] = []
    if args.units:
        units = [u.strip() for u in args.units.split(",") if u.strip()]
    elif args.from_defects:
        units = json.loads((cdir / "defect_units.json").read_text(encoding="utf-8"))["units"]
    if args.skip:
        skip = {u.strip() for u in args.skip.split(",") if u.strip()}
        units = [u for u in units if u not in skip]
    if args.limit:
        units = units[:args.limit]
    if not units:
        print("対象ユニットがありません。--units か --from-defects を指定してください。")
        return

    print(f"修復対象: {len(units)}ユニット / 並列 {args.workers}", flush=True)
    cost = gen.CostTracker()
    lock = threading.Lock()
    done = {"n": 0}

    def work(u):
        try:
            r = repair_one(cdir, u, cost)
        except Exception as e:
            r = {"unit_id": u, "status": "error", "reason": str(e)[:300]}
        with lock:
            done["n"] += 1
            print(f"[{done['n']}/{len(units)}] {u}: {r['status']}"
                  + (f" ({r.get('reason','')[:80]})" if r["status"] != "repaired" else ""), flush=True)
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(work, units))

    ok = [r["unit_id"] for r in results if r["status"] == "repaired"]
    print(f"\n修復成功 {len(ok)} / {len(units)}", flush=True)

    if ok and not args.no_audio:
        # 設問音声だけ作り直す（本文音声はそのまま＝Aivisはローカル生成で無料）
        print("\n=== 設問音声の再生成 ===", flush=True)
        cmd = [sys.executable, "-X", "utf8", "-m", "pipeline.audio", "--units", ",".join(ok)]
        subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent))

    (cdir / "repair_report.json").write_text(
        json.dumps({"results": results, "repaired": ok}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nレポート: {cdir / 'repair_report.json'}", flush=True)
    print("次の確認: python -m pipeline.inspect_content --out inspect_after.jsonl  で再検品", flush=True)


if __name__ == "__main__":
    main()
