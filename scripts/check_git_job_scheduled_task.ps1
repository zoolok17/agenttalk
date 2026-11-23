<#
.SYNOPSIS
  Runs the #907 host check (tests/test_git_job_capability.py) through a Windows scheduled task, twice, and judges the
  evidence of each run. Windows only. Written for Windows PowerShell 5.1 and PowerShell 7.

.DESCRIPTION
  Why a scheduled task: a program that a scheduled task starts runs in a different session and with a different logon
  mode than a program started from a console. The host check records which, and shows that Git can be started paused,
  put in a job and let go there too.

  Modes
    Verify  Judges ONE log, once, with no scheduler. Exit code 0: the run finished with exit=0 and shows its host facts.
            2: no completion evidence (no log, an empty log, a log of another run, or a log without its last line).
            3: it finished but is not acceptable (a non-zero pytest exit code, or no host facts line).
    Run     The whole procedure (needs a person: the second run asks for the account password at the prompt).
              Run 1, interactive only: registers the task with /IT, runs it, waits for this run's own completion
              evidence, verifies the log, copies it, deletes the task.
              Run 2, whether logged on or not: registers it again WITHOUT /IT and WITH /RP *, so schtasks itself asks
              for the password. The password is typed at that prompt and is never read, stored, logged or printed here.
            Add -DryRun to print every command that would run and register nothing.
            Exit code 0 only when every run was accepted; otherwise the code of the first run that was not (2 or 3).
    Task    What the scheduled task itself runs: writes the run id as the first line of the log, then the output of the
            host check, and pytest's own exit code ("exit=<n>") as the last line.

  Completion evidence for a run needs BOTH: the log begins with this run's id and ends with "exit=<n>", and the
  scheduler shows the task as neither Running nor Queued. "Not Running" alone is never taken as completion (a task can
  also be Queued). Seeing "Running" at all is not required: a short run can finish between two polls. The scheduler's
  state, logon type and last result are read with Get-ScheduledTask, which does not depend on the Windows language.

  A log is acceptable only when the exit code is 0 and it shows the host facts line
  (GitJobHostFacts: git-job-capability: {...}) with the keys windows, session_id and pytest_process_in_a_job.

  Every path is a parameter or relative to the repository. The command that is registered is short (it names this script
  and a small arguments file written next to the logs); schtasks accepts at most 261 characters, and this script checks
  that before it registers anything. Task names must not contain spaces.

.EXAMPLE
  powershell -NoProfile -File scripts\check_git_job_scheduled_task.ps1 -Mode Verify -Log some.log -RunId run-0123456789ab

.EXAMPLE
  powershell -NoProfile -File scripts\check_git_job_scheduled_task.ps1 -Mode Run -DryRun -Python python -OutDir out

.EXAMPLE
  powershell -NoProfile -File scripts\check_git_job_scheduled_task.ps1 -Mode Run -Python python -OutDir out
#>
[CmdletBinding()]
param(
    [ValidateSet('Run', 'Verify', 'Task')] [string]$Mode = 'Run',
    [ValidateSet('Both', 'Interactive', 'Background')] [string]$Runs = 'Both',
    [string]$Repo = '',
    [string]$Python = 'python',
    [string]$OutDir = '',
    [string]$BaseTemp = '',
    [string]$User = '',
    [string]$TaskName = 'agenttalk-git-job-check',
    [int]$TimeoutSeconds = 600,
    [string]$Log = '',
    [string]$RunId = '',
    [string]$ArgsFile = '',
    [int]$CommandLengthLimit = 261,  # what schtasks /TR accepts; only a test lowers or raises it
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }  # this script lives in <repo>\scripts

$FactsMarker = 'GitJobHostFacts: git-job-capability: '
$FactsKeys = @('"windows"', '"session_id"', '"pytest_process_in_a_job"')
$MaxCommandLength = $CommandLengthLimit  # the longest command schtasks /TR accepts

function Read-Evidence {
    param([string]$Path, [string]$Id)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @{ state = 'missing' } }
    $lines = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue |
            ForEach-Object { $_.TrimStart([char]0xFEFF) } | Where-Object { $_ -ne '' })
    if ($lines.Count -eq 0) { return @{ state = 'empty' } }
    if ($lines[0].Trim() -ne "run-id=$Id") { return @{ state = 'other-run' } }
    if ($lines[-1].Trim() -notmatch '^exit=(-?\d+)$') { return @{ state = 'partial' } }
    return @{ state = 'finished'; exit = [int]$Matches[1]; lines = $lines }
}

