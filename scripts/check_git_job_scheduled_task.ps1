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
            A missing -BaseTemp folder is created before registering a task.
            If a task is not known to be gone after cleanup, the next run is not started.
            Exit code 0 only when every run was accepted and its task is KNOWN to be gone (the scheduler answered that
            there is no such task); otherwise the code of the first run that was not (2 or 3: a run that was not
            accepted keeps its own code even when its task is also still there), or 4 when a run was accepted but its
            task could not be deleted or the scheduler could not be read to confirm it (the accepted log is kept, and
            the output says how to look for and delete the task by hand, with the warning printed in every case).
    Decide  Judges ONE log together with a MADE-UP scheduler state (-TaskExists, -TaskState), with no scheduler: the whole
            completion rule that Run applies, so a test can show it. Exit codes as Verify; 2 also when the log is
            finished but the scheduler does not show the task as finished.
    WriteArgs  Writes the small arguments file a registered task reads (the same function Run uses), so a test can show
            that every character of a path survives it. Needs -ArgsFile, -Repo, -Python, -Log, -RunId and -BaseTemp.
    Facts   Asks the scheduler (read only, nothing is registered) about -TaskName and prints `exists=<bool>
            unreadable=<bool>`: absent is a confirmed answer (the scheduler says there is no such task), unreadable is a
            scheduler that could not be asked, which is never read as a task that is gone. Exit code 0.
    Task    What the scheduled task itself runs: writes the run id as the first line of the log, then the output of the
            host check, and pytest's own exit code ("exit=<n>") as the last line.

  Completion evidence for a run needs BOTH halves: the log begins with this run's id and ends with "exit=<n>", AND the
  scheduler shows the task as Ready (the state a one-time task is in after it ran). Running, Queued, Disabled, Unknown,
  any other state, and a task the scheduler cannot show, are all "not complete": "not Running" alone is never taken as
  completion. If the wait ends by its time limit without both halves, the run is NOT accepted (exit code 2), whatever the
  log holds. Seeing "Running" at all is not required: a short run can finish between two polls. The scheduler's state,
  logon type and last result are read with Get-ScheduledTask, which does not depend on the Windows language.

  A log is acceptable only when the exit code is 0 and it shows the host facts line
  (GitJobHostFacts: git-job-capability: {...}) with the keys windows, session_id and pytest_process_in_a_job.

  The arguments file is UTF-8 with a byte order mark and is read as UTF-8, in both editions, so a folder name with an accent
  or any other character comes back unchanged. -SchedulerStub (tests only) names a script that replaces the two functions
  that talk to the scheduler, so `-Mode Run` can be run without registering anything.

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
    [ValidateSet('Run', 'Verify', 'Decide', 'WriteArgs', 'Facts', 'Task')] [string]$Mode = 'Run',
    [ValidateSet('Both', 'Interactive', 'Background')] [string]$Runs = 'Both',
    [string]$Repo = '',
    [string]$Python = 'python',
    [string]$OutDir = '',
    [string]$BaseTemp = '',
    [string]$User = '',
    [string]$TaskName = 'agenttalk-git-job-check',
    [int]$TimeoutSeconds = 600,
    [int]$StopWaitSeconds = 30,  # how long the cleanup waits for the task to show Ready after /End
    [string]$Log = '',
    [string]$RunId = '',
    [string]$ArgsFile = '',
    [string]$TaskExists = 'true',  # Decide only: whether the (made-up) scheduler shows the task
    [string]$TaskState = '',       # Decide only: the (made-up) state of the task
    [int]$CommandLengthLimit = 261,  # what schtasks /TR accepts; only a test lowers or raises it
    [string]$SchedulerStub = '',   # tests only: a script that replaces Invoke-Schtasks and Get-TaskFacts
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }  # this script lives in <repo>\scripts

# What Get-ScheduledTask says, in its error id, when the scheduler has no task of that name (the same in Windows
# PowerShell 5.1 and PowerShell 7, whatever the language of the system).
$TaskNotFoundId = 'CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask'
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

# The one state in which a one-time task is complete after it ran. Anything else, an unknown or disabled task included,
# is not complete.
$FinishedStates = @('Ready')

function Test-SchedulerDone {
    param([bool]$Exists, [string]$State)
    return ($Exists -and ($State -in $FinishedStates))
}

