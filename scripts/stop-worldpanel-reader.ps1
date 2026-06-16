param(
    [int]$Port = 8000
)

$Listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (!$Listeners) {
    Write-Host "Worldpanel Reader is not running on port $Port."
    exit 0
}

foreach ($Item in $Listeners) {
    Stop-Process -Id $Item.OwningProcess -Force -ErrorAction SilentlyContinue
}

Write-Host "Worldpanel Reader stopped on port $Port."
