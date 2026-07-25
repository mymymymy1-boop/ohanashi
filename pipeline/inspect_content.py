# -*- coding: utf-8 -*-
"""
意味検品（LLMによる第二の読み手） — 機械QC(qc.py)では判定できない「中身の妥当性」を検品する。

qc.py が見るのは構造（文字数・設問数・選択肢数・ダミー種別など機械測定できるもの）。
このスクリプトが見るのは、人間の講師が読んで初めて気づく次の観点:

  R-A 正解が本文から一意に決まるか（複数正解・正解なしを検出）
  R-B ダミーが正解になりうる曖昧さ（ひっかけが厳しすぎる／実は正しい）
  R-C 本文に根拠のない設問（聞いていても答えられない）
  R-D 設問文が年長児に音だけで理解できるか（係り受け・長さ・言い回し）
  R-E 季節・常識・数量の誤り（本文と設問の矛盾）
  R-F 選択肢の並びが絵カードで区別できるか（同義・紛らわしい）
  R-G 本文自体の不自然さ・矛盾（登場人物の消失、因果の飛躍）

使い方:
    python -m pipeline.inspect_content --limit 5              # 試し打ち(5ユニット)
    python -m pipeline.inspect_content                        # 全ユニット
    python -m pipeline.inspect_content --group C --level 2    # 絞り込み
    python -m pipeline.inspect_content --workers 4

出力: content/pilot/inspect_report.jsonl（1行1ユニット・追記式で中断に強い）
      content/pilot/inspect_summary.json
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

MODEL = os.getenv("INSPECT_MODEL", "claude-sonnet-5").strip()
MAX_RETRY = 3
client = anthropic.Anthropic()

SYSTEM = """あなたは小学校受験「お話の記憶」を20年教えているプロ講師で、教材の最終検品者です。
これから、年長児(6歳)に音声で読み上げる「お話」と、その設問セットを検品します。

【重要な前提】
- 子どもは本文を1回だけ耳で聞き、絵カード4択から選びます。文字は読めません。
- 選択肢は絵カードで表示されます（ラベルは検品者向けの読みです）。
- 設問文もすべて音声で読まれます。

【検品する観点】次の各点について、実際に問題があるものだけを指摘してください。
- unique: 正解が本文の記述から一意に決まらない（複数が正解になる／どれも正解でない）
- dummy: ダミー選択肢が実は正解になりうる、または紛らわしすぎて不公平
- evidence: 本文に根拠がなく、聞いていても答えられない
- wording: 設問文が年長児に音だけでは理解しにくい（長い・係り受けが複雑・言い回しが不自然）
- fact: 季節・常識・数量が本文と矛盾している、または一般常識として誤り
- choices: 選択肢が絵で区別できない（同義・ほぼ同じ絵になる）
- story: 本文自体が不自然（登場人物が消える、因果が飛ぶ、日本語がおかしい）

【仕様上の意図＝これらは欠陥ではないので指摘しないこと】
以下は実際の入試を再現するための意図的な設計です。難しさそのものを問題視しないでください。
- **「あてはまるもの ぜんぶに○」（複数正解）はレベル3以上で必須**。実際の入試に出る形式であり、正解が2つ以上あること自体は正しい。
- **記号と色の指定（赤い○・青い△など）が問ごとに変わるのも意図的**。指示の聞き取り自体が採点対象。
- **属性入れ替えダミー（正しい人物×違う色/持ち物）はレベル4-5で必須**。紛らわしいのは狙いどおり。
- **本文に登場した別の人物をダミーにするのも意図的**（strongダミー）。
- 制限時間が短いこと、本文が1回しか流れないことも本番仕様。
- 「年長児には難しすぎる」という難易度そのものへの意見は不要。**間違いや不公平さ**だけを挙げる。

【厳しさの基準】
- 教材として出荷できないものを high、直したほうが良いものを medium、好みの範囲を low とする。
- **問題がなければ findings を空配列にする**。粗探しをして無理に指摘を作らないこと。
- 「もっと良くできる」という改善案は指摘ではない。**事実として誤っている／答えが決まらない／絵で区別できない**ものだけを挙げる。
- 特に価値がある指摘: 正解が本文と矛盾している、ダミーも正解になってしまう、絵カードが複数の物を含んで何を指すか不明、季節や常識の誤り。

