"""修復（設問の作り直し）で新しく生まれた image_key の絵カードを補充する。

repair_questions.py / repair_story.py は設問を作り直すときに新しい image_key を作るが、
絵カードの生成は呼んでいないため、ライブラリに無い絵カードが生まれる（＝子どもの画面で
選択肢が文字だけになる）。このツールはその差分だけを埋める。

  1) manifest を読み、出題中(=defect_units.json で除外されていない)の設問が参照する
     image_key を alias 解決して、PNG が無いものを洗い出す
  2) 語彙(image_vocab.json)に未登録なら追加し、kind=ai の分だけプロンプトをLLMで起案
     （既存エントリのプロンプトは触らない＝絵柄の一貫性を保つ）
  3) build_image_lib と同じ経路で生成（ai=Gemini+視覚QC+枠切除 / count=機械合成 / dots=描画）

使い方:
  python -m pipeline.fix_missing_images --plan                 # 何を作るかだけ表示(無料)
  python -m pipeline.fix_missing_images --plan --only-text-only  # 文字だけの設問に絞る
  python -m pipeline.fix_missing_images --run  --only-text-only  # 実行(課金あり)
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from pipeline.common import CONTENT_DIR
from pipeline import image_vocab as IV

IMAGES_DIR = CONTENT_DIR / "images"

# 個数カードはAI描画禁止（機械合成のみ）。ただし image_vocab.COUNT_UNIT は
# 濁音の助数詞(3bon)や「袋」を拾えないため、AI生成に流れてしまうキーを明示的に振り直す。
# 値は (base画像のキー, 個数)。base が「豆1粒」ではなく「豆の袋1つ」であるように、
# 助数詞が容器を数えている場合は base も容器の絵にする。
COUNT_OVERRIDE = {
    "himawari_3bon": ("himawari", 3),
    "mame_2fukuro": ("mame_fukuro", 2),
    "mame_3fukuro": ("mame_fukuro", 3),
    "mame_4fukuro": ("mame_fukuro", 4),
    "mame_5fukuro": ("mame_fukuro", 5),
}

# 機械合成に回せない（＝場面として1枚で描く必要がある）のに個数が意味を持つキーは、
# 個数をプロンプトに明示して人が目視で検品する。
PROMPT_SEED = {
    "mame_fukuro": "大豆がたくさん入った、口を紐でしばった布の袋 1つ",
    "suisou_kirei_kingyo_2biki":
        "水がきれいな金魚鉢。赤い金魚が ちょうど2匹だけ 泳いでいて、底に小石が敷かれている",
    # 「切符」は絵に文字が入りやすく視覚QCの has_text と衝突する（2026-07-24の既知）。
    # 文字を持たない「紙片」として描かせる。
    "mon_kippu": "うすい黄色の無地の長方形の紙が1枚。文字や数字や模様は一切ない、"
                 "角が少し丸い厚紙。白い背景に置かれている",
    "usagi_kippu": "うさぎの子どもが、うすい黄色の無地の長方形の厚紙を1枚 手に持っている。"
                   "紙には文字や数字は一切ない",
    "zou_kippu": "ぞうの子どもが、うすい黄色の無地の長方形の厚紙を1枚 鼻で持っている。"
                 "紙には文字や数字は一切ない",
    # LLMは oningyo を「くまのぬいぐるみ」と解釈した（設問はひな祭りの人形の数）
    "oningyo": "ひな祭りの おひなさまの 人形が1体だけ。着物を着た すわった人形。"
               "白い無地の背景の真ん中に大きく描く。ひな壇・他の人形・道具・文字は描かない",
    # 本文が「あおい おりがみで おりづるを おった」なので白い鶴では本文と食い違う
    "orizuru": "青い折り紙で折った 折り鶴が1羽だけ。はっきりした青色。"
               "白い無地の背景の真ん中に大きく描く。他の鶴・背景・文字は描かない",
}


# 個数カードの元絵は「1つだけ・白背景」でなければ数えられない。
# LLMに任せると「夏空の下に咲くひまわりの花」のような風景を書き、それをN枚並べても
# 「ひまわり畑の絵がN枚」になってしまう（2026-07-26 himawari_3bon で実際に発生）。
COUNT_BASE_RULE = ("白い無地の背景の真ん中に、これを1つだけ大きく描く。"
                   "風景・地面・空・他の物・文字は一切描かない")


def load(name: str, default):
    p = CONTENT_DIR / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def png_exists(key: str) -> bool:
    return (IMAGES_DIR / f"{key}.png").exists()


def scan(only_text_only: bool) -> dict:
    """足りない絵カードを洗い出す。only_text_only=True なら
    「選択肢が全部 絵なし＝文字だけ」になっている設問に絞る。"""
    manifest = load("review_manifest.json", {"items": []})["items"]
    alias = load("image_alias_map.json", {})
    defects = set(load("defect_units.json", {}).get("units", []))

    need: set[str] = set()
    units: set[str] = set()
    questions = 0
    for it in manifest:
        if it["unit_id"] in defects:      # 隔離中の話は出題されないので対象外
            continue
        for q in it["questions"]:
            keys = [c["image_key"] for c in q["choices"] if c.get("image_key")]
            if not keys:
                continue
            miss = [k for k in keys if not png_exists(alias.get(k, k))]
            if not miss:
                continue
            if only_text_only and len(miss) != len(keys):
                continue
            questions += 1
            units.add(it["unit_id"])
            need |= {alias.get(k, k) for k in miss}
    return {"need": sorted(need), "units": sorted(units), "questions": questions}


def count_base_nouns(keys: list[str], model: str = "claude-sonnet-5") -> dict:
    """個数カードの元絵キーについて「物の名前（1つぶん）」だけをLLMに出させる。
    風景を描かせないため、文はこちらで組み立てる。"""
    import anthropic
    client = anthropic.Anthropic()
    out = {}
    for i in range(0, len(keys), 20):
        batch = keys[i:i + 20]
        prompt = ("小学校受験「お話の記憶」の絵カード用に、次のローマ字キーが指す物の名前を"
                  "日本語の名詞句で1つずつ書け。数を数えるカードなので必ず『1つぶんの物』として書く"
                  "（風景・場所・複数形にしない）。色指定(aka=赤 ao=青 kiiro=黄 midori=緑 kuro=黒 "
                  "murasaki=紫)は反映する。出力はJSONのみ: {\"<key>\": \"<名詞句>\", ...}\n\n"
                  + "\n".join(f"- {k}" for k in batch))
        for _ in range(3):
            resp = client.messages.create(model=model, max_tokens=8000,
                                          messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in resp.content if b.type == "text")
            text = text.replace("```json", "").replace("```", "").strip()
            if not text:
                continue
            try:
                out.update(json.loads(text[text.find("{"):text.rfind("}") + 1]))
                break
            except (json.JSONDecodeError, ValueError):
                continue
    return out


def ensure_vocab(need: list[str], vocab: dict) -> tuple[dict, list[str]]:
    """語彙に未登録のキーを分類して追加。count の base も(無ければ)ai として登録。
    返り値: (対象キー→エントリ, プロンプト起案が必要なキー)"""
    contexts = IV.collect_contexts()
    targets: dict[str, dict] = {}
    for k in need:
        if k not in vocab:
            kind = IV.classify(k)
            if k in COUNT_OVERRIDE:
                base, n = COUNT_OVERRIDE[k]
                kind = {"kind": "count", "n": n, "pair": False, "base": base}
            vocab[k] = {"aliases": [], "contexts": contexts.get(k, []), **kind}
        elif k in COUNT_OVERRIDE and vocab[k].get("kind") != "count":
            base, n = COUNT_OVERRIDE[k]
            vocab[k].update({"kind": "count", "n": n, "pair": False, "base": base})
        if k in PROMPT_SEED:
            vocab[k]["prompt"] = PROMPT_SEED[k]     # 手書きの指定はLLMの下書きより優先
        targets[k] = vocab[k]
        base = vocab[k].get("base")
        if vocab[k]["kind"] == "count" and base:
            if base not in vocab:
                vocab[base] = {"aliases": [], "contexts": contexts.get(k, []), "kind": "ai"}
            vocab[base]["count_base"] = True      # 「1つだけ・白背景」で描かせる印
            if base in PROMPT_SEED:
                vocab[base]["prompt"] = PROMPT_SEED[base]
            if not png_exists(base):
                targets[base] = vocab[base]
    draft = [k for k, v in targets.items() if v["kind"] in ("ai", "count") and not v.get("prompt")]
    return targets, draft


def fix_count_base_prompts(targets: dict, vocab: dict) -> list[str]:
    """個数カードの元絵に「1つだけ・白背景」のプロンプトを必ず持たせる。
    すでに風景として描かれてしまった元絵は作り直しの対象として返す。"""
    bases = [k for k, v in targets.items() if v.get("count_base") and k not in PROMPT_SEED]
    todo = [k for k in bases if COUNT_BASE_RULE not in (vocab[k].get("prompt") or "")]
    if not todo:
        return []
    nouns = count_base_nouns(todo)
    fixed = []
    for k in todo:
        noun = (nouns.get(k) or "").strip()
        if not noun:
            print(f"  ⚠ {k}: 物の名前が取れなかったのでプロンプトを変更しない")
            continue
        vocab[k]["prompt"] = f"{noun}。{COUNT_BASE_RULE}"
        fixed.append(k)
    return fixed


def main():
    ap = argparse.ArgumentParser(description="修復で増えた image_key の絵カードを補充")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="生成計画を出すだけ(課金なし)")
    g.add_argument("--run", action="store_true", help="実際に生成する(課金あり)")
    ap.add_argument("--only-text-only", action="store_true",
                    help="選択肢が全部絵なし(文字だけ)の設問に絞る")
    ap.add_argument("--regen", default="",
                    help="作り直すキーをカンマ区切りで指定（元絵を指定すると、"
                         "その元絵で合成された個数カードも作り直す）")
    ap.add_argument("--fix-count-bases", action="store_true",
                    help="個数カードの元絵のプロンプトを「1つだけ・白背景」に直し、作り直す")
    args = ap.parse_args()

    vocab = load("image_vocab.json", {})
    found = scan(args.only_text_only)
    need = found["need"]

    # --regen: 指定キー（と、その元絵を使う個数カード）のPNGを消して作り直させる
    # 作り直しは必ず明示指定にする（既存の合格済みカードを勝手に描き替えないため）
    regen = sorted({k for k in args.regen.split(",") if k.strip()})
    if regen:
        derived = [k for k, v in vocab.items() if v.get("kind") == "count" and v.get("base") in regen]
        for k in regen + derived:
            p = IMAGES_DIR / f"{k}.png"
            if p.exists():
                p.unlink()
                print(f"作り直しのため削除: {k}.png")
        need = sorted(set(need) | {k for k in regen + derived if k in vocab or k in need})
        found["need"] = need

    targets, draft = ensure_vocab(need, vocab)
    if args.fix_count_bases and not args.plan:
        fixed = fix_count_base_prompts(targets, vocab)
        if fixed:
            print(f"元絵のプロンプトを「1つだけ・白背景」に直した: {len(fixed)}件")
            for k in fixed:
                print(f"  {k}: {vocab[k]['prompt']}")
            (CONTENT_DIR / "image_vocab.json").write_text(
                json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
            draft = [k for k, v in targets.items()
                     if v["kind"] in ("ai", "count") and not v.get("prompt")]

    kinds = Counter(v["kind"] for v in targets.values())
    ai_keys = sorted(k for k, v in targets.items() if v["kind"] == "ai" and not png_exists(k))
    print(f"対象の設問: {found['questions']}問 / {len(found['units'])}話")
    print(f"足りない絵カード: {len(need)}種（+ count用のbase {len(targets) - len(need)}種）")
    print(f"内訳: {dict(kinds)}")
    print(f"AI生成: {len(ai_keys)}枚 ≒ ${len(ai_keys) * 0.04:.2f} / プロンプト起案: {len(draft)}キー")
    if args.plan:
        print("\n--- AI生成する分 ---")
        for k in ai_keys:
            print(f"  {k}: {targets[k].get('prompt') or '(プロンプト未起案)'}")
        print("\n--- 機械合成する分 ---")
        for k in sorted(k for k, v in targets.items() if v["kind"] in ("count", "dots")):
            v = targets[k]
            print(f"  {k}: {v['kind']} n={v.get('n')} base={v.get('base')}")
        print("\n※ 実行するには --run")
        return

    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY が未設定です")

    if draft:
        print(f"\nプロンプト起案 {len(draft)}キー（claude-sonnet-5）…", flush=True)
        sub = {k: vocab[k] for k in draft}
        IV.draft_prompts(sub)
        for k in draft:
            if sub[k].get("prompt"):
                vocab[k]["prompt"] = sub[k]["prompt"]
        still = [k for k in draft if not vocab[k].get("prompt")]
        if still:
            print(f"  ⚠ プロンプトが起案できなかったキー {len(still)}件: {still[:5]}")
        (CONTENT_DIR / "image_vocab.json").write_text(
            json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  image_vocab.json を更新（既存プロンプトは無変更）")

    # 生成は build_image_lib に委譲（視覚QC・枠切除・needs_human の作法をそのまま使う）
    from pipeline import build_image_lib as BIL
    import sys

    # base(ai) を先に作ってから count を合成する（base が無いと合成できない）
    phase1 = [k for k, v in targets.items() if v["kind"] == "ai" and not png_exists(k)]
    phase2 = [k for k, v in targets.items() if v["kind"] in ("count", "dots") and not png_exists(k)]
    for label, keys in (("AI生成", phase1), ("機械合成", phase2)):
        if not keys:
            continue
        print(f"\n=== {label}: {len(keys)}枚 ===", flush=True)
        argv = sys.argv
        sys.argv = ["build_image_lib", "--only-keys", ",".join(keys)]
        try:
            BIL.main()
        finally:
            sys.argv = argv

    rest = scan(args.only_text_only)
    print(f"\n仕上がり: 対象の設問で絵が欠けるもの {rest['questions']}問（0なら完了）")


if __name__ == "__main__":
    main()
