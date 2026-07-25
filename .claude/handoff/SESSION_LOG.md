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
