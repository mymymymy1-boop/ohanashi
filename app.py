# -*- coding: utf-8 -*-
"""
おはなしのきおく  -  小学校受験「お話の記憶」練習サーバー
- 問題文を Anthropic API で生成
- 読み上げ音声を ElevenLabs で生成（同一テキストはキャッシュしてクレジット節約）
- イラスト選択肢をタップして解答
"""
import os, json, time, base64, hmac, hashlib, datetime, webbrowser, threading, random
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

# 【フェイルクローズド】本番(Render)で APP_PASSWORD 未設定なら起動を止める。
# 空パスワード＝認証なし公開＝Anthropic/ElevenLabs のクレジット悪用に直結するため、
# 設定ミスで無防備に公開されるより起動を落として気づけるようにする（Render は環境変数 RENDER=true を自動付与）。
if os.getenv("RENDER") and not APP_PASSWORD:
    raise RuntimeError(
        "APP_PASSWORD が未設定です。本番公開では必須です"
        "（Render ダッシュボード → Environment に APP_PASSWORD を設定してください）。"
    )

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
    # ヘルスチェックは認証不要（外形監視・Renderのプローブが401で弾かれないように）
    if request.path == "/healthz":
        return
    # APP_PASSWORD 未設定（ローカル開発）なら認証なしで通す
    if not APP_PASSWORD:
        return
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", APP_USERNAME) \
            and hmac.compare_digest(auth.password or "", APP_PASSWORD):
        return
    # 認証失敗はログに残す（不審なアクセス・総当たりの検知用）
    print(f"[auth] 認証失敗 path={request.path} ip={request.remote_addr}", flush=True)
    return Response("認証が必要です", 401,
                    {"WWW-Authenticate": 'Basic realm="ohanashi"'})

@app.after_request
def _security_headers(resp):
    # アプリはインラインJS/CSSに依存するため厳格なCSPは付けない（壊れる）。
    # 破綻しない範囲の安全ヘッダーだけ付与する。
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if os.getenv("RENDER"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

@app.route("/healthz")
def healthz():
    # 200を返すだけの死活確認。キーの有無も返してレディネス判断に使えるようにする。
    return jsonify({"ok": True,
                    "anthropic_key": bool(ANTHROPIC_API_KEY),
                    "elevenlabs_key": bool(ELEVENLABS_API_KEY)})

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
    # ▼ 変化球（直接思い出すだけでなく“考えて答える”問題。実際の入試でも常識・数の操作・推理として出る）
    {"key": "季節の仲間", "desc": "季節の常識（お話に出てきた花・行事・食べ物・虫などの『季節』を手がかりに、それと同じ季節のものを選ぶ。例：お話に出た『ひまわり』とおなじ季節のものはどれ→正解は夏のもの）"},
    {"key": "なかま分け", "desc": "仲間分け・分類の常識（お話に出てきたものと『おなじなかま』を選ぶ。くだもの・やさい・のりもの・どうぶつ・むし・はな などの分類。例：お話に出た『りんご』とおなじなかま＝くだものはどれ）"},
    {"key": "数を分ける", "desc": "数の操作・分配（お話に出てきた数を、何人かで同じ数ずつ分けると1人いくつになるかを問う。例：あめが6こ、2人で同じ数ずつ分けたら1人なんこ→3こ）"},
    {"key": "くらべる",   "desc": "数や大きさのくらべっこ（お話の別々の場面に出てきた2つ以上のものを、どちらが多い／大きい／速いかを考えて答える。例：りんご3こ・みかん5こ、多かったのはどっち→みかん）"},
    {"key": "すいり",     "desc": "かんたんな推理（お話の手がかりから考えると答えが1つに決まる問い。例：かさを持っていたのは、雨がふったとき外にいた誰か／いちばん早くおきた子は朝ごはんを最初に食べた誰か など）"},
]
PATTERN_BY_KEY = {p["key"]: p for p in QUESTION_PATTERNS}