【出力】JSONのみ。前置き・コードフェンス禁止。
{"findings":[{"where":"<q_idまたはstory>","severity":"high|medium|low","category":"unique|dummy|evidence|wording|fact|choices|story","detail":"何がなぜ問題か(1〜2文)","suggestion":"具体的な直し方(1文)"}]}"""

_write_lock = threading.Lock()


def _extract_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if 0 <= s < e:
            return json.loads(text[s:e + 1])
        raise


def build_payload(unit: dict, labels: dict) -> str:
    """検品対象を人が読める形に整形（LLMへの入力）。"""
    lines = [f"【グループ{unit.get('group')} / レベル{unit.get('level')} / 季節{unit.get('season')} / テーマ{unit.get('theme')}】", "", "■ お話（読み上げ本文）"]
    for s in unit["story_scenes"]:
        lines.append(f"（場面{s['seq']}）{s['text']}")
    lines.append("")
    lines.append("■ 設問")
    for i, q in enumerate(unit["questions"], 1):
        ins = q["instruction"]
        mk = {"maru": "○", "sankaku": "△", "shikaku": "□", "batsu": "×"}.get(ins["mark"], ins["mark"])
        col = {"aka": "赤", "ao": "青", "midori": "緑", "kuro": "黒", "kiiro": "黄"}.get(ins["color"], ins["color"])
        multi = "（あてはまるものすべて）" if ins.get("multi") else ""
        lines.append(f"{i}. [{q['q_id']}] type={q['type']} 記号={col}{mk}{multi}")
        lines.append(f"   設問文: {q['prompt_text']}")
        for c in q["choices"]:
            mark = "★正解" if c["id"] in q["correct"] else "　ダミー"
            lab = labels.get(c["id"], c["id"])
            kind = f"({c['dummy_kind']})" if c.get("dummy_kind") else ""
            lines.append(f"   {mark} {lab} [絵カード:{c['image_key']}]{kind}")
        lines.append("")
    return "\n".join(lines)


def inspect_unit(unit: dict, labels: dict) -> dict:
    payload = build_payload(unit, labels)
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=int(os.getenv("INSPECT_MAX_TOKENS", "8000")),
                system=SYSTEM,
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                resp = stream.get_final_message()
            if resp.stop_reason == "max_tokens":
                raise ValueError("出力が max_tokens で途切れた")
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = _extract_json(text)
            findings = data.get("findings", [])
            if not isinstance(findings, list):
                raise ValueError("findings が配列でない")
            return {
                "unit_id": unit["unit_id"], "group": unit.get("group"), "level": unit.get("level"),
                "theme": unit.get("theme"), "status": "ok", "findings": findings,
                "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens},
            }
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError, anthropic.APIStatusError,
                ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < MAX_RETRY - 1:
                time.sleep(2 + attempt * 4)
    return {"unit_id": unit["unit_id"], "group": unit.get("group"), "level": unit.get("level"),
            "theme": unit.get("theme"), "status": "error", "error": str(last_err)[:300], "findings": []}


def main():
    ap = argparse.ArgumentParser(description="お話の記憶 PRO 意味検品（LLM第二の読み手）")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ検品（試し打ち）")
    ap.add_argument("--group", default="", help="グループ絞り込み(例 C)")
    ap.add_argument("--level", type=int, default=0, help="レベル絞り込み")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="inspect_report.jsonl")
    ap.add_argument("--resume", action="store_true", help="既存レポートにある unit_id をスキップ")
    args = ap.parse_args()

    cdir = Path(args.content_dir)
    manifest = json.loads((cdir / "review_manifest.json").read_text(encoding="utf-8"))
    labels = json.loads((cdir / "choice_labels.json").read_text(encoding="utf-8"))
    items = manifest["items"]
    if args.group:
        items = [x for x in items if x.get("group") == args.group]
    if args.level:
        items = [x for x in items if x.get("level") == args.level]

    out_path = cdir / args.out
    done = set()
    if args.resume and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["unit_id"])
            except Exception:
                pass
        items = [x for x in items if x["unit_id"] not in done]
    if args.limit:
        items = items[:args.limit]

    print(f"検品対象: {len(items)}ユニット / モデル {MODEL} / 並列 {args.workers}"
          + (f" / 既存スキップ {len(done)}" if done else ""), flush=True)
    if not items:
        print("対象がありません。", flush=True)
        return

    f = open(out_path, "a", encoding="utf-8")
    counter = {"n": 0}

    def work(u):
        res = inspect_unit(u, labels)
        with _write_lock:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.flush()
            counter["n"] += 1
            hi = sum(1 for x in res["findings"] if x.get("severity") == "high")
            print(f"[{counter['n']}/{len(items)}] {res['unit_id']} "
                  f"{res['status']} 指摘{len(res['findings'])}件"
                  + (f"(high {hi})" if hi else ""), flush=True)
        return res

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(work, items))
    f.close()

    # サマリ
    sev = {"high": 0, "medium": 0, "low": 0}
    cat: dict[str, int] = {}
    tin = tout = 0
    errs = []
    for r in results:
        tin += r.get("usage", {}).get("in", 0)
        tout += r.get("usage", {}).get("out", 0)
        if r["status"] != "ok":
            errs.append(r["unit_id"])
        for x in r["findings"]:
            s = x.get("severity", "low")
            sev[s] = sev.get(s, 0) + 1
            c = x.get("category", "other")
            cat[c] = cat.get(c, 0) + 1
    clean = sum(1 for r in results if r["status"] == "ok" and not r["findings"])
    summary = {
        "inspected": len(results), "clean_units": clean,
        "units_with_findings": len(results) - clean - len(errs),
        "errors": errs, "severity": sev, "category": dict(sorted(cat.items(), key=lambda kv: -kv[1])),
        "cost_estimate_usd": round(tin * 2e-6 + tout * 10e-6, 3),
        "tokens": {"in": tin, "out": tout}, "model": MODEL,
    }
    (cdir / "inspect_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== サマリ ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
