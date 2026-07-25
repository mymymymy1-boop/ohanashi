# SESSION LOG — ohanashi

## 2026-07-07 総点検 → 改善 → 本番デプロイ → 検証（一連クローズ）

### 経緯・合意
- ユーザー依頼: 「慶應横浜の生成エラー調査」→「フィーチャーデブ(Fable5)＋ワークフロー(Fable5/監査役)で世界基準の総点検」→「推奨も全部やって・プッシュして」→「再デプロイ代わりにやって」→「保存して(Obsidian含む)」。
- 本番反映はユーザー明示指示に基づき実行（STOP条件クリア）。

### 実行と結果
- 慶應間欠エラー根治: `max_tokens` 1500→4000＋自動リトライ＋構造検証（コミット `3eb72f5`）。本番5/5成功。
- 総点検: フィーチャーデブ4体＝71件、ワークフロー独立スイープ4系統＝25件、監査役4体が全件裏取り。ダッシュボード公開（artifact e0dae6a5）。
- HIGH 6件修正 → コミット `2b29c79` / push。
  - 音声二重課金の根治（synth生成ロック＋アトミック書込）、SW上書き修正、フェイルクローズド起動、パストラバーサル対策、パック部分成功＋synthリトライ、Render揮発FS明記。
- MEDIUM/LOW多数修正 → コミット `102d334` / push。
  - タイマー音声被り、再生横取り、本番モード状態リーク、オフライン誘導、414対策、Fisher-Yates、XSSエスケープ、保存50件上限、hmac認証＋失敗ログ、/healthz、安全ヘッダー、SSOT文書更新（README/.env.example/render.yaml/Obsidian）。
- 本番デプロイ: `.env` の `RENDER_DEPLOY_HOOK` を `curl -X POST`（deploy `dep-d96mv2lckfvc73dkbl3g`）。`/healthz`200で反映検知。
- 本番検証（全合格）: /healthz 200・保護ルート401・安全ヘッダー4種・パストラバーサル404・慶應生成200（story456字・5問・構造妥当）。

### 検証コマンド要旨
- 単体: `test_high_fixes.py` 9/9、`test_fix.py`(回帰) 13/13。
- フロント: `node --check`（index.html / pack_template.html）OK。
- 実API: 生成・TTS往復・`</script>`エスケープ再現テストOK。

### 保留（要判断・STOP条件/大規模のため未実装）
1. レート制限/使用量サーキットブレーカー（新依存 or 自前）
2. index/pack_template の出題ロジック共通化（ビルド機構要）
3. IndexedDBスキーマ・マイグレーション
4. パスワード `5525` 強化（本番auth・ユーザー操作）
5. Render永続ディスク（有料課金）

### Obsidian保存（2026-07-07）
- `02_Projects/ohanashi-app.md`: 現在地を「デプロイ完了・検証合格」に更新、設問18→23パターン・構成6種に修正。
- `03_Knowledge/render-deploy-hook-healthz.md`: 新規（Deploy Hook＋/healthz検知・フェイルクローズド・二段監査・\uエスケープ事故の知見）。
- auto-memory `ohanashi-deployment.md`: 再デプロイ手段・/healthz・フェイルクローズドを追記。

## 2026-07-22〜24 PRO P2量産（音声確定→グループC完成→絵カード→4グループ量産→252問完成）

### 経緯・合意
- 「次のフェーズにいって」→P2着手。「量産ゴー」→残り4グループ承認。品質先行＋Gemini画像生成を選択。
- 本文1.25倍速の焼き込み、D専用軸の実装、SACオフ、いずれも泰介さん判断。

### 主要な実行と結果
- ボイス確定: 本文morioki(497929760)/設問TANAKA(1628969216)。1.25倍速はSTORY_SPEED_FACTOR=1.25で合成に焼き込み。
- グループC 50問完成(合格率100%まで修復)→絵カード460枚→検品UI組込(グリッド+ひらがなラベル)。
- A/B/D/E量産: 各50問。D専用軸を新規実装(話数1/1/2/2/3・設問1-5・T5>=50%)。B以降は歩留まり97-98%。
- 絵カード全量: 語彙1680・1656枚・カバレッジ99.8%・要人間11種(images_needs_human.json)。
- 総コスト: API $134.5 + 画像 ≈$70。
- 検証: 回帰テスト18/18、話速±10%内、実ブラウザで検品UI目視確認。

### 事故と対処（詳細は MyBrain 03_Knowledge/ai-content-mass-production.md）
- バックグラウンドkillでpython残存→エンジン渋滞 → detached+Monitor方式へ全面切替。
- Smart App Controlが未署名run.exeをブロック → 泰介さん判断でSACオフ。
- 実行中プロセスの書込先を移動してクラッシュ / 実行中のコード編集で新旧不整合クラッシュ → 運用ルール化。

