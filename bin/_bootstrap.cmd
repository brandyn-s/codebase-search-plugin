@echo off
rem Shared bootstrap for the Windows launchers (called by code-graph.cmd and
rem run-code-search.cmd). Same contract as bin/_bootstrap.sh: if either
rem component is missing, run install.ps1 once, serialized across launchers
rem with a lock directory, with installer output redirected to a log file.
rem Everything printed here goes to stderr; stdout is the MCP channel.
rem
rem Differences from the bash version: the installer runs in the foreground of
rem the first launcher (cmd has no detach-and-wait), and a lock is treated as
rem stale after CODE_INTEL_BOOTSTRAP_WAIT_SECONDS (default 1800) rather than
rem by checking the owner pid.
setlocal EnableDelayedExpansion
set "PLUGIN_DIR=%~1"
set "RUNTIME_DIR=%PLUGIN_DIR%\.runtime"
set "LOCK=%RUNTIME_DIR%\bootstrap.lock"
set "LOG=%RUNTIME_DIR%\bootstrap.log"
set "STATUS=%RUNTIME_DIR%\bootstrap.status"
set "SEARCH_EXE=%PLUGIN_DIR%\.venv\Scripts\code-search-mcp.exe"
set "GRAPH_EXE=%PLUGIN_DIR%\.runtime\bin\code-graph.exe"
if "%CODE_INTEL_BOOTSTRAP_WAIT_SECONDS%"=="" set "CODE_INTEL_BOOTSTRAP_WAIT_SECONDS=1800"
set /a WAITED=0

if exist "%SEARCH_EXE%" if exist "%GRAPH_EXE%" exit /b 0

if "%CODE_INTEL_NO_BOOTSTRAP%"=="1" (
    echo [code-intelligence] components are not installed and CODE_INTEL_NO_BOOTSTRAP=1; run: powershell -ExecutionPolicy Bypass -File "%PLUGIN_DIR%\install.ps1" 1>&2
    exit /b 1
)
if not exist "%PLUGIN_DIR%\install.ps1" (
    echo [code-intelligence] install.ps1 not found in "%PLUGIN_DIR%" 1>&2
    exit /b 1
)
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%"

:acquire
mkdir "%LOCK%" 2>nul && goto :install
if exist "%SEARCH_EXE%" if exist "%GRAPH_EXE%" exit /b 0
if !WAITED! GEQ %CODE_INTEL_BOOTSTRAP_WAIT_SECONDS% (
    echo [code-intelligence] gave up after !WAITED!s waiting for another bootstrap; removing stale lock "%LOCK%" 1>&2
    rmdir /s /q "%LOCK%" 2>nul
    goto :acquire
)
set /a MOD=WAITED %% 30
if !MOD! EQU 0 echo [code-intelligence] another launcher is installing the components; waiting ^(log: "%LOG%"^) 1>&2
rem ping is the portable sleep: two seconds, no console input required.
ping -n 3 127.0.0.1 >nul
set /a WAITED+=2
goto :acquire

:install
if exist "%SEARCH_EXE%" if exist "%GRAPH_EXE%" (
    rmdir /s /q "%LOCK%" 2>nul
    exit /b 0
)
echo [code-intelligence] components are not installed yet; running install.ps1 ^(first launch only, log: "%LOG%"^) 1>&2
set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"
pushd "%PLUGIN_DIR%"
%PS% -NoProfile -ExecutionPolicy Bypass -File "%PLUGIN_DIR%\install.ps1" > "%LOG%" 2>&1
set "RC=!ERRORLEVEL!"
popd
> "%STATUS%" echo !RC!
rmdir /s /q "%LOCK%" 2>nul
if not "!RC!"=="0" goto :failed
if not exist "%SEARCH_EXE%" goto :failed
if not exist "%GRAPH_EXE%" goto :failed
echo [code-intelligence] components installed 1>&2
exit /b 0

:failed
echo [code-intelligence] component installation failed ^(exit !RC!^); last lines of "%LOG%": 1>&2
if exist "%LOG%" %PS% -NoProfile -Command "Get-Content -Tail 8 -LiteralPath '%LOG%' | ForEach-Object { '[code-intelligence]   ' + $_ }" 1>&2
echo [code-intelligence] fix the cause and restart the MCP server, or run: powershell -ExecutionPolicy Bypass -File "%PLUGIN_DIR%\install.ps1" 1>&2
exit /b 1
