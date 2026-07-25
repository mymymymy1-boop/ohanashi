# -*- coding: utf-8 -*-
"""
音声生成ステップ — QC通過ユニットにのみ ElevenLabs で音声を付ける。

使い方:
    python -m pipeline.audio --select 1:3,2:6,3:9,4:7,5:5   # Lv別に件数を割り当てて選抜(パイロット30問)
    python -m pipeline.audio --units sk_20260721_0001_lv3    # ユニット指定
    python -m pipeline.audio --all                           # QC通過分すべて

- 本文 = クローンボイス / 設問 = 松井さくら (voice IDは pipeline/tts.py・envで変更可)
- R205: 実測秒数が想定±10%を外れたら speed を再計算して1回だけ再生成(クレジット節約のため1回まで)
- 出力: content/pilot/{skeleton_id}/lv{n}_audio/story.mp3, q1.mp3..qN.mp3
- TTS課金文字数を cost.json に追記し、review用マニフェスト(review_manifest.json)を書き出す
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from pipeline.common import CONTENT_DIR
from pipeline.qc import load_unit, qc_unit
from pipeline.tts import VOICE_QUESTION, VOICE_STORY, synth_pro

# 音声バックエンド: aivis (既定・ローカルAivisSpeech Engine・生成0円) | elevenlabs
# 2026-07-21にElevenLabsは「日本語のピッチアクセントが構造的にダメ」と検品で確定し、
# 本番音声は AivisSpeech (本文=morioki / 設問=TANAKA) に全面移行済み。
# 既定が elevenlabs のままだと、env未設定の実行で**課金され、しかも声が本番と違う音声**が
# 生成される(2026-07-25に実際に54ファイル誤生成)。既定を実態に合わせる。
TTS_BACKEND = os.getenv("TTS_BACKEND", "aivis").strip().lower()
# TTS原稿の漢字化(同音語の解釈を確定させアクセント辞書を正しく引かせる)に使う軽量モデル
KANJI_MODEL = os.getenv("KANJI_MODEL", "claude-haiku-4-5").strip()


def _synth(text: str, kind: str, speed: float = 1.0):
    """バックエンドを吸収した合成。kind: 'story' | 'question'"""
    if TTS_BACKEND == "aivis":
        from pipeline.tts_aivis import STYLE_QUESTION, STYLE_STORY, synth_aivis
        style = STYLE_STORY if kind == "story" else STYLE_QUESTION
        return synth_aivis(text, style, speed)
    voice = VOICE_STORY if kind == "story" else VOICE_QUESTION
    return synth_pro(text, voice, speed)


def get_tts_script(skel_dir: Path, level: int, text_model, qset) -> tuple[str, list[str]]:
    """TTSに渡す原稿を返す。aivisバックエンドでは漢字かな交じりに変換した原稿を使う
    (かな分かち書きのままだと形態素解析が誤り、アクセント辞書を正しく引けないため)。
    変換結果は lv{n}_tts_script.json にキャッシュし、変換APIは1ユニット1回だけ呼ぶ。"""
    kana_story = " ".join(s.text for s in text_model.scenes_text)
    kana_questions = [q.prompt_text for q in qset.questions]
    if TTS_BACKEND != "aivis":
        return kana_story, kana_questions

    script_file = skel_dir / f"lv{level}_tts_script.json"
    if script_file.exists():
        data = json.loads(script_file.read_text(encoding="utf-8"))
        if len(data.get("questions", [])) == len(kana_questions):
            return data["story"], data["questions"]

    import anthropic
    client = anthropic.Anthropic()
    prompt = (
        "次のひらがな分かち書きの文章(小学校受験の読み上げ原稿)を、自然な漢字かな交じり表記に"
        "変換してください。ルール: ①読み(発音)が変わる書き換えは禁止 ②語順・語彙は一切変えない "
        "③数は「みっつ」等のかな表記のまま ④記号(○△□×)はそのまま残す "
        "⑤動物名は「うさぎ」等かなのままでよい(無理に漢字化しない) "
        "⑥出力はJSONのみ: {\"story\": \"...\", \"questions\": [\"...\"]} で questions の数は入力と同じ。\n\n"
        + json.dumps({"story": kana_story, "questions": kana_questions}, ensure_ascii=False)
    )
    resp = client.messages.create(model=KANJI_MODEL, max_tokens=4000,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    if len(data.get("questions", [])) != len(kana_questions):
        print("  [tts_script] 設問数不一致 → かな原稿のまま使用", flush=True)
        return kana_story, kana_questions
    script_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data["story"], data["questions"]

# 設問文の指示記号は画面表示用。音声化ではTTSが誤読する(○を「よん」等)ため読みに変換する。
_TTS_SYMBOL_READINGS = [
    ("○", "まる"), ("◯", "まる"), ("〇", "まる"),
    ("△", "さんかく"), ("▲", "さんかく"),
    ("□", "しかく"), ("■", "しかく"),
    ("×", "ばつ"), ("✕", "ばつ"),
]

def tts_normalize(text: str) -> str:
    """TTSに渡す直前のテキスト正規化(表示用テキストは変えない)。"""
    for sym, yomi in _TTS_SYMBOL_READINGS:
        text = text.replace(sym, yomi)
    return text


def _mp3_duration(data: bytes) -> float:
    from mutagen.mp3 import MP3
    return MP3(io.BytesIO(data)).info.length


def synth_story_with_rate_adjust(story_text: str, expected: float, out_path: Path,
                                 rate_adjust: bool = False) -> int:
    """本文を合成して課金文字数を返す。
    合成目標は 想定秒数÷STORY_SPEED_FACTOR(=1.25倍速。泰介さん耳判定 2026-07-22)。
    rate_adjust=True のときだけ、実測が目標±10%外なら speed を補正して1回再生成する。
    ※ElevenLabs(eleven_v3)は speed の効きが弱く既定OFF。aivisバックエンドは speedScale が
      正確に効くため rate_adjust=True 推奨(ローカル生成なので再生成コストも0円)。"""
    from pipeline.common import STORY_SPEED_FACTOR
    billed = 0
    story = tts_normalize(story_text)
    audio, cached, chars = _synth(story, "story", speed=1.0)
    billed += chars
    measured = _mp3_duration(audio)
    target = expected / STORY_SPEED_FACTOR if expected > 0 else 0
    if target > 0 and abs(measured - target) / target > 0.10:
        print(f"    実測{measured:.1f}s / 目標{target:.1f}s (想定{expected:.1f}s÷{STORY_SPEED_FACTOR}) ±10%外", flush=True)
        if rate_adjust:
            new_speed = max(0.5, min(2.0, measured / target))
            print(f"    speed={new_speed:.2f} で再生成", flush=True)
            audio, cached, chars = _synth(story, "story", speed=round(new_speed, 2))
            billed += chars
            print(f"    再生成後 {_mp3_duration(audio):.1f}s", flush=True)
    out_path.write_bytes(audio)
    return billed


def build_audio_for_unit(skel_dir: Path, level: int, rate_adjust: bool = False) -> dict:
    """1ユニットの音声一式を生成し、{tts_chars, status} を返す。"""
    skeleton, text, qset = load_unit(skel_dir, level)
    audio_dir = skel_dir / f"lv{level}_audio"
    audio_dir.mkdir(exist_ok=True)
    billed = 0

    story_script, q_scripts = get_tts_script(skel_dir, level, text, qset)
    print(f"  story ({text.char_count}字, backend={TTS_BACKEND})…", flush=True)
    billed += synth_story_with_rate_adjust(story_script, text.expected_duration_sec,
                                           audio_dir / "story.mp3", rate_adjust)

    for i, script in enumerate(q_scripts):
        audio, cached, chars = _synth(tts_normalize(script), "question", speed=1.0)
        billed += chars
        (audio_dir / f"q{i+1}.mp3").write_bytes(audio)

    # 音声込みQC (R602 はFAIL対象 / R205・R603 は警告として記録され人間検品に回る)
    res = qc_unit(skeleton, text, qset, audio_dir=audio_dir)
    audio_fails = [f for f in res.failures if f["rule_id"] in ("R205", "R602", "R603")]
    status = "pass" if not audio_fails else "fail"
    if audio_fails:
        print(f"  [qc-audio] FAIL: {audio_fails}", flush=True)
    return {"tts_chars": billed, "status": status, "audio_failures": audio_fails}


def _update_cost(skel_dir: Path, level: int, tts_chars: int):
    cost_file = skel_dir / "cost.json"
    cost = json.loads(cost_file.read_text(encoding="utf-8")) if cost_file.exists() else {}
    d = cost.setdefault(f"lv{level}", {"api_input_tokens": 0, "api_output_tokens": 0, "tts_chars": 0})
    d["tts_chars"] = d.get("tts_chars", 0) + tts_chars
    cost_file.write_text(json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8")


def qc_passed_units(content_dir: Path) -> list[tuple[Path, int]]:
    """直近の qc_report.jsonl から pass ユニットを列挙(なければその場でQC)。"""
    report = content_dir / "qc_report.jsonl"
    units = []
    if report.exists():
        for line in report.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["status"] != "pass":
                continue
            uid = row["q_pack_id"]                      # sk_YYYYMMDD_NNNN_lvN
            skel_name, lv = uid.rsplit("_lv", 1)
            units.append((content_dir / skel_name, int(lv)))
    return units


def select_targets(passed: list[tuple[Path, int]], select_str: str) -> list[tuple[Path, int]]:
    """"1:3,2:6,..." 形式のLv別割当でQC通過ユニットから選抜する。"""
    alloc = {}
    for part in select_str.split(","):
        lv, n = part.split(":")
        alloc[int(lv)] = int(n)
    targets = []
    for d, lv in sorted(passed, key=lambda t: (t[1], t[0].name)):
        if alloc.get(lv, 0) > 0:
            targets.append((d, lv))
            alloc[lv] -= 1
    rest = {k: v for k, v in alloc.items() if v > 0}
    if rest:
        print(f"注意: 割当を満たせなかったLvがあります: {rest} (QC通過数不足)", flush=True)
    return targets


def write_review_manifest(content_dir: Path, done_units: list[str], merge: bool = True):
    """検品キュー(review/index.html)が読むマニフェストを書き出す。
    merge=True: 既存マニフェストのユニットを保持して統合(グループ量産で上書き消失しない)。"""
    manifest = content_dir / "review_manifest.json"
    units = list(dict.fromkeys(done_units))
    if merge and manifest.exists():
        try:
            old = json.loads(manifest.read_text(encoding="utf-8")).get("items", [])
            units += [it["unit_id"] for it in old if it["unit_id"] not in set(units)]
        except (json.JSONDecodeError, KeyError):
            pass
    items = []
    for uid in sorted(units):
        skel_name, lv = uid.rsplit("_lv", 1)
        skel_dir = content_dir / skel_name
        try:
            skeleton, text, qset = load_unit(skel_dir, int(lv))
        except Exception:
            continue
        measured = None
        story_mp3 = skel_dir / f"lv{lv}_audio" / "story.mp3"
        if story_mp3.exists():
            try:
                from mutagen.mp3 import MP3
                measured = round(MP3(story_mp3).info.length, 1)
            except Exception:
                pass
        items.append({
            "unit_id": uid,
            "skeleton_id": skel_name,
            "group": getattr(skeleton, "group", None),
            "level": int(lv),
            "theme": skeleton.theme,
            "season": skeleton.season,
            "char_count": text.char_count,
            "expected_duration_sec": text.expected_duration_sec,
            "measured_duration_sec": measured,
            "story_scenes": [{"seq": s.seq, "text": s.text} for s in text.scenes_text],
            "questions": [{
                "q_id": q.q_id, "type": q.type, "prompt_text": q.prompt_text,
                "instruction": q.instruction.model_dump(),
                "correct": q.correct,
                "choices": [c.model_dump() for c in q.choices],
                "audio": f"{skel_name}/lv{lv}_audio/q{i+1}.mp3",
            } for i, q in enumerate(qset.questions)],
            "story_audio": f"{skel_name}/lv{lv}_audio/story.mp3",
        })
    manifest.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"検品マニフェスト: {manifest} ({len(items)}件)", flush=True)


def main():
    ap = argparse.ArgumentParser(description="お話の記憶 PRO 音声生成 (QC通過分のみ)")
    ap.add_argument("--content-dir", default=str(CONTENT_DIR))
    ap.add_argument("--select", default="", help="Lv別件数割当 例: 1:3,2:6,3:9,4:7,5:5")
    ap.add_argument("--units", default="", help="ユニットID指定(カンマ区切り)")
    ap.add_argument("--all", action="store_true", help="QC通過分すべて")
    ap.add_argument("--rate-adjust", action="store_true",
                    help="実測±10%外のとき speed 補正で1回再生成する(クレジット約2倍・効果は限定的)")
    args = ap.parse_args()
    content_dir = Path(args.content_dir)

    passed = qc_passed_units(content_dir)
    if not passed:
        print("QC通過ユニットがありません。先に python -m pipeline.qc を実行してください。")
        return

    targets: list[tuple[Path, int]] = []
    if args.units:
        want = set(args.units.split(","))
        targets = [(d, lv) for d, lv in passed if f"{d.name}_lv{lv}" in want]
    elif args.select:
        targets = select_targets(passed, args.select)
    elif args.all:
        targets = passed
    else:
        print("--select / --units / --all のいずれかを指定してください。")
        return

    if not targets:
        print("対象ユニットがありません（指定を確認してください）。")
        return

    total_chars = 0
    done = []
    # aivisバックエンドではエンジンの起動〜終了をこのブロックが持つ。
    # 放置されたエンジンは数GB〜十数GBを占有し続ける（2026-07-24/25に実際に発生）ため、
    # 例外・Ctrl-Cで抜けても engine_session の finally が必ず終了させる。
    if TTS_BACKEND == "aivis":
        from pipeline.tts_aivis import engine_session
        engine_ctx = engine_session()
    else:
        engine_ctx = contextlib.nullcontext()

    with engine_ctx:
        for skel_dir, level in targets:
            uid = f"{skel_dir.name}_lv{level}"
            print(f"[audio] {uid}", flush=True)
            try:
                # aivisはspeedScaleが正確&再生成0円のため常に話速補正を有効化
                r = build_audio_for_unit(skel_dir, level,
                                         rate_adjust=args.rate_adjust or TTS_BACKEND == "aivis")
            except Exception as e:
                print(f"  失敗: {e}", flush=True)
                continue
            _update_cost(skel_dir, level, r["tts_chars"])
            total_chars += r["tts_chars"]
            if r["status"] == "pass":
                done.append(uid)
            else:
                print(f"  音声QC不合格のまま: {uid}", flush=True)
                done.append(uid)  # 検品キューには載せて人間判定に回す

    write_review_manifest(content_dir, done)
    print(f"\n音声生成完了: {len(done)}ユニット / TTS課金 {total_chars}字", flush=True)


if __name__ == "__main__":
    main()
