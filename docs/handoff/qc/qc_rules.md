# 自動QCゲート 機械判定ルール (pipeline/qc.py 実装対象)

各ルールは `rule_id` で失敗レポートに出力する。1件でもFAILで問題全体をreject(リトライ上限2回、超過は人間キューへ「生成不能」フラグで回す)。

## R1xx: スキーマ・構造

- R101: 3ファイル(skeleton/text/questions)がcontent_schemas.jsonに適合
- R102: questionsの skeleton_id / level が text と一致
- R103: correct の全要素が choices の id に存在
- R104: choices の id / image_key に重複なし

## R2xx: 文字数・場面・時間 (仕様§1.2)

- R201: char_count がLv規定範囲内 (L1:200-300 / L2:400-450 / L3:600-650 / L4:850-900 / L5:1000-1200。group=Dは別表)
- R202: 使用場面数がLv規定と一致 (1:2, 2:4, 3:6, 4:8, 5:10)
- R203: 各場面 50〜150字(Lv1のみ100〜150字許容)
- R204: expected_duration_sec = char_count / speech_rate_cpm * 60 (誤差1%以内)
- R205: 音声生成後、実測秒数が (expected_duration_sec ÷ STORY_SPEED_FACTOR) ±10%以内(超過は話速再調整して再生成)。STORY_SPEED_FACTOR=1.25 — 2026-07-22 泰介検品「仕様想定の1.25倍速がちょうど良い」により本文の合成目標を1.25倍速に変更(pipeline/common.py が実装正本、env で変更可)
- R206: 1文の長さ45字以内(「。」区切りで判定)

## R3xx: 設問構成 (仕様§1.3)

- R301: 設問数がLv規定(group=Bは+1)
- R302: 選択肢数が全Lv4択(2026-07-25 泰介決定「実試験は4択が主流」で統一。旧規定 1:3/2:4/3:4/4:4-5/5:5 は廃止)
- R303: Lv別禁止タイプが含まれていない
- R304: Lv別必須タイプを充足(Lv1: T1比率>=70% / Lv5: {T6,T7,T8,T9}のうち3タイプ以上 等)
- R305: group=D で T5比率>=50% かつ T2なし
- R306: time_limit_sec がLv規定(group=Bは-3秒)
- R307: Lv3以上で instruction の (mark,color) 組合せが2種類以上、multi:true が1問存在
- R308: 正解位置の偏りなし(同一位置に3問以上連続で正解が来ない)

## R4xx: ダミー配合 (仕様§2.4)

- R401: Lv3で strong>=1、Lv4-5で strong>=1(推奨2)
- R402: Lv4-5で attribute_swap>=1、Lv1-2に attribute_swap なし
- R403: T6(Lv4+)に scene_composite あり
- R404: T5の選択肢が emotion_set 4種構成
- R405: T2の choices に numeric ダミー(正解±1を含む)
- R406: strong ダミーのidが本文(scenes_text結合)に実際に登場している
- R407: category ダミーのidが本文に登場していない

## R5xx: 本文・語彙・整合性

- R501: 本文に漢字が含まれない(ひらがな・カタカナ・「」『』、。のみ)
- R502: 語彙レベル違反なし(L3語彙リスト外の難語検出。Lv1-2はL1リスト外の語を警告)
- R503: 季節矛盾なし(季節DB照合: aki の話に「ひまわり」「せみ」等が出たらFAIL)
- R504: Lv4以上の本文に mentioned_absent への言及があり、かつ当該キャラの行動描写がない
- R505: Lv5(該当時)の本文に dual_orders の2系列が両方読み取れる
- R506: T7/T8/T9/T10 の正解根拠が本文に存在(evidence_scene_seq の場面テキストに根拠語が含まれる)
- R507: 数の合計が10以下、個々の数が1〜5
- R508: 教育的NG語(暴力・恐怖・差別的表現)の禁止リスト照合

## R6xx: 音声・アセット

- R601: 全 image_key が画像ライブラリに存在(なければ「要生成リスト」に出力し、SVGエンジンで生成後に再判定)
- R602: 本文mp3・各設問mp3が生成済みでビットレート64kbps mono
- R603: 音声冒頭・末尾の無音が0.3〜0.8秒(トリミング検証)

## レポート形式

qc.py は問題ごとに `{"q_pack_id": ..., "status": "pass|fail", "failures": [{"rule_id": "R503", "detail": "akiの話にひまわり(scene 3)"}], "warnings": [...], "cost": {"api_tokens": n, "tts_chars": n}}` をJSONL出力。パイロット全体のサマリ(pass率・ルール別FAIL数・1問あたりコスト)も出力する。
