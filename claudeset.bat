@echo off
setlocal EnableExtensions

REM ===== НАСТРОЙКИ =====

set "ANTHROPIC_API_KEY=sk-qTgYcsp1WN9bi4JYpd9Wc2KQ5htAxbUuh6FRVzPuJ1a6p2bT"
set "ANTHROPIC_BASE_URL=https://seekai.cc"
set "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
set "ANTHROPIC_MODEL=claude-opus-5"

REM Проверяем наличие claude
where claude >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Команда "claude" не найдена.
    echo Установи Claude Code CLI и добавь его в PATH.
    pause
    exit /b 1
)

echo.
echo Starting Claude Code via OmniRoute...
echo Endpoint: %ANTHROPIC_BASE_URL%
echo Model: %ANTHROPIC_MODEL%
echo.

claude

set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Claude завершился с кодом: %EXIT_CODE%
    pause
)

endlocal & exit /b %EXIT_CODE%
