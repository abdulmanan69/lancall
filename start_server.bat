@echo off
title LAN Voice Call Server

echo LAN Voice Call Server Starter
echo =============================

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH
    echo Please install Python 3.6 or later and try again
    pause
    exit /b 1
)

echo Python is installed
echo.

REM Check if pip is installed
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing pip...
    python -m ensurepip --upgrade
    if %errorlevel% neq 0 (
        echo Error: Failed to install pip
        pause
        exit /b 1
    )
)

echo Pip is available
echo.

REM Install required packages
echo Installing required packages...
python -m pip install pyOpenSSL websockets
if %errorlevel% neq 0 (
    echo Error: Failed to install required packages
    pause
    exit /b 1
)

echo Required packages installed
echo.

REM Check if SSL certificates exist, generate if not
if not exist "server.crt" (
    echo Generating SSL certificates...
    python generate_cert.py
    if %errorlevel% neq 0 (
        echo Error: Failed to generate SSL certificates
        pause
        exit /b 1
    )
    echo SSL certificates generated
) else (
    echo SSL certificates found
)

echo.
echo Starting both HTTPS and Signaling servers...
echo ============================================
echo.

REM Start both servers in separate windows
start "HTTPS Server" python https_server.py
start "Signaling Server" python signaling_server.py

echo Both servers started in separate windows
echo Close both windows to stop the servers
echo.

pause