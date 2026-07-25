# -*- coding: utf-8 -*-
"""
ElevenLabs 音声合成 (パイプライン用) — app.py の synth と同じ堅牢化パターンを
声・出力フォーマット可変で切り出したもの。既存アプリには手を入れない。

- 出力は仕様 §4 の mp3 64kbps mono (output_format=mp3_44100_64)
- 同一 (voice, model, speed, format, text) は audio_cache/ を共有してクレジット二重消費を防ぐ
- 429/5xx/接続断/空応答は指数バックオフで最大3回リトライ。空応答はキャッシュに焼き付けない
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from pipeline.common import BASE

load_dotenv(BASE / ".env")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_MODEL = os.getenv("PIPELINE_ELEVENLABS_MODEL",
                             os.getenv("ELEVENLABS_MODEL", "eleven_v3")).strip()
# 仕様 §4: 本文=クローンボイス / 設問=AI女性ボイス「松井さくら」
VOICE_STORY = os.getenv("PRO_VOICE_STORY", "sRsoEYfdps0ByjODUjOo").strip()
VOICE_QUESTION = os.getenv("PRO_VOICE_QUESTION", "a0MsDWokG5Xsuji8g8er").strip()

OUTPUT_FORMAT = "mp3_44100_64"   # R602: 64kbps mono
AUDIO_CACHE = BASE / "audio_cache"
AUDIO_CACHE.mkdir(exist_ok=True)


def synth_pro(text: str, voice_id: str, speed: float = 1.0) -> tuple[bytes, bool, int]:
    """音声合成して (bytes, from_cache, billed_chars) を返す。失敗時は例外。"""
    speed = max(0.7, min(1.2, float(speed)))
    key = hashlib.md5(
        f"pro:{voice_id}:{ELEVENLABS_MODEL}:{speed}:{OUTPUT_FORMAT}:{text}".encode("utf-8")
    ).hexdigest()
    cache_path = AUDIO_CACHE / f"pro_{key}.mp3"
    if cache_path.exists():
        return cache_path.read_bytes(), True, 0

    voice_settings = {"stability": 0.80, "similarity_boost": 0.70,
                      "style": 0.0, "use_speaker_boost": True}
    if abs(speed - 1.0) > 0.001:
        voice_settings["speed"] = speed
    payload = {"text": text, "model_id": ELEVENLABS_MODEL,
               "language_code": "ja", "voice_settings": voice_settings}

    def _post(pl):
        return requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json=pl, timeout=120,
        )

    def _post_with_param_fallback():
        r = _post(payload)
        if r.status_code >= 400 and "language_code" in (r.text or ""):
            payload.pop("language_code", None)
            r = _post(payload)
        if r.status_code >= 400 and "speed" in (r.text or "") and "speed" in payload["voice_settings"]:
            payload["voice_settings"].pop("speed", None)
            r = _post(payload)
        return r

    last_err = None
    for attempt in range(3):
        try:
            r = _post_with_param_fallback()
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"ElevenLabs 一時エラー {r.status_code}")
            r.raise_for_status()
            if len(r.content) < 500:
                raise RuntimeError(f"ElevenLabs 空応答 ({len(r.content)}B)")
            break
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            print(f"  [tts] {attempt+1}回目失敗: {e}", flush=True)
            if attempt < 2:
                time.sleep(1 + attempt * 2)
    else:
        raise RuntimeError(f"音声生成に3回失敗 (最後のエラー: {last_err})")

    tmp = cache_path.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    os.replace(tmp, cache_path)
    return r.content, False, len(text)