function Test-Log {
    # Judges one log once. Returns @{ code = 0|2|3; text = <why> }: 0 good, 2 no completion evidence, 3 rejected.
    param([string]$Path, [string]$Id)
    $evidence = Read-Evidence -Path $Path -Id $Id
    if ($evidence.state -ne 'finished') {
        return @{ code = 2; text = "NO COMPLETION EVIDENCE: the log is '$($evidence.state)' (a finished log of this run starts with run-id=$Id and ends with exit=<n>)" }
    }
    if ($evidence.exit -ne 0) { return @{ code = 3; text = "REJECT: pytest exit code was $($evidence.exit), not 0" } }
    $facts = @($evidence.lines | Where-Object { $_ -like "*$FactsMarker*" })
    if ($facts.Count -eq 0) {
        return @{ code = 3; text = 'REJECT: the log shows no host facts line (GitJobHostFacts: git-job-capability:): exit 0 without facts is not proof' }
    }
    foreach ($key in $FactsKeys) {
        if (-not ($facts[0] -like "*$key*")) { return @{ code = 3; text = "REJECT: the host facts do not name $key" } }
    }
    return @{ code = 0; text = "OK: run $Id finished with exit=0 and shows its host facts" }
}

function Get-TaskFacts {
    # What the scheduler says about the task: state (Ready, Running, Queued, ...), logon type and last result.
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return @{ exists = $false; status = 'unknown'; logon = 'unknown'; result = 'unknown' } }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $logon = switch ([string]$task.Principal.LogonType) {
        'Interactive' { 'interactive only' }
        'InteractiveToken' { 'interactive only' }
        'Password' { 'whether logged on or not (interactive and background)' }
        default { [string]$task.Principal.LogonType }
    }
    return @{ exists = $true; status = [string]$task.State; logon = $logon; result = $(if ($info) { [string]$info.LastTaskResult } else { 'unknown' }) }
}

