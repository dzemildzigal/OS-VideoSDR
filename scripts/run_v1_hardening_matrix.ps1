param(
    [Parameter(Mandatory = $true)]
    [string]$PynqHost,
    [string]$PynqRepoPath = "/home/xilinx/jupyter_notebooks/OS-VideoSDR",
    [string]$PynqPython = "python",
    [string]$BitstreamPath = "/home/xilinx/jupyter_notebooks/AES256/aes_gcm_dma_wrapper.bit",
    [string]$PcTargetIp = "",
    [int]$TargetPort = 5000,
    [string]$AesKeyHex = "000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F",
    [string]$ConfigDir = "config",
    [string]$MatrixFile = "scripts/v1_hardening_matrix.json",
    [string]$OutputRoot = "artifacts/metrics/v1_hardening",
    [switch]$IncludeSoak,
    [string[]]$CaseIds = @()
)

$ErrorActionPreference = "Stop"

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
    foreach ($cid in $CaseIds) { $idSet[$cid] = $true }
    $cases = @($cases | Where-Object { $idSet.ContainsKey($_.id) })
}

Write-Host "V1 hardening matrix run directory: $runDir"
Write-Host "Cases selected: $($cases.Count)"

$results = @()

foreach ($case in $cases) {
    if ($case.type -eq "soak" -and -not $IncludeSoak.IsPresent) {
        Write-Host "Skipping $($case.id) ($($case.description)) because -IncludeSoak was not set."
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
        "python -m pc.runtime.main_rx --config-dir '$ConfigDir' --max-frames $($case.maxFrames) --display-mode headless --strict-nonce"
    ) -join "; "

    $rxProc = Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-Command", $rxCmd) -PassThru -RedirectStandardOutput $rxOut -RedirectStandardError $rxErr

    Start-Sleep -Seconds 2

    $remoteCmd = @(
        "cd $PynqRepoPath",
        "export OSV_AES_KEY_HEX=$AesKeyHex",
        "$PynqPython -m pynq.runtime.main --config-dir $PynqRepoPath/$ConfigDir --source synthetic --crypto-mode dma --bitstream $BitstreamPath --target-ip $PcTargetIp --target-port $TargetPort --frames $($case.maxFrames) --fps $($case.fps) --frame-bytes $($case.frameBytes) --segment-bytes $($case.segmentBytes)"
    ) -join " && "

    & ssh $PynqHost $remoteCmd 1> $txOut 2> $txErr
    $txExit = $LASTEXITCODE

    $rxTimedOut = $false
    if (-not $rxProc.WaitForExit(120000)) {
        $rxTimedOut = $true
        try { Stop-Process -Id $rxProc.Id -Force } catch {}
    }
    $rxExit = if ($rxTimedOut) { -1 } else { $rxProc.ExitCode }

    $completeLine = Select-String -Path $rxOut -Pattern "RX complete:\s+(\d+)\s+frames,\s+(\d+)\s+dropped" | Select-Object -Last 1
    $completedFrames = 0
    $droppedFrames = -1
    if ($completeLine) {
        $completedFrames = [int]$completeLine.Matches[0].Groups[1].Value
        $droppedFrames = [int]$completeLine.Matches[0].Groups[2].Value
    }

    $decryptFails = (Select-String -Path $rxOut -Pattern "RX decrypt failed" -SimpleMatch | Measure-Object).Count
    $keyMismatch = (Select-String -Path $rxOut -Pattern "RX key_id mismatch" -SimpleMatch | Measure-Object).Count
    $nonceReject = (Select-String -Path $rxOut -Pattern "RX nonce rejected" -SimpleMatch | Measure-Object).Count

    $pass = ($txExit -eq 0 -and $rxExit -eq 0 -and -not $rxTimedOut -and $completedFrames -eq [int]$case.maxFrames -and $droppedFrames -eq 0 -and $decryptFails -eq 0 -and $keyMismatch -eq 0 -and $nonceReject -eq 0)

    $result = [PSCustomObject]@{
        id = $case.id
        description = $case.description
        type = $case.type
        mandatory = [bool]$case.mandatory
        fps = [int]$case.fps
        frameBytes = [int]$case.frameBytes
        segmentBytes = [int]$case.segmentBytes
        maxFrames = [int]$case.maxFrames
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
    Write-Host ("Result {0}: pass={1} completed={2}/{3} dropped={4} txExit={5} rxExit={6}" -f $case.id, $pass, $completedFrames, $case.maxFrames, $droppedFrames, $txExit, $rxExit)
}

$resultsJson = Join-Path $runDir "results.json"
$resultsCsv = Join-Path $runDir "results.csv"
$results | ConvertTo-Json -Depth 5 | Set-Content -Path $resultsJson
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
$summary | ConvertTo-Json -Depth 5 | Set-Content -Path $summaryPath

Write-Host ""
Write-Host "Matrix summary:"
Write-Host "  Cases run: $($summary.caseCount)"
Write-Host "  Passed:    $($summary.passCount)"
Write-Host "  Failed:    $($summary.failCount)"
Write-Host "  Mandatory pass: $($summary.allMandatoryPass)"
Write-Host "  Output: $runDir"

if (-not $allMandatoryPass) {
    Write-Host "Mandatory failures: $($summary.mandatoryFailIds -join ', ')"
    exit 2
}