# 変化球パターンへの“念押し”指示。正解の一意性・常識の正確さを担保するため、設問ごとにこの注意をプロンプトへ添える。
PATTERN_EXTRA = {
    "季節の仲間": "（お話の中に季節の手がかり＝花・行事・食べ物・虫などを1つ自然に登場させ、その季節を子どもが特定できるようにする。設問は『お話に出てきた○○とおなじ季節のものはどれ？』の形。選択肢4つはちがう季節を代表する物にし、正解だけ同じ季節にする。季節は正しく＝春:さくら/つくし/ちょうちょ、夏:ひまわり/すいか/せみ/かぶとむし、秋:もみじ/コスモス/どんぐり/さつまいも、冬:つばき/みかん/ゆきだるま 等）",
    "なかま分け": "（設問は『お話に出てきた○○とおなじなかまはどれ？』。ひっかけ3つはちがうなかまにし、正解1つだけ同じなかまにする。分類は正確に＝トマト/きゅうりはやさい、いちご/ばななはくだもの、ちょうちょ/かぶとむしはむし、ひこうき/ふねはのりもの 等）",
    "数を分ける": "（お話の本文に『○個を△人で同じ数ずつ分けた』場面を自然に入れる。必ず割り切れる数にし、答えが一意に決まるようにする。1人ぶんの数は2〜5こ程度のやさしい数に。選択肢は『●こ』のように数で示し、正解は正しい1人ぶんの数）",
    "くらべる":   "（くらべる2〜3個のものの数や大きさを本文にはっきり書いておく。設問は『いちばん多い／大きい／速いのはどれ？』。※答えそのもの（大きい/小さい等）を設問やラベルに書かず、具体物や数で示して考えさせる。例：『りんごとすいか、大きいのはどっち』『3こと5こ、多いのはどっち』。差がはっきりして答えが一意になるようにする）",
    "すいり":     "（お話の手がかりから論理的にただ1つに決まる問いにする。あいまい・複数正解になりそうな問いは作らない。手がかりは必ず本文に入れておく）",
}

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
        # 変化球パターン（季節の仲間・なかま分け・数を分ける・くらべる・すいり）は一意性・常識の正確さを念押し
        elif p["key"] in PATTERN_EXTRA:
            extra = PATTERN_EXTRA[p["key"]]
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

# 問題生成モデル。環境変数 ANTHROPIC_MODEL で差し替え可能（コード変更なしで復旧できる）。
# 提供終了(404)を検出したら次候補へ自動で切り替える（2026-06-16 の claude-sonnet-4-20250514 退役事故の再発防止）。
_env_model = os.getenv("ANTHROPIC_MODEL", "").strip()
MODEL_CANDIDATES = []
for _m in [_env_model, "claude-sonnet-4-6", "claude-sonnet-4-5"]:
    if _m and _m not in MODEL_CANDIDATES:
        MODEL_CANDIDATES.append(_m)
_model_idx = 0  # 現在使うモデル候補の位置（404検出で進める。プロセス生存中は戻さない）
_MODEL_LOCK = threading.Lock()  # _model_idx の同時インクリメント（候補飛ばし）を防ぐ

def _extract_json(text: str) -> dict:
    """モデル出力からJSON本体を取り出してパースする。前後に説明文やコードフェンスが混ざっても救出する。"""
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start:end + 1])
        raise

def _validate_story(data) -> str:
    """生成結果の構造チェック。壊れていれば理由を返す（正常なら空文字）。"""
    if not isinstance(data, dict) or not str(data.get("story", "")).strip():
        return "story がありません"
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        return "questions がありません"
    for i, q in enumerate(qs):
        if not str(q.get("q", "")).strip():
            return f"設問{i+1}の文がありません"
        ch = q.get("choices")
        if not isinstance(ch, list) or len(ch) != 4:
            return f"設問{i+1}の選択肢が4つではありません"
        if sum(1 for c in ch if c.get("correct") is True) != 1:
            return f"設問{i+1}の正解が1つに決まっていません"
    return ""

