param(
    [string]$Profile = "U10",
    [string]$Profiles = "config/profiles.yaml",
    [string]$Network = "config/network.yaml",
    [string]$Crypto = "config/crypto.yaml",
    [string]$CryptoMode = "none"
)

$ErrorActionPreference = "Stop"

Write-Host "OS-VideoSDR wired TX launcher"
Write-Host "Profile: $Profile"
Write-Host "Profiles config: $Profiles"
Write-Host "Network config: $Network"
Write-Host "Crypto config: $Crypto"
Write-Host "Crypto mode: $CryptoMode"

python pynq/runtime/tx_main.py --profile $Profile --profiles $Profiles --network $Network --crypto $Crypto --crypto-mode $CryptoMode @Args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
