# -*- coding: utf-8 -*-
"""
自動QCゲート — docs/handoff/qc/qc_rules.md の機械判定ルールを実装する。

使い方:
    python -m pipeline.qc                       # content/pilot 全ユニットを判定しレポート出力
    python -m pipeline.qc --content-dir <dir>   # 対象ディレクトリ指定

1ユニット = (skeleton, level) の組。1件でもFAILで問題全体をreject。
レポートは qc_report.jsonl(1行1ユニット) と qc_summary.json を content ディレクトリ直下に書く。

実装メモ(仕様からの解釈・簡略化は各ルールの detail に明記):
- 選択肢数は全Lv4択に統一(2026-07-25 泰介さん指示)。R404(T5=表情4択)との衝突は解消済み。
- R502(語彙レベル) は辞書照合の代わりに new_vocab メタデータで簡易判定(Lv1-2は空、Lv4-5は1〜2語)。
- R601(画像ライブラリ) はP1時点でライブラリ未接続のため「要生成リスト」を警告出力(FAILにしない)。
- R603(無音トリミング) は pydub+ffmpeg があるときのみ判定、なければ警告「未検証」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.content import Question, QuestionSet, StorySkeleton, StoryText
from pipeline.common import (
    CHAR_RANGE, CHOICE_COUNT, CONTENT_DIR, D_CHAR_RANGE, D_FORBIDDEN_TYPES,
    D_STORY_CHAR_RANGE, D_STORY_COUNT, FORBIDDEN_TYPES, MAX_SENTENCE_LEN,
    NG_WORDS, REQUIRED_TYPES, SCENE_CHAR_DEFAULT, SCENE_CHAR_RANGE, SCENE_COUNT,
    SEASON_MARKERS, SPEECH_RATE, count_chars, find_disallowed_chars,
    question_count_range, romaji_to_hiragana, time_limit_for, tokenize_kana,
)


class QCResult:
    def __init__(self, unit_id: str):
        self.unit_id = unit_id
        self.failures: list[dict] = []
        self.warnings: list[dict] = []
        self.missing_images: list[str] = []

    def fail(self, rule_id: str, detail: str):
        self.failures.append({"rule_id": rule_id, "detail": detail})

    def warn(self, rule_id: str, detail: str):
        self.warnings.append({"rule_id": rule_id, "detail": detail})

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self, cost: Optional[dict] = None) -> dict:
        return {
            "q_pack_id": self.unit_id,
            "status": "pass" if self.passed else "fail",
            "failures": self.failures,
            "warnings": self.warnings,
            "missing_images": self.missing_images,
            "cost": cost or {},
        }


# ---------------- 名前解決ヘルパー ----------------

def _display_map(skeleton: StorySkeleton) -> dict[str, str]:
    """id → 本文照合用のかな名。display優先、なければローマ字変換。"""
    m = {}
    for c in skeleton.characters:
        name = (c.display or "").strip() or romaji_to_hiragana(c.id)
        # 「うさぎさん」→ 照合は「うさぎ」でも当たるよう敬称を落とした形を使う
        m[c.id] = re.sub(r"(さん|くん|ちゃん)$", "", name)
    for mid in skeleton.mentioned_absent:
        m.setdefault(mid, romaji_to_hiragana(mid))
    return m

def _base_id(choice_id: str) -> str:
    """"zou_blue_hat" → "zou" のようにベースIDへ落とす。"""
    return choice_id.split("_")[0]

def _name_in_text(name: str, text: str) -> bool:
    return bool(name) and name in text


# ---------------- 本体 ----------------

def qc_unit(
    skeleton: StorySkeleton,
    text: StoryText,
    qset: QuestionSet,
    audio_dir: Optional[Path] = None,
) -> QCResult:
    """1ユニット((skeleton, level)の組)にR1xx〜R6xxを適用する。"""
    level, group = text.level, text.group
    res = QCResult(f"{skeleton.skeleton_id}_lv{level}")
    full = text.full_text()
    names = _display_map(skeleton)

    # ----- R1xx: スキーマ・構造 -----
    # R101 はロード時の pydantic 検証で担保(ここに来た時点で適合)。
    if qset.questions:
        for q in qset.questions:
            if q.skeleton_id != text.skeleton_id or q.level != text.level:
                res.fail("R102", f"{q.q_id}: skeleton_id/level が text と不一致")
    for q in qset.questions:
        ids = [c.id for c in q.choices]
        for cid in q.correct:
            if cid not in ids:
                res.fail("R103", f"{q.q_id}: 正解 {cid} が choices に存在しない")
        if len(set(ids)) != len(ids):
            res.fail("R104", f"{q.q_id}: choices の id に重複")
        keys = [c.image_key for c in q.choices]
        if len(set(keys)) != len(keys):
            res.fail("R104", f"{q.q_id}: choices の image_key に重複")

    # ----- R2xx: 文字数・場面・時間 (group=D は専用軸 2026-07-24実装) -----
    actual_chars = count_chars(full)
    lo, hi = (D_CHAR_RANGE if group == "D" else CHAR_RANGE)[level]
    if not (lo <= actual_chars <= hi):
        res.fail("R201", f"文字数 {actual_chars} が Lv{level} 規定 {lo}-{hi} の範囲外")
    if abs(actual_chars - text.char_count) > max(5, actual_chars * 0.02):
        res.warn("R201", f"char_count メタ({text.char_count})と実測({actual_chars})が乖離")

    want_scenes = D_STORY_COUNT[level] if group == "D" else SCENE_COUNT[level]
    if len(text.scenes_text) != want_scenes:
        res.fail("R202", f"場面数 {len(text.scenes_text)} が Lv{level} 規定 {want_scenes} と不一致")

    slo, shi = D_STORY_CHAR_RANGE if group == "D" else \
        SCENE_CHAR_RANGE.get(level, SCENE_CHAR_DEFAULT)
    for s in text.scenes_text:
        n = count_chars(s.text)
        if not (slo <= n <= shi):
            res.fail("R203", f"場面{s.seq} が {n}字 (規定 {slo}-{shi}字)")

    rate = text.speech_rate_cpm or SPEECH_RATE[level]
    if text.speech_rate_cpm and text.speech_rate_cpm != SPEECH_RATE[level]:
        res.warn("R204", f"speech_rate_cpm={text.speech_rate_cpm} がLvプリセット{SPEECH_RATE[level]}と異なる")
    expected = text.char_count / rate * 60
    if expected > 0 and abs(text.expected_duration_sec - expected) / expected > 0.01:
        res.fail("R204", f"expected_duration_sec={text.expected_duration_sec:.1f} が計算値 {expected:.1f} と1%超乖離")

    for sent in [s for s in re.split(r"[。！？!?]", full) if s.strip()]:
        n = count_chars(sent)
        if n > MAX_SENTENCE_LEN:
            res.fail("R206", f"1文が{n}字 (上限{MAX_SENTENCE_LEN}字): 「{sent[:20]}…」")

    # ----- R3xx: 設問構成 -----
    qlo, qhi = question_count_range(level, group)
    if not (qlo <= len(qset.questions) <= qhi):
        res.fail("R301", f"設問数 {len(qset.questions)} が規定 {qlo}-{qhi} と不一致")

    clo, chi = CHOICE_COUNT[level]  # 2026-07-25 全Lv・全グループ4択に統一
    for q in qset.questions:
        if not (clo <= len(q.choices) <= chi):
            res.fail("R302", f"{q.q_id}: 選択肢数 {len(q.choices)} が規定 {clo}-{chi} と不一致")

    types = [q.type for q in qset.questions]
    if group == "D":
        # D専用軸(心情型): T2/T9は全Lv禁止、T5(心情)が50%以上
        for t in set(types) & D_FORBIDDEN_TYPES:
            res.fail("R303", f"group=D 禁止タイプ {t} が含まれる")
        if types and types.count("T5") * 2 < len(types):
            res.fail("R304", f"group=D は T5 が50%以上必須 (現在 {types.count('T5')}/{len(types)})")
    elif level == 1:
        for t in set(types) & FORBIDDEN_TYPES[level]:
            res.fail("R303", f"Lv{level} 禁止タイプ {t} が含まれる")
        # 「T1を70%以上」— Lv1は3問構成でT2が任意許容のため 2/3(=67%) を合格とする(四捨五入判定)
        need = round(0.7 * len(types))
        if types and types.count("T1") < need:
            res.fail("R304", f"Lv1 の T1 比率 {types.count('T1')}/{len(types)} が70%未満")
    elif level == 5:
        for t in set(types) & FORBIDDEN_TYPES[level]:
            res.fail("R303", f"Lv{level} 禁止タイプ {t} が含まれる")
        got = {"T6", "T7", "T8", "T9"} & set(types)
        if len(got) < 3:
            res.fail("R304", f"Lv5 は T6/T7/T8/T9 から3タイプ以上必須 (現在 {sorted(got)})")
    else:
        for t in set(types) & FORBIDDEN_TYPES[level]:
            res.fail("R303", f"Lv{level} 禁止タイプ {t} が含まれる")
        missing = REQUIRED_TYPES.get(level, set()) - set(types)
        if missing:
            res.fail("R304", f"Lv{level} 必須タイプ不足: {sorted(missing)}")

    if group == "D":
        if types and types.count("T5") / len(types) < 0.5:
            res.fail("R305", "group=D は T5 比率 50% 以上必須")
        if "T2" in types:
            res.fail("R305", "group=D に T2 は禁止")

    tl = time_limit_for(level, group)
    for q in qset.questions:
        if q.time_limit_sec != tl:
            res.fail("R306", f"{q.q_id}: time_limit_sec={q.time_limit_sec} (規定 {tl})")

    if level >= 3:
        combos = {(q.instruction.mark, q.instruction.color) for q in qset.questions}
        if len(combos) < 2:
            res.fail("R307", "Lv3以上は (mark,color) 組合せ2種類以上必須")
        # D(心情表情4択中心)は「ぜんぶに○」型が成立しないため multi 必須を免除
        if group != "D" and not any(q.instruction.multi for q in qset.questions):
            res.fail("R307", "Lv3以上は multi:true の設問が1問必須")

    run, prev_pos = 1, None
    for q in qset.questions:
        ids = [c.id for c in q.choices]
        pos = ids.index(q.correct[0]) if q.correct and q.correct[0] in ids else None
        if pos is not None and pos == prev_pos:
            run += 1
            if run >= 3:
                res.fail("R308", f"正解位置 {pos+1}番目 が3問以上連続 ({q.q_id} 時点)")
        else:
            run = 1
        prev_pos = pos

    # ----- R4xx: ダミー配合 -----
    def dummies(kind: str) -> int:
        return sum(1 for q in qset.questions for c in q.choices if c.dummy_kind == kind)

    if level >= 3 and dummies("strong") < 1:
        res.fail("R401", f"Lv{level} は strong ダミー1個以上必須(セット全体で0)")
    if level >= 4 and dummies("strong") < 2:
        res.warn("R401", "Lv4-5 は strong 2個推奨(現在1個)")
    if level >= 4 and dummies("attribute_swap") < 1:
        res.fail("R402", "Lv4-5 は attribute_swap ダミー1個以上必須")
    if level <= 2 and dummies("attribute_swap") > 0:
        res.fail("R402", "Lv1-2 に attribute_swap は禁止")

    for q in qset.questions:
        if q.type == "T6" and level >= 4:
            if not any(c.dummy_kind == "scene_composite" for c in q.choices):
                res.fail("R403", f"{q.q_id}: T6(Lv4+) に scene_composite ダミーがない")
        if q.type == "T5":
            dks = [c.dummy_kind for c in q.choices if c.dummy_kind is not None]
            if len(q.choices) != 4 or (dks and any(dk != "emotion_set" for dk in dks)):
                res.fail("R404", f"{q.q_id}: T5 は表情4択(emotion_set)構成必須")
        if q.type == "T2":
            if not any(c.dummy_kind == "numeric" for c in q.choices):
                res.fail("R405", f"{q.q_id}: T2 に numeric ダミーがない")

    # 既知エンティティ(登場キャラ・言及キャラ・数の対象・状態変化の対象)に解決できるIDだけ本文照合する。
    # 合成ID(scene_* 等)や1文字かな(「て」等)の無理照合は誤検出になるため警告扱い。
    known_ids = ({c.id for c in skeleton.characters} | set(skeleton.mentioned_absent)
                 | {k for sc in skeleton.scenes for k in sc.numbers}
                 | {s.obj for s in skeleton.state_changes})
    for q in qset.questions:
        for c in q.choices:
            if c.dummy_kind not in ("strong", "category"):
                continue
            base = c.id if c.id in known_ids else _base_id(c.id)
            kana = names.get(base) or romaji_to_hiragana(base)
            resolvable = base in known_ids and len(kana) >= 2
            if not resolvable:
                res.warn("R406" if c.dummy_kind == "strong" else "R407",
                         f"{q.q_id}: {c.id} は既知IDに解決できず未照合(要目視)")
                continue
            if c.dummy_kind == "strong" and not _name_in_text(kana, full):
                res.fail("R406", f"{q.q_id}: strong ダミー {c.id}({kana}) が本文に登場しない")
            if c.dummy_kind == "category" and _name_in_text(kana, full):
                res.fail("R407", f"{q.q_id}: category ダミー {c.id}({kana}) が本文に登場している")

    # ----- R5xx: 本文・語彙・整合性 -----
    bad_chars = find_disallowed_chars(full)
    if bad_chars:
        res.fail("R501", f"許可外文字(漢字等): {''.join(bad_chars[:10])}")
    # 設問文は「〜に ○を つけましょう」形式(prompt 03)のため記号○△□×は許可する
    _marks = set("○◯△▲□■×✕")
    for q in qset.questions:
        qbad = [c for c in find_disallowed_chars(q.prompt_text) if c not in _marks]
        if qbad:
            res.fail("R501", f"{q.q_id}: 設問文に許可外文字: {''.join(qbad[:10])}")

    if level <= 2 and text.new_vocab:
        res.fail("R502", f"Lv1-2 はL1語彙のみ(new_vocab {len(text.new_vocab)}語) ※new_vocabメタでの簡易判定")
    if level >= 4 and not (1 <= len(text.new_vocab) <= 2):
        res.warn("R502", f"Lv4-5 はL3語彙1〜2語を意図的混入(現在{len(text.new_vocab)}語)")

    tokens = tokenize_kana(full)
    # 前方一致の誤検出除外: 「はなびら(花びら=春)」を「はなび(花火=夏)」と誤らないように
    _season_exceptions = {"はなび": ("はなびら",)}
    for season, markers in SEASON_MARKERS.items():
        if season == skeleton.season:
            continue
        for mk in markers:
            excl = _season_exceptions.get(mk, ())
            if any(tok.startswith(mk) and not tok.startswith(excl) for tok in tokens):
                res.fail("R503", f"{skeleton.season}の話に他季節({season})の「{mk}」")

    if level >= 4 and group != "D":  # D(短文心情型)に不在キャラ構造は不要
        if not skeleton.mentioned_absent:
            res.fail("R504", "Lv4以上の骨格に mentioned_absent がない")
        for mid in skeleton.mentioned_absent:
            # display名が未登録でも一般的な動物名なら照合できるようフォールバック(dual_ordersと同じ)
            kana = names.get(mid) or romaji_to_hiragana(mid)
            if not _name_in_text(kana, full):
                res.fail("R504", f"mentioned_absent {mid}({kana}) への言及が本文にない")
            else:
                used_seqs = {s.seq for s in text.scenes_text}
                acting = any(mid in sc.chars for sc in skeleton.scenes if sc.seq in used_seqs)
                if acting:
                    res.fail("R504", f"mentioned_absent {mid} が場面の行動キャラに含まれている")

    if level == 5 and group != "D" and skeleton.dual_orders:
        if len(skeleton.dual_orders) < 2:
            res.fail("R505", "Lv5 の dual_orders が2系列未満")
        else:
            for key, members in skeleton.dual_orders.items():
                hit = sum(1 for m in members if _name_in_text(names.get(m, romaji_to_hiragana(m)), full))
                if hit < 2:
                    res.warn("R505", f"dual_orders[{key}] のメンバー言及が本文で{hit}件(2件未満・要目視)")

    used_seqs = {s.seq for s in text.scenes_text}
    scene_text_by_seq = {s.seq: s.text for s in text.scenes_text}
    for q in qset.questions:
        if q.type in ("T7", "T8", "T9", "T10"):
            if not q.evidence_scene_seq:
                res.fail("R506", f"{q.q_id}: {q.type} に evidence_scene_seq がない")
                continue
            if not set(q.evidence_scene_seq) <= used_seqs:
                res.fail("R506", f"{q.q_id}: evidence_scene_seq {q.evidence_scene_seq} が使用場面外")
                continue
            ev_text = "".join(scene_text_by_seq.get(s, "") for s in q.evidence_scene_seq)
            if q.type == "T8" and ("『" not in ev_text and "「" not in ev_text):
                res.fail("R506", f"{q.q_id}: T8 の根拠場面にセリフがない")
            elif q.type == "T7":
                # 非登場の根拠は「来られなかった」等の言及。mentioned_absent名が本文全体にあればよい
                ok = any(_name_in_text(names.get(_base_id(cid), romaji_to_hiragana(_base_id(cid))), full)
                         for cid in q.correct)
                if not ok:
                    res.fail("R506", f"{q.q_id}: T7 正解の言及が本文にない")
            else:
                ok = any(_name_in_text(names.get(_base_id(cid), romaji_to_hiragana(_base_id(cid))), ev_text)
                         for cid in q.correct)
                if not ok:
                    res.warn("R506", f"{q.q_id}: 正解語が根拠場面テキストで照合できない(要目視)")

    nums = [v for sc in skeleton.scenes if sc.seq in used_seqs for v in sc.numbers.values()]
    if any(not (1 <= v <= 5) for v in nums):
        res.fail("R507", f"個々の数が1〜5の範囲外: {nums}")
    if sum(nums) > 10:
        res.fail("R507", f"数の合計 {sum(nums)} が10超")

    ng_text = full + "".join(q.prompt_text for q in qset.questions)
    for ng in NG_WORDS:
        if ng in ng_text:
            res.fail("R508", f"教育的NG語「{ng}」を検出")

    # ----- R6xx: 音声・アセット -----
    for q in qset.questions:
        for c in q.choices:
            res.missing_images.append(c.image_key)
    if res.missing_images:
        res.warn("R601", f"画像ライブラリ未接続: image_key {len(res.missing_images)}件を要生成リストに出力")

    if audio_dir is not None:
        _qc_audio(res, text, qset, audio_dir)

    return res


def _qc_audio(res: QCResult, text: StoryText, qset: QuestionSet, audio_dir: Path):
    """R205 / R602 / R603 — 音声生成後にのみ実行。"""
    try:
        from mutagen.mp3 import MP3
    except ImportError:
        res.warn("R602", "mutagen 未インストールのため音声検証をスキップ")
        return

    files = [("story", audio_dir / "story.mp3")]
    files += [(q.q_id, audio_dir / f"q{i+1}.mp3") for i, q in enumerate(qset.questions)]
    for label, path in files:
        if not path.exists():
            res.fail("R602", f"{label}: mp3 が存在しない ({path.name})")
            continue
        try:
            info = MP3(path).info
        except Exception as e:
            res.fail("R602", f"{label}: mp3 解析失敗 ({e})")
            continue
        if abs(info.bitrate - 64000) > 4000:
            res.fail("R602", f"{label}: ビットレート {info.bitrate//1000}kbps (規定64kbps)")
        if getattr(info, "channels", 1) != 1:
            res.fail("R602", f"{label}: {info.channels}ch (規定 mono)")
        if label == "story":
            from pipeline.common import STORY_SPEED_FACTOR
            # 合成目標 = 想定÷話速係数(1.25倍速。泰介さん耳判定 2026-07-22)。R205も目標基準で判定。
            exp = text.expected_duration_sec / STORY_SPEED_FACTOR
            if exp > 0 and abs(info.length - exp) / exp > 0.10:
                # README_CLAUDE_CODE.md 実装上の注意より「±10%乖離したらQC警告」。
                # eleven_v3 は speed の効きが弱く、機械では±10%に寄せ切れないため
                # 話速の最終判定は人間検品(チェックリスト「話速がLv基準±10%以内」)に委ねる。
                res.warn("R205", f"実測 {info.length:.1f}s が目標 {exp:.1f}s (想定÷{STORY_SPEED_FACTOR}) の±10%外(要耳判定)")

    try:
        from pydub import AudioSegment
        from pydub.silence import detect_leading_silence
        seg = AudioSegment.from_mp3(audio_dir / "story.mp3")
        lead = detect_leading_silence(seg) / 1000
        tail = detect_leading_silence(seg.reverse()) / 1000
        for name, v in (("冒頭", lead), ("末尾", tail)):
            if not (0.3 <= v <= 0.8):
                res.warn("R603", f"story {name}無音 {v:.2f}s (規定0.3〜0.8s)")
    except Exception:
        res.warn("R603", "pydub/ffmpeg 不在のため無音トリミング検証は未実施")


# ---------------- ロードとCLI ----------------

def load_unit(unit_dir: Path, level: int):
    """skeleton.json / lv{n}_text.json / lv{n}_questions.json を読み検証して返す。"""
    skeleton = StorySkeleton.model_validate_json((unit_dir / "skeleton.json").read_text(encoding="utf-8"))
    text = StoryText.model_validate_json((unit_dir / f"lv{level}_text.json").read_text(encoding="utf-8"))
    qraw = json.loads((unit_dir / f"lv{level}_questions.json").read_text(encoding="utf-8"))
    if isinstance(qraw, list):
        qraw = {"questions": qraw}
    qset = QuestionSet.model_validate(qraw)
    return skeleton, text, qset


def run_qc(content_dir: Path, with_audio: bool = True) -> dict:
    """content_dir 配下の全ユニットを判定し、レポート2種を書いてサマリを返す。"""
    rows = []
    rule_fail_count: dict[str, int] = {}
    missing_images: set[str] = set()

    for skel_dir in sorted(p for p in content_dir.iterdir() if p.is_dir() and (p / "skeleton.json").exists()):
        cost = {}
        cost_file = skel_dir / "cost.json"
        if cost_file.exists():
            cost = json.loads(cost_file.read_text(encoding="utf-8"))
        for level in range(1, 6):
            if not (skel_dir / f"lv{level}_text.json").exists():
                continue
            unit_id = f"{skel_dir.name}_lv{level}"
            try:
                skeleton, text, qset = load_unit(skel_dir, level)
            except (ValidationError, json.JSONDecodeError) as e:
                rows.append({"q_pack_id": unit_id, "status": "fail",
                             "failures": [{"rule_id": "R101", "detail": str(e)[:500]}],
                             "warnings": [], "missing_images": [], "cost": cost.get(f"lv{level}", {})})
                rule_fail_count["R101"] = rule_fail_count.get("R101", 0) + 1
                continue
            audio_dir = skel_dir / f"lv{level}_audio"
            res = qc_unit(skeleton, text, qset,
                          audio_dir=audio_dir if (with_audio and audio_dir.exists()) else None)
            missing_images.update(res.missing_images)
            for f in res.failures:
                rule_fail_count[f["rule_id"]] = rule_fail_count.get(f["rule_id"], 0) + 1
            rows.append(res.to_dict(cost.get(f"lv{level}", {})))

    report_path = content_dir / "qc_report.jsonl"
    with open(report_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_pass = sum(1 for r in rows if r["status"] == "pass")
    total_cost = {"api_input_tokens": 0, "api_output_tokens": 0, "tts_chars": 0}
    for r in rows:
        for k in total_cost:
            total_cost[k] += r.get("cost", {}).get(k, 0)
    est_usd = round(total_cost["api_input_tokens"] * 2e-6
                    + total_cost["api_output_tokens"] * 10e-6, 3)
    summary = {
        "units": len(rows),
        "pass": n_pass,
        "fail": len(rows) - n_pass,
        "pass_rate": round(n_pass / len(rows), 3) if rows else None,
        "rule_fail_count": dict(sorted(rule_fail_count.items())),
        "total_cost": total_cost,
        "cost_per_unit": {k: round(v / len(rows), 1) for k, v in total_cost.items()} if rows else {},
        "cost_estimate": {
            "api_usd": est_usd,
            "api_usd_per_unit": round(est_usd / len(rows), 3) if rows else None,
            "tts_credits": total_cost["tts_chars"],
            "note": "claude-sonnet-5 導入価格 $2/$10 perMTok で概算。TTSはElevenLabsクレジット=文字数",
        },
    }
    (content_dir / "qc_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (content_dir / "images_needed.json").write_text(
        json.dumps(sorted(missing_images), ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    ap = argparse.ArgumentParser(description="お話の記憶 PRO 自動QCゲート")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--no-audio", action="store_true", help="R6xx音声検証を行わない")
    args = ap.parse_args()
    summary = run_qc(Path(args.content_dir), with_audio=not args.no_audio)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
