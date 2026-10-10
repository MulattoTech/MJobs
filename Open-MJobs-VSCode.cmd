@echo off
setlocal
REM Run this file directly from Command Prompt, PowerShell or Win+R.
REM No Git operations modify branches, commits, worktrees or origin.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Open-MJobs-VSCode.ps1" %*
exit /b %errorlevel%
