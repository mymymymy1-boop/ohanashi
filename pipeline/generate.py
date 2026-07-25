# -*- coding: utf-8 -*-
"""
生成パイプライン — 骨格(01) → Lv別本文(02) → 設問(03) を Claude API に順に投げ、
各段でスキーマ検証 + 自動QCゲートを通して content/pilot/{skeleton_id}/ に出力する。

使い方:
    python -m pipeline.generate --count 12 --group C --seed 20260721   # パイロット生成
    python -m pipeline.generate --count 1 --levels 1                    # スモークテスト

- モデルは環境変数 PIPELINE_ANTHROPIC_MODEL (既定 claude-sonnet-5)。
- システムプロンプトの正本は docs/handoff/prompts/*.md (実行時に読み込む)。
- QC失敗はリトライ最大2回(qc_rules.md)。超過は meta.json に「生成不能」を記録して先へ進む。
- コストは cost.json (トークン数) に記録。TTS分は pipeline/audio.py が追記する。
"""
from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from models.content import QuestionSet, StorySkeleton, StoryText
from pipeline.common import (BASE, CHAR_RANGE, CONTENT_DIR, HANDOFF, PROMPT_DIR,
                             SCENE_COUNT, SEASON_MARKERS, THEMES, THEMES_C,
                             romaji_to_hiragana)
from pipeline.qc import _display_map, qc_unit

load_dotenv(BASE / ".env")

import os
MODEL = os.getenv("PIPELINE_ANTHROPIC_MODEL", "claude-sonnet-5").strip()
MAX_QC_RETRY = 2          # qc_rules.md: リトライ上限2回
MAX_API_RETRY = 3         # 一時エラー(429/5xx/タイムアウト)の自動リトライ

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY は .env / 環境変数から

_PROMPTS: dict[str, str] = {}
_SCHEMAS = json.loads(
    (HANDOFF / "schemas" / "content_schemas.json").read_text(encoding="utf-8")
)["definitions"]

# プロンプトmdは「content_schemas.json に準拠」とだけ書いてあるため、
# スキーマ本体を添付しないとモデルが独自のフィールド名を発明する(スモークテストで実証)。
_STAGE_SCHEMA = {
    "01_skeleton_gen.md": "story_skeleton",
    "02_level_expand.md": "story_text",
    "03_question_gen.md": "question",
}

def _prompt(name: str) -> str:
    if name not in _PROMPTS:
        body = (PROMPT_DIR / name).read_text(encoding="utf-8")
        schema_key = _STAGE_SCHEMA.get(name)
        if schema_key:
            schema = json.dumps(_SCHEMAS[schema_key], ensure_ascii=False, indent=1)
            note = ("\n\n## 出力スキーマ(厳守 — フィールド名・enum値はこのとおりに)\n\n"
                    f"```json\n{schema}\n```\n")
            if schema_key == "question":
                note += "\n出力は question オブジェクトの配列: {\"questions\": [ ... ]} 形式で返すこと。\n"
            body += note
        _PROMPTS[name] = body
    return _PROMPTS[name]


class CostTracker:
    """API呼び出しトークンをステージ単位で積算する。"""
    def __init__(self):
        self.data: dict[str, dict] = {}

    def add(self, stage: str, usage):
        d = self.data.setdefault(stage, {"api_input_tokens": 0, "api_output_tokens": 0, "tts_chars": 0})
        d["api_input_tokens"] += usage.input_tokens
        d["api_output_tokens"] += usage.output_tokens


def _extract_json(text: str):
    """モデル出力からJSONを取り出す(コードフェンス・前後の説明文を許容)。"""
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if 0 <= start < end:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        raise