function Resolve-Run {
    # The whole rule, both halves. @{ code = 0|2|3; text = <why> }. A finished log alone is never enough.
    param([string]$Path, [string]$Id, [bool]$Exists, [string]$State, [bool]$Unreadable = $false)
    $evidence = Read-Evidence -Path $Path -Id $Id
    if ($evidence.state -ne 'finished') { return Test-Log -Path $Path -Id $Id }  # the log half is not met: code 2 and why
    if (-not (Test-SchedulerDone -Exists $Exists -State $State)) {
        $shown = if ($Exists) { "the task is '$State'" } elseif ($Unreadable) { 'the scheduler could not be read' } else { 'the scheduler does not show the task' }
        return @{ code = 2; text = "NO COMPLETION EVIDENCE FROM THE SCHEDULER in time: the log is finished but $shown (a run is complete only when the log is finished AND the task is Ready)" }
    }
    return Test-Log -Path $Path -Id $Id
}

function Get-TaskFacts {
    # What the scheduler says about the task: state (Ready, Running, Queued, ...), logon type and last result.
    # Two different "no": the scheduler answers that there is no such task (a confirmed absence), or it could not be asked
    # (access denied, the service not running, the command or its module missing, any other error): then `unreadable` is
    # true and the absence is NOT established. A caller never reads an unreadable scheduler as a task that is gone.
    # The absence is recognised by the scheduler's own error id (`FullyQualifiedErrorId`, a resource key that is the same
    # in every language), never by the error CATEGORY: ObjectNotFound is also the category of a Get-ScheduledTask that
    # does not exist at all (CommandNotFoundException), which says nothing about the task.
    $none = @{ exists = $false; unreadable = $false; status = 'unknown'; logon = 'unknown'; result = 'unknown' }
    try { $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    catch {
        if ($_.FullyQualifiedErrorId -ne $TaskNotFoundId) { $none.unreadable = $true }
        return $none
    }
    if (-not $task) { $none.unreadable = $true; return $none }  # no error and no answer: not the scheduler saying "no such task"
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $logon = switch ([string]$task.Principal.LogonType) {
        'Interactive' { 'interactive only' }
        'InteractiveToken' { 'interactive only' }
        'Password' { 'whether logged on or not (interactive and background)' }
        default { [string]$task.Principal.LogonType }
    }
    return @{ exists = $true; unreadable = $false; status = [string]$task.State; logon = $logon; result = $(if ($info) { [string]$info.LastTaskResult } else { 'unknown' }) }
}

function Invoke-Schtasks {
    # schtasks gets ONE explicit argument string, so quotes inside it reach it exactly as written in every PowerShell.
    # Output goes to the console (the password prompt of /RP * needs it). Returns the exit code.
    param([string]$Line)
    $process = Start-Process -FilePath 'schtasks.exe' -ArgumentList $Line -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

function Write-ArgsFile {
    # UTF-8 with a byte order mark, written through .NET so both editions write the same bytes and keep every character.
    param([string]$Path, [hashtable]$Values)
    [IO.File]::WriteAllText($Path, ($Values | ConvertTo-Json), (New-Object Text.UTF8Encoding($true)))
}

function Read-ArgsFile {
    param([string]$Path)
    return ([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json)
}

function Resolve-Cleanup {
    # A run that was judged is not finished until its task is known to be gone. @{ code; text }. A task that is still
    # there after the delete, or that the scheduler could not be asked about (the absence is not established), is a
    # failed cleanup: the accepted evidence stays, but the exit code is not 0 (4 when the run itself was accepted,
    # otherwise the run's own code, which wins) and the text says how to delete the task by hand. So is a stop of the
    # task that is not confirmed (-StopNotConfirmed, #920): that task was kept, and the text says how to end and delete it.
    param($Judged, [int]$DeleteExit, [bool]$StillExists, [bool]$Unreadable = $false, [string]$Name, [string]$StopNotConfirmed = '')
    $stop = if ($StopNotConfirmed) { " | TASK STOP NOT CONFIRMED ($StopNotConfirmed), so the task was kept: look with schtasks /Query /TN $Name, end it with schtasks /End /TN $Name, then delete it with schtasks /Delete /TN $Name /F, and do not run this check again before it is gone (Ready is the scheduler's own state; programs the task started are not checked here)" } else { '' }
    if ($stop) { return @{ code = $(if ($Judged.code -eq 0) { 4 } else { $Judged.code }); text = "$($Judged.text)$stop" } }
    if (-not $StillExists -and -not $Unreadable) { return $Judged }
    $how = "schtasks /Delete /TN $Name /F"
    $text = if ($StillExists) {
        "TASK NOT DELETED (schtasks /Delete returned $DeleteExit and the task is still there): delete it yourself with $how, and do not run this check again before it is gone"
    } else {
        "TASK NOT CONFIRMED DELETED (schtasks /Delete returned $DeleteExit and the scheduler could not be read to confirm that the task is gone): look with schtasks /Query /TN $Name, delete it yourself with $how if it is there, and do not run this check again before it is gone"
    }
    $code = if ($Judged.code -eq 0) { 4 } else { $Judged.code }
    return @{ code = $code; text = "$($Judged.text) | $text$stop" }
}

function Get-StopProblem {
    # After /End: '' when the stop is confirmed (the task shows Ready, and /End returned 0 or it was Ready before), else why not.
    param($Before, [int]$EndExit)
    $limit = (Get-Date).AddSeconds($StopWaitSeconds)
    while (-not (($now = Get-TaskFacts).exists -and $now.status -eq 'Ready')) {
        if ((Get-Date) -gt $limit) { return "after /End the task is '$($now.status)', not Ready (scheduler unreadable: $([bool]$now.unreadable))" }
        Start-Sleep -Seconds 2
    }
    $wasReady = $Before.exists -and -not $Before.unreadable -and $Before.status -eq 'Ready'
    if ($EndExit -ne 0 -and -not $wasReady) { return "schtasks /End returned $EndExit while the task was '$($Before.status)'" }
    return ''
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
    # One run. Prints what it does and returns its code and whether its task is known to be gone.
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
        Write-Host "[$Label] would wait at most $TimeoutSeconds s for a log that starts with run-id=$id and ends with exit=<n>, and for the task to be Ready (not Running, Queued, Disabled or unknown)"
        Write-Host "[$Label] would judge the log, copy an accepted one, and run: schtasks /Delete /TN $TaskName /F"
        return @{ code = 0; gone = $true }  # no task was registered
    }

    $before = Get-TaskFacts
    if ([bool]$before.unreadable) {
        throw "The scheduler could not be read, so it is not known whether a task named $TaskName exists. Nothing was registered; fix the access to the scheduler (or pass another -TaskName) and run this again."
    }
    if ($before.exists) {
        throw "A scheduled task named $TaskName already exists. Delete it or pass another -TaskName; this script never replaces a task it did not make."
    }
    if (Test-Path -LiteralPath $logPath) { Remove-Item -LiteralPath $logPath -Force }
    Write-ArgsFile -Path $argsPath -Values @{ Repo = $Repo; Python = $Python; Log = $logPath; RunId = $id; BaseTemp = $temp }
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
            if ($evidence.state -eq 'finished' -and (Test-SchedulerDone -Exists $task.exists -State $task.status)) { break }
            if ((Get-Date) -gt $deadline) { break }  # the time limit: what was seen is judged below, and is not enough alone
            Start-Sleep -Seconds 5
        }
        $task = Get-TaskFacts
        Write-Host "[$Label] registered mode: $($task.logon); last result: $($task.result)"
        $judged = Resolve-Run -Path $logPath -Id $id -Exists $task.exists -State $task.status -Unreadable ([bool]$task.unreadable)
        Write-Host "[$Label] $($judged.text)"
        if ($judged.code -eq 0) {
            Copy-Item -LiteralPath $logPath -Destination (Join-Path $OutDir "$Label-accepted.log") -Force
            Write-Host "[$Label] log copied to $Label-accepted.log"
        }
    }
    finally {
        if ($registered) {
            $beforeEnd = Get-TaskFacts  # the state just before /End
            $stop = Get-StopProblem -Before $beforeEnd -EndExit (Invoke-Schtasks -Line "/End /TN $TaskName")
            # Kept when its stop is not confirmed: /Delete ends no program, and the name is the way to stop it (#920).
            $deleteExit = if ($stop) { 0 } else { Invoke-Schtasks -Line "/Delete /TN $TaskName /F" }
            $after = Get-TaskFacts
            $stillThere = [bool]$after.exists
            $unreadable = [bool]$after.unreadable  # the scheduler could not be asked: the task is NOT known to be gone
            $gone = -not $stillThere -and -not $unreadable
            if ($stop) { Write-Host "[$Label] task kept: its stop is not confirmed" } elseif ($gone -and $deleteExit -ne 0) {
                # The scheduler says the task is not there, but the delete command itself failed: it is not "deleted".
                Write-Host "[$Label] task absent (delete command failed, schtasks returned $deleteExit; the scheduler confirms that the task is not there)"
            } else {
                Write-Host "[$Label] task deleted: $gone"
            }
            $judged = Resolve-Cleanup -Judged $judged -DeleteExit $deleteExit -StillExists $stillThere -Unreadable $unreadable -Name $TaskName -StopNotConfirmed $stop
            if ($stop) { $gone = $false }  # no next run beside a task whose stop is not confirmed
            if (-not $gone) { Write-Host "[$Label] $($judged.text)" }
        }
    }
    return @{ code = $judged.code; gone = $gone }
}

