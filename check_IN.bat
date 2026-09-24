@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "$script = Get-ChildItem -Path $env:USERPROFILE -Filter check_in_app.py -File -Recurse | Where-Object { $_.FullName -like '*GitHub\Check_IN\check_in_app.py' } | Select-Object -First 1 -ExpandProperty FullName; if (-not $script) { Write-Error 'check_in_app.py not found'; exit 1 }; & py -3 $script"
