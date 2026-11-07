@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

:check_ollama
curl.exe -fs --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>&1
if not errorlevel 1 goto loop

echo [!] Запускаю сервер Ollama...
start "" ollama.exe serve

for /l %%I in (1,1,10) do (
    timeout /t 1 /nobreak >nul
    curl.exe -fs --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>&1
    if not errorlevel 1 goto loop
)

echo [ОШИБКА] API Ollama не ответил.
echo Запустите приложение Ollama Desktop и нажми любую клавишу для повторной проверки.
pause >nul
goto check_ollama

:loop
python generate_cards.py
goto loop