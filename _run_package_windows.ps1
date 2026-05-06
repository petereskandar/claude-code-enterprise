Set-Location "c:\Users\eskandar\git\AWS - Amazon Bedrock for Claude Code\AWS - Amazon Bedrock for Claude Code - Peter\source"
poetry run ccwb package --target-platform windows 2>&1 | Tee-Object -FilePath "..\ccwb_package_windows.log"
Write-Host "EXIT: $LASTEXITCODE"