if ($SchedulerStub) { . $SchedulerStub }  # tests only: replaces Invoke-Schtasks and Get-TaskFacts

switch ($Mode) {
    'Verify' {
        if (-not $Log -or -not $RunId) { Write-Output 'Verify needs -Log and -RunId'; exit 9 }
        $judged = Test-Log -Path $Log -Id $RunId
        Write-Output $judged.text
        exit $judged.code
    }
    'Decide' {
        if (-not $Log -or -not $RunId) { Write-Output 'Decide needs -Log and -RunId'; exit 9 }
        $judged = Resolve-Run -Path $Log -Id $RunId -Exists ($TaskExists -eq 'true') -State $TaskState
        Write-Output $judged.text
        exit $judged.code
    }
    'WriteArgs' {
        if (-not $ArgsFile -or -not $Repo -or -not $Python -or -not $Log -or -not $RunId -or -not $BaseTemp) {
            Write-Output 'WriteArgs needs -ArgsFile, -Repo, -Python, -Log, -RunId and -BaseTemp'; exit 9
        }
        Write-ArgsFile -Path $ArgsFile -Values @{ Repo = $Repo; Python = $Python; Log = $Log; RunId = $RunId; BaseTemp = $BaseTemp }
        exit 0
    }
    'Facts' {
        $facts = Get-TaskFacts
        Write-Output ("exists={0} unreadable={1}" -f ([bool]$facts.exists).ToString().ToLower(), ([bool]$facts.unreadable).ToString().ToLower())
        exit 0
    }
    'Task' {
        if ($ArgsFile) {
            $saved = Read-ArgsFile -Path $ArgsFile
            $Repo = $saved.Repo; $Python = $saved.Python; $Log = $saved.Log; $RunId = $saved.RunId; $BaseTemp = $saved.BaseTemp
        }
        if (-not $Log -or -not $RunId -or -not $BaseTemp) { Write-Output 'Task needs -Log, -RunId and -BaseTemp (or -ArgsFile)'; exit 9 }
        # Windows PowerShell 5.1 turns a stderr line of a native command (redirected with 2>&1) into a terminating error
        # while the preference is Stop, which would end this before it wrote "exit=<n>". Native output is only text here.
        $ErrorActionPreference = 'Continue'
        Set-Location -LiteralPath $Repo
        "run-id=$RunId" | Out-File -LiteralPath $Log -Encoding ascii
        (& where.exe git 2>&1 | ForEach-Object { "$_" } | Out-String).Trim() | Out-File -LiteralPath $Log -Append -Encoding ascii  # which Git this user's PATH gives
        & $Python -m pytest tests/test_git_job_capability.py -m windows_capability -p no:cacheprovider --basetemp $BaseTemp 2>&1 |
            ForEach-Object { "$_" } | Out-File -LiteralPath $Log -Append -Encoding utf8
        $code = $LASTEXITCODE
        "exit=$code" | Out-File -LiteralPath $Log -Append -Encoding ascii
        exit $code
    }
    'Run' {
        if (-not $OutDir) { Write-Output 'Run needs -OutDir (where the logs are kept)'; exit 9 }
        if (-not $User) { $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
        # The task runs from another working folder: every path it is given must be absolute.
        foreach ($name in 'Repo', 'OutDir', 'BaseTemp') {
            $value = Get-Variable -Name $name -ValueOnly
            if ($value -and -not [IO.Path]::IsPathRooted($value)) {
                Set-Variable -Name $name -Value (Join-Path (Get-Location).ProviderPath $value)
            }
        }
        if (-not $DryRun -and -not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
        if ($BaseTemp -and -not (Test-Path -LiteralPath $BaseTemp -PathType Container)) {
            if ($DryRun) {
                Write-Host "would create -BaseTemp folder: $BaseTemp"
            } else {
                try {
                    New-Item -ItemType Directory -Path $BaseTemp -Force -ErrorAction Stop | Out-Null
                    if (-not (Test-Path -LiteralPath $BaseTemp -PathType Container)) { throw 'not a folder' }
                } catch {
                    Write-Output "Could not create -BaseTemp folder: $BaseTemp. $($_.Exception.Message)"
                    exit 9
                }
            }
        }
        $plan = @()
        if ($Runs -in @('Both', 'Interactive')) { $plan += , @('interactive', $true) }
        if ($Runs -in @('Both', 'Background')) { $plan += , @('background', $false) }
        $worst = 0
        for ($i = 0; $i -lt $plan.Count; $i++) {
            $item = $plan[$i]
            $outcome = Invoke-OneRun -Label $item[0] -Interactive $item[1]
            if ($worst -eq 0 -and $outcome.code -ne 0) { $worst = $outcome.code }
            if (-not $outcome.gone -and $i -lt $plan.Count - 1) {
                Write-Host 'The next run was not started because the earlier task may still exist.'
                exit $outcome.code
            }
        }
        exit $worst
    }
}
