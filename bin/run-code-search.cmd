@echo off
rem MCP launcher for code-search on Windows. Mirrors bin/run-code-search:
rem installs the pinned component on first launch via install.ps1, then runs it.
rem stdout is the MCP protocol channel; every message here goes to stderr.
setlocal
set "PLUGIN_DIR=%~dp0.."
set "EXE=%PLUGIN_DIR%\.venv\Scripts\code-search-mcp.exe"
call "%~dp0_bootstrap.cmd" "%PLUGIN_DIR%" || exit /b 1
if not exist "%EXE%" (
    echo [code-intelligence] code-search-mcp is not installed at "%EXE%"; run install.ps1 from "%PLUGIN_DIR%" 1>&2
    exit /b 1
)
"%EXE%" %*
exit /b %ERRORLEVEL%
