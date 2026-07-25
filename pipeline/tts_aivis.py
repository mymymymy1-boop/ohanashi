# -*- coding: utf-8 -*-
"""
AivisSpeech Engine バックエンド — ローカルエンジン(VOICEVOX互換API)で合成し、
仕様どおり mp3 64kbps mono (ffmpeg変換) で返す。生成コストは0円。

前提: AivisSpeech Engine が起動していること
    C:\\dev\\_tools\\Windows-x64\\run.exe --host 127.0.0.1 --port 10101

スタイルIDは環境変数で差し替え可能:
    AIVIS_STYLE_STORY    (既定: morioki/ノーマル 497929760 — 2026-07-22 泰介さん選定・本文用)
    AIVIS_STYLE_QUESTION (既定: TANAKA/ノーマル 1628969216 — 同・設問用)
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from pipeline.common import BASE

load_dotenv(BASE / ".env")

AIVIS_BASE_URL = os.getenv("AIVIS_BASE_URL", "http://127.0.0.1:10101").strip()
STYLE_STORY = int(os.getenv("AIVIS_STYLE_STORY", "497929760"))
STYLE_QUESTION = int(os.getenv("AIVIS_STYLE_QUESTION", "1628969216"))

AUDIO_CACHE = BASE / "audio_cache"
AUDIO_CACHE.mkdir(exist_ok=True)


def engine_alive() -> bool:
    try:
        return requests.get(f"{AIVIS_BASE_URL}/version", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def synth_aivis(text: str, style_id: int, speed: float = 1.0) -> tuple[bytes, bool, int]:
    """AivisSpeechで合成して (mp3bytes, from_cache, billed_chars=0) を返す。失敗時は例外。
    ElevenLabs系(synth_pro)と同じ戻り値形にしてバックエンド差し替えを容易にする。"""
    speed = max(0.5, min(2.0, float(speed)))
    key = hashlib.md5(f"aivis:{style_id}:{speed}:{text}".encode("utf-8")).hexdigest()
    cache_path = AUDIO_CACHE / f"aivis_{key}.mp3"
    if cache_path.exists():
        return cache_path.read_bytes(), True, 0

    last_err = None
    for attempt in range(3):
        try:
            q = requests.post(f"{AIVIS_BASE_URL}/audio_query",
                              params={"text": text, "speaker": style_id}, timeout=60)
            q.raise_for_status()
            query = q.json()
            if abs(speed - 1.0) > 0.001:
                query["speedScale"] = speed
            # Lv5(1100字超)×話速補正はCPU合成で10分を超えることがある(2026-07-22実測)
            w = requests.post(f"{AIVIS_BASE_URL}/synthesis", params={"speaker": style_id},
                              json=query, headers={"Content-Type": "application/json"},
                              timeout=1800)
            w.raise_for_status()
            if len(w.content) < 1000:
                raise RuntimeError(f"AivisSpeech 空応答 ({len(w.content)}B)")
            break
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            print(f"  [aivis] {attempt+1}回目失敗: {e}", flush=True)
            if attempt < 2:
                time.sleep(1 + attempt * 2)
    else:
        raise RuntimeError(f"AivisSpeech 合成に3回失敗 (最後のエラー: {last_err})")

    # wav → mp3 64kbps mono (R602準拠)。一時wavを経由してffmpegで変換。
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(w.content)
        wav_path = Path(f.name)
    try:
        tmp_mp3 = cache_path.with_suffix(".tmp.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
             "-ac", "1", "-b:a", "64k", str(tmp_mp3)],
            check=True, capture_output=True)
        os.replace(tmp_mp3, cache_path)
    finally:
        wav_path.unlink(missing_ok=True)
    return cache_path.read_bytes(), False, 0
