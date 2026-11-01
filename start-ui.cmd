@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---------- is something already listening on 8501? ----------
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient('127.0.0.1',8501);$c.Close();exit 0}catch{exit 1}"
if errorlevel 1 goto launch

powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -TimeoutSec 3 -UseBasicParsing;if($r.StatusCode -eq 200 -and ($r.Content -match 'ok')){exit 0}else{exit 1}}catch{exit 1}"
if errorlevel 1 goto portbusy

echo [INFO] The console is already running on port 8501.
echo [INFO] Opening http://localhost:8501
start "" http://localhost:8501
exit /b 0

:portbusy
echo [ERROR] Port 8501 is occupied by another program (not this console).
echo [ERROR] Start on a free port instead, for example:
echo           uv run streamlit run streamlit_app.py --server.port 8599
echo.
pause
exit /b 1

:launch
set "HAS_KEY="
if not "%DEEPSEEK_API_KEY%"=="" set "HAS_KEY=1"
if exist ".env" (
  findstr /R /C:"^DEEPSEEK_API_KEY=sk-" ".env" >nul 2>&1
  if not errorlevel 1 set "HAS_KEY=1"
)
if not defined HAS_KEY (
  echo [WARN] DEEPSEEK_API_KEY not found in environment or .env
  echo        The UI can still browse past runs, but cannot start new ones.
)

echo [INFO] Starting LangGraph experiment console
echo [INFO] Open http://localhost:8501 in your browser
echo.

rem 优先用 uv（项目由 pyproject.toml + uv.lock 管理）；找不到 uv 时回退到 venv 解释器
where uv >nul 2>&1
if errorlevel 1 (
  echo [WARN] uv not found on PATH - falling back to the project virtualenv
  ".venv\Scripts\python.exe" -m streamlit run streamlit_app.py
) else (
  uv run streamlit run streamlit_app.py
)
pause