def generate_story(level: str, focus=None) -> dict:
    """Anthropic APIで問題1セットを生成して dict を返す。
    一時エラー(429/5xx/タイムアウト)・JSON崩れ・出力途切れは自動リトライ（最大3回）。
    モデル提供終了(404)は次候補モデルへ恒久切り替え。3回失敗で例外。"""
    global _model_idx
    last_err = None
    for attempt in range(3):
        model = MODEL_CANDIDATES[min(_model_idx, len(MODEL_CANDIDATES) - 1)]
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    # 慶應レベルは平均1250トークン出るため1500では途切れることがあった。
                    # 課金は実際に生成した分だけなので上限は大きく取って途切れを根絶する。
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": build_prompt(level, focus)}],
                },
                timeout=60,
            )
            # モデル提供終了 → 次候補へ切り替えて即やり直し（同時404での二重加算をロックで防ぐ）
            if r.status_code == 404 and _model_idx < len(MODEL_CANDIDATES) - 1:
                with _MODEL_LOCK:
                    # まだ誰も切り替えていなければ（今失敗したモデルが現役なら）1段だけ進める
                    if _model_idx < len(MODEL_CANDIDATES) - 1 and MODEL_CANDIDATES[_model_idx] == model:
                        print(f"[generate_story] モデル {model} が404（提供終了の可能性）→ "
                              f"{MODEL_CANDIDATES[_model_idx + 1]} に切り替え", flush=True)
                        _model_idx += 1
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("stop_reason") == "max_tokens":
                raise ValueError("出力が上限で途切れました(max_tokens)")
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            parsed = _extract_json(text)
            bad = _validate_story(parsed)
            if bad:
                raise ValueError(f"生成結果が不正: {bad}")
            return parsed
        except Exception as e:
            last_err = e
            print(f"[generate_story] {attempt + 1}回目失敗 (model={model}, level={level}): {e}", flush=True)
            if attempt < 2:
                time.sleep(1 + attempt * 2)  # 1秒→3秒の軽いバックオフ（429/529対策）
    raise RuntimeError(f"3回試しても生成できませんでした（最後のエラー: {last_err}）")

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
        print(f"[api_story] 生成失敗: {e}", flush=True)  # 詳細はサーバーログのみ
        return jsonify({"error": "おはなしを つくれませんでした。すこし まってから もういちど ためしてね。"}), 500

# ---------------- 音声生成（キャッシュ付き） ----------------
_LOG_LOCK = threading.Lock()

def log_usage(kind: str, chars: int, cached: bool):
    # 複数スレッド同時書き込みでの行混線・ヘッダー重複を防ぐ
    with _LOG_LOCK:
        new = not LOG_FILE.exists()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            if new:
                f.write("datetime,kind,chars,cached\n")
            f.write(f"{datetime.datetime.now().isoformat()},{kind},{chars},{int(cached)}\n")

# 同一テキストの同時生成でElevenLabsを二重に叩かないための、キャッシュキー単位の生成ロック。
# 先着1本だけが実際に生成し、後続は完了を待って同じキャッシュを読む（クレジット二重消費の根治）。
_SYNTH_LOCKS = {}
_SYNTH_LOCKS_GUARD = threading.Lock()

