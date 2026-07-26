# -*- coding: utf-8 -*-
"""
画像語彙の正規化 — review_manifest.json が参照する image_key を正規語彙に統合し、
生成に必要な情報(種別・個数・プロンプト)を付けて image_vocab.json を書き出す。

問題点(2026-07-22発見): 問題生成時にモデルが image_key を自由発明するため
boushi/bousi、face_/kao_/emotion_/kaoemoji_ など同義スラッグが大量発生する。
そのまま生成すると同じ絵を重複生産するので、生成前にここで統合する。

出力: content/pilot/image_vocab.json
  { "<canonical>": {"aliases": [...], "kind": "ai"|"count"|"dots",
                    "n": 3, "pair": false, "base": "<baseobj canonical>",
                    "prompt": "...", "contexts": [...] } }
       + alias_map.json  { "<alias>": "<canonical>" }

使い方:
    python -m pipeline.image_vocab            # ルール正規化+プロンプト起案(API使用)
    python -m pipeline.image_vocab --no-llm   # ルール正規化のみ(プロンプト空)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import CONTENT_DIR

# ---------------- 決定的正規化ルール ----------------

# 単語置換(トークン単位)。左=表記ゆれ / 右=正規形
TOKEN_MAP = {
    "bousi": "boushi", "hat": "boushi",
    "kao": "face", "kaoi": "face", "kaoiro": "face", "kaoemoji": "face",
    "emotion": "face",
    "socks": "kutsushita",
    "mafura": "mafuraa", "muffler": "mafuraa",
    "ribon": "ribbon",
    "apron": "epuron",
    "towel": "taoru",
    "tarou": "taro",
    "obaasan": "obaachan",
    "red": "aka", "blue": "ao", "yellow": "kiiro", "green": "midori",
    "black": "kuro", "purple": "murasaki", "white": "shiro",
    "aoi": "ao", "akai": "aka",
    "bag": "kaban", "basket": "kago", "gloves": "tebukuro",
    "backpack": "ryukku", "rucksack": "ryukku",
    "spring": "haru", "summer": "natsu", "autumn": "aki", "winter": "fuyu",
    "weather": "tenki", "season": "kisetsu",
    "grandma": "", "illust": "", "icon": "", "generic": "", "no": "",
    # _absent は「お話に登場しなかった」の意味でカード自体は通常のキャラ絵
    "absent": "",
    "normal": "default", "plain": "default",
    "number": "kazu", "num": "kazu", "suuji": "kazu",
    "yuki": "yuki", "snow": "yuki", "sun": "taiyou",
}

# かな数詞 → 数値
JNUM = {"hitotsu": 1, "futatsu": 2, "mittsu": 3, "yottsu": 4, "yotsu": 4,
        "itsutsu": 5, "muttsu": 6, "nanatsu": 7, "yattsu": 8,
        "ichi": 1, "ni": 2, "san": 3, "yon": 4, "go": 5, "roku": 6}

# 個数の単位サフィックス (kumi/zoku/pairs はペア扱い)
COUNT_UNIT = re.compile(r"^(?:x)?(\d+)(ko|kumi|mai|pai|hon|zoku|pairs?|pair|dai|tsu)?$")

CHAR_WORDS = {"usagi", "kuma", "kitsune", "tanuki", "risu", "buta", "neko",
              "saru", "kame", "zou", "panda", "harinezumi", "taro", "hanako",
              "obaachan", "okaasan", "kenchan", "miho", "momoka", "yuki_chan", "tori"}


def normalize(key: str) -> str:
    toks = [t for t in key.lower().split("_") if t]
    out = []
    for t in toks:
        t = TOKEN_MAP.get(t, t)
        if t:
            out.append(t)
    # face_X 系: 先頭が face でなくても face が含まれれば face_<emotion> に寄せる
    if "face" in out:
        emo = [t for t in out if t in ("ureshii", "kanashii", "okotta", "bikkuri",
                                       "kowai", "anshin", "heiki", "egao", "naki")]
        who = [t for t in out if t in CHAR_WORDS]
        if emo:
            return "_".join((who[:1] or []) + ["face", emo[0]])
    # 数詞かな → 数値 (kazu_mittsu → kazu_3)
    out = [str(JNUM[t]) if (t in JNUM and out[0] == "kazu") else t for t in out]
    # <char>_<color>_<item> の語順ゆれを吸収: 色とアイテムを並べ替え
    COLORS = {"aka", "ao", "kiiro", "midori", "kuro", "murasaki", "shiro"}
    ITEMS = {"boushi", "kaban", "kago", "tebukuro", "taoru", "mafuraa",
             "ribbon", "epuron", "ryukku", "jouro", "fukuro", "hankachi"}
    if len(out) >= 3 and out[0] in CHAR_WORDS:
        colors = [t for t in out[1:] if t in COLORS]
        items = [t for t in out[1:] if t in ITEMS]
        rest = [t for t in out[1:] if t not in COLORS and t not in ITEMS]
        # 「色1つ+持ち物1つ」だけのとき限定で語順を統一する。
        # 持ち物2つ以上(例: 赤帽子+青じょうろ)は別の絵なので潰さない。
        if len(colors) == 1 and len(items) == 1 and not rest:
            return "_".join([out[0], colors[0], items[0]])
    # 単独キャラ名は <char>_default に寄せる (usagi == usagi_default)
    if len(out) == 1 and out[0] in CHAR_WORDS:
        return f"{out[0]}_default"
    # 重複トークン除去 (kuma_kiiroepuron のような結合は対象外=そのまま)
    dedup = []
    for t in out:
        if not dedup or dedup[-1] != t:
            dedup.append(t)
    return "_".join(dedup)


def classify(canon: str) -> dict:
    """kind: dots(数字→ドットカード) / count(物×N 機械合成) / ai"""
    toks = canon.split("_")
    if toks[0] == "kazu" and len(toks) == 2 and toks[1].isdigit():
        return {"kind": "dots", "n": int(toks[1])}
    if len(toks) >= 2:
        m = COUNT_UNIT.match(toks[-1])
        if m and not toks[0].isdigit():
            n = int(m.group(1))
            unit = m.group(2) or ""
            pair = unit in ("kumi", "zoku", "pair", "pairs")
            base = "_".join(toks[:-1])
            if 1 <= n <= 10 and base and base not in ("junban", "scene", "kazu"):
                return {"kind": "count", "n": n, "pair": pair, "base": base}
    return {"kind": "ai"}


def collect_contexts() -> dict[str, list[str]]:
    """manifestから key→[設問文の例] を集める(プロンプト起案の材料)。"""
    m = json.loads((CONTENT_DIR / "review_manifest.json").read_text(encoding="utf-8"))
    ctx = defaultdict(list)
    for it in m["items"]:
        for q in it["questions"]:
            for c in q["choices"]:
                k = c.get("image_key")
                if k and len(ctx[k]) < 2:
                    ctx[k].append(f"選択肢id={c['id']} / 設問「{q['prompt_text'][:60]}」/ お話テーマ「{it['theme']}」")
    return dict(ctx)


def draft_prompts(vocab: dict, model: str = "claude-sonnet-5"):
    """kind=ai と count の base について、日本語の描画プロンプトをLLMで起案。"""
    import anthropic
    client = anthropic.Anthropic()
    targets = {k: v for k, v in vocab.items() if v["kind"] in ("ai", "count") and not v.get("prompt")}
    keys = sorted(targets.keys())
    # sonnet-5 は思考に出力トークンを使うため、max_tokens が小さいと本文が空で返る
    # （2026-07-26: BATCH=50 / max_tokens=8000 で50件ぶんのバッチが3回連続で空応答になった）。
    BATCH = 20
    MAX_TOKENS = 20000
    for i in range(0, len(keys), BATCH):
        batch = keys[i:i + BATCH]
        lines = []
        for k in batch:
            v = vocab[k]
            what = v["base"] if v["kind"] == "count" else k
            ctx = " / ".join(v.get("contexts", [])[:2]) or "(文脈なし)"
            lines.append(f"- key: {what}  (種別:{v['kind']}, 文脈: {ctx})")
        prompt = (
            "小学校受験(年長児)向け「お話の記憶」アプリの選択肢絵カードを画像生成AIで作る。"
            "各keyについて、絵の内容だけを表す簡潔な日本語プロンプト(1文、体言止め可)を書け。\n"
            "ルール: ①スタイル(水彩・色調等)は書かない(共通プレフィックスで付与済み) "
            "②文字・数字を絵に入れる指示をしない ③kindがcountのkeyは「1個だけ」の絵として書く"
            "(後で機械的にN個並べる) ④動物キャラは「〜の子ども」として親しみやすく "
            "⑤人間の子どもは「黒髪の日本人の子ども」 ⑥色指定(aka=赤 ao=青 kiiro=黄 midori=緑 kuro=黒)は正確に反映 "
            "⑦出力はJSONのみ: {\"<key>\": \"<プロンプト>\", ...} キーは入力のkeyそのまま。\n\n"
            + "\n".join(lines)
        )
        data = None
        for attempt in range(3):
            resp = client.messages.create(model=model, max_tokens=MAX_TOKENS,
                                          messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in resp.content if b.type == "text")
            text = text.replace("```json", "").replace("```", "").strip()
            if not text:
                print(f"  [draft] 応答が空({attempt + 1}回目) stop_reason={resp.stop_reason} "
                      f"out_tokens={resp.usage.output_tokens} → リトライ", flush=True)
                continue
            try:
                data = json.loads(text[text.find("{"):text.rfind("}") + 1])
                break
            except (json.JSONDecodeError, ValueError) as e:
                print(f"  [draft] JSON崩れ({attempt + 1}回目): {e} → リトライ", flush=True)
        if data is None:
            print(f"  [draft] バッチ {i} をスキップ(3回失敗)。該当キーはプロンプト未起案扱い", flush=True)
            continue
        for k in batch:
            v = vocab[k]
            what = v["base"] if v["kind"] == "count" else k
            if what in data:
                v["prompt"] = data[what].strip()
        print(f"プロンプト起案 {min(i + BATCH, len(keys))}/{len(keys)}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="画像語彙の正規化")
    ap.add_argument("--no-llm", action="store_true", help="プロンプト起案をスキップ")
    args = ap.parse_args()

    contexts = collect_contexts()
    alias_map = {}
    vocab: dict[str, dict] = {}
    for key, ctx in sorted(contexts.items()):
        canon = normalize(key)
        alias_map[key] = canon
        v = vocab.setdefault(canon, {"aliases": [], "contexts": [], **classify(canon)})
        if key != canon:
            v["aliases"].append(key)
        v["contexts"] = (v["contexts"] + ctx)[:2]

    # count の base が語彙に無ければ base 生成用エントリを足す(1個絵はbaseとして共有)
    for canon in list(vocab.keys()):
        v = vocab[canon]
        if v["kind"] == "count":
            base = v["base"]
            if base not in vocab:
                vocab[base] = {"aliases": [], "contexts": v["contexts"], "kind": "ai",
                               "_base_only": True}

    # 既存vocabのプロンプトを引き継ぐ(再起案のコストと絵柄変化を避ける)
    out = CONTENT_DIR / "image_vocab.json"
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            for canon, v in vocab.items():
                if canon in prev and prev[canon].get("prompt") and not v.get("prompt"):
                    v["prompt"] = prev[canon]["prompt"]
        except json.JSONDecodeError:
            pass

    if not args.no_llm:
        draft_prompts(vocab)

    out = CONTENT_DIR / "image_vocab.json"
    out.write_text(json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
    (CONTENT_DIR / "image_alias_map.json").write_text(
        json.dumps(alias_map, ensure_ascii=False, indent=2), encoding="utf-8")

    kinds = defaultdict(int)
    for v in vocab.values():
        kinds[v["kind"]] += 1
    merged = sum(1 for a, c in alias_map.items() if a != c)
    print(f"referenced keys: {len(alias_map)} → 正規語彙: {len(vocab)} "
          f"(統合された別名 {merged}件)", flush=True)
    print(f"種別内訳: {dict(kinds)}", flush=True)
    print(f"出力: {out}", flush=True)


if __name__ == "__main__":
    main()
