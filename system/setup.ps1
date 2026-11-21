# SnipRiot one-time setup. Run by SETUP.bat. Installs free tools, keeps the laptop awake on the charger,
# registers the recorder to start at sign-in (and restart if it stops), then starts it.
$ErrorActionPreference = "Continue"
$sys = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $sys
$dataRoot = Join-Path $env:USERPROFILE "SnipRiot"   # outside protected folders (Videos is blocked by Controlled folder access)
New-Item -ItemType Directory -Force -Path "$dataRoot\data\logs" | Out-Null
$report = "$dataRoot\data\setup_check.txt"
# stop an earlier copy of the recorder before re-registering
Stop-ScheduledTask -TaskName "SnipRiot Recorder" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "SnipRiot.system.START_RECORDER" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$pidFile = "$dataRoot\data\worker.pid"
if (Test-Path $pidFile) { Stop-Process -Id ([int](Get-Content $pidFile)) -Force -ErrorAction SilentlyContinue }
"SnipRiot setup $(Get-Date -Format s)" | Out-File $report -Encoding utf8

Write-Host "`n[1/4] Installing free tools (Python, FFmpeg, yt-dlp, Deno). This can take a few minutes..." -ForegroundColor Cyan
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Write-Host "winget is missing. Install 'App Installer' from the Microsoft Store, then run SETUP.bat again." -ForegroundColor Red
  "ERROR: winget missing" | Out-File $report -Append; exit 1
}
foreach ($id in "Python.Python.3.12", "Gyan.FFmpeg", "yt-dlp.yt-dlp", "DenoLand.Deno") {
  Write-Host "  - $id"
  winget install -e --id $id --scope user --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { winget install -e --id $id --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null }
}
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
try { yt-dlp -U 2>&1 | Out-Null } catch {}

Write-Host "`n[2/4] Checking tools..." -ForegroundColor Cyan
$ok = $true
foreach ($t in @(@("py", "-3 --version"), @("ffmpeg", "-version"), @("ffprobe", "-version"), @("yt-dlp", "--version"), @("deno", "--version"))) {
  $cmd = Get-Command $t[0] -ErrorAction SilentlyContinue
  if ($cmd) {
    $v = (& $t[0] $t[1].Split(" ") 2>&1 | Select-Object -First 1)
    Write-Host "  OK  $($t[0]): $v" -ForegroundColor Green
    "OK $($t[0]) $v ($($cmd.Source))" | Out-File $report -Append
  } else {
    Write-Host "  MISSING $($t[0])" -ForegroundColor Red; "MISSING $($t[0])" | Out-File $report -Append; $ok = $false
  }
}

Write-Host "`n[3/4] Keeping the laptop awake while plugged in (lid closed = keep running)..." -ForegroundColor Cyan
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
"power: sleep never on AC, lid = do nothing on AC" | Out-File $report -Append

Write-Host "`n[4/4] Registering the recorder to start automatically..." -ForegroundColor Cyan
$a = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$sys\hidden.vbs`""
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "SnipRiot Recorder" -Action $a -Trigger $t -Settings $s -Force | Out-Null
"task: registered" | Out-File $report -Append
if ($ok) {
  Start-ScheduledTask -TaskName "SnipRiot Recorder"
  "started: $(Get-Date -Format s)" | Out-File $report -Append
  Write-Host "`nDone. The recorder is running in the background." -ForegroundColor Green
  Write-Host "SnipRiot Control Centre: http://localhost:8765  (give it a minute to start)" -ForegroundColor Green
  Write-Host "Recordings and clips: $dataRoot" -ForegroundColor Green
  Start-Sleep 8; Start-Process "http://localhost:8765"
} else {
  Write-Host "`nSome tools are missing. Close this window, restart the laptop and run SETUP.bat again." -ForegroundColor Yellow
}
