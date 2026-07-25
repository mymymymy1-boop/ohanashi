# お話の記憶 PRO — Claude Code ハンドオフ (P1実装指示)

対象リポジトリ: C:\dev\ohanashi\ (既存Flask + ElevenLabsパイプライン + PWAパックビルダーを拡張)
このフォルダをリポジトリ直下に `docs/handoff/` として配置し、CLAUDE.md から参照させること。

## 同梱物

- `spec/ohanashi_pro_spec_v1.1.md` — 製品仕様書(正)。実装判断はすべてこれに従う
- `prompts/01_skeleton_gen.md` — 骨格JSON生成用システムプロンプト(Claude API)
- `prompts/02_level_expand.md` — Lv別本文展開用システムプロンプト
- `prompts/03_question_gen.md` — 設問+ダミー選択肢生成用システムプロンプト
- `schemas/content_schemas.json` — story_skeleton / story_text / question のJSON Schema
- `qc/qc_rules.md` — 自動QCゲートの機械判定ルール(実装対象)

## P1スコープ(これだけやる。他はやらない)

1. **スキーマ実装**: `schemas/content_schemas.json` をPythonのpydanticモデルに落とす(`models/content.py`)
2. **生成パイプライン**: `pipeline/generate.py`
   - 入力: グループID(P1は "C" 固定)、テーマヒントリスト、季節
   - 処理: 01→02→03 のプロンプトを順にClaude API(claude-sonnet-4-6)へ投げ、各段でスキーマバリデーション
   - 出力: `content/pilot/{skeleton_id}/` に skeleton.json / lv{1-5}_text.json / lv{1-5}_questions.json
3. **自動QCゲート**: `pipeline/qc.py` — `qc/qc_rules.md` の全ルールを実装。失敗理由付きでreject、自動リトライは2回まで
4. **音声生成**: 既存ElevenLabsパイプラインを流用し、QC通過分のみ `content/pilot/.../audio/` にmp3出力(本文=クローンボイス、設問=松井さくら)
5. **検品キューHTML**: `review/index.html` — 問題ごとに音声再生+本文+設問を表示し、OK/NG/メモをlocalStorageに記録→JSONエクスポート。スマホ閲覧可能なシンプル構成
6. **パイロット実行**: グループCで骨格12本→Lv展開60本→うち30問を検品対象として出力

## 受け入れ基準(P1完了の定義)

- [ ] 30問がQCゲートを全通過して音声付きで出力されている
- [ ] 文字数・場面数・設問数・タイプ比率が仕様§1.2/§1.3のマトリクスに適合(qc.pyのレポートで確認可能)
- [ ] 検品キューがスマホで動作し、判定結果をJSONで書き出せる
- [ ] 生成コスト(API+ElevenLabs)が1問あたりで記録されている(P2の250問の予算根拠にする)

## 実装上の注意

- 話速換算: 本文文字数÷話速(字/分)で想定再生秒数を算出しメタデータに保存。ElevenLabs出力の実測秒数と±10%乖離したらQC警告
- ひらがな比率: 幼児向け読み上げ原稿のため漢字使用は不可(すべてひらがな+カタカナ)。ただしskeleton内部キーは英数字
- 乱数シード: 変奏再生成の再現性のため、骨格生成時のテーマ・キャラ選択はシード記録
- 既存の個人用アプリのルート・データは壊さない。新機能は `/pro/` 名前空間で追加
