@echo off
REM Check if Python is installed
where python >nul 2>&1
if errorlevel 1 (
    echo Python not found in PATH. Please install Python or add it to your PATH.
    pause
    exit /b
)

REM Install dependencies from requirements.txt
echo Installing dependencies...
python -m pip install -r requirements.txt

REM Start your Python application in a new window
echo Starting the application...
start "PythonApp" python app.py

REM Wait a few seconds to allow the server to start (adjust if needed)
timeout /t 3 /nobreak >nul

REM Open the default web browser to the server URL
echo Opening web browser...
start "" "http://localhost:5005"

REM Pause to keep the window open
pause