### 保存（2026-07-24）
- MyBrain: 02_Projects/ohanashi-app.md 現在地更新、03_Knowledge/ai-content-mass-production.md 新規。
- リポジトリ: 本HANDOFF.md全面更新。auto-memory ohanashi-pro-p1.md 随時更新済み。

## 2026-07-25 4択統一 → こども本番モード → UI一新 → 機能拡充 → VPS本番公開

### 経緯・合意（ユーザー指示の流れ）
- 「だいたい4択が多いから4択にして」→ 全252問1279設問を4択に統一。
- 「推奨で」→ 案A（こども本番モードMVP先行）を承認・着手。
- 「今回実装するアプリのUIも一新したい」→ 3方向を提示→A統合案「答案用紙メソッド」→モック提示→「ナイス。これで以降」で本番反映承認。
- 「全部今日続けてやって」→ スタンプ帳/バッジ・模試モード・PWA化の3点を同日実装。
- 「次のフェーズへ」→ 選択肢提示→**「本番公開の準備」**を選択。認証方針は最終的に**「今は認証なしのまま」**を選択（課金EP公開の理解の上で）。

### 実行と結果
- **4択統一**: `pipeline/normalize_choices.py` 新規。5択546トリム(QC必須ダミー保護)/3択65追加(T2=数値カード機械追加, T1/T3=LLM選定)/multi 4of5→3of4を13件/R308修正3。SSOT連動=common.py・models・qc.py・仕様書・プロンプト03・qc_rules。回帰18/18。旧データ=content/_backup_pre4choice_20260725。
- **絵カード11種解消**: 実体は視覚QC隔離。目視判定で復活3＋alias1＋Gemini再生成7（全数目視合格）→ manifest画像欠落0。
- **こども本番モード**: `/pro/play`（play/index.html）＋app.pyルート。指示→本文1回→4択→2段階けってい→朱の採点丸→けっか。
- **UI一新（答案用紙メソッド）**: わら半紙/濃紺/朱丸/明朝、親ゾーン濃紺反転。モック artifact 1e44273f。
- **機能拡充**: 成績表（履歴から算出・タイプ別にがて分析／場面別ヒートマップは算出不能につき意図的に未実装）、がんばりの記録（連続日数・スタンプ帳・バッジ6種）、模試モード（3話連続・strict）、PWA（manifest/sw.js＝/pro/スコープ限定/アイコン）。
- **VPS本番公開**: コミット `f8eb935` push → VPS `git merge --ff-only` → pip → `pm2 restart ohanashi`。コンテンツ1.9GBを**tar-over-ssh**で転送（画像1681・mp3 1531・manifest252）。**https://ohanashi.bizsp.net/pro/play** 稼働。

### 検証（実物確認）
- test_client 実HTTP: v2/v3で全項目PASS（ルート・PWA配信・Content-Type・パストラバーサル防御・全4択維持・括弧均衡）。
- agent-browser 実レンダリング: ホーム/きく/もんだい/朱丸採点/けっか/カレンダー/親設定/成績表(空・データ)/スタンプ・バッジ/模試結果。
- 本番HTTPS: 全 `/pro/*` 200・SWスコープヘッダ・PWA成立を実測。

### 検証中に発見・修正したもの
- `.grade` の朱丸が全カードに表示 → 作者CSSの `display` がUAの `[hidden]` に勝つため。クラス制御へ修正。
- 置いた印が弱い → 手描きSVG○（クーピー色）に強化＋採点後は子の印を隠す。
- **【重要】VPSの `APP_PASSWORD` が空＝旧アプリ時代から無認証公開**（`/api/story`・`/api/tts` の課金EPを含む）。Renderのフェイルクローズドは `RENDER` 環境変数依存でVPSでは無効。→ 報告し、泰介さん判断で当面このまま。

### 保存（2026-07-25）
- MyBrain: `02_Projects/ohanashi-app.md`（2026-07-25の現在地/Next Actionを先頭に追加・frontmatter description更新・公開状況にVPS移行注記）、`03_Knowledge/vps-python-app-deploy.md`（gunicorn+PM2実例・tar-over-ssh・フェイルクローズドの罠を追記）。
- リポジトリ: HANDOFF.md 先頭を「VPS本番公開完了」に更新、本SESSION_LOG追記。
- auto-memory: `ohanashi-deployment.md`（VPS公開セクション新設）、`ohanashi-pro-p1.md`（4択統一・UI一新・3点セット・成績表を追記）。