def _synth_lock(key: str) -> threading.Lock:
    with _SYNTH_LOCKS_GUARD:
        lk = _SYNTH_LOCKS.get(key)
        if lk is None:
            lk = _SYNTH_LOCKS[key] = threading.Lock()
        return lk

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
    kind = "story" if len(text) > 60 else "question"
    if cache_path.exists():
        log_usage(kind, len(text), True)
        return cache_path.read_bytes(), True

    # 同一キーの生成を直列化。ロック取得後にもう一度キャッシュを見る（先着スレッドが作り終えていれば読むだけ）。
    with _synth_lock(key):
        if cache_path.exists():
            log_usage(kind, len(text), True)
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

        def _post_with_param_fallback():
            r = _post(payload)
            # language_code 非対応モデルの場合に備えてリトライ
            if r.status_code >= 400 and "language_code" in (r.text or ""):
                payload.pop("language_code", None)
                r = _post(payload)
            # speed 非対応モデルの場合に備えてリトライ（speedを外す）
            if r.status_code >= 400 and "speed" in (r.text or "") and "speed" in payload["voice_settings"]:
                payload["voice_settings"].pop("speed", None)
                r = _post(payload)
            return r

        # 一時エラー(429/5xx/接続タイムアウト)は指数バックオフで最大3回リトライ（お話生成側と対称にする）
        last_err = None
        for attempt in range(3):
            try:
                r = _post_with_param_fallback()
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"ElevenLabs 一時エラー {r.status_code}")
                r.raise_for_status()
                # 200でも本文が空/極小＝実質無音のことがある（長文や稀な入力でElevenLabsが空返し）。
                # これをそのままキャッシュすると、そのテキストは以後ずっと無音固着になる。
                # 失敗として扱いリトライし、最後まで空なら例外にしてキャッシュを書かない。
                if len(r.content) < 500:
                    raise RuntimeError(f"ElevenLabs 空応答 ({len(r.content)}B)")
                break
            except (requests.RequestException, RuntimeError) as e:
                last_err = e
                print(f"[synth] {attempt + 1}回目失敗: {e}", flush=True)
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
        else:
            raise RuntimeError(f"音声生成に3回失敗しました（最後のエラー: {last_err}）")

        # 一時ファイルへ書いてから os.replace でアトミックに差し替え（途中ファイルの読取を防ぐ）
        tmp_path = cache_path.with_suffix(".tmp")
        tmp_path.write_bytes(r.content)
        os.replace(tmp_path, cache_path)
        log_usage(kind, len(text), False)
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
        print(f"[api_tts] 音声失敗: {e}", flush=True)
        return jsonify({"error": "おとを つくれませんでした。すこし まってから もういちど。"}), 500

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
        print(f"[api_compare_audio] 音声失敗: {e}", flush=True)
        return jsonify({"error": "音声を生成できませんでした。時間をおいて再度お試しください。"}), 500

@app.route("/api/voice")
def api_voice():
    return jsonify({"voice_id": ELEVENLABS_VOICE_ID, "model": ELEVENLABS_MODEL,
                    "compare_available": len(COMPARE_VOICES) * len(COMPARE_MODELS) > 1})

# ---------------- おでかけパック（オフライン用HTML書き出し） ----------------
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
            # 1話ずつ独立して try。途中1話が失敗しても、生成済み（＝課金済み）の話は捨てずに成果物へ残す。
            try:
                PACK_PROGRESS["msg"] = f"{i+1}問目のお話を生成中…"
                data = generate_story(level)
                # お話の音声
                PACK_PROGRESS["msg"] = f"{i+1}問目の音声を生成中…"
                data["story_audio"] = b64_audio(data["story"], ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL)
                # 各設問の音声
                for q in data.get("questions", []):
                    q["q_audio"] = b64_audio(q["q"], ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL)
                sets.append(data)
            except Exception as e:
                print(f"[build_pack] {i+1}問目を生成できずスキップ: {e}", flush=True)
            PACK_PROGRESS["done"] = i + 1
        if not sets:
            PACK_PROGRESS.update(running=False, error="1問も作れませんでした", msg="失敗しました")
            return
        PACK_PROGRESS["msg"] = "ファイルを書き出し中…"
        out_path = write_pack_html(sets, level)
        done_msg = "完成しました" if len(sets) == count else f"完成しました（{len(sets)}/{count}問。一部は作れませんでした）"
        PACK_PROGRESS.update(running=False, msg=done_msg, file=str(out_path.name))
    except Exception as e:
        PACK_PROGRESS.update(running=False, error=str(e), msg="失敗しました")

