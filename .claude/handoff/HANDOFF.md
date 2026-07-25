# HANDOFF — ohanashi（お話の記憶アプリ + PRO）

## 0. 最新状態（2026-07-25・VPS本番公開 完了）

**PROアプリを XサーバーVPS に公開完了 → https://ohanashi.bizsp.net/pro/play （PWA・オフライン対応・認証は現在オフ）。**
- 既存VPSデプロイ(`/opt/apps/ohanashi`・PM2`ohanashi`・gunicorn:3002)を現行コード(f8eb935)にff更新＋コンテンツ1.9GB(画像1681/音声mp3 1531/manifest252)をtar-over-sshで転送＋pm2 restart。全/pro/*ルート200・SWスコープ/pro/・PWA成立を実測検証済み。
- **⚠️認証オフ**: VPSの`APP_PASSWORD`が空(旧アプリ時代から)＝`/pro`も`/api`(課金)も未認証公開。泰介さん判断で「今は認証なしのまま」。将来`.env`にAPP_PASSWORD設定→`pm2 restart ohanashi`で全体Basic認証化可。
- 再デプロイ: `git push`→VPSで`git merge --ff-only origin/master && venv/bin/pip install -qr requirements.txt && pm2 restart ohanashi && pm2 save`。コンテンツ更新はtar-over-ssh(rsync不可)。詳細はメモリ[[ohanashi-deployment]]。

## 0-a. 全252問の意味検品と隔離（2026-07-25・**本番反映済み** コミット `d348d07`）

**検品完了。確定欠陥45話を出題から隔離し、健全な207話で練習できる状態。**

- **方法(2層+人)**: `pipeline/inspect_content.py`（LLM第二の読み手＝機械QCで測れない観点）→ `pipeline/verify_findings.py`（**反証側に立つ敵対的検証**・迷ったら棄却）→ 私が本文/実画像で抜き取り裏取り
- **結果**: 意味検品 high 69件 → 検証で**46件確定・22件棄却（偽陽性32%を除去）**。影響45ユニット/252（18%）。実コスト$6.8
- **内訳**: 正解が一意に決まらない23 / 本文に根拠がない17 / 本文の不自然さ3 / 選択肢が絵で区別不能2 / ダミーも正解1
- **レベル別健全率**: Lv1 96% / Lv2 96% / Lv3 77% / Lv4 89% / **Lv5 56%**（長文＋T9二重順序に集中）
- **根本原因**: `generate.gen_questions` が骨格(`dual_orders`/`numbers`/`emotions`)と本文の両方を受け取るため、**本文に書かれていない骨格の事実で出題**していた。機械QCは「正解語が根拠場面にあるか」しか見ず構造的に見逃す
- **再発防止(済)**: `03_question_gen.md`＝根拠は本文の一文を指させる／同時行動(「AとBは〜」)の前後を問うの禁止、`02_level_expand.md`＝骨格の順序・数・心情は本文に明示して書く（実例つき）
- **隔離の実装**: `content/pilot/defect_units.json`(45件) を `/pro/play` が読んで出題から除外。**データは残すので修復後に戻せる**。親設定に「見直し中の45話は出題から外しています」と表示。本番で207話・欠陥混入0を実測
- **修復（2026-07-25完了・本番反映 `544d417`）**: `pipeline/repair_questions.py`（本文と本文音声を保全し**設問だけ**を強化プロンプトで作り直す）。元の設問は `lv{n}_questions.pre_repair.json` に退避。
  - **45話すべて再生成 → 再検品 → 指摘ゼロの17話を隔離解除（出題可能 207→224話）**。本番で欠陥混入0を実測
  - **残り28話は隔離継続**。多くは`story`系＝**本文自体の矛盾**（例: 同じ出来事に「かなしい」と「おどろいた」が併記）で、設問の作り直しでは直らない。**次は本文から作り直す2段目**（text再生成→設問再生成→全音声再生成→再検品）
  - 修復ツールの要点: `generate._autofix_qset` を QC 前に挟むと、ダミー種別の貼り間違い(R406/R407)と正解位置3連続(R308)が**API再呼び出しなしで**解消する。これを入れる前は4件が失敗していた
  - **事故と対処**: `audio.py` の `TTS_BACKEND` 既定が `elevenlabs` のままで、env未設定の実行により**54ファイルをElevenLabsで誤生成**（課金＋本番と違う声）。Aivisで作り直し済み。**既定を `aivis` に変更**して再発防止（本番音声はAivis: 本文=morioki / 設問=TANAKA）

## 0-b. 学習機能の拡充（2026-07-25・仕様§6.7・**本番反映済み** コミット `de98fcd`）

3機能を `play/index.html` に実装し、**VPS本番に反映済み**（push → `git merge --ff-only` → `pm2 restart ohanashi`）。本番URLで実レンダリング確認済み（まちがいなおしボタン・252問ロード・SW登録・新関数すべて配信）。
- **まちがい直し（変奏復習）**: 誤答を忘却曲線で再出題（翌日→3日→1週間→卒業）。`REVIEW_KEY`にunit_id|q_idで保存。ホームに朱のボタン＋期限到来件数バッジ。復習セッションは**該当設問だけを含む複製ユニット**をキュー化するので既存の出題機構がそのまま動く。結果画面は「のこりNもん」を表示
- **会場ノイズモード**: WebAudioで合成（**音源ファイル不要**）。低域ノイズ(gain .012)＋鉛筆/椅子のような小さな物音をランダム間隔。本文再生中のみ。設定でON/OFF・既定OFF
- **親が読むモード**: 音声を使わず原稿（明朝・場面ごと）＋「めやす◯秒・◯字/分」＋進行するペースバー＋「よみおわった→もんだいへ」。設定でON/OFF・既定OFF
- 検証: test_client 32項目PASS（新機能・構文均衡・既存機能の維持）＋agent-browserで忘却曲線ロジック（誤答→翌日／正解で3日→7日→卒業／未登録は積まない）・復習キューが期限到来分だけを出題・ホームバッジ・親読み画面・ノイズ生成/停止/OFF尊重・設定画面・復習結果画面を実確認

## 過去の到達点（2026-07-25・4択統一＋絵カード全解決）

**「お話の記憶PRO」252問1279設問を全数4択に統一（泰介指示「実試験は4択が主流」）。絵カード欠落も0になり、残るゲートは泰介検品のみ。**

- 4択化: `pipeline/normalize_choices.py`（5択546トリム/3択65追加/R308修正3）。CHOICE_COUNT全Lv(4,4)・models choices=4固定・R302統一・仕様/プロンプト03/qc_rules同期済み。旧データ=`content/_backup_pre4choice_20260725`。音声・本文は無変更
- 絵カード11種解消: 目視判定で復活3（takigi_3bon等・QC誤検知）＋alias1（hare→tenki_hare）＋Gemini再生成7（全数目視合格・strip_frame済み）。マニフェスト画像欠落0・HTTP 11/11確認済み
- QC: 380ユニット中374合格（残6failは移行前からの既知・全て検品対象外）。回帰テスト18/18
- **こども本番モードMVP実装（2026-07-25・P2後半の第一歩）**: `/pro/play`（`play/index.html`）。指示→本文(波形のみ・1回きり)→設問(4択タップ・2段階けってい・制限時間リング)→朱の○採点→自動送り→けっか→がんばりカレンダー(localStorage)。親ゲート(長押し1.5秒)→グループ/レベル/やさしいモード設定。検品済み252問をそのまま出題。
- **P2機能3点セット実装（2026-07-25・泰介さん「全部今日」指示）**:
  - **スタンプ帳・バッジ**: がんばり画面を「がんばりの きろく」に拡張。連続日数バナー＋朱丸/花丸カレンダー＋スタンプ帳(1問=1スタンプ・10個で1ページ・ボーナス縁起物)＋バッジ6種(はじめての満点/7日/30日連続/100問/500問/Lv3クリア・記録から自動判定)。
  - **模試モード**: ホーム「🎯 もぎしけん」。同グループ・レベルから最大3話を`strict`(やさしいモード無効・本文1回・タイマー本番)で連続出題→per-story内訳＋合計判定。`completeUnit`で単発/模試を分岐(単発=showSingleResult, 模試=次話へ/showMoshiResult)。
  - **PWA化**: `play/play.webmanifest`・`play/sw.js`(スコープ/pro/限定・旧アプリ無干渉・シェルはネット優先/画像音声はキャッシュ優先でオフライン再生)・`play/icon-{192,512}.png`(朱丸+明朝「話」)。app.pyに`/pro/sw.js`(Service-Worker-Allowed:/pro/)・`/pro/play.webmanifest`・`/pro/icon-<size>.png`追加。SW登録スコープ/pro/確認済。
  - 検証: test_client全30項目PASS(ルート・PWA・パストラバーサル防御・4択維持)＋agent-browserでホーム/がんばり(スタンプ28個2ページ・バッジ獲得ロック判定)/模試結果/単発結果を実レンダリング確認。**サーバー再起動済(新ルート反映)**。
- **親ゾーンに本物の成績表を実装（2026-07-25）**: おうちのかた設定→「📊 せいせきを みる」で濃紺の成績表。プレイ完了ごとに`recordHistory`が per-answer(type,correct)をlocalStorage(`ohanashi_play_history_v1`・上限300)に記録→**記録から算出**したタイプ別正答率(にがて=60%未満を朱で強調・低い順)・総練習数・総正答率・連続日数(calから)・直近12回の推移を表示。空状態あり。**場面別ヒートマップは骨格→場面の対応が全設問で取れないため意図的に未実装(捏造しない)**。agent-browserで空/データ/一気通貫(finishSession→記録→表示)を実レンダリング検証済み。
- **UI一新「答案用紙メソッド」を本番反映（2026-07-25・泰介さん「これで以降」承認）**: わら半紙罫線＋濃紺＋朱の採点丸、手描き朱丸のシグネチャー、解答欄マス目、明朝ブランド、親ゾーンは濃紺に反転。**agent-browserで全6画面を実レンダリング目視検証**（表紙/きく/もんだい/採点朱丸/けっか花丸/カレンダー/親設定）。検証中に発見・修正した2件: ①`.grade`採点丸が`[hidden]`をCSSが打ち消し全カード表示→クラスベース表示に修正 ②置いた印を手描きSVG○に強化＋採点後は子の印を隠す。ルート＋データはtest_client実HTTP全PASS。**残る唯一の未検証はブラウザでの音声自動再生フロー（iOS自動再生ロック）＝要泰介さん実機確認**。サーバーはsend_from_directoryで都度ファイル読込のため再起動不要→ http://192.168.0.242:5000/pro/play

- 検品UI: サーバー起動後 `http://192.168.0.242:5000/pro/review`（252問・グループバッジ・絵カードグリッド・ひらがなラベル）
- サーバー: `cd C:\dev\ohanashi && python app.py`（ローカルは認証なし）
- 音声エンジン: `C:\dev\_tools\Windows-x64\run.exe --host 127.0.0.1 --port 10101`（AivisSpeech。Smart App Controlはオフ済み）
- 総コスト: API $134.5 + 画像 ≈$70

## 1. 現在のタスク

P2（仕様 `docs/handoff/spec/ohanashi_pro_spec_v1.1.md` §11）のコンテンツ目標「250問ストック」達成。
次は (1) 泰介検品 → NG系統修正 → (2) P2残りのアプリ機能（本番再現UI・こどもモード・カレンダー・ペアレンタルゲート）の設計。

## 2. 直近セッションで完了（2026-07-22〜24）

- **音声**: 本文=morioki 1.25倍速焼き込み（`STORY_SPEED_FACTOR=1.25` in `pipeline/common.py`・R205も目標基準）／設問=TANAKA。AivisSpeechで生成0円
- **量産**: グループA/B/D/E各50問（run_pilot --group X --new 15 → 補完パス --new 0）。Lv5プロンプト強化・R504骨格事前検証・32000トークン・修復不能骨格スキップ
- **グループD専用軸を新規実装**（泰介承認）: 話数{1,1,2,2,3}・設問数{1..5}・T5≥50%・T2/T9禁止・multi免除。`common.py D_*` / `qc.py` D分岐 / プロンプト3本
- **絵カード1656枚**: `pipeline/image_vocab.py`(語彙正規化1752→1680) → `build_image_lib.py`(AI生成+視覚QC+枠切除) → `image_render.py`(個数=機械合成・数字=ドット)。カバレッジ99.8%
- **検品UI**: 絵カードグリッド・ひらがなラベル（`pipeline/choice_labels.py`）・グループバッジ・マニフェストのマージ化

## 3. 次にやること

1. **`/pro/play` を泰介さん実機確認**（iPad/スマホでブラウザ音声再生フローが通るか。iOS自動再生ロックが未検証の最大リスク。落ちる場合は「指示音声→本文」の遷移を各ステップ手動タップ起点に変更する）
2. 泰介検品（各グループ5問サンプルから。特にD=3話連続・E=長文）→ UIメモ→「JSON書き出し」で回収
3. 検品NGの系統修正（プロンプト正本は `docs/handoff/prompts/`）
4. MVPの次の詰め（実機確認後）: クーピー色パレットで記号を「置く」操作／読み上げ後の設問文自動送り微調整／スタンプ帳・マイルストーンバッジ／模試モード。オフライン化(PWA/Capacitor)・課金はP3

## 4. 重要な決定・制約

- 本文音声は**1.25倍速をファイルに焼き込み**（プレイヤーは等倍再生）。泰介耳判定・qc_rules.mdにも同期済み
- 絵柄の正本 = `pipeline/gen_images.py` の `STYLE_PREFIX`（泰介承認の水彩パステル）。変更はそこだけ
- 個数カードはAI描画禁止（機械合成のみ）。image_key命名規約は `docs/handoff/prompts/03_question_gen.md` に明記
- 長時間バッチは detached起動+Monitor監視（バックグラウンドkillはpython残存→エンジン渋滞の事故歴）
- 実行中プロセスがある間はpipelineコードを編集しない／書き込み先フォルダを動かさない（クラッシュ事故歴）

## 5. 主要ファイル

- パイプライン: `pipeline/` — run_pilot(一括) / generate / qc / audio / tts_aivis / gen_images / image_vocab / image_render / image_qc / build_image_lib / choice_labels
- コンテンツ: `content/pilot/`（review_manifest.json=252問 / images/=1656枚+gallery.html / image_vocab.json / image_alias_map.json / choice_labels.json / qc_report.jsonl / images_needs_human.json）
- 検品UI: `review/index.html`（/pro/review で配信）
- 仕様正本: `docs/handoff/`（spec / prompts×3 / qc_rules）
- 回帰テスト: `python test_p1_qc.py`（18/18）

## 6. 未解決・ブロッカー

- 泰介検品待ち（品質の最終判定は人間の耳と目）
- 絵カード11種が未生成（複合場面系・影響は各1選択肢のみ）
- PRO問題の本番アプリ（こども向けUI）への組み込みは未設計（P2後半）
- 旧単体アプリ（Render公開版）は無変更で稼働中（このセッションでは触っていない）
