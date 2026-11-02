@echo off
setlocal
chcp 65001 >nul
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%~dp0.." || exit /b 1
"%~dp0..\week11\.venv\Scripts\python.exe" -X utf8 -B -m week13.src.case_console %*
set "v12_exit_code=%errorlevel%"
popd
exit /b %v12_exit_code%
