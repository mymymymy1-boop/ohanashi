# -*- coding: utf-8 -*-
"""
画像自動QCゲート — 生成画像を Gemini 視覚判定にかけ、量産事故を機械的に弾く。

チェック項目(2026-07-22試作で実際に起きたエラーに対応):
  subject_ok : 期待した内容が描かれているか
  has_text   : 文字・数字が写り込んでいないか(靴下カードの枠内文字等)
  has_frame  : 枠線・カード縁取りが描かれていないか(スタイル逸脱)
  bg_white   : 背景が白か

判定モデルはテキスト応答の gemini-2.5-flash (画像生成より安価)。
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from pipeline.common import BASE

load_dotenv(BASE / ".env")

QC_MODEL = os.getenv("GEMINI_QC_MODEL", "gemini-2.5-flash")
API_KEY = os.getenv("GEMINI_API_KEY", "")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{QC_MODEL}:generateContent"


def check_image(png_path: Path, expected: str, timeout: int = 60,
                require_white_bg: bool = True) -> dict:
    """{ok, subject_ok, has_text, has_frame, bg_white, notes} を返す。失敗時は例外。
    require_white_bg=False は場面カード用(空・地面を含む絵は白背景を要求しない)。"""
    b64 = base64.b64encode(png_path.read_bytes()).decode()
    ask = (
        "この画像は幼児向け教材の絵カード。以下をJSONだけで答えよ:\n"
        f"1. subject_ok: 画像の主対象が「{expected}」の主対象と合っているか。"
        "背景の有無・背景の色・構図の違いは判定に含めない(別項目で判定する)。"
        "ただし数の指定があれば数はぴったり一致すること。\n"
        "2. has_text: 文字・数字・記号が描かれているか\n"
        "3. has_frame: 画像の縁に枠線・カードの縁取りが描かれているか\n"
        "4. bg_white: 背景が白か\n"
        '出力: {"subject_ok": true/false, "has_text": true/false, '
        '"has_frame": true/false, "bg_white": true/false, "notes": "問題があれば一言"}'
    )
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "image/png", "data": b64}},
        {"text": ask},
    ]}]}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(URL, params={"key": API_KEY}, json=body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 529):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            text = "".join(p.get("text", "") for p in
                           r.json()["candidates"][0]["content"]["parts"])
            text = text.replace("```json", "").replace("```", "").strip()
            v = json.loads(text[text.find("{"):text.rfind("}") + 1])
            v["ok"] = bool(v.get("subject_ok")) and not v.get("has_text") \
                and not v.get("has_frame") \
                and (bool(v.get("bg_white")) or not require_white_bg)
            return v
        except (requests.RequestException, RuntimeError, KeyError,
                IndexError, ValueError) as e:
            last_err = e
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"画像QCに3回失敗: {last_err}")