def call_claude(system: str, user_payload: dict, cost: CostTracker, stage: str,
                extra_instruction: str = ""):
    """1ステージ分のAPI呼び出し。一時エラーは自動リトライ。JSONを返す。"""
    content = json.dumps(user_payload, ensure_ascii=False)
    if extra_instruction:
        content += "\n\n" + extra_instruction
    last_err = None
    for attempt in range(MAX_API_RETRY):
        try:
            # Sonnet 5 は adaptive thinking が既定ONで、思考トークンも max_tokens を消費する。
            # 16000 でも Lv5(設問8問) が途切れ、24000 でもグループA(長文精読・細部設問)の
            # Lv4 が途切れたため 32000 に拡大(2026-07-23)。環境変数で調整可。
            with client.messages.stream(
                model=MODEL,
                max_tokens=int(os.getenv("GEN_MAX_TOKENS", "32000")),
                system=system,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                resp = stream.get_final_message()
            cost.add(stage, resp.usage)
            if resp.stop_reason == "max_tokens":
                raise ValueError("出力が max_tokens で途切れた")
            text = "".join(b.text for b in resp.content if b.type == "text")
            return _extract_json(text)
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            print(f"  [api] {stage} {attempt+1}回目失敗: {e}", flush=True)
            if attempt < MAX_API_RETRY - 1:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{stage}: API {MAX_API_RETRY}回失敗 (最後のエラー: {last_err})")


# ---------------- 各ステージ ----------------

def gen_skeleton(skeleton_id: str, group: str, theme: str, season: str,
                 seed_note: str, cost: CostTracker,
                 feedback: str = "") -> StorySkeleton:
    extra = (
        f"補足(必ず守ること): skeleton_id は「{skeleton_id}」をそのまま使う。"
        f"group と max_level(=5) フィールドを必ず含める。"
        f'characters の id・mentioned_absent・scenes の chars は必ずローマ字スラッグ'
        f'(例 "kitsune", "usagi")で書く。日本語(きつねさん等)をidに使わない。'
        f"characters と mentioned_absent の全キャラに対応できるよう、"
        f"characters の各要素には display(「うさぎさん」形式のひらがな呼称)を必ず含める。"
        f"attrs の色・instruction系の値は content_schemas.json の enum どおり "
        f'ローマ字("aka","ao","kiiro","midori" / season は "haru","natsu","aki","fuyu")で出力する。'
        f"scenes は必ず {SCENE_COUNT[5]} 場面作る。"
        f"scenes の numbers は使用範囲(先頭2場面/4場面/6場面/8場面/全体)ごとに合計10以下に収まるよう控えめに配置する。"
    )
    if feedback:
        extra += f"\n\n前回の出力は次のQC違反でrejectされた。全て修正して再生成すること:\n{feedback}"
    payload = {"group": group, "max_level": 5, "theme_hint": theme,
               "season": season, "seed_note": seed_note}
    data = call_claude(_prompt("01_skeleton_gen.md"), payload, cost, "skeleton", extra)
    return StorySkeleton.model_validate(data)


def gen_text(skeleton: StorySkeleton, level: int, group: str, cost: CostTracker,
             feedback: str = "") -> StoryText:
    lo, hi = CHAR_RANGE[level]
    other_season = "、".join(mk for s, lst in SEASON_MARKERS.items()
                             if s != skeleton.season for mk in lst)
    extra = (
        f"補足(必ず守ること): 本文の合計文字数(空白・句読点を除いた かな文字数)は必ず {lo}〜{hi} 字。"
        f"出力前に自分で数えて範囲内に調整する。1文は45字以内。"
        f"季節は {skeleton.season}。次の他季節のことばは使用禁止: {other_season}。"
    )
    if feedback:
        extra += f"\n\n前回の出力は次のQC違反でrejectされた。全て修正して再生成すること:\n{feedback}"
    payload = {"skeleton": skeleton.model_dump(by_alias=True, exclude_none=True),
               "level": level, "group": group}
    data = call_claude(_prompt("02_level_expand.md"), payload, cost, f"lv{level}", extra)
    return StoryText.model_validate(data)


def gen_questions(skeleton: StorySkeleton, text: StoryText, level: int, group: str,
                  cost: CostTracker, feedback: str = "") -> QuestionSet:
    extra = (
        f"補足(必ず守ること): 各設問に q_id(q_{skeleton.skeleton_id[3:]}_{level}_連番) と "
        f"skeleton_id(「{skeleton.skeleton_id}」)、level({level}) を含める。"
        f"T7/T8/T9/T10 の設問には根拠場面の evidence_scene_seq を必ず含める。"
        f'choices の id は skeleton に登場する対象の id(例 "usagi", "donguri_3")か'
        f'属性替えの合成id(例 "zou_aka_hat")を使う。"c1" "c2" のような無意味なIDは禁止。'
        f"正解の位置(choicesの並び順)は設問ごとに変え、同じ位置に3問連続で正解を置かない。"
        f"ダミーの使い分け: strong=本文に登場したが正解でないもの / "
        f"category=本文に一言も登場しない同カテゴリの対象。本文に出た対象を category にしない。"
        f"設問文はすべてひらがな+カタカナ(算用数字・漢字は禁止。かずは「ふたつ」等のかな表記)。"
        f"Lv4-5では attribute_swap ダミーを必ず1個以上含める。"
        f"T5設問は選択肢4つ{{うれしい/かなしい/おこった/びっくり}}の emotion_set 構成にする。"
        f"evidence_scene_seq は このレベルで使用する場面番号(story_text の scenes_text にある seq)の範囲内のみ。"
    )
    if feedback:
        extra += f"\n\n前回の出力は次のQC違反でrejectされた。全て修正して再生成すること:\n{feedback}"
    payload = {"skeleton": skeleton.model_dump(by_alias=True, exclude_none=True),
               "story_text": text.model_dump(exclude_none=True),
               "level": level, "group": group}
    data = call_claude(_prompt("03_question_gen.md"), payload, cost, f"lv{level}", extra)
    if isinstance(data, list):
        data = {"questions": data}
    return QuestionSet.model_validate(data)


# ---------------- 機械オートフィックス (API呼び直し不要の違反を潰す) ----------------

def _autofix_qset(skeleton: StorySkeleton, text: StoryText, qset: QuestionSet):
    """API再生成せずに直せる違反を機械修正する。
    1. strong/category の貼り間違い: 本文照合で正しいラベルに貼り直す (R406/R407対策)
    2. 正解位置の3連続: 選択肢並びはこちらの管轄(プロンプト03「シャッフル前提でよい」)なので
       回転シャッフルで解消する (R308対策)
    """
    full = text.full_text()
    names = _display_map(skeleton)
    known_ids = ({c.id for c in skeleton.characters} | set(skeleton.mentioned_absent)
                 | {k for sc in skeleton.scenes for k in sc.numbers}
                 | {s.obj for s in skeleton.state_changes})
    for q in qset.questions:
        for c in q.choices:
            if c.dummy_kind not in ("strong", "category"):
                continue
            base = c.id if c.id in known_ids else c.id.split("_")[0]
            kana = names.get(base) or romaji_to_hiragana(base)
            if base in known_ids and len(kana) >= 2:
                c.dummy_kind = "strong" if kana in full else "category"

    prev, run = None, 1
    for q in qset.questions:
        ids = [c.id for c in q.choices]
        pos = ids.index(q.correct[0]) if q.correct and q.correct[0] in ids else None
        if pos is not None and pos == prev:
            run += 1
            if run >= 3:
                q.choices = q.choices[1:] + q.choices[:1]
                pos = [c.id for c in q.choices].index(q.correct[0])
                run = 1
        else:
            run = 1
        prev = pos


# ---------------- QC連動リトライ ----------------

_SKELETON_RULES = {"R507"}
_QUESTION_RULES = {"R102", "R103", "R104", "R301", "R302", "R303", "R304", "R305",
                   "R306", "R307", "R308", "R401", "R402", "R403", "R404", "R405",
                   "R406", "R407", "R506"}

def _fmt_failures(failures: list[dict]) -> str:
    return "\n".join(f"- {f['rule_id']}: {f['detail']}" for f in failures)


def _gen_text_validated(skeleton, level, group, cost, feedback="") -> StoryText:
    """スキーマ違反はエラー内容をフィードバックして1回だけ作り直す。"""
    try:
        return gen_text(skeleton, level, group, cost, feedback=feedback)
    except ValidationError as e:
        print(f"  [lv{level}] text スキーマ違反 → フィードバック再生成", flush=True)
        fb = (feedback + "\n" if feedback else "") + f"- スキーマ違反: {str(e)[:800]}"
        return gen_text(skeleton, level, group, cost, feedback=fb)


def _gen_questions_validated(skeleton, text, level, group, cost, feedback="") -> QuestionSet:
    try:
        return gen_questions(skeleton, text, level, group, cost, feedback=feedback)
    except ValidationError as e:
        print(f"  [lv{level}] questions スキーマ違反 → フィードバック再生成", flush=True)
        fb = (feedback + "\n" if feedback else "") + f"- スキーマ違反: {str(e)[:800]}"
        return gen_questions(skeleton, text, level, group, cost, feedback=fb)


def build_level(skeleton: StorySkeleton, level: int, group: str, cost: CostTracker):
    """1レベル分の text+questions を生成し、QC(音声以外)を通す。
    戻り値: (text, qset, qc_result, status)  status = "pass" | "fail" | "skeleton_ng"
    """
    text = _gen_text_validated(skeleton, level, group, cost)
    qset = _gen_questions_validated(skeleton, text, level, group, cost)

    for retry in range(MAX_QC_RETRY + 1):
        _autofix_qset(skeleton, text, qset)
        res = qc_unit(skeleton, text, qset, audio_dir=None)
        if res.passed:
            return text, qset, res, "pass"
        rules = {f["rule_id"] for f in res.failures}
        print(f"  [qc] lv{level} FAIL({retry+1}回目): {sorted(rules)}", flush=True)
        if rules & _SKELETON_RULES:
            return text, qset, res, "skeleton_ng"
        if retry >= MAX_QC_RETRY:
            break
        fb = _fmt_failures(res.failures)
        if rules <= _QUESTION_RULES:
            # 設問だけ作り直せば直る違反
            qset = _gen_questions_validated(skeleton, text, level, group, cost, feedback=fb)
        else:
            # 本文由来(文字数・場面・語彙・季節等)は本文から作り直し、設問も追随
            text = _gen_text_validated(skeleton, level, group, cost, feedback=fb)
            qset = _gen_questions_validated(skeleton, text, level, group, cost)
    return text, qset, res, "fail"


def _precheck_skeleton(skeleton: StorySkeleton, skeleton_id: str) -> list[str]:
    """骨格自体の要件チェック(NGならリトライのフィードバックにする)。"""
    problems = []
    if skeleton.skeleton_id != skeleton_id:
        problems.append(f"skeleton_id は「{skeleton_id}」でなければならない")
    if getattr(skeleton, "group", "") == "D":
        # D専用軸(短文・心情型): 3つの独立した小さな話。不在キャラ・二重順序は不要。
        if len(skeleton.scenes) < 3:
            problems.append(f"group=D は scenes が3話ぶん必要 (現在{len(skeleton.scenes)})")
        if not skeleton.emotions:
            problems.append("group=D は emotions(気持ちの動き+原因)が必須")
        if not skeleton.quotes:
            problems.append("quotes(話者明示のセリフ)が1つ以上必要")
        return problems
    if len(skeleton.scenes) < SCENE_COUNT[5]:
        problems.append(f"scenes が {len(skeleton.scenes)} 場面(Lv5展開には{SCENE_COUNT[5]}場面必要)")
    if not skeleton.mentioned_absent:
        problems.append("max_level>=4 のため mentioned_absent が1〜2体必要")
    for mid in skeleton.mentioned_absent:
        # 不在キャラの構造検証(ここで弾かないと本文をどう書いてもR504不合格の骨格になる)
        if not (_display_map(skeleton).get(mid) or romaji_to_hiragana(mid)):
            problems.append(f"mentioned_absent「{mid}」の表示名が解決できない(charactersに登録する)")
        if any(mid in sc.chars for sc in skeleton.scenes):
            problems.append(f"mentioned_absent「{mid}」が場面の行動キャラに入っている(不在キャラは scenes の chars に入れない)")
    if len(skeleton.dual_orders) < 2:
        problems.append("max_level=5 のため dual_orders が2系列必要")
    if len(skeleton.quotes) < 2:
        problems.append("quotes(話者明示のセリフ)が2つ必要")
    if not skeleton.state_changes:
        problems.append("state_changes が1つ必要")
    if not skeleton.emotions:
        problems.append("emotions(感情変化+原因)が1件必要")
    for upto in (2, 4, 6, 8, 10):
        nums = [v for sc in skeleton.scenes[:upto] for v in sc.numbers.values()]
        if any(not (1 <= v <= 5) for v in nums) or sum(nums) > 10:
            problems.append(f"先頭{upto}場面の numbers 合計が10超または1〜5範囲外: {nums}")
            break
    return problems


# ---------------- 骨格1本の実行 ----------------

def _run_level_and_save(skeleton: StorySkeleton, level: int, group: str,
                        cost: CostTracker, out_dir: Path, meta: dict):
    """1レベル生成→保存→metaに結果を記録する。"""
    print(f"  Lv{level} 生成中…", flush=True)
    try:
        text, qset, res, status = build_level(skeleton, level, group, cost)
    except (ValidationError, RuntimeError) as e:
        print(f"  [lv{level}] 生成失敗: {e}", flush=True)
        meta["levels"][f"lv{level}"] = {"status": "生成不能", "error": str(e)[:300]}
        return
    (out_dir / f"lv{level}_text.json").write_text(
        text.model_dump_json(exclude_none=True, indent=2), encoding="utf-8")
    # questions は dummy_kind: null が required フィールドのためスキーマどおり null を残す
    (out_dir / f"lv{level}_questions.json").write_text(
        qset.model_dump_json(indent=2), encoding="utf-8")
    meta["levels"][f"lv{level}"] = {
        "status": status,
        "qc_failures": res.failures,
        "chars": text.char_count,
        "questions": len(qset.questions),
    }
    if status == "skeleton_ng":
        print(f"  [lv{level}] 骨格由来のQC違反 → この骨格は人間キューへ", flush=True)

def build_skeleton_unit(skeleton_id: str, group: str, theme: str, season: str,
                        seed_note: str, levels: list[int]) -> dict:
    """骨格1本を生成し、指定レベルを展開して保存する。meta(結果概要)を返す。"""
    out_dir = CONTENT_DIR / skeleton_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cost = CostTracker()
    meta = {"skeleton_id": skeleton_id, "group": group, "theme": theme, "season": season,
            "seed_note": seed_note, "model": MODEL,
            "generated_at": datetime.datetime.now().isoformat(), "levels": {}}

    # --- 骨格(検証NGはフィードバック付きで最大2回作り直し) ---
    skeleton, feedback = None, ""
    for attempt in range(MAX_QC_RETRY + 1):
        try:
            skeleton = gen_skeleton(skeleton_id, group, theme, season, seed_note, cost, feedback)
        except ValidationError as e:
            feedback = f"- スキーマ違反: {str(e)[:800]}"
            print(f"  [skeleton] スキーマ違反({attempt+1}回目): {str(e)[:300]}", flush=True)
            continue
        problems = _precheck_skeleton(skeleton, skeleton_id)
        if not problems:
            break
        feedback = "\n".join(f"- {p}" for p in problems)
        print(f"  [skeleton] 要件NG({attempt+1}回目): {problems}", flush=True)
    else:
        meta["status"] = "生成不能(骨格)"
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    (out_dir / "skeleton.json").write_text(
        skeleton.model_dump_json(by_alias=True, exclude_none=True, indent=2), encoding="utf-8")

    # --- レベル展開 ---
    for level in levels:
        _run_level_and_save(skeleton, level, group, cost, out_dir, meta)

    meta["status"] = "done"
    (out_dir / "cost.json").write_text(json.dumps(cost.data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


# ---------------- リペア(未合格レベルの作り直し) ----------------

def repair_existing(levels: list[int], group: str):
    """既存骨格の未合格・未生成レベルだけを作り直す(骨格の再生成コストを払わない)。"""
    repaired = 0
    for skel_dir in sorted(p for p in CONTENT_DIR.iterdir()
                           if p.is_dir() and (p / "skeleton.json").exists()):
        skeleton = StorySkeleton.model_validate_json(
            (skel_dir / "skeleton.json").read_text(encoding="utf-8"))
        meta_file = skel_dir / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else \
            {"skeleton_id": skeleton.skeleton_id, "group": group, "levels": {}}
        meta.setdefault("levels", {})
        need = [lv for lv in levels
                if meta["levels"].get(f"lv{lv}", {}).get("status") != "pass"
                or not (skel_dir / f"lv{lv}_text.json").exists()]
        if not need:
            continue
        # 骨格が構造検証NG(例: 不在キャラが場面で行動)の場合、Lv4+は本文をどう
        # 書き直してもR504不合格になる。無駄なリトライ課金を避けてスキップする。
        problems = _precheck_skeleton(skeleton, skeleton.skeleton_id)
        if problems:
            fixable = [lv for lv in need if lv < 4]
            skipped = [lv for lv in need if lv >= 4]
            if skipped:
                print(f"[repair] {skeleton.skeleton_id} Lv{skipped} をスキップ"
                      f"(骨格の構造検証NG: {problems[:2]})", flush=True)
            need = fixable
            if not need:
                continue
        # 修復は骨格自身のgroupで行う(引数groupで別グループの骨格を作り直さない)
        skel_group = getattr(skeleton, "group", None) or meta.get("group") or group
        print(f"[repair] {skeleton.skeleton_id} (group={skel_group}) → Lv{need}", flush=True)
        cost = CostTracker()
        for level in need:
            _run_level_and_save(skeleton, level, skel_group, cost, skel_dir, meta)
            repaired += 1
        # 既存 cost.json に加算マージ
        cost_file = skel_dir / "cost.json"
        old = json.loads(cost_file.read_text(encoding="utf-8")) if cost_file.exists() else {}
        for stage, d in cost.data.items():
            o = old.setdefault(stage, {"api_input_tokens": 0, "api_output_tokens": 0, "tts_chars": 0})
            for k in ("api_input_tokens", "api_output_tokens"):
                o[k] = o.get(k, 0) + d[k]
        cost_file.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        meta["repaired_at"] = datetime.datetime.now().isoformat()
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[repair] {repaired} レベルを作り直しました", flush=True)


# ---------------- CLI ----------------

def next_skeleton_id() -> str:
    """sk_YYYYMMDD_NNNN 形式で既存の続き番号を発番する。"""
    today = datetime.date.today().strftime("%Y%m%d")
    n = 0
    if CONTENT_DIR.exists():
        for p in CONTENT_DIR.iterdir():
            if p.is_dir() and p.name.startswith("sk_"):
                try:
                    n = max(n, int(p.name.split("_")[2]))
                except (IndexError, ValueError):
                    pass
    return f"sk_{today}_{n + 1:04d}"


def main():
    ap = argparse.ArgumentParser(description="お話の記憶 PRO 生成パイプライン")
    ap.add_argument("--count", type=int, default=1, help="生成する骨格数")
    ap.add_argument("--group", default="C")
    ap.add_argument("--levels", default="1-5", help="展開するLv (例: 1-5, 3, 1,3,5)")
    ap.add_argument("--seed", type=int, default=None, help="テーマ/季節選択の乱数シード(再現用)")
    ap.add_argument("--repair", action="store_true",
                    help="既存骨格の未合格・未生成レベルだけ作り直す(新規骨格は作らない)")
    args = ap.parse_args()

    if "-" in args.levels:
        lo, hi = args.levels.split("-")
        levels = list(range(int(lo), int(hi) + 1))
    else:
        levels = [int(x) for x in args.levels.split(",")]

    if args.repair:
        repair_existing(levels, args.group)
        return

    seed = args.seed if args.seed is not None else random.randrange(10 ** 8)
    rng = random.Random(seed)
    pool = THEMES[args.group.upper()]
    themes = rng.sample(pool, k=min(args.count, len(pool)))
    seasons = ["haru", "natsu", "aki", "fuyu"]

    print(f"モデル: {MODEL} / 骨格 {args.count} 本 / Lv {levels} / seed={seed}", flush=True)
    results = []
    for i in range(args.count):
        skeleton_id = next_skeleton_id()
        theme = themes[i % len(themes)]
        season = seasons[i % 4]
        seed_note = f"seed={seed} idx={i} theme={theme} season={season}"
        print(f"[{i+1}/{args.count}] {skeleton_id} 「{theme}」({season})", flush=True)
        meta = build_skeleton_unit(skeleton_id, args.group, theme, season, seed_note, levels)
        results.append(meta)

    ok = sum(1 for m in results
             for lv in m.get("levels", {}).values() if lv.get("status") == "pass")
    total = sum(len(m.get("levels", {})) for m in results)
    print(f"\n完了: {ok}/{total} ユニットがQC通過。詳細は各 meta.json / python -m pipeline.qc で確認。", flush=True)


if __name__ == "__main__":
    main()
