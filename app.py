# -*- coding: utf-8 -*-
"""
おはなしのきおく  -  小学校受験「お話の記憶」練習サーバー
- 問題文を Anthropic API で生成
- 読み上げ音声を ElevenLabs で生成（同一テキストはキャッシュしてクレジット節約）
- イラスト選択肢をタップして解答
"""
import os, json, hashlib, datetime, webbrowser, threading, random
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
import requests
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "").strip()
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
ELEVENLABS_MODEL    = os.getenv("ELEVENLABS_MODEL", "eleven_v3").strip()

# ネット公開時のパスワード保護（APP_PASSWORD が空ならローカル扱いで認証なし）
APP_USERNAME = os.getenv("APP_USERNAME", "ohanashi").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

def _parse_pairs(s):
    out = []
    for item in (s or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, vid = item.split(":", 1)
            out.append({"name": name.strip(), "voice_id": vid.strip()})
        else:
            out.append({"name": item, "voice_id": item})
    return out

COMPARE_VOICES = _parse_pairs(os.getenv("COMPARE_VOICES", "Rachel:21m00Tcm4TlvDq8ikWAM"))
COMPARE_MODELS = [m.strip() for m in (os.getenv("COMPARE_MODELS", "eleven_v3").split(",")) if m.strip()]

# 声くらべ用の標準サンプル文（数・固有名詞・順序を含み、誤読チェックに最適）
COMPARE_SAMPLE = (
    "あきこさんは、おかあさんと いっしょに こうえんへ いきました。"
    "さいしょに、あかい ふうせんを ふたつ かいました。"
    "つぎに、ベンチで みかんを みっつ たべました。"
    "さいごに、しろい いぬと あおい ことりを みつけました。"
    "ぜんぶで、どうぶつは にひき いましたね。"
)

AUDIO_DIR = BASE / "audio_cache"
AUDIO_DIR.mkdir(exist_ok=True)
LOG_FILE  = BASE / "usage_log.csv"

app = Flask(__name__, static_folder=str(BASE / "static"))

# ---------------- 簡易パスワード保護（クレジット悪用防止） ----------------
@app.before_request
def _require_auth():
    # APP_PASSWORD 未設定（ローカル開発）なら認証なしで通す
    if not APP_PASSWORD:
        return
    auth = request.authorization
    if auth and auth.username == APP_USERNAME and auth.password == APP_PASSWORD:
        return
    return Response("認証が必要です", 401,
                    {"WWW-Authenticate": 'Basic realm="ohanashi"'})

# ---------------- 問題生成 ----------------
LEVEL_SPEC = {
    "keio": (
        "【慶應義塾横浜初等部レベル】長文（10〜12文）。登場人物が複数（2〜3人）、"
        "場面転換が2回以上、色・数・位置・順序が複合的に登場する。時系列や因果を含み難度は高め。"
        "設問は5問、うち1〜2問は「○番目に出てきたもの」「○○が持っていたもの」など順序・対応記憶を問う。"
    ),
    "toin": (
        "【桐蔭学園小学部レベル】中程度（7〜9文）。登場人物1〜2人、場面転換1回程度。"
        "色・数・誰が何をしたかを素直に問う。設問は4問。やや易しめだが油断できない長さ。"
    ),
}

# 実際の小学校受験「お話の記憶」で出題される設問パターン（ネット上の過去問・プロ解説を調査して作成）
# 毎回ここからランダムに選ぶことで、出題の偏り（色×動物・人×持ち物に集中）を防ぐ。
# key=苦手分析のカテゴリー識別子（設問ごとに type として出力させ集計に使う）
QUESTION_PATTERNS = [
    {"key": "数",        "desc": "数を問う（いくつ拾った・何個・何人・何匹）"},
    {"key": "色",        "desc": "色を問う（風船・服・花・持ち物などの色）"},
    {"key": "順番",      "desc": "出てきた順番／何番目かを問う（最初に・2番目に・最後に出てきたのは）"},
    {"key": "人物と行動", "desc": "誰が何をしたか（人物と行動の対応。例：かけっこで転んだのは誰）"},
    {"key": "持ち物",    "desc": "誰が何を持っていたか（持ち物と人物の対応）"},
    {"key": "場所",      "desc": "どこへ行ったか・どこにあったか（場所・目的地。例：山か湖か）"},
    {"key": "乗り物",    "desc": "何に乗って行ったか（乗り物・移動手段）"},
    {"key": "季節",      "desc": "このお話の季節はいつか（春夏秋冬。手がかりは情景に自然に入れる）"},
    {"key": "天気",      "desc": "そのときの天気はどうだったか（晴れ・くもり・雨・雪）"},
    {"key": "気持ち",    "desc": "登場人物の気持ち・表情を問う（その場面の出来事に合った気持ち。うれしい・かなしい・びっくり・こわい・おこった・ざんねん・あんしん等から毎回ちがう気持ちを選び『うれしい』に偏らせない。選択肢は4つの顔の絵文字😊😢😮😠など）"},
    {"key": "食べ物",    "desc": "誰が何を食べた／飲んだか・好き嫌い（食べたい物／食べたくない物）"},
    {"key": "服装",      "desc": "登場人物の服装の特徴（帽子・くつ・かばんなどの色や種類）"},
    {"key": "位置",      "desc": "位置関係を問う（列のまんなか・先頭は誰か／誰のとなりに座ったか／かばんや棚の中で何のとなり・上下に置いたか等。お話の中で実際にはっきり述べた配置だけを問い、『雪だるまの頭はどこ→上』のように当たり前すぎる・自明な問いは絶対に作らない）"},
    {"key": "出てこない", "desc": "お話に出てこなかったものはどれか（3つは登場、1つだけ登場しない）"},
    {"key": "数の変化",  "desc": "数の変化を問う（増えた・減った・残りはいくつ・合計でいくつ）"},
    {"key": "発言",      "desc": "誰が何と言ったか・どんな約束をしたか"},
    {"key": "時間",      "desc": "時間帯を問う（朝・昼・夜）または何曜日か"},
    {"key": "行事",      "desc": "季節の行事・イベント（ひな祭り・節分・運動会・お月見など）"},
]
PATTERN_BY_KEY = {p["key"]: p for p in QUESTION_PATTERNS}

# お話の舞台・題材。毎回ここからランダムに1つ選び、公園・動物園への偏りを防ぐ。
STORY_THEMES = [
    "近所の公園での出来事", "動物園や水族館への遠足", "家族でのキャンプや山のぼり",
    "海や川での水あそび・つり", "スーパーや商店街でのお買いもの", "夏祭り・盆おどり・花火大会",
    "運動会・かけっこ大会", "お誕生日会・お楽しみ会", "おじいちゃんおばあちゃんの家へのお泊まり",
    "電車やバスに乗ってのお出かけ", "雪の日の雪あそび・雪だるま作り", "畑でのいもほり・野さいの収かく",
    "幼稚園・保育園での一日", "おうちでのお手つだい（料理やそうじ）", "雨の日のおうちあそび",
    "七夕やお月見の行事", "お正月・節分・ひな祭り", "クリスマス会",
    "野原や公園での虫とり", "お花見・どんぐりひろい・落ち葉ひろい", "プール・海水浴",
    "図書館や絵本の世界", "パン屋・ケーキ屋さんやおかし作り", "牧場や農場での動物のお世話",
    "あたらしいペットをむかえる日", "遠くの親せきをたずねる旅", "海べでの貝がらひろい",
    "駅やデパートでのまいごと再会", "森の動物たちのお話", "お祭りの金魚すくい・屋台めぐり",
]

# お話の主人公。毎回変えて単調さを防ぐ。題材と矛盾しないよう自然に選ぶ。
PROTAGONISTS = [
    "元気な男の子", "やさしい女の子", "なかよし兄妹（兄と妹）", "姉と弟",
    "家族みんな（おとうさん・おかあさんと子ども）", "なかよしのお友だち2〜3人",
    "おじいちゃんと孫", "森の動物たち（うさぎ・くま・りすなど）",
]

# お話の「構成（物語の展開の型）」。毎回ここから1つ選び、毎回おなじ流れ（おでかけ→順に体験→帰る）に偏らないようにする。
# どの型でも「色・数・順番・持ち物…」など思い出す手がかりを本文に自然に盛り込める形にしてある。
# ※1番目は従来の既定構成（時系列のおでかけ型）をそのまま残したもの。
STORY_STRUCTURES = [
    {"key": "時系列おでかけ",
     "desc": "時系列のおでかけ型。主人公がどこかへ出かけ、最初に→つぎに→さいごに、と順番にいくつかの出来事を体験して帰ってくる。シンプルで王道。"},
    {"key": "こまった解決",
     "desc": "小さな事件・解決型。とちゅうで困ったこと（ころんだ・道にまよった・物がこわれた・けんか・雨がふってきた等）が起き、みんなで力を合わせて解決して、さいごはほっとする。"},
    {"key": "さがし物",
     "desc": "さがし物型。大切な物や生きもの（ぼうし・おもちゃ・ねこ・弟など）がいなくなり、あちこち（いくつかの場所）をじゅんばんに探し回り、さいごに見つかる。どこを探したか・どこで見つかったかが手がかりになる。"},
    {"key": "準備と本番",
     "desc": "準備〜本番型。お誕生日会・運動会・遠足などの行事にむけて、必要な物をそろえたり練習したりして準備し、当日をむかえて本番を楽しむ。準備で用意した物・当日の出来事が手がかりになる。"},
    {"key": "おてつだい・親切",
     "desc": "おてつだい・親切型。困っている人や動物に出会い、いくつかのことをしてあげて助ける。さいごにお礼をもらったり感謝されたりして、あたたかい気持ちでおわる。"},
    {"key": "あつめて作る",
     "desc": "集めて作る型。料理・工作・収かくなど、材料や物をじゅんばんに集めたり、手順どおりに作業したりして、さいごに一つのものを完成させる。集めた物・作った順番・できあがった物が手がかりになる。"},
]

# 「気持ち」を問う設問で目標にする感情。毎回ランダムに選び「うれしい」偏りを防ぐ。
EMOTION_TARGETS = [
    "うれしい・しあわせ", "かなしい・さみしい", "びっくり・おどろいた",
    "どきどき・きんちょう", "こわい・ふあん", "おこった・ぷんぷん",
    "ざんねん・がっかり", "ほっとした・あんしん", "わくわく・たのしみ", "はずかしい",
]

def build_prompt(level: str, focus=None) -> str:
    spec = LEVEL_SPEC.get(level, LEVEL_SPEC["keio"])
    n_q = 5 if level == "keio" else 4
    # 苦手練習(focus)指定があればそのカテゴリーを優先採用、足りない分はランダム補充
    if focus:
        chosen = [PATTERN_BY_KEY[k] for k in focus if k in PATTERN_BY_KEY][:n_q]
        if len(chosen) < n_q:
            rest = [p for p in QUESTION_PATTERNS if p not in chosen]
            chosen += random.sample(rest, n_q - len(chosen))
    else:
        chosen = random.sample(QUESTION_PATTERNS, n_q)
    random.shuffle(chosen)
    lines = []
    for i, p in enumerate(chosen):
        extra = ""
        # 気持ちパターンは「うれしい」偏りを防ぐため、毎回ちがう目標感情を指定する
        if p["key"] == "気持ち":
            target = random.choice(EMOTION_TARGETS)
            extra = f"（今回は主人公が『{target}』という気持ちになる出来事をお話に自然に入れて、その気持ちを問う。正解は『{target}』系にする）"
        lines.append(f"  ・設問{i+1}は「{p['desc']}」のパターンで出す。type は \"{p['key']}\"。{extra}")
    patterns_text = "\n".join(lines)
    # 毎回ちがう舞台・主人公・構成を選ぶ（題材や展開の偏りを防ぐ）
    theme = random.choice(STORY_THEMES)
    hero = random.choice(PROTAGONISTS)
    structure = random.choice(STORY_STRUCTURES)
    return f"""あなたは日本の私立小学校受験（年長児・6歳）向けの「お話の記憶」問題作成のプロです。以下の条件で問題を1セット作ってください。

{spec}

★今回の舞台・題材：「{theme}」。このお話は必ずこの場面を舞台にすること。公園や動物園に安易に寄せず、指定の舞台で作る。
★今回の主人公：{hero}。題材に合うように登場させ、名前も毎回ちがうものにする（前回と同じ名前を使わない）。

★今回の構成（物語の展開の型）：{structure['desc']}　この展開の型でお話を組み立てる。毎回おなじ流れに寄せず、指定の型で作る。ただし型に無理に当てはめて不自然にはせず、舞台・主人公に自然になじませる。

★今回の設問パターン（必ずこの指定どおり、1問ずつ別パターンで作ること。同じパターンを繰り返さない）:
{patterns_text}
　→ 上記パターンで問うために必要な要素（色・数・順番・天気・気持ち・持ち物・場所など）を、お話の本文に自然に盛り込んでから出題すること。設問の順番は入れ替えてよい。

重要ルール:
- 6歳児が音声で聞いて理解できる、やさしく具体的な日本語。1文は短め。
- お話は上記「★今回の舞台・題材」に必ず沿わせる。安易に公園・動物園・遠足に寄せない。登場人物の名前・持ち物・展開も毎回ちがうものにする。
- 各設問は必ず4択。選択肢は絵カードで表示するので、それぞれ絵文字(emoji)1つと短いラベル(2〜5文字)で表せるものにする。
- 正解はお話の内容から一意に決まること。ひっかけ選択肢も自然なものにする。
- 設問文は子ども向けのやさしい口調。
- 「出てこなかったもの」を問う場合は、ひっかけ3つは本当にお話に登場させ、正解1つだけ登場させないこと。
- 【重要】設問は、お話の中で実際に・はっきり描写された“思い出す価値のある”事実だけを問う。指定パターンを無理に当てはめて、当たり前すぎる問いや不自然な問い（例：「雪だるまの頭はどこ→上」）は作らない。そのパターンが今回のお話に自然に作れない場合は、お話の方に手がかりとなる場面を自然に足してから問う。
- 気持ちを問う設問は、その出来事に本当に合う気持ちにし、毎回同じ（特に「うれしい」）に偏らせない。

各設問には、上で指定した type（カテゴリー識別子。例: 数・色・順番・気持ち・位置 など）を必ず付けること。

必ず以下のJSON形式のみで出力。前後の説明やマークダウン、コードフェンスは一切付けない:
{{
  "story": "お話の本文。読み上げる文章をそのまま。",
  "questions": [
    {{
      "q": "設問文",
      "type": "数",
      "choices": [
        {{"emoji":"🍎","label":"りんご","correct":true}},
        {{"emoji":"🍌","label":"バナナ","correct":false}},
        {{"emoji":"🍇","label":"ぶどう","correct":false}},
        {{"emoji":"🍊","label":"みかん","correct":false}}
      ]
    }}
  ]
}}"""

def generate_story(level: str, focus=None) -> dict:
    """Anthropic APIで問題1セットを生成して dict を返す。失敗時は例外。"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": build_prompt(level, focus)}],
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

@app.route("/api/story")
def api_story():
    level = request.args.get("level", "keio")
    # 苦手練習: focus=順番,位置 のようにカテゴリーを指定するとそのパターンを優先出題
    focus = [k.strip() for k in (request.args.get("focus", "") or "").split(",") if k.strip()]
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": ".env に ANTHROPIC_API_KEY を設定してください"}), 400
    try:
        parsed = generate_story(level, focus or None)
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": f"問題生成に失敗しました: {e}"}), 500

# ---------------- 音声生成（キャッシュ付き） ----------------
def log_usage(kind: str, chars: int, cached: bool):
    new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,kind,chars,cached\n")
        f.write(f"{datetime.datetime.now().isoformat()},{kind},{chars},{int(cached)}\n")

def synth(text, voice_id, model_id, speed=1.0):
    """ElevenLabsで音声生成。(bytes, from_cache) を返す。失敗時は例外。speedは0.7〜1.2。"""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.7, min(1.2, speed))
    # speedごとに別キャッシュ（速度を変えても正しく作り分ける）
    key = hashlib.md5(f"{voice_id}:{model_id}:{speed}:{text}".encode("utf-8")).hexdigest()
    cache_path = AUDIO_DIR / f"{key}.mp3"
    if cache_path.exists():
        log_usage("story" if len(text) > 60 else "question", len(text), True)
        return cache_path.read_bytes(), True

    voice_settings = {
        "stability": 0.80,
        "similarity_boost": 0.70,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    # 等速(1.0)のときは speed を送らない（非対応モデルでのエラーを避ける）
    if abs(speed - 1.0) > 0.001:
        voice_settings["speed"] = speed
    payload = {
        "text": text,
        "model_id": model_id,
        # 日本語を明示して誤読・言語誤認を抑える
        "language_code": "ja",
        # 試験官の落ち着いた読み聞かせに寄せる：安定性高め・抑揚控えめ
        "voice_settings": voice_settings,
    }

    def _post(pl):
        return requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json=pl, timeout=90,
        )

    r = _post(payload)
    # language_code 非対応モデルの場合に備えてリトライ
    if r.status_code >= 400 and "language_code" in (r.text or ""):
        payload.pop("language_code", None)
        r = _post(payload)
    # speed 非対応モデルの場合に備えてリトライ（speedを外す）
    if r.status_code >= 400 and "speed" in (r.text or "") and "speed" in payload["voice_settings"]:
        payload["voice_settings"].pop("speed", None)
        r = _post(payload)
    r.raise_for_status()
    cache_path.write_bytes(r.content)
    log_usage("story" if len(text) > 60 else "question", len(text), False)
    return r.content, False

@app.route("/api/tts")
def api_tts():
    text = (request.args.get("text") or "").strip()
    speed = request.args.get("speed", "1.0")
    if not text:
        return jsonify({"error": "text がありません"}), 400
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": ".env に ELEVENLABS_API_KEY を設定してください"}), 400
    try:
        audio, _ = synth(text, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL, speed)
        return Response(audio, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"error": f"音声生成に失敗しました: {e}"}), 500

# ---------------- 声くらべ ----------------
@app.route("/api/compare/list")
def api_compare_list():
    """比較対象（声×モデル）の一覧とサンプル文を返す。"""
    combos = []
    for v in COMPARE_VOICES:
        for m in COMPARE_MODELS:
            combos.append({
                "id": hashlib.md5(f"{v['voice_id']}:{m}".encode()).hexdigest()[:10],
                "voice_name": v["name"],
                "voice_id": v["voice_id"],
                "model": m,
            })
    return jsonify({"sample": COMPARE_SAMPLE, "combos": combos, "current": {
        "voice_id": ELEVENLABS_VOICE_ID, "model": ELEVENLABS_MODEL}})

@app.route("/api/compare/audio")
def api_compare_audio():
    voice_id = (request.args.get("voice_id") or "").strip()
    model    = (request.args.get("model") or ELEVENLABS_MODEL).strip()
    text     = (request.args.get("text") or COMPARE_SAMPLE).strip()
    if not ELEVENLABS_API_KEY:
        return jsonify({"error": ".env に ELEVENLABS_API_KEY を設定してください"}), 400
    if not voice_id:
        return jsonify({"error": "voice_id がありません"}), 400
    try:
        audio, _ = synth(text, voice_id, model)
        return Response(audio, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"error": f"音声生成に失敗しました: {e}"}), 500

@app.route("/api/voice")
def api_voice():
    return jsonify({"voice_id": ELEVENLABS_VOICE_ID, "model": ELEVENLABS_MODEL,
                    "compare_available": len(COMPARE_VOICES) * len(COMPARE_MODELS) > 1})

# ---------------- おでかけパック（オフライン用HTML書き出し） ----------------
import base64

# 進捗を保持（簡易）
PACK_PROGRESS = {"running": False, "done": 0, "total": 0, "msg": "", "file": None, "error": None}

def b64_audio(text, voice_id, model):
    audio, _ = synth(text, voice_id, model)
    return "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii")

def build_pack(level, count):
    global PACK_PROGRESS
    try:
        PACK_PROGRESS.update(running=True, done=0, total=count, msg="準備中…", file=None, error=None)
        sets = []
        for i in range(count):
            PACK_PROGRESS["msg"] = f"{i+1}問目のお話を生成中…"
            data = generate_story(level)
            # お話の音声
            PACK_PROGRESS["msg"] = f"{i+1}問目の音声を生成中…"
            data["story_audio"] = b64_audio(data["story"], ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL)
            # 各設問の音声
            for q in data.get("questions", []):
                q["q_audio"] = b64_audio(q["q"], ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL)
            sets.append(data)
            PACK_PROGRESS["done"] = i + 1
        PACK_PROGRESS["msg"] = "ファイルを書き出し中…"
        out_path = write_pack_html(sets, level)
        PACK_PROGRESS.update(running=False, msg="完成しました", file=str(out_path.name))
    except Exception as e:
        PACK_PROGRESS.update(running=False, error=str(e), msg="失敗しました")

def write_pack_html(sets, level):
    tpl_path = BASE / "static" / "pack_template.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    lv_name = "慶應横浜" if level == "keio" else "桐蔭学園"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    payload = json.dumps({"level": level, "level_name": lv_name, "sets": sets}, ensure_ascii=False)
    html = tpl.replace("/*__PACK_DATA__*/null/*__END__*/", payload)
    out_dir = BASE / "packs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"odekake_{level}_{stamp}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path

@app.route("/api/pack/start")
def api_pack_start():
    if not ANTHROPIC_API_KEY or not ELEVENLABS_API_KEY:
        return jsonify({"error": ".env に ANTHROPIC_API_KEY と ELEVENLABS_API_KEY を設定してください"}), 400
    if PACK_PROGRESS["running"]:
        return jsonify({"error": "すでに生成中です"}), 400
    level = request.args.get("level", "keio")
    count = max(1, min(20, int(request.args.get("count", 10))))
    threading.Thread(target=build_pack, args=(level, count), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/pack/progress")
def api_pack_progress():
    return jsonify(PACK_PROGRESS)

@app.route("/api/pack/download")
def api_pack_download():
    name = request.args.get("file", "")
    path = BASE / "packs" / name
    if not name or not path.exists():
        return jsonify({"error": "ファイルが見つかりません"}), 404
    return send_file(path, mimetype="text/html", as_attachment=True, download_name=name)

# ---------------- 静的ファイル ----------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/sw.js")
def service_worker():
    # Service Worker はルート直下から配信し、スコープを "/" にする（オフライン対応）
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")

@app.route("/compare")
def compare():
    return send_from_directory(app.static_folder, "compare.html")

@app.route("/pack")
def pack():
    return send_from_directory(app.static_folder, "pack.html")

def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def open_browser():
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    lan = _lan_ip()
    threading.Timer(1.2, open_browser).start()
    print("おはなしのきおく サーバー起動中")
    print("  このPC      →  http://127.0.0.1:5000")
    print(f"  ほかの端末  →  http://{lan}:5000  （同じWi-Fiに接続）")
    app.run(host="0.0.0.0", port=5000, debug=False)
