# -*- coding: utf-8 -*-
"""P1 自動QCゲートの回帰テスト (API不要・合成フィクスチャ)。
実行: python test_p1_qc.py
"""
import copy
import sys

from models.content import QuestionSet, StorySkeleton, StoryText
from pipeline.common import count_chars, romaji_to_hiragana
from pipeline.qc import qc_unit

# ---------- フィクスチャ ----------

SKELETON = {
    "skeleton_id": "sk_20260721_9999",
    "group": "C",
    "max_level": 5,
    "theme": "こうえんの あきさがし",
    "season": "aki",
    "weather": "hare",
    "characters": [
        {"id": "usagi", "display": "うさぎさん", "attrs": {"hat_color": "aka"}},
        {"id": "zou", "display": "ぞうさん", "attrs": {"hat_color": "ao"}},
        {"id": "risu", "display": "りすさん", "attrs": {"hat_color": "kiiro"}},
    ],
    "mentioned_absent": ["kitsune"],
    "scenes": [
        {"seq": 1, "chars": ["usagi"], "event": "arrive", "numbers": {}},
        {"seq": 2, "chars": ["zou"], "event": "find", "numbers": {"donguri": 3}},
        {"seq": 3, "chars": ["usagi", "zou"], "event": "play", "numbers": {}},
        {"seq": 4, "chars": ["risu"], "event": "join", "numbers": {"kuri": 2}},
        {"seq": 5, "chars": ["usagi", "zou", "risu"], "event": "sing", "numbers": {}},
        {"seq": 6, "chars": ["usagi"], "event": "rest", "numbers": {}},
        {"seq": 7, "chars": ["zou"], "event": "share", "numbers": {"mikan": 2}},
        {"seq": 8, "chars": ["risu"], "event": "clean", "numbers": {}},
        {"seq": 9, "chars": ["usagi", "zou"], "event": "goal", "numbers": {}},
        {"seq": 10, "chars": ["usagi", "zou", "risu"], "event": "home", "numbers": {}},
    ],
    "dual_orders": {"arrival": ["usagi", "zou", "risu"], "goal": ["zou", "usagi", "risu"]},
    "quotes": [{"speaker": "usagi", "text_key": "tanoshii_ne"},
               {"speaker": "zou", "text_key": "mata_asobou"}],
    "state_changes": [{"obj": "kaban", "from": "kara", "to": "ippai"}],
    "emotions": [{"char": "risu", "emotion": "sabishii", "then": "ureshii",
                  "cause": "nakama_ni_haireta"}],
}

SCENE1 = ("あきの あさ、うさぎさんは あかい ぼうしを かぶって こうえんへ いきました。"
          "こうえんには、おおきな いちょうの きが たって いました。"
          "うさぎさんは きいろい はっぱを さんまい ひろいました。"
          "そこへ ぞうさんが あおい ぼうしを かぶって やって きました。")
SCENE2 = ("ぞうさんは どんぐりを みっつ みつけて、うさぎさんに ひとつ あげました。"
          "ふたりは ならんで べんちに すわって、おやつを たべました。"
          "りすさんも きて、みんなで たのしく うたを うたいました。"
          "かえりみち、うさぎさんは にこにこ わらって いました。")

_chars = count_chars(SCENE1 + SCENE2)
TEXT = {
    "skeleton_id": "sk_20260721_9999",
    "level": 1,
    "group": "C",
    "scenes_text": [{"seq": 1, "text": SCENE1}, {"seq": 2, "text": SCENE2}],
    "char_count": _chars,
    "expected_duration_sec": round(_chars / 280 * 60, 2),
    "speech_rate_cpm": 280,
    "new_vocab": [],
}

