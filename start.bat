@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo Python 3 is not installed. Install Python 3.10+ and try again.
    pause
    exit /b 1
  )
  call .venv\Scripts\activate.bat
  echo Installing required packages. Internet is required for the first setup.
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  if errorlevel 1 (
    echo Package installation failed. Check your internet connection and run start.bat again.
    pause
    exit /b 1
  )
) else (
  call .venv\Scripts\activate.bat
)
start "Tuition Fee Manager" http://127.0.0.1:5000
python app.py
pause
