# 솔랭 타임라인 + 상세 집계를 Claude 앱과 상관없이 따로 돌림 (앱·터미널을 꺼도 계속 돔).
# 중간에 PC를 끄거나 멈춰도 이 파일을 다시 실행하면 받은 타임라인은 건너뛰고 이어서 받음.
#   실행:  powershell -ExecutionPolicy Bypass -File src\pipeline\run_soloq_detail.ps1        (타임라인 + 집계)
#          powershell -ExecutionPolicy Bypass -File src\pipeline\run_soloq_detail.ps1 late   (30분 이후 프레임 → data\raw\riot\timeline_late, 로그 "30분 이후 프레임: N/M")
#          powershell -ExecutionPolicy Bypass -File src\pipeline\run_soloq_detail.ps1 killdmg   (킬 피해 스킬별 → data\raw\riot\timeline_killdmg, 로그 "킬 피해 스킬별: N/M")
#   진행:  data\logs\g_soloq_detail.log 의 마지막 줄 ("타임라인: N/24707")
#   멈춤:  Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*g_soloq_detail*' | ForEach-Object { Stop-Process -Id $_.ProcessId }
param([string]$Steps = "timeline,build")
$ErrorActionPreference = "Stop"
$pipe = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $pipe "..\..")
$log  = Join-Path $root "data\logs\g_soloq_detail.log"

$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*g_soloq_detail*' }
if ($running) { Write-Output "이미 돌고 있음 (PID $($running.ProcessId -join ', '))"; exit 0 }

$env:PYTHONIOENCODING = "utf-8"
$cmd = "python -X utf8 g_soloq_detail.py --steps $Steps >> `"$log`" 2>&1"
$p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd -WorkingDirectory $pipe -WindowStyle Hidden -PassThru
Write-Output "시작함 (cmd PID $($p.Id)). 로그: $log"
