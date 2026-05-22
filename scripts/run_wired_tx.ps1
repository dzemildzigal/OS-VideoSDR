param(
    [string]$ConfigDir = "config",
    [string]$Source = "synthetic",
    [string]$CryptoMode = "dma",
    [string]$Bitstream = "/home/xilinx/jupyter_notebooks/AES256/aes_gcm_dma_wrapper.bit",
    [string]$TargetIp = "192.168.1.100",
    [int]$TargetPort = 5000,
    [int]$Frames = 120,
    [int]$Fps = 10,
    [int]$FrameBytes = 120000,
    [int]$SegmentBytes = 1200
)

$ErrorActionPreference = "Stop"

Write-Host "OS-VideoSDR wired TX launcher"
Write-Host "Config dir: $ConfigDir"
Write-Host "Source: $Source"
Write-Host "Crypto mode: $CryptoMode"
Write-Host "Target: $TargetIp`:$TargetPort"

python -m pynq.runtime.main --config-dir $ConfigDir --source $Source --crypto-mode $CryptoMode --bitstream $Bitstream --target-ip $TargetIp --target-port $TargetPort --frames $Frames --fps $Fps --frame-bytes $FrameBytes --segment-bytes $SegmentBytes @Args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
