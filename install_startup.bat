@echo off
set "APPDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $startup=[Environment]::GetFolderPath('Startup'); $s=$ws.CreateShortcut((Join-Path $startup 'Tuition Fee Manager.lnk')); $s.TargetPath=Join-Path '%APPDIR%' 'start.bat'; $s.WorkingDirectory='%APPDIR%'; $s.WindowStyle=1; $s.Save()"
echo Startup shortcut created. The PC must be on and the app running for scheduled reminders.
pause
