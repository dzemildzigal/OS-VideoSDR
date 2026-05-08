param(
    [string]$Profile = "U10",
    [string]$Profiles = "config/profiles.yaml",
    [string]$Network = "config/network.yaml",
    [string]$Crypto = "config/crypto.yaml"
)

$ErrorActionPreference = "Stop"

Write-Host "OS-VideoSDR wired RX launcher"
Write-Host "Profile: $Profile"
Write-Host "Profiles config: $Profiles"
Write-Host "Network config: $Network"
Write-Host "Crypto config: $Crypto"

# TODO: replace with actual entrypoint once pipeline module is implemented.
$cmd = "python -m pynq.runtime.rx_main --profile $Profile --profiles $Profiles --network $Network --crypto $Crypto"
Write-Host "Planned command: $cmd"
