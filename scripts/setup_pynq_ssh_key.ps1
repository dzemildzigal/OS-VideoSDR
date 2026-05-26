param(
    [Parameter(Mandatory = $true)]
    [string]$PynqHost,
    [string]$SshKeyPath = "$HOME/.ssh/id_ed25519"
)

$ErrorActionPreference = "Stop"

$SshKeyPath = [System.IO.Path]::GetFullPath($SshKeyPath)
$keyDir = Split-Path -Parent $SshKeyPath
if (-not (Test-Path $keyDir)) {
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
}

$pubKeyPath = "$SshKeyPath.pub"
if (-not (Test-Path $SshKeyPath) -or -not (Test-Path $pubKeyPath)) {
    Write-Host "Generating SSH key at $SshKeyPath"
    # In Windows PowerShell, empty-string native args can be dropped.
    # Run via cmd.exe so -N "" is preserved for ssh-keygen.
    $escapedPath = $SshKeyPath.Replace('"', '""')
    $sshKeygenCmd = 'ssh-keygen -t ed25519 -N "" -f "' + $escapedPath + '"'
    & cmd.exe /c $sshKeygenCmd
    if ($LASTEXITCODE -ne 0) {
        throw "ssh-keygen failed with exit code $LASTEXITCODE"
    }
}

$pubKey = (Get-Content -Path $pubKeyPath -Raw).Trim()
if (-not $pubKey) {
    throw "Public key is empty: $pubKeyPath"
}

$remoteInstallCmd = "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF '$pubKey' ~/.ssh/authorized_keys || echo '$pubKey' >> ~/.ssh/authorized_keys"

Write-Host "Installing public key on $PynqHost (you may be prompted for password once)..."
& ssh -o StrictHostKeyChecking=accept-new $PynqHost $remoteInstallCmd
if ($LASTEXITCODE -ne 0) {
    throw "Initial SSH install step failed with exit code $LASTEXITCODE"
}

Write-Host "Verifying passwordless key auth..."
& ssh -o BatchMode=yes -o PasswordAuthentication=no -o PreferredAuthentications=publickey -i $SshKeyPath $PynqHost "echo key-auth-ok"
if ($LASTEXITCODE -ne 0) {
    throw "Key auth verification failed with exit code $LASTEXITCODE"
}

Write-Host "SSH key auth is configured and working."
Write-Host "Use this in matrix runner:"
Write-Host "  -SshKeyPath '$SshKeyPath' -SshKeyOnly"