QUESTIONS = {"questions": [
    {
        "q_id": "q_9999_1_1", "skeleton_id": "sk_20260721_9999", "level": 1, "type": "T1",
        "instruction": {"mark": "maru", "color": "aka", "multi": False},
        "prompt_text": "あかい ぼうしを かぶって いたのは だれですか。あかい まるを つけましょう。",
        "correct": ["usagi"],
        "choices": [{"id": "usagi", "image_key": "usagi_red_hat", "dummy_kind": None},
                    {"id": "zou", "image_key": "zou_blue_hat_c", "dummy_kind": "strong"},
                    {"id": "kitsune", "image_key": "kitsune_1", "dummy_kind": "category"},
                    {"id": "buta", "image_key": "buta_1", "dummy_kind": "category"}],
        "time_limit_sec": 20,
    },
    {
        "q_id": "q_9999_1_2", "skeleton_id": "sk_20260721_9999", "level": 1, "type": "T1",
        "instruction": {"mark": "maru", "color": "aka", "multi": False},
        "prompt_text": "どんぐりを みつけたのは だれですか。あかい まるを つけましょう。",
        "correct": ["zou"],
        "choices": [{"id": "usagi", "image_key": "usagi_1", "dummy_kind": "strong"},
                    {"id": "zou", "image_key": "zou_1", "dummy_kind": None},
                    {"id": "tanuki", "image_key": "tanuki_1", "dummy_kind": "category"},
                    {"id": "kuma", "image_key": "kuma_2", "dummy_kind": "category"}],
        "time_limit_sec": 20,
    },
    {
        "q_id": "q_9999_1_3", "skeleton_id": "sk_20260721_9999", "level": 1, "type": "T1",
        "instruction": {"mark": "maru", "color": "aka", "multi": False},
        "prompt_text": "みんなと いっしょに うたを うたったのは だれですか。あかい まるを つけましょう。",
        "correct": ["risu"],
        "choices": [{"id": "kuma", "image_key": "kuma_1", "dummy_kind": "category"},
                    {"id": "tanuki", "image_key": "tanuki_2", "dummy_kind": "category"},
                    {"id": "risu", "image_key": "risu_1", "dummy_kind": None},
                    {"id": "buta", "image_key": "buta_2", "dummy_kind": "category"}],
        "time_limit_sec": 20,
    },
]}

def _load(sk=SKELETON, tx=TEXT, qs=QUESTIONS):
    return (StorySkeleton.model_validate(sk), StoryText.model_validate(tx),
            QuestionSet.model_validate(qs))

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail and not cond else ""))

# ---------- 1. 正常フィクスチャは全ルール通過 ----------
sk, tx, qs = _load()
res = qc_unit(sk, tx, qs)
check("正常Lv1ユニットがQC通過", res.passed, str(res.failures))

# ---------- 2. ローマ字→かな変換 ----------
check("romaji: kitsune", romaji_to_hiragana("kitsune") == "きつね", romaji_to_hiragana("kitsune"))
check("romaji: zou", romaji_to_hiragana("zou") == "ぞう", romaji_to_hiragana("zou"))
check("romaji: usagi_red_hat", romaji_to_hiragana("usagi_red_hat") == "うさぎ", romaji_to_hiragana("usagi_red_hat"))

# ---------- 3. 違反を仕込んで各ルールが検出されるか ----------
def rules_of(res):
    return {f["rule_id"] for f in res.failures}

# R501: 漢字混入
tx2 = copy.deepcopy(TEXT); tx2["scenes_text"][0]["text"] = SCENE1 + "公園は たのしいね。"
tx2["char_count"] = count_chars(tx2["scenes_text"][0]["text"] + SCENE2)
tx2["expected_duration_sec"] = round(tx2["char_count"] / 280 * 60, 2)
r = qc_unit(*_load(tx=tx2))
check("R501 漢字検出", "R501" in rules_of(r), str(rules_of(r)))

# R303: Lv1にT7
qs2 = copy.deepcopy(QUESTIONS); qs2["questions"][2]["type"] = "T7"
qs2["questions"][2]["evidence_scene_seq"] = [1]
r = qc_unit(*_load(qs=qs2))
check("R303 Lv1禁止タイプT7検出", "R303" in rules_of(r), str(rules_of(r)))

# R306: 制限時間違反
qs3 = copy.deepcopy(QUESTIONS); qs3["questions"][0]["time_limit_sec"] = 15
r = qc_unit(*_load(qs=qs3))
check("R306 制限時間違反検出", "R306" in rules_of(r), str(rules_of(r)))

# R103: 正解が選択肢にない
qs4 = copy.deepcopy(QUESTIONS); qs4["questions"][1]["correct"] = ["panda"]
r = qc_unit(*_load(qs=qs4))
check("R103 正解不在検出", "R103" in rules_of(r), str(rules_of(r)))

