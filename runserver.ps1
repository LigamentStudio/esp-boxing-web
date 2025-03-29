# Check if Python is installed
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python not found in PATH. Please install Python or add it to your PATH."
    Read-Host -Prompt "Press Enter to exit"
    exit
}

# Install dependencies from requirements.txt
Write-Host "Installing dependencies..."
python -m pip install -r requirements.txt

# Start your Python application in a separate process
Write-Host "Starting the application..."
Start-Process python -ArgumentList "app.py"

# Wait a few seconds to allow the server to start up (adjust time if needed)
Start-Sleep -Seconds 3

# Automatically open the default web browser to the server URL
Write-Host "Opening web browser..."
Start-Process "http://localhost:5005"

# Optionally pause if you want the PowerShell window to remain open
Read-Host -Prompt "Press Enter to exit"