function Invoke-Schtasks {
    # schtasks gets ONE explicit argument string, so quotes inside it reach it exactly as written in every PowerShell.
    # Output goes to the console (the password prompt of /RP * needs it). Returns the exit code.
    param([string]$Line)
    $process = Start-Process -FilePath 'schtasks.exe' -ArgumentList $Line -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

function Get-TaskCommand {
    # The registered command is kept short on purpose: everything the task needs is in a small arguments file.
    param([string]$ArgsPath)
    $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Mode Task -ArgsFile "{1}"' -f $PSCommandPath, $ArgsPath
    if ($cmd.Length -gt $MaxCommandLength) {
        throw "The task command is $($cmd.Length) characters; schtasks accepts at most $MaxCommandLength. Use a shorter -OutDir, or keep the repository at a shorter path."
    }
    return $cmd
}

function Invoke-OneRun {
    # One registered run. Prints what it does and returns @{ code = 0|2|3 }.
    param([string]$Label, [bool]$Interactive)
    $id = 'run-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $logPath = Join-Path $OutDir "$Label.log"
    $temp = if ($BaseTemp) { Join-Path $BaseTemp $Label } else { Join-Path $OutDir "$Label-tmp" }
    $argsPath = Join-Path $OutDir "$Label.args.json"
    $taskCommand = Get-TaskCommand -ArgsPath $argsPath
    $create = "/Create /TN $TaskName /SC ONCE /ST 23:59"
    if ($Interactive) { $create += ' /IT' }
    $create += " /RU `"$User`""
    if (-not $Interactive) { $create += ' /RP *' }  # schtasks asks for the password itself; it is never seen here
    $create += ' /RL LIMITED /TR "' + $taskCommand.Replace('"', '\"') + '"'
    $kind = if ($Interactive) { 'interactive only (/IT)' } else { 'whether logged on or not (no /IT, /RP *)' }
    Write-Host "[$Label] $kind, run id $id"

    if ($DryRun) {
        Write-Host "[$Label] would write $Label.args.json with the repository, interpreter, log, run id and temporary folder"
        Write-Host "[$Label] would run: schtasks $create"
        Write-Host "[$Label] would run: schtasks /Run /TN $TaskName"
        Write-Host "[$Label] would wait at most $TimeoutSeconds s for a log that starts with run-id=$id and ends with exit=<n>, and for the task to be neither Running nor Queued"
        Write-Host "[$Label] would judge the log, copy an accepted one, and run: schtasks /Delete /TN $TaskName /F"
        return @{ code = 0 }
    }

    if ((Get-TaskFacts).exists) {
        throw "A scheduled task named $TaskName already exists. Delete it or pass another -TaskName; this script never replaces a task it did not make."
    }
    if (Test-Path -LiteralPath $logPath) { Remove-Item -LiteralPath $logPath -Force }
    @{ Repo = $Repo; Python = $Python; Log = $logPath; RunId = $id; BaseTemp = $temp } | ConvertTo-Json |
        Set-Content -LiteralPath $argsPath -Encoding ascii
    $registered = $false
    $judged = @{ code = 2; text = 'NO COMPLETION EVIDENCE: the run did not start' }
    try {
        $exit = Invoke-Schtasks -Line $create
        if ($exit -ne 0) { throw "schtasks /Create failed with exit code $exit" }
        $registered = $true
        $exit = Invoke-Schtasks -Line "/Run /TN $TaskName"
        if ($exit -ne 0) { throw "schtasks /Run failed with exit code $exit" }
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ($true) {
            $evidence = Read-Evidence -Path $logPath -Id $id
            $task = Get-TaskFacts
            if ($evidence.state -eq 'finished' -and $task.exists -and $task.status -notin @('Running', 'Queued')) { break }
            if ((Get-Date) -gt $deadline) { break }
            Start-Sleep -Seconds 5
        }
        $task = Get-TaskFacts
        Write-Host "[$Label] registered mode: $($task.logon); last result: $($task.result)"
        $judged = Test-Log -Path $logPath -Id $id
        Write-Host "[$Label] $($judged.text)"
        if ($judged.code -eq 0) {
            Copy-Item -LiteralPath $logPath -Destination (Join-Path $OutDir "$Label-accepted.log") -Force
            Write-Host "[$Label] log copied to $Label-accepted.log"
        }
    }
    finally {
        if ($registered) {
            [void](Invoke-Schtasks -Line "/End /TN $TaskName")  # a no-op when it is not running
            [void](Invoke-Schtasks -Line "/Delete /TN $TaskName /F")
            Write-Host "[$Label] task deleted: $(-not (Get-TaskFacts).exists)"
        }
    }
    return @{ code = $judged.code }
}

switch ($Mode) {
    'Verify' {
        if (-not $Log -or -not $RunId) { Write-Output 'Verify needs -Log and -RunId'; exit 9 }
        $judged = Test-Log -Path $Log -Id $RunId
        Write-Output $judged.text
        exit $judged.code
    }
    'Task' {
        if ($ArgsFile) {
            $saved = Get-Content -Raw -LiteralPath $ArgsFile | ConvertFrom-Json
            $Repo = $saved.Repo; $Python = $saved.Python; $Log = $saved.Log; $RunId = $saved.RunId; $BaseTemp = $saved.BaseTemp
        }
        if (-not $Log -or -not $RunId -or -not $BaseTemp) { Write-Output 'Task needs -Log, -RunId and -BaseTemp (or -ArgsFile)'; exit 9 }
        Set-Location -LiteralPath $Repo
        "run-id=$RunId" | Out-File -LiteralPath $Log -Encoding ascii
        (& where.exe git 2>&1 | Out-String).Trim() | Out-File -LiteralPath $Log -Append -Encoding ascii  # which Git this user's PATH gives
        & $Python -m pytest tests/test_git_job_capability.py -m windows_capability -p no:cacheprovider --basetemp $BaseTemp 2>&1 |
            Out-File -LiteralPath $Log -Append -Encoding utf8
        $code = $LASTEXITCODE
        "exit=$code" | Out-File -LiteralPath $Log -Append -Encoding ascii
        exit $code
    }
    'Run' {
        if (-not $OutDir) { Write-Output 'Run needs -OutDir (where the logs are kept)'; exit 9 }
        if (-not $User) { $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
        # The task runs from another working folder: every path it is given must be absolute.
        foreach ($name in 'OutDir', 'BaseTemp') {
            $value = Get-Variable -Name $name -ValueOnly
            if ($value -and -not [IO.Path]::IsPathRooted($value)) {
                Set-Variable -Name $name -Value (Join-Path (Get-Location).ProviderPath $value)
            }
        }
        if (-not $DryRun -and -not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
        $plan = @()
        if ($Runs -in @('Both', 'Interactive')) { $plan += , @('interactive', $true) }
        if ($Runs -in @('Both', 'Background')) { $plan += , @('background', $false) }
        $worst = 0
        foreach ($item in $plan) {
            $outcome = Invoke-OneRun -Label $item[0] -Interactive $item[1]
            if ($worst -eq 0 -and $outcome.code -ne 0) { $worst = $outcome.code }
        }
        exit $worst
    }
}
