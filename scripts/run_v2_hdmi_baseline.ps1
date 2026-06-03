param(
    [Parameter(Mandatory = $true)]
    [string]$PynqHost,
    [string]$PynqRepoPath = "/home/xilinx/jupyter_notebooks/OS-VideoSDR",
    [string]$PynqPython = "python",
    [string]$BitstreamPath = "/home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/overlays/tx/hdmi_aes_tx_wrapper.bit",
    [string]$PcTargetIp = "",
    [int]$TargetPort = 5000,
    [string]$AesKeyHex = "000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F",
    [string]$ConfigDir = "config",
    [string]$MatrixFile = "scripts/v2_hdmi_baseline_matrix.json",
    [string]$OutputRoot = "artifacts/metrics/v2_hdmi_baseline",
    [string]$DisplayMode = "headless",
    [switch]$IncludeSoak,
    [switch]$IncludeExploratory,
    [switch]$SkipPreflight,
    [int]$PreflightTimeoutSeconds = 45,
    [string]$SshKeyPath = "",
    [switch]$SshKeyOnly,
    [string]$PynqSudoPassword = "xilinx",
    [string[]]$CaseIds = @()
)

$ErrorActionPreference = "Stop"

if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Get-BytesPerPixel {
    param([string]$PixelFormat)

    $fmt = $PixelFormat.ToUpperInvariant()
    if ($fmt.Contains("RGB")) {
        return 3
    }
    if ($fmt.Contains("YUV")) {
        return 2
    }
    return 1
}

