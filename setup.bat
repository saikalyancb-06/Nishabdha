@echo off
REM ===== UNCLONE backend - one-time setup (Windows, GPU/CPU) =====
cd /d %~dp0
echo ==== UNCLONE setup %date% %time% ==== > setup.log
echo [check] where python / py: >> setup.log
where python >> setup.log 2>&1
where py >> setup.log 2>&1
py -0p >> setup.log 2>&1
python --version >> setup.log 2>&1

set PY=
for %%V in (3.11 3.12 3.13 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set PY=py -%%V
  )
)
if not defined PY (
  python -c "import sys; v=sys.version_info[:2]; sys.exit(0 if (3,10)<=v<=(3,13) else 1)" >nul 2>nul && set PY=python
)
if not defined PY (
  echo Python 3.11 not found - installing it with the Python install manager...
  echo [step] py install 3.11 >> setup.log
  py install 3.11 >> setup.log 2>&1
  py -3.11 -c "import sys" >nul 2>nul && set PY=py -3.11
)
if not defined PY (
  echo.
  echo [ERROR] Python 3.10 - 3.13 not found. Install Python 3.11 from https://www.python.org/downloads/release/python-3119/
  echo         Tick "Add python.exe to PATH" during install, then run setup.bat again.
  echo [ERROR] no suitable python >> setup.log
  pause
  exit /b 1
)
echo Using: %PY%
echo [ok] using %PY% >> setup.log
%PY% --version >> setup.log 2>&1

if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv >> setup.log 2>&1 || goto :err
)
call .venv\Scripts\activate.bat
echo Installing packages (5-10 minutes)...
python -m pip install --upgrade pip >> setup.log 2>&1

where nvidia-smi >nul 2>nul
if %errorlevel% equ 0 (
  echo [GPU] NVIDIA GPU detected. Installing PyTorch with CUDA 12.4 support...
  echo [step] installing torch CUDA 12.4 >> setup.log
  pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124 >> setup.log 2>&1 || goto :err
) else (
  echo [CPU] No NVIDIA GPU detected. Installing PyTorch CPU...
  echo [step] installing torch CPU >> setup.log
  pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu >> setup.log 2>&1 || goto :err
)
echo [ok] torch >> setup.log
pip install -r requirements-dev.txt >> setup.log 2>&1 || goto :err
echo [ok] requirements >> setup.log
if not exist .env copy .env.example .env >nul
echo.
echo Downloading models (about 1.5 GB)...
set PYTHONUTF8=1
python scripts\download_models.py >> setup.log 2>&1
type setup.log | findstr /C:"[OK]" /C:"[FAIL]"
echo [done] setup finished >> setup.log
echo.
echo Setup done. Put your GROQ_API_KEY in .env, then double-click run.bat
pause
goto :eof
:err
echo [ERROR] step failed, see setup.log >> setup.log
echo.
echo SETUP FAILED - details are in setup.log
pause
exit /b 1
