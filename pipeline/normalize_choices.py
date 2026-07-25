# -*- coding: utf-8 -*-
"""
選択肢の4択統一マイグレーション (2026-07-25 泰介さん指示: 実試験は4択が主流)。

- 5択 → 4択: ダミー1個を削除。QCのFAIL条件を壊さない選択
  (T2のnumeric最後の1個 / T6(Lv4+)のscene_composite最後の1個 /
   セット内 strong(Lv3+で1個・Lv4+で2個) / attribute_swap(Lv4+で1個) は保護)。
  multi正解4個(4of5)の設問は正解1個を削除して3of4化
  (設問文は全て「ぜんぶに○」形式で個数を言わないため音声・本文は無変更でよい)。
- 3択 → 4択: ダミー1個を追加。
  T2 = 数値カードを機械追加(既存画像があるものだけ)。
  T1/T3 = 既存画像ライブラリの語彙からLLMが同カテゴリ未登場ダミーを選ぶ
  (R407: 本文に登場する語は候補から除外)。
- 仕上げに R308(正解位置3連続) を選択肢入れ替えで解消。

使い方:
    python -m pipeline.normalize_choices --dry-run   # 変更計画のみ表示(LLM呼び出しなし)
    python -m pipeline.normalize_choices             # 実行(ファイル書き換え)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import CONTENT_DIR
from pipeline.choice_labels import id_to_label

IMAGES_DIR = CONTENT_DIR / "images"

# ---------------- 画像解決 ----------------

def _load_alias() -> dict:
    p = CONTENT_DIR / "image_alias_map.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

ALIAS = _load_alias()

def image_ok(key: str) -> bool:
    stem = ALIAS.get(key, key)
    return (IMAGES_DIR / f"{stem}.png").exists()


# ---------------- 5択→4択 トリム ----------------

_DROP_PRIORITY = {"category": 0, None: 1, "numeric": 2, "scene_composite": 3,
                  "attribute_swap": 4, "strong": 5, "emotion_set": 6}

def _set_dummy_count(questions: list[dict], kind: str) -> int:
    return sum(1 for q in questions for c in q["choices"] if c.get("dummy_kind") == kind)

def trim_question(q: dict, questions: list[dict], level: int) -> str:
    """5択設問から1個削る。削った選択肢のidを返す。"""
    correct = set(q["correct"])
    choices = q["choices"]

    # multi 4of5 → 正解を1個削って 3of4 に(表示されないカードは選ばせない、で成立)
    if len(correct) >= 4:
        victim = next(c for c in reversed(choices) if c["id"] in correct)
        choices.remove(victim)
        q["correct"] = [cid for cid in q["correct"] if cid != victim["id"]]
        return victim["id"] + "(correct)"

    droppable = [c for c in choices if c["id"] not in correct]

    def protected(c: dict) -> bool:
        dk = c.get("dummy_kind")
        if dk == "numeric" and q["type"] == "T2":
            if sum(1 for x in choices if x.get("dummy_kind") == "numeric") <= 1:
                return True
        if dk == "scene_composite" and q["type"] == "T6" and level >= 4:
            if sum(1 for x in choices if x.get("dummy_kind") == "scene_composite") <= 1:
                return True
        if dk == "strong":
            keep = 2 if level >= 4 else (1 if level >= 3 else 0)
            if _set_dummy_count(questions, "strong") <= keep:
                return True
        if dk == "attribute_swap" and level >= 4:
            if _set_dummy_count(questions, "attribute_swap") <= 1:
                return True
        return False

    cands = [c for c in droppable if not protected(c)]
    if not cands:  # 保護を warn 水準まで緩める(FAIL水準 strong>=1/swap>=1 だけ守る)
        def hard_protected(c: dict) -> bool:
            dk = c.get("dummy_kind")
            if dk == "numeric" and q["type"] == "T2":
                return sum(1 for x in choices if x.get("dummy_kind") == "numeric") <= 1
            if dk == "scene_composite" and q["type"] == "T6" and level >= 4:
                return sum(1 for x in choices if x.get("dummy_kind") == "scene_composite") <= 1
            if dk == "strong" and level >= 3:
                return _set_dummy_count(questions, "strong") <= 1
            if dk == "attribute_swap" and level >= 4:
                return _set_dummy_count(questions, "attribute_swap") <= 1
            return False
        cands = [c for c in droppable if not hard_protected(c)] or droppable

    def score(c: dict):
        s = _DROP_PRIORITY.get(c.get("dummy_kind"), 1)
        if not image_ok(c.get("image_key", "")):
            s -= 10  # 画像が無い選択肢を最優先で落とす
        return s

    victim = min(reversed(cands), key=score)  # 同点なら後ろの選択肢を落とす
    choices.remove(victim)
    return victim["id"]


# ---------------- 3択→4択 追加 (T2: 数値カード機械追加) ----------------

_NUM_TABLES = {
    "hiki": {1: "ippiki", 2: "nihiki", 3: "sanbiki", 4: "yonhiki", 5: "gohiki"},
    "tsu":  {1: "hitotsu", 2: "futatsu", 3: "mittsu", 4: "yottsu", 5: "itsutsu"},
    "nin":  {1: "hitori", 2: "futari", 3: "sannin", 4: "yonin", 5: "gonin"},
}
_ID_STYLE_LOOKUP = {v: (tbl, n) for tbl, m in _NUM_TABLES.items() for n, v in m.items()}

_KEY_RE = re.compile(r"^(.+?)_(\d)(hiki|biki|bon|ko|mai|hon|pai|nin|dai|wa)?$")

def add_numeric(q: dict) -> str | None:
    """T2 3択に数値カードを1枚追加。追加idを返す(不可能ならNone)。"""
    choices = q["choices"]
    parsed = []
    for c in choices:
        m = _KEY_RE.match(c["image_key"])
        if not m:
            return None
        parsed.append((c, m.group(1), int(m.group(2)), m.group(3) or ""))
    # ベースは正解選択肢のものに合わせる(混在設問 kanzume+onigiri 等では正解側)
    correct_ids = set(q["correct"])
    base = next((p[1] for p in parsed if p[0]["id"] in correct_ids), parsed[0][1])
    counter = next((p[3] for p in parsed if p[1] == base and p[3]), "")
    used = {p[2] for p in parsed if p[1] == base}  # 使用済み数字は同一ベースのみで判定
    correct_n = next((p[2] for p in parsed if p[0]["id"] in correct_ids), 3)

    # ベース画像が1枚も無い設問(hikari_ishi等)は標準ドットカード kazu_N に張り替える
    if not any(image_ok(f"{p[1]}_{p[2]}{p[3]}") for p in parsed):
        for c, _b, n, _cnt in parsed:
            c["image_key"] = f"kazu_{n}"
        used_all = {p[2] for p in parsed}
        for n in sorted(set(range(1, 6)) - used_all, key=lambda x: (abs(x - correct_n), x)):
            key = f"kazu_{n}"
            if image_ok(key) and f"kazu_{n}" not in [c["id"] for c in choices]:
                q["choices"].append({"id": f"kazu_{n}", "image_key": key,
                                     "dummy_kind": "numeric"})
                return f"kazu_{n}(dots-remap)"
        return None

    for n in sorted(set(range(1, 6)) - used, key=lambda x: (abs(x - correct_n), x)):
        for cnt in ([counter, "hiki", "biki", ""] if counter else [""]):
            key = f"{base}_{n}{cnt}" if cnt else f"{base}_{n}"
            if not image_ok(key):
                continue
            # id はきょうだいの命名流儀に合わせる
            sib_ids = [c["id"] for c in choices]
            if all(c["id"] == c["image_key"] for c in choices):
                new_id = key
            else:
                style = next((_ID_STYLE_LOOKUP[i][0] for i in sib_ids
                              if i in _ID_STYLE_LOOKUP), None)
                new_id = _NUM_TABLES[style][n] if style else key
            if new_id in sib_ids:
                continue
            q["choices"].append({"id": new_id, "image_key": key, "dummy_kind": "numeric"})
            return new_id
    return None


# ---------------- 3択→4択 追加 (T1/T3: LLMで同カテゴリ未登場ダミー) ----------------

_LLM_SYSTEM = (
    "あなたは小学校受験「お話の記憶」の設問設計者。3択の設問に4枚目のダミー絵カードを1枚だけ足す。"
    "条件: (1)候補リストのidから必ず選ぶ (2)お話に登場しない物・人 (3)既存の選択肢や正解と同カテゴリで、"
    "5歳児が正解と取り違えない明確に別の物 (4)設問の答えとして正しくなり得ないこと。"
    '出力はJSONのみ: {"add": "<候補id>"}'
)

def build_candidates(story: str, prompt_text: str, choices: list[dict],
                     labels_cache: dict, rng: random.Random, k: int = 80) -> list[tuple[str, str]]:
    used_bases = {c["id"].split("_")[0] for c in choices}
    used_bases |= {c["image_key"].split("_")[0] for c in choices}
    out = {}
    for key in ALIAS:
        if any(ch.isdigit() for ch in key):
            continue
        if "face" in key or "scene" in key or "count" in key:
            continue
        if key.split("_")[0] in used_bases:
            continue
        if not image_ok(key):
            continue
        if key not in labels_cache:
            labels_cache[key] = id_to_label(key)
        label = labels_cache[key]
        if len(label.replace(" ", "")) < 2:
            continue
        core = label.replace(" ", "")
        if core in story or core in prompt_text:
            continue  # R407: 本文登場語は category ダミーにできない
        # R407はベースID(先頭トークン)で本文照合するため、ベースの読みも必ず検査する
        # (例: kuma_murasaki_boushi は「くまむらさきぼうし」でなく「くま」で照合される)
        base_kana = id_to_label(key.split("_")[0]).replace(" ", "")
        if base_kana and base_kana in story:
            continue
        if label not in {v for _, v in out.items()}:
            out[key] = label
    items = sorted(out.items())
    rng.shuffle(items)
    return items[:k]

def add_semantic(q: dict, story: str, dry: bool, labels_cache: dict,
                 client=None, model: str = "") -> str | None:
    rng = random.Random(q["q_id"])
    cands = build_candidates(story, q["prompt_text"], q["choices"], labels_cache, rng)
    if not cands:
        return None
    if dry:
        return f"(dry: {len(cands)}候補)"
    cand_text = "\n".join(f"{k}: {v}" for k, v in cands)
    user = (f"【お話】\n{story}\n\n【設問】\n{q['prompt_text']}\n\n"
            f"【既存の選択肢】\n" + "\n".join(labels_cache.get(c["id"]) or id_to_label(c["id"]) for c in q["choices"])
            + f"\n\n【候補リスト】\n{cand_text}")
    chosen = None
    for _ in range(2):
        try:
            # sonnet-5 は adaptive thinking が max_tokens を消費するため余裕を持たせる
            resp = client.messages.create(
                model=model, max_tokens=4000, system=_LLM_SYSTEM,
                messages=[{"role": "user", "content": user}])
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r'"add"\s*:\s*"([^"]+)"', text)
            if m and any(m.group(1) == k for k, _ in cands):
                chosen = m.group(1)
                break
        except Exception as e:
            print(f"  [llm] {q['q_id']} 失敗: {e}", flush=True)
    if chosen is None:  # フォールバック: フィルタ済み候補から決定的に1個
        chosen = cands[0][0]
        print(f"  [llm] {q['q_id']} フォールバック採用: {chosen}", flush=True)
    q["choices"].append({"id": chosen, "image_key": chosen, "dummy_kind": "category"})
    return chosen


# ---------------- R308(正解位置3連続) 解消 ----------------

def fix_r308(questions: list[dict]) -> int:
    fixes = 0
    while fixes < 100:
        run, prev = 1, None
        target = None
        for q in questions:
            ids = [c["id"] for c in q["choices"]]
            pos = ids.index(q["correct"][0]) if q["correct"] and q["correct"][0] in ids else None
            if pos is not None and pos == prev:
                run += 1
                if run >= 3:
                    target = (q, pos)
                    break
            else:
                run = 1
            prev = pos
        if target is None:
            return fixes
        q, pos = target
        swap = (pos + 1) % len(q["choices"])
        q["choices"][pos], q["choices"][swap] = q["choices"][swap], q["choices"][pos]
        fixes += 1
    return fixes


# ---------------- メイン ----------------

def main():
    ap = argparse.ArgumentParser(description="選択肢の4択統一マイグレーション")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    args = ap.parse_args()
    content_dir = Path(args.content_dir)
    dry = args.dry_run

    client, model = None, ""
    if not dry:
        import anthropic
        client = anthropic.Anthropic()
        import os
        model = os.getenv("PIPELINE_ANTHROPIC_MODEL", "claude-sonnet-5").strip()

    labels_cache = {}
    stats = {"trim": 0, "trim_correct": 0, "add_numeric": 0, "add_llm": 0,
             "r308_fix": 0, "unresolved": []}

    for skel_dir in sorted(p for p in content_dir.iterdir()
                           if p.is_dir() and (p / "skeleton.json").exists()):
        for level in range(1, 6):
            qfile = skel_dir / f"lv{level}_questions.json"
            tfile = skel_dir / f"lv{level}_text.json"
            if not qfile.exists() or not tfile.exists():
                continue
            tdata = json.loads(tfile.read_text(encoding="utf-8"))
            story = "".join(s["text"] for s in tdata["scenes_text"])
            data = json.loads(qfile.read_text(encoding="utf-8"))
            questions = data["questions"] if isinstance(data, dict) else data
            changed = False
            for q in questions:
                n = len(q["choices"])
                if n == 5:
                    dropped = trim_question(q, questions, level)
                    stats["trim" if "(correct)" not in dropped else "trim_correct"] += 1
                    changed = True
                    print(f"[trim] {q['q_id']}: -{dropped}", flush=True)
                elif n == 3:
                    if q["type"] == "T2":
                        added = add_numeric(q)
                        if added:
                            stats["add_numeric"] += 1
                            changed = True
                            print(f"[add-num] {q['q_id']}: +{added}", flush=True)
                        else:
                            stats["unresolved"].append(q["q_id"])
                            print(f"[SKIP] {q['q_id']}: 数値カード追加不可(画像なし)", flush=True)
                    else:
                        added = add_semantic(q, story, dry, labels_cache, client, model)
                        if added:
                            stats["add_llm"] += 1
                            changed = not dry
                            print(f"[add-llm] {q['q_id']}: +{added}", flush=True)
                        else:
                            stats["unresolved"].append(q["q_id"])
                            print(f"[SKIP] {q['q_id']}: 候補なし", flush=True)
            if changed and not dry:
                stats["r308_fix"] += fix_r308(questions)
                qfile.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    print("\n=== サマリ ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