function Invoke-SshToFile {
    param(
        [string]$SshHost,
        [string[]]$Options,
        [string]$RemoteShell,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    $argList = @()
    $argList += $Options
    $argList += $SshHost
    $argList += $RemoteShell

    $proc = Start-Process -FilePath "ssh" -ArgumentList $argList -PassThru -NoNewWindow -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    $proc.WaitForExit()
    $proc.Refresh()
    if ($null -ne $proc.ExitCode) {
        return [int]$proc.ExitCode
    }
    return 255
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $PcTargetIp) {
    throw "PcTargetIp is required (use your PC LAN IP that PYNQ can reach)."
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runDir = Join-Path (Join-Path $repoRoot $OutputRoot) $timestamp
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$matrixPath = Join-Path $repoRoot $MatrixFile
if (-not (Test-Path $matrixPath)) {
    throw "Matrix file not found: $matrixPath"
}

$matrix = Get-Content -Raw -Path $matrixPath | ConvertFrom-Json
$cases = @($matrix.cases)
if ($CaseIds.Count -gt 0) {
    $idSet = @{}
    foreach ($entry in $CaseIds) {
        foreach ($cid in ($entry -split ',')) {
            $trimmed = $cid.Trim()
            if ($trimmed) {
                $idSet[$trimmed] = $true
            }
        }
    }
    $cases = @($cases | Where-Object { $idSet.ContainsKey($_.id) })
}

if ($cases.Count -eq 0) {
    throw "No cases selected. Check -CaseIds and matrix file."
}

Write-Host "V2 HDMI baseline run directory: $runDir"
Write-Host "Cases selected: $($cases.Count)"

$preferredAuth = if ($SshKeyOnly.IsPresent) { "publickey" } else { "publickey,password" }
$batchMode = if ($SshKeyOnly.IsPresent) { "yes" } else { "no" }
$sshOptions = @(
    "-o", "PreferredAuthentications=$preferredAuth",
    "-o", "BatchMode=$batchMode",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2"
)
if ($SshKeyPath) {
    if (-not (Test-Path $SshKeyPath)) {
        throw "SshKeyPath does not exist: $SshKeyPath"
    }
    $sshOptions += @("-i", $SshKeyPath)
}

if (-not $SkipPreflight.IsPresent) {
    $bitstreamDir = [System.IO.Path]::GetDirectoryName($BitstreamPath).Replace("\\", "/")
    $bitstreamBase = [System.IO.Path]::GetFileNameWithoutExtension($BitstreamPath)
    $hwhPath = "$bitstreamDir/$bitstreamBase.hwh"

    $artifactOut = Join-Path $runDir "artifact_check.out.log"
    $artifactErr = Join-Path $runDir "artifact_check.err.log"
    $artifactCmd = "test -f '$BitstreamPath' && test -f '$hwhPath'"
    $artifactShell = "bash -lc '$artifactCmd'"

    $artifactExit = Invoke-SshToFile -SshHost $PynqHost -Options $sshOptions -RemoteShell $artifactShell -StdoutPath $artifactOut -StderrPath $artifactErr
    if ($artifactExit -ne 0) {
        throw "Missing HDMI overlay artifacts on board. Required files: '$BitstreamPath' and '$hwhPath'. Build/copy them first, then rerun V2 script."
    }
}

if (-not $SkipPreflight.IsPresent) {
    $pre = $cases[0]
    $preCmd = @(
        "cd '$PynqRepoPath'",
        "export PYTHONPATH='$PynqRepoPath/pynq'",
        "timeout ${PreflightTimeoutSeconds}s $PynqPython -m runtime.preflight_hdmi_check --bitstream '$BitstreamPath' --width $($pre.width) --height $($pre.height) --fps $($pre.fps) --pixel-format '$($pre.pixelFormat)' --frames 2 --skip-output"
    ) -join " && "

    $preShell = "echo '$PynqSudoPassword' | sudo -S -p '' bash -lc '$preCmd'"
    $preOut = Join-Path $runDir "preflight.out.log"
    $preErr = Join-Path $runDir "preflight.err.log"

    Write-Host ""
    Write-Host "=== Running HDMI preflight ==="
    Write-Host "Preflight command: $preCmd"

    $preExit = Invoke-SshToFile -SshHost $PynqHost -Options $sshOptions -RemoteShell $preShell -StdoutPath $preOut -StderrPath $preErr
    if ($preExit -ne 0) {
        $preErrText = ""
        if (Test-Path $preErr) {
            $preErrText = Get-Content -Raw -Path $preErr
        }

        if ($preErrText -match "Bitstream file .* does not exist") {
            throw "HDMI preflight failed: bitstream not found on board at '$BitstreamPath'. Build/copy HDMI overlay (.bit + .hwh), or pass -BitstreamPath to the correct file. See $preErr"
        }
        if ($preExit -eq 124) {
            throw "HDMI preflight timed out after ${PreflightTimeoutSeconds}s (likely no active HDMI source/signal). Check $preOut and $preErr"
        }
        if ($preErrText) {
            $tail = ($preErrText -split "`r?`n" | Select-Object -Last 8) -join "`n"
            throw "HDMI preflight failed (exit=$preExit). Last stderr lines:`n$tail`nFull logs: $preOut and $preErr"
        }
        throw "HDMI preflight failed (exit=$preExit). Check $preOut and $preErr"
    }
}

$results = @()

foreach ($case in $cases) {
    if ($case.type -eq "soak" -and -not $IncludeSoak.IsPresent) {
        Write-Host "Skipping $($case.id) ($($case.description)) because -IncludeSoak was not set."
        continue
    }
    if ($case.type -eq "exploratory" -and -not $IncludeExploratory.IsPresent) {
        Write-Host "Skipping $($case.id) ($($case.description)) because -IncludeExploratory was not set."
        continue
    }

    $caseDir = Join-Path $runDir $case.id
    New-Item -ItemType Directory -Path $caseDir -Force | Out-Null
    $rxOut = Join-Path $caseDir "rx.out.log"
    $rxErr = Join-Path $caseDir "rx.err.log"
    $txOut = Join-Path $caseDir "tx.out.log"
    $txErr = Join-Path $caseDir "tx.err.log"

    Write-Host ""
    Write-Host "=== Running $($case.id): $($case.description) ==="

    $rxCmd = @(
        "Set-Location '$repoRoot'",
        "`$env:OSV_AES_KEY_HEX='$AesKeyHex'",
        "python -m pc.runtime.main_rx --config-dir '$ConfigDir' --max-frames $($case.maxFrames) --display-mode '$DisplayMode' --strict-nonce"
    ) -join "; "

    $rxProc = Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-Command", $rxCmd) -PassThru -RedirectStandardOutput $rxOut -RedirectStandardError $rxErr
    Start-Sleep -Seconds 2

    $remoteCmd = @(
        "cd '$PynqRepoPath'",
        "export OSV_AES_KEY_HEX='$AesKeyHex'",
        "export PYTHONPATH='$PynqRepoPath/pynq'",
        "$PynqPython -m runtime.main --config-dir '$PynqRepoPath/$ConfigDir' --source hdmi --crypto-mode dma --bitstream '$BitstreamPath' --target-ip '$PcTargetIp' --target-port $TargetPort --frames $($case.maxFrames) --fps $($case.fps) --segment-bytes $($case.segmentBytes) --hdmi-width $($case.width) --hdmi-height $($case.height) --hdmi-fps $($case.fps) --hdmi-pixel-format '$($case.pixelFormat)'"
    ) -join " && "

    Write-Host "PYNQ command: $remoteCmd"

    $remoteShellCmd = "echo '$PynqSudoPassword' | sudo -S -p '' bash -lc '$remoteCmd'"

    $txExit = Invoke-SshToFile -SshHost $PynqHost -Options $sshOptions -RemoteShell $remoteShellCmd -StdoutPath $txOut -StderrPath $txErr

    $rxTimedOut = $false
    if (-not $rxProc.WaitForExit(120000)) {
        $rxTimedOut = $true
        try { Stop-Process -Id $rxProc.Id -Force } catch {}
    }

    $rxExit = -1
    if (-not $rxTimedOut) {
        $rxProc.WaitForExit()
        $rxProc.Refresh()
        if ($null -ne $rxProc.ExitCode) {
            $rxExit = [int]$rxProc.ExitCode
        }
    }

    $completeLine = Select-String -Path $rxOut -Pattern "RX complete:\s+(\d+)\s+frames,\s+(\d+)\s+dropped" | Select-Object -Last 1
    $completedFrames = 0
    $droppedFrames = -1
    if ($completeLine) {
        $completedFrames = [int]$completeLine.Matches[0].Groups[1].Value
        $droppedFrames = [int]$completeLine.Matches[0].Groups[2].Value
    }

    $frameLine = Select-String -Path $rxOut -Pattern "RX frame\s+\d+/\d+\s+bytes=(\d+)" | Select-Object -First 1
    $observedFrameBytes = -1
    if ($frameLine) {
        $observedFrameBytes = [int]$frameLine.Matches[0].Groups[1].Value
    }

    $bytesPerPixel = Get-BytesPerPixel -PixelFormat ([string]$case.pixelFormat)
    $expectedFrameBytes = [int]$case.width * [int]$case.height * $bytesPerPixel

    $decryptFails = (Select-String -Path $rxOut -Pattern "RX decrypt failed" -SimpleMatch | Measure-Object).Count
    $keyMismatch = (Select-String -Path $rxOut -Pattern "RX key_id mismatch" -SimpleMatch | Measure-Object).Count
    $nonceReject = (Select-String -Path $rxOut -Pattern "RX nonce rejected" -SimpleMatch | Measure-Object).Count

    if ($null -eq $rxProc.ExitCode -and -not $rxTimedOut -and $completedFrames -eq [int]$case.maxFrames -and $droppedFrames -eq 0 -and $decryptFails -eq 0 -and $keyMismatch -eq 0 -and $nonceReject -eq 0) {
        $rxExit = 0
    }

    $durationSeconds = [int]$case.durationSeconds
    if ($durationSeconds -lt 1) {
        $durationSeconds = 1
    }
    $payloadMiBps = [math]::Round((($completedFrames * $expectedFrameBytes) / $durationSeconds) / 1MB, 3)

    $pass = (
        $txExit -eq 0 -and
        $rxExit -eq 0 -and
        -not $rxTimedOut -and
        $completedFrames -eq [int]$case.maxFrames -and
        $droppedFrames -eq 0 -and
        $decryptFails -eq 0 -and
        $keyMismatch -eq 0 -and
        $nonceReject -eq 0 -and
        $observedFrameBytes -eq $expectedFrameBytes
    )

    $result = [PSCustomObject]@{
        id = $case.id
        description = $case.description
        type = $case.type
        mandatory = [bool]$case.mandatory
        pixelFormat = [string]$case.pixelFormat
        width = [int]$case.width
        height = [int]$case.height
        fps = [int]$case.fps
        segmentBytes = [int]$case.segmentBytes
        maxFrames = [int]$case.maxFrames
        durationSeconds = [int]$case.durationSeconds
        expectedFrameBytes = $expectedFrameBytes
        observedFrameBytes = $observedFrameBytes
        payloadMiBps = $payloadMiBps
        txExit = $txExit
        rxExit = $rxExit
        rxTimedOut = $rxTimedOut
        completedFrames = $completedFrames
        droppedFrames = $droppedFrames
        decryptFails = $decryptFails
        keyMismatch = $keyMismatch
        nonceReject = $nonceReject
        pass = $pass
        caseDir = $caseDir
    }

    $results += $result
    Write-Host (
        "Result {0}: pass={1} completed={2}/{3} dropped={4} frameBytes={5}/{6} payloadMiBps={7} txExit={8} rxExit={9}" -f
        $case.id, $pass, $completedFrames, $case.maxFrames, $droppedFrames, $observedFrameBytes, $expectedFrameBytes, $payloadMiBps, $txExit, $rxExit
    )
}

$resultsJson = Join-Path $runDir "results.json"
$resultsCsv = Join-Path $runDir "results.csv"
$results | ConvertTo-Json -Depth 6 | Set-Content -Path $resultsJson
$results | Export-Csv -Path $resultsCsv -NoTypeInformation

$mandatoryFails = @($results | Where-Object { $_.mandatory -and -not $_.pass })
$allMandatoryPass = ($mandatoryFails.Count -eq 0)

$summary = [PSCustomObject]@{
    timestamp = $timestamp
    runDir = $runDir
    caseCount = $results.Count
    passCount = @($results | Where-Object { $_.pass }).Count
    failCount = @($results | Where-Object { -not $_.pass }).Count
    allMandatoryPass = $allMandatoryPass
    mandatoryFailIds = @($mandatoryFails | ForEach-Object { $_.id })
}

$summaryPath = Join-Path $runDir "summary.json"
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $summaryPath

Write-Host ""
Write-Host "V2 HDMI baseline summary:"
Write-Host "  Cases run: $($summary.caseCount)"
Write-Host "  Passed:    $($summary.passCount)"
Write-Host "  Failed:    $($summary.failCount)"
Write-Host "  Mandatory pass: $($summary.allMandatoryPass)"
Write-Host "  Output: $runDir"

if (-not $allMandatoryPass) {
    Write-Host "Mandatory failures: $($summary.mandatoryFailIds -join ', ')"
    exit 2
}