# R201: 文字数不足 (場面を短くする)
tx3 = copy.deepcopy(TEXT)
tx3["scenes_text"] = [{"seq": 1, "text": "うさぎさんが こうえんへ いきました。"},
                      {"seq": 2, "text": "ぞうさんも きました。"}]
tx3["char_count"] = count_chars("".join(s["text"] for s in tx3["scenes_text"]))
tx3["expected_duration_sec"] = round(tx3["char_count"] / 280 * 60, 2)
r = qc_unit(*_load(tx=tx3))
check("R201/R203 文字数不足検出", {"R201", "R203"} <= rules_of(r), str(rules_of(r)))

# R503: 秋の話にひまわり
tx4 = copy.deepcopy(TEXT)
tx4["scenes_text"][1]["text"] = SCENE2.replace("どんぐりを", "ひまわりを")
r = qc_unit(*_load(tx=tx4))
check("R503 季節矛盾検出", "R503" in rules_of(r), str(rules_of(r)))

# R508: NG語
tx5 = copy.deepcopy(TEXT)
tx5["scenes_text"][1]["text"] = SCENE2.replace("にこにこ わらって", "ばかと いって")
r = qc_unit(*_load(tx=tx5))
check("R508 NG語検出", "R508" in rules_of(r), str(rules_of(r)))

# R402: Lv1にattribute_swap
qs5 = copy.deepcopy(QUESTIONS); qs5["questions"][0]["choices"][1]["dummy_kind"] = "attribute_swap"
r = qc_unit(*_load(qs=qs5))
check("R402 Lv1のattribute_swap検出", "R402" in rules_of(r), str(rules_of(r)))

# R407: categoryダミーが本文に登場
qs6 = copy.deepcopy(QUESTIONS); qs6["questions"][2]["choices"][0] = \
    {"id": "zou", "image_key": "zou_2", "dummy_kind": "category"}
r = qc_unit(*_load(qs=qs6))
check("R407 category登場検出", "R407" in rules_of(r), str(rules_of(r)))

# R308: 正解位置3連続
qs7 = copy.deepcopy(QUESTIONS)
for q in qs7["questions"]:
    ids = [c["id"] for c in q["choices"]]
    cid = q["correct"][0]
    i = ids.index(cid)
    q["choices"][0], q["choices"][i] = q["choices"][i], q["choices"][0]
r = qc_unit(*_load(qs=qs7))
check("R308 正解位置偏り検出", "R308" in rules_of(r), str(rules_of(r)))

# R204: expected_duration の乖離
tx6 = copy.deepcopy(TEXT); tx6["expected_duration_sec"] = 99.0
r = qc_unit(*_load(tx=tx6))
check("R204 想定秒数乖離検出", "R204" in rules_of(r), str(rules_of(r)))

# ---------- 4. 日本語id・かなパススルー (2026-07-21 パイロットで発覚したバグの回帰) ----------
check("kana passthrough: きつねさん", romaji_to_hiragana("きつねさん") == "きつね",
      romaji_to_hiragana("きつねさん"))
check("kana passthrough: ぶたくん", romaji_to_hiragana("ぶたくん") == "ぶた",
      romaji_to_hiragana("ぶたくん"))
# mentioned_absent が日本語でも、本文に言及があれば R504 でFAILしないこと
sk8 = copy.deepcopy(SKELETON); sk8["mentioned_absent"] = ["きつねさん"]
tx8 = copy.deepcopy(TEXT); tx8["level"] = 4
tx8["scenes_text"][1]["text"] = SCENE2 + "きつねさんは かぜで こられませんでした。"
tx8["char_count"] = count_chars(tx8["scenes_text"][0]["text"] + tx8["scenes_text"][1]["text"])
tx8["expected_duration_sec"] = round(tx8["char_count"] / 280 * 60, 2)
r = qc_unit(*_load(sk=sk8, tx=tx8))
r504 = [f for f in r.failures if f["rule_id"] == "R504"]
check("R504 日本語mentioned_absentの言及を認識", not r504, str(r504))

# ---------- 結果 ----------
n_ok = sum(1 for _, c, _ in results if c)
print(f"\n{n_ok}/{len(results)} 合格")
sys.exit(0 if n_ok == len(results) else 1)
