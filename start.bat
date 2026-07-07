@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   おはなしのきおく  起動中...
echo ============================================

REM 依存をインストール（既に入っていれば数秒で完了。requirements.txt を更新したら自動で反映される）
echo 必要なライブラリを確認中です...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo [エラー] Python が見つからないか、インストールに失敗しました。
    echo Python 3.11 系がインストールされ、PATH に通っているか確認してください。
    pause
    exit /b 1
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
