# 상시 수집을 Windows 작업 스케줄러에 등록한다 (집 PC용, 관리자 권한 불필요).
#   powershell -ExecutionPolicy Bypass -File scripts\install_snapshot_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_snapshot_task.ps1 -Remove
# 6시간마다 수집하고 이어서 워커(적재·파싱·변경 기록)를 돌린다. 절전 중이면 PC를 깨워서 돌리고,
# 꺼져 있어 놓친 실행은 켜진 뒤 바로 한다. 로그인한 동안에만 돈다.
# 절전에서 깨우려면 Windows 전원 옵션의 "절전 모드 해제 타이머 허용"이 켜져 있어야 한다.
param([int]$EveryHours = 6, [switch]$Remove)

$TaskName = "NoticeDelta Snapshot"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "삭제함: $TaskName"
    return
}

$Repo = Split-Path -Parent $PSScriptRoot
$Uv = (Get-Command uv -ErrorAction Stop).Source
$Log = Join-Path $Repo "data\snapshots\task.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

# 출력은 로그 파일에 덧붙인다.
# PowerShell 5.1의 *>>는 UTF-16으로 쓰므로 Out-File로 UTF-8 로그를 남긴다.
$Run = "& '$Uv' run python scripts/{0} *>&1 | Out-File -FilePath '$Log' -Append -Encoding utf8"
$Cmd = "Set-Location '$Repo'; " + ($Run -f "snapshot.py") + "; " + ($Run -f "worker.py")
# powershell -WindowStyle Hidden은 창이 잠깐 떴다 사라진다. conhost --headless로 창 없이 띄운다.
$Action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -Command `"$Cmd`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "NoticeDelta: K-Startup·기업마당 열린 공고 스냅샷과 적재 (data/snapshots)" -Force | Out-Null
Write-Output "등록함: $TaskName (${EveryHours}시간마다, 로그 ${Log})"
