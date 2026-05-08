param(
    [int]$Minutes = 30
)

$ErrorActionPreference = "Stop"

Write-Host "OS-VideoSDR soak test launcher"
Write-Host "Duration (minutes): $Minutes"

# TODO: replace with the final soak runner command.
$cmd = "python -m pytest tests/soak -q"
Write-Host "Planned command: $cmd"
