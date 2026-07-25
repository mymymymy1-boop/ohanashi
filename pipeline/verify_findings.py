# -*- coding: utf-8 -*-
"""
検品指摘の敵対的検証 — inspect_content.py の指摘が「本当に欠陥か」を独立に判定する。

検品(第一の読み手)は粗探しに寄る傾向があるため、指摘をそのまま欠陥として扱わない。
この工程では、本文と該当設問だけを渡し、**指摘を反証する側**に立って判定させる。
疑わしい場合は「反証(refuted)」に倒す＝偽陽性を落とすことを優先する。

使い方:
    python -m pipeline.verify_findings --severity high        # highのみ検証
    python -m pipeline.verify_findings --severity high,medium
出力: content/pilot/verify_report.jsonl / verify_summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import BASE, CONTENT_DIR

load_dotenv(BASE / ".env")
MODEL = os.getenv("VERIFY_MODEL", "claude-sonnet-5").strip()
client = anthropic.Anthropic()
_lock = threading.Lock()

SYSTEM = """あなたは教材検品の「反証担当」です。別の検品者が挙げた指摘について、それが**本当に欠陥かどうか**を厳しく判定します。

あなたの役割は指摘を鵜呑みにせず、**まず反証を試みる**ことです。次の場合は refuted（＝指摘は誤り）と判定してください。
- 本文をよく読めば、指摘された曖昧さは実際には存在しない（根拠が本文にある）
- 指摘が仕様上の意図を誤解している（複数正解「ぜんぶに○」はLv3以上で必須、属性入れ替えダミーはLv4-5で必須、strongダミー＝本文登場人物を使うのも意図的、記号と色が問ごとに変わるのも意図的）
- 「難しすぎる」「年長児には酷」という難易度の主観であって、事実誤りではない
- 改善提案にすぎず、現状のままでも正解が一意に決まり公平である

confirmed（＝本当に欠陥）とするのは、次が客観的に示せるときだけです。
- 正解が本文の記述から一意に決まらない（複数が正解になる／正解が存在しない）
- 正解が本文と矛盾している
- ダミーが本文の記述上も正解になってしまう
- 事実・季節・数量が客観的に誤っている
- 選択肢が絵として区別できない（同じ物を指している）

**迷ったら refuted**。教材を無闇に作り直すコストの方が高いためです。

出力はJSONのみ:
{"verdict":"confirmed|refuted","reason":"判定理由(1〜2文・本文の該当箇所を引用して示す)","severity":"high|medium|low"}
severityは confirmed のときだけ、実害の大きさで付け直してください。"""


def _extract_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if 0 <= s < e:
            return json.loads(text[s:e + 1])
        raise


def build_payload(unit, finding, labels):
    lines = ["■ お話（読み上げ本文・全文）"]
    for s in unit["story_scenes"]:
        lines.append(f"（場面{s['seq']}）{s['text']}")
    where = finding.get("where", "")
    qs = [q for q in unit["questions"] if q["q_id"] == where] or unit["questions"]
    lines.append("")
    lines.append("■ 対象の設問")
    for q in qs:
        ins = q["instruction"]
        multi = "（あてはまるものすべてに印）" if ins.get("multi") else "（1つだけ選ぶ）"
        lines.append(f"[{q['q_id']}] type={q['type']} {multi}")
        lines.append(f"設問文: {q['prompt_text']}")
        for c in q["choices"]:
            mark = "★正解" if c["id"] in q["correct"] else "　ダミー"
            lines.append(f"  {mark} {labels.get(c['id'], c['id'])} [絵:{c['image_key']}]")
    lines.append("")
    lines.append("■ 検証対象の指摘")
    lines.append(f"分類: {finding.get('category')} / 元の重大度: {finding.get('severity')}")
    lines.append(f"指摘内容: {finding.get('detail')}")
    lines.append("")
    lines.append("この指摘は本当に欠陥ですか？ まず反証を試みたうえで判定してください。")
    return "\n".join(lines)


def verify_one(task):
    unit, finding, labels = task
    payload = build_payload(unit, finding, labels)
    last = None
    for attempt in range(3):
        try:
            with client.messages.stream(
                model=MODEL, max_tokens=4000, system=SYSTEM,
                messages=[{"role": "user", "content": payload}],
            ) as st:
                resp = st.get_final_message()
            if resp.stop_reason == "max_tokens":
                raise ValueError("truncated")
            data = _extract_json("".join(b.text for b in resp.content if b.type == "text"))
            v = data.get("verdict")
            if v not in ("confirmed", "refuted"):
                raise ValueError(f"bad verdict {v}")
            return {"unit_id": unit["unit_id"], "level": unit.get("level"), "group": unit.get("group"),
                    "theme": unit.get("theme"), "where": finding.get("where"),
                    "category": finding.get("category"), "orig_severity": finding.get("severity"),
                    "detail": finding.get("detail"), "suggestion": finding.get("suggestion"),
                    "verdict": v, "reason": data.get("reason", ""),
                    "severity": data.get("severity") or finding.get("severity"),
                    "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}}
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(2 + attempt * 4)
    return {"unit_id": unit["unit_id"], "where": finding.get("where"),
            "category": finding.get("category"), "verdict": "error", "reason": str(last)[:200],
            "detail": finding.get("detail"), "orig_severity": finding.get("severity")}


def main():
    ap = argparse.ArgumentParser(description="検品指摘の敵対的検証")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--severity", default="high", help="検証対象(カンマ区切り)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    cdir = Path(args.content_dir)
    manifest = json.loads((cdir / "review_manifest.json").read_text(encoding="utf-8"))
    labels = json.loads((cdir / "choice_labels.json").read_text(encoding="utf-8"))
    units = {it["unit_id"]: it for it in manifest["items"]}
    want = {s.strip() for s in args.severity.split(",") if s.strip()}

    seen = {}
    for line in (cdir / "inspect_report.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            seen[r["unit_id"]] = r
    tasks = []
    for r in seen.values():
        u = units.get(r["unit_id"])
        if not u:
            continue
        for f in r["findings"]:
            if f.get("severity") in want:
                tasks.append((u, f, labels))
    print(f"検証対象: {len(tasks)}件 (severity={sorted(want)}) / モデル {MODEL}", flush=True)
    if not tasks:
        return

    out = cdir / "verify_report.jsonl"
    f = open(out, "w", encoding="utf-8")
    cnt = {"n": 0, "c": 0, "r": 0}

    def work(t):
        res = verify_one(t)
        with _lock:
            f.write(json.dumps(res, ensure_ascii=False) + "\n"); f.flush()
            cnt["n"] += 1
            if res["verdict"] == "confirmed": cnt["c"] += 1
            elif res["verdict"] == "refuted": cnt["r"] += 1
            print(f"[{cnt['n']}/{len(tasks)}] {res['unit_id']} {res.get('where')} → {res['verdict']}", flush=True)
        return res

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(work, tasks))
    f.close()

    tin = sum(r.get("usage", {}).get("in", 0) for r in results)
    tout = sum(r.get("usage", {}).get("out", 0) for r in results)
    by_cat = {}
    for r in results:
        if r["verdict"] == "confirmed":
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    summary = {
        "verified": len(results), "confirmed": cnt["c"], "refuted": cnt["r"],
        "errors": sum(1 for r in results if r["verdict"] == "error"),
        "true_positive_rate": round(cnt["c"] / max(1, cnt["c"] + cnt["r"]), 3),
        "confirmed_by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "cost_estimate_usd": round(tin * 2e-6 + tout * 10e-6, 3),
    }
    (cdir / "verify_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 検証サマリ ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
