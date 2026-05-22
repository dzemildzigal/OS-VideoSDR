param(
    [string]$ConfigDir = "config",
    [int]$MaxFrames = 120,
    [string]$DisplayMode = "headless",
    [switch]$StrictNonce
)

$ErrorActionPreference = "Stop"

Write-Host "OS-VideoSDR wired RX launcher"
Write-Host "Config dir: $ConfigDir"
Write-Host "Max frames: $MaxFrames"
Write-Host "Display mode: $DisplayMode"

$strictArg = ""
if ($StrictNonce.IsPresent) {
    $strictArg = "--strict-nonce"
}

python -m pc.runtime.main_rx --config-dir $ConfigDir --max-frames $MaxFrames --display-mode $DisplayMode $strictArg @Args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
