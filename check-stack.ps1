$ErrorActionPreference = 'Continue'

$cdp = Test-NetConnection -ComputerName 127.0.0.1 -Port 9222 -InformationLevel Quiet
$lotl = Test-NetConnection -ComputerName 127.0.0.1 -Port 3000 -InformationLevel Quiet
$ui = Test-NetConnection -ComputerName 127.0.0.1 -Port 8501 -InformationLevel Quiet

Write-Host "CDP 9222: $cdp"
Write-Host "LotL 3000: $lotl"
Write-Host "UI 8501:  $ui"
