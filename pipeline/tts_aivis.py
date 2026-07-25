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

import atexit
import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from pipeline.common import BASE

load_dotenv(BASE / ".env")

AIVIS_BASE_URL = os.getenv("AIVIS_BASE_URL", "http://127.0.0.1:10101").strip()
STYLE_STORY = int(os.getenv("AIVIS_STYLE_STORY", "497929760"))
STYLE_QUESTION = int(os.getenv("AIVIS_STYLE_QUESTION", "1628969216"))

# エンジン実行ファイル。起動・終了の面倒をパイプライン側で見る（下の engine_session 参照）
AIVIS_ENGINE_EXE = os.getenv("AIVIS_ENGINE_EXE", r"C:\dev\_tools\Windows-x64\run.exe")
# 終了ポリシー: always=パイプライン終了時に必ず止める（既定） / auto=自分で起動した分だけ / never=止めない
# 既定を always にしているのは、エンジンが放置されると数GB〜十数GBを占有し続け、
# 実際に2026-07-24(18.8GB)・07-25(2.7GB)とPCのメモリ逼迫を起こしたため。
# エンジンは ohanashi の音声生成専用なので、パイプラインが終わったら不要になる。
AIVIS_STOP_POLICY = os.getenv("AIVIS_STOP_POLICY", "always").strip().lower()
ENGINE_BOOT_TIMEOUT = int(os.getenv("AIVIS_BOOT_TIMEOUT", "300"))

AUDIO_CACHE = BASE / "audio_cache"
AUDIO_CACHE.mkdir(exist_ok=True)

_engine_proc: subprocess.Popen | None = None
_started_by_us = False


def engine_alive() -> bool:
    try:
        return requests.get(f"{AIVIS_BASE_URL}/version", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def start_engine(timeout: int = ENGINE_BOOT_TIMEOUT) -> bool:
    """エンジンが動いていなければ起動する。自分で起動したときだけ True。

    既に動いているエンジン（人が手で立てたもの等）には触らず False を返す。
    モデル読み込みで起動に数十秒かかるため /version が通るまで待つ。
    """
    global _engine_proc, _started_by_us
    if engine_alive():
        return False

    exe = Path(AIVIS_ENGINE_EXE)
    if not exe.exists():
        raise RuntimeError(
            f"AivisSpeech Engine が見つかりません: {exe}\n"
            "AIVIS_ENGINE_EXE で場所を指定するか、手動で起動してください。"
        )

    u = urlparse(AIVIS_BASE_URL)
    cmd = [str(exe), "--host", u.hostname or "127.0.0.1", "--port", str(u.port or 10101)]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    print(f"  [aivis] エンジン起動中… ({exe.name})", flush=True)
    try:
        _engine_proc = subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        # Smart App Control(SAC)が有効だと未署名のrun.exeはプログラムからも手動からも起動不可
        # （WinError 4551）。SACはセキュリティ設定なのでスクリプトからは変更しない。
        raise RuntimeError(
            f"AivisSpeech Engine を起動できませんでした: {e}\n"
            "Smart App Control が有効だと未署名の run.exe はブロックされます。\n"
            "対処: Windowsセキュリティ →「アプリとブラウザーの制御」→ Smart App Control をオフにする\n"
            "  （設定変更は人が行ってください。オフ後は自動起動・自動終了が有効になります）\n"
            f"  もしくは手動で先に起動: {exe} --host 127.0.0.1 --port {u.port or 10101}"
        ) from e
    _started_by_us = True
    # プロセスが異常終了した場合に備え、atexitでも後始末する（Ctrl-C・例外時の保険）
    atexit.register(stop_engine)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _engine_proc.poll() is not None:
            _started_by_us = False
            raise RuntimeError(f"エンジンが起動直後に終了しました (exit={_engine_proc.returncode})")
        if engine_alive():
            print(f"  [aivis] エンジン起動完了 (pid={_engine_proc.pid})", flush=True)
            return True
        time.sleep(2)
    stop_engine(force=True)
    raise RuntimeError(f"エンジンが{timeout}秒以内に応答しませんでした")


def find_engine_pids() -> list[int]:
    """AIVIS_ENGINE_EXE と同じ実行ファイルパスで動いているプロセスのPIDを返す。

    プロセス名だけで taskkill /IM すると、同名の無関係な run.exe まで巻き添えにするため、
    必ず実行ファイルのフルパス一致で特定する。
    """
    if sys.platform != "win32":
        return []
    target = str(Path(AIVIS_ENGINE_EXE)).replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ExecutablePath -eq '{target}' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except (subprocess.SubprocessError, ValueError):
        return []


def stop_engine(force: bool = False) -> bool:
    """エンジンを終了する。止めたら True。

    既定（AIVIS_STOP_POLICY=always）では、誰が起動したエンジンでもパイプライン終了時に止める。
    放置すると数GB〜十数GBを占有し続けるため（実際にPCのメモリ逼迫を2度起こした）。
    AIVIS_STOP_POLICY=auto なら自分で起動した分だけ、never なら止めない。
    """
    global _engine_proc, _started_by_us
    if AIVIS_STOP_POLICY == "never" and not force:
        return False
    if not force and AIVIS_STOP_POLICY != "always" and not _started_by_us:
        return False

    proc, was_ours, _engine_proc, _started_by_us = _engine_proc, _started_by_us, None, False
    stopped = False

    # 1) 自分で起動した子プロセスは行儀よく終了させる
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        stopped = True

    # 2) 他所で起動されたエンジンも（always/force のとき）実行パス一致で確実に落とす
    if not was_ours or engine_alive():
        for pid in find_engine_pids():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            stopped = True

    if stopped:
        print("  [aivis] エンジンを終了しました（メモリ解放）", flush=True)
    return stopped


@contextlib.contextmanager
def engine_session():
    """with で囲んだ範囲だけエンジンを生かす。

        with engine_session():
            ...合成処理...

    自分で起動した場合は抜けるときに必ず終了する（例外・Ctrl-Cでも finally で回収）。
    既に起動していたエンジンは既定では触らない（AIVIS_STOP_POLICY=always で強制終了）。
    """
    started = start_engine()
    if not started:
        print("  [aivis] 既存のエンジンを使用（終了時に停止します）", flush=True)
    try:
        yield started
    finally:
        stop_engine()


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
