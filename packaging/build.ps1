# 建置 PyInstaller 發行資料夾：dist\SystemAudioSubtitle\
# 之後把 models\ 資料夾複製到 dist\SystemAudioSubtitle\ 旁（或跑 setup_models.py）。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
& .venv\Scripts\python.exe -m PyInstaller packaging\sas.spec --noconfirm --distpath dist --workpath build
Write-Host "完成：dist\SystemAudioSubtitle\SystemAudioSubtitle.exe"
Write-Host "提醒：models\ 不在包裡，請複製到 exe 旁邊，或在目標機器跑 scripts\setup_models.py"
