# -*- coding: utf-8 -*-
"""
本文からの作り直し（修復2段目） — 設問だけの修復では直らなかった欠陥ユニットを本文から再生成する。

1段目(`repair_questions.py`)は本文を保全して設問だけを作り直した。それで直らなかった残りは
**本文自体が矛盾している**型（同じ出来事に相反する気持ちが併記される／到着順の記述が一意でない／
「みんな」と個人の行動が混ざる 等）で、設問をどう作り直しても正解が一意に決まらない。

そこで本文から作り直す。**前回の検品指摘を「同じ失敗を繰り返すな」というフィードバックとして
本文生成に渡す**のが肝（プロンプト正本の強化だけでは同じ癖が再発しうるため）。

処理: 本文再生成 → 設問再生成 → 機械QC(音声以外) → 合格分のみ書き込み
      → TTS原稿キャッシュを破棄（本文が変わったのに古い原稿で読み上げる事故を防ぐ）
      → `--audio` で本文・設問の音声を作り直し（Aivis・ローカル生成で無料）

使い方:
    python -m pipeline.repair_story --from-defects --limit 2          # 試し打ち
    python -m pipeline.repair_story --from-defects --audio            # 全件＋音声まで
    python -m pipeline.repair_story --units sk_x_lv5,sk_y_lv3 --audio
その後:
    python -m pipeline.inspect_content --units <成功分> --out inspect_after2.jsonl   # 再検品
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

from models.content import StorySkeleton, StoryText
from pipeline.common import CONTENT_DIR
from pipeline import generate as gen
from pipeline import qc as qcmod

MAX_ATTEMPT = 3


def load_findings(cdir: Path, files: list[str]) -> dict[str, list[dict]]:
    """検品レポート(jsonl)から unit_id -> findings を作る。後のファイルほど優先。"""
    out: dict[str, list[dict]] = {}
    for name in files:
        p = cdir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("unit_id"):
                out[row["unit_id"]] = row.get("findings", [])
    return out


def build_feedback(findings: list[dict]) -> str:
    """検品指摘を本文生成へのフィードバック文にする。"""
    if not findings:
        return ""
    lines = [
        "この骨格から前回作った本文と設問には、人手の検品で次の欠陥が確定した。",
        "**本文の書き方でこれらが起こらないようにすること**（設問側の工夫では直らない）。",
    ]
    for f in findings[:6]:
        detail = (f.get("detail") or "").strip().replace("\n", " ")
        sug = (f.get("suggestion") or "").strip().replace("\n", " ")
        lines.append(f"- [{f.get('category','?')}] {detail[:300]}" + (f" / 直し方: {sug[:200]}" if sug else ""))
    lines += [
        "とくに次を守る:",
        "- 同じ出来事に相反する気持ち（かなしい／おどろいた 等）を併記しない。1場面の気持ちは1つに絞る",
        "- 到着順・順番は「さいしょに」「つぎに」「さいごに」で一意に書く。同時（AとBはいっしょに）を作らない",
        "- 「みんなが〜した」と書くなら全員が実際にそれをしたと読める書き方にする（1人の行動を全体に広げない）",
        "- 数・持ち物・色は場面ごとに1回だけ確定させ、あとで矛盾させない",
    ]
    return "\n".join(lines)


def repair_one(cdir: Path, unit_id: str, feedback0: str, cost) -> dict:
    skel_name, lv_s = unit_id.rsplit("_lv", 1)
    lv = int(lv_s)
    d = cdir / skel_name
    skeleton = StorySkeleton.model_validate_json((d / "skeleton.json").read_text(encoding="utf-8"))
    tfile, qfile = d / f"lv{lv}_text.json", d / f"lv{lv}_questions.json"
    old_text = StoryText.model_validate_json(tfile.read_text(encoding="utf-8"))
    group = getattr(skeleton, "group", "") or old_text.group

    fb = feedback0
    last_fail = ""
    for attempt in range(MAX_ATTEMPT):
        try:
            text = gen._gen_text_validated(skeleton, lv, group, cost, feedback=fb)
            qset = gen._gen_questions_validated(skeleton, text, lv, group, cost)
        except Exception as e:
            last_fail = f"生成失敗: {e}"
            continue
        # API再呼び出し不要の違反(R406/R407/R308)は先に機械修正する
        gen._autofix_qset(skeleton, text, qset)
        res = qcmod.qc_unit(skeleton, text, qset, audio_dir=None)   # 音声はこの後で作るので除外
        if not res.passed:
            last_fail = "; ".join(f"{f['rule_id']}: {f['detail']}" for f in res.failures[:6])
            fb = feedback0 + "\n直前の生成は自動QCに落ちた:\n" + gen._fmt_failures(res.failures)
            continue

        # 元に戻せるよう退避（1回目だけ。2回目以降の実行で元本を上書きしない）
        for src, bak in ((tfile, d / f"lv{lv}_text.pre_repair2.json"),
                         (qfile, d / f"lv{lv}_questions.pre_repair2.json")):
            if src.exists() and not bak.exists():
                bak.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        tfile.write_text(text.model_dump_json(exclude_none=True, indent=2), encoding="utf-8")
        qfile.write_text(qset.model_dump_json(indent=2), encoding="utf-8")
        # 本文が変わったのでTTS原稿キャッシュは必ず捨てる（古い原稿で読み上げる事故を防ぐ）
        script = d / f"lv{lv}_tts_script.json"
        if script.exists():
            script.unlink()
        return {"unit_id": unit_id, "status": "repaired", "attempts": attempt + 1,
                "chars": text.char_count, "questions": len(qset.questions)}
    return {"unit_id": unit_id, "status": "failed", "reason": last_fail[:300]}


def main():
    ap = argparse.ArgumentParser(description="本文からの作り直し（修復2段目）")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--units", default="", help="ユニットID(カンマ区切り)")
    ap.add_argument("--from-defects", action="store_true", help="defect_units.json から読む")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip", default="", help="除外するユニットID(修復済みの再実行防止)")
    ap.add_argument("--workers", type=int, default=3, help="並列数(Lv5生成は数分かかる)")
    ap.add_argument("--findings", default="inspect_after.jsonl,inspect_report.jsonl",
                    help="フィードバック元の検品レポート(カンマ区切り・後のものほど優先)")
    ap.add_argument("--audio", action="store_true", help="成功分の音声(本文＋設問)を作り直す")
    ap.add_argument("--out", default="repair_story_report.json")
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

    # フィードバック元は「後のファイルほど優先」＝修復1段目のあとの検品(inspect_after)を優先する
    files = [f.strip() for f in args.findings.split(",") if f.strip()][::-1]
    fmap = load_findings(cdir, files)
    n_fb = sum(1 for u in units if fmap.get(u))
    print(f"修復対象: {len(units)}ユニット / 並列 {args.workers} / 指摘つき {n_fb}件", flush=True)

    cost = gen.CostTracker()
    lock = threading.Lock()
    done = {"n": 0}

    def work(u):
        try:
            r = repair_one(cdir, u, build_feedback(fmap.get(u, [])), cost)
        except Exception as e:
            r = {"unit_id": u, "status": "error", "reason": str(e)[:300]}
        with lock:
            done["n"] += 1
            print(f"[{done['n']}/{len(units)}] {u}: {r['status']}"
                  + (f" ({r.get('reason','')[:80]})" if r["status"] != "repaired" else
                     f" ({r['attempts']}回目 / {r['chars']}字 / 設問{r['questions']})"), flush=True)
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(work, units))

    ok = [r["unit_id"] for r in results if r["status"] == "repaired"]
    print(f"\n本文の作り直し成功 {len(ok)} / {len(units)}", flush=True)
    tin = sum(v["api_input_tokens"] for v in cost.data.values())
    tout = sum(v["api_output_tokens"] for v in cost.data.values())
    usd = round(tin * 2e-6 + tout * 10e-6, 2)      # qc.py と同じ単価基準
    print(f"API: in {tin:,} / out {tout:,} tok ≒ ${usd}", flush=True)

    if ok and args.audio:
        print("\n=== 音声の再生成（本文＋設問・Aivisはローカル生成で無料） ===", flush=True)
        cmd = [sys.executable, "-X", "utf8", "-m", "pipeline.audio", "--units", ",".join(ok)]
        subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent))

    (cdir / args.out).write_text(
        json.dumps({"results": results, "repaired": ok}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nレポート: {cdir / args.out}", flush=True)
    if ok:
        print("次の確認: python -m pipeline.inspect_content --units "
              + ",".join(ok) + " --out inspect_after2.jsonl", flush=True)


if __name__ == "__main__":
    main()
