@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   おはなしのきおく  起動中...
echo ============================================

REM 初回のみ依存をインストール
if not exist ".installed" (
    echo 初回セットアップ中です。少しお待ちください...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [エラー] Python が見つからないか、インストールに失敗しました。
        echo Python がインストールされているか確認してください。
        pause
        exit /b 1
    )
    echo done > .installed
)

REM .env 確認
if not exist ".env" (
    echo.
    echo [注意] .env ファイルがありません。
    echo .env.example をコピーして .env を作り、APIキーを入力してください。
    echo.
    pause
    exit /b 1
)

python app.py
pause