def write_pack_html(sets, level):
    tpl_path = BASE / "static" / "pack_template.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    lv_name = "慶應横浜" if level == "keio" else "桐蔭学園"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    payload = json.dumps({"level": level, "level_name": lv_name, "sets": sets}, ensure_ascii=False)
    # <script> 内へ埋め込むため '<' を \u003c にエスケープし </script> による早期終了・タグ注入を防ぐ（JSON文字列として等価）
    payload = payload.replace("<", chr(92) + "u003c")
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
    try:
        count = int(request.args.get("count", 10))
    except (TypeError, ValueError):
        count = 10
    count = max(1, min(20, count))
    threading.Thread(target=build_pack, args=(level, count), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/pack/progress")
def api_pack_progress():
    return jsonify(PACK_PROGRESS)

@app.route("/api/pack/download")
def api_pack_download():
    # パストラバーサル対策: ディレクトリ成分を除去し、解決後パスが packs/ 直下であることを検証する。
    name = os.path.basename(request.args.get("file", ""))
    packs_dir = (BASE / "packs").resolve()
    path = (packs_dir / name).resolve()
    if not name or path.parent != packs_dir or not path.exists():
        return jsonify({"error": "ファイルが見つかりません"}), 404
    return send_file(path, mimetype="text/html", as_attachment=True, download_name=name)

# ---------------- お話の記憶 PRO (P1) 検品キュー ----------------
# 新機能は /pro/ 名前空間に隔離し、既存アプリのルート・データには触れない(P1実装上の注意)。
PRO_CONTENT_DIR = BASE / "content" / "pilot"
PRO_REVIEW_DIR = BASE / "review"
PRO_PLAY_DIR = BASE / "play"

@app.route("/pro/review")
def pro_review():
    if not (PRO_REVIEW_DIR / "index.html").exists():
        return jsonify({"error": "検品キューがまだ生成されていません"}), 404
    return send_from_directory(str(PRO_REVIEW_DIR), "index.html")

@app.route("/pro/play")
def pro_play():
    # こども本番モード(P2 MVP)。検品済みコンテンツを本番作法で出題する。
    # 配信データは既存の /pro/content/ をそのまま使う(manifest・画像・音声・alias・labels)。
    if not (PRO_PLAY_DIR / "index.html").exists():
        return jsonify({"error": "プレイ画面がまだありません"}), 404
    return send_from_directory(str(PRO_PLAY_DIR), "index.html")

@app.route("/pro/sw.js")
def pro_sw():
    # Service Worker は /pro/ スコープで配信(旧アプリ / には干渉しない)。
    if not (PRO_PLAY_DIR / "sw.js").exists():
        return jsonify({"error": "sw.js がありません"}), 404
    resp = send_from_directory(str(PRO_PLAY_DIR), "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/pro/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/pro/play.webmanifest")
def pro_webmanifest():
    if not (PRO_PLAY_DIR / "play.webmanifest").exists():
        return jsonify({"error": "manifest がありません"}), 404
    return send_from_directory(str(PRO_PLAY_DIR), "play.webmanifest",
                               mimetype="application/manifest+json")

@app.route("/pro/icon-<size>.png")
def pro_icon(size):
    # PWA/ホーム画面アイコン。size はサニタイズしてファイル名固定パターンに閉じる。
    fn = "icon-" + os.path.basename(size) + ".png"
    if not (PRO_PLAY_DIR / fn).exists():
        return jsonify({"error": "icon がありません"}), 404
    return send_from_directory(str(PRO_PLAY_DIR), fn, mimetype="image/png")

@app.route("/pro/content/<path:relpath>")
def pro_content(relpath):
    # パストラバーサル対策: 解決後パスが content/pilot 配下であることを検証する。
    root = PRO_CONTENT_DIR.resolve()
    try:
        path = (root / relpath).resolve()
    except (OSError, ValueError):
        return jsonify({"error": "不正なパスです"}), 400
    if not (path == root or root in path.parents) or not path.is_file():
        return jsonify({"error": "ファイルが見つかりません"}), 404
    mimetype = "audio/mpeg" if path.suffix == ".mp3" else None
    resp = send_file(path, mimetype=mimetype)
    resp.headers["Cache-Control"] = "no-cache"
    return resp

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
