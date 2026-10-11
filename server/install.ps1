# me installer — irm __SERVER__/i.ps1 | iex
$ErrorActionPreference = "Stop"
$Server = "__SERVER__"
$MeHome = if ($env:ME_HOME) { $env:ME_HOME } else { Join-Path $HOME ".me" }
$Bin = Join-Path $MeHome "bin"

$Py = $null
foreach ($p in @("py", "python", "python3")) {
  $cmd = Get-Command $p -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
    $args_ = if ($p -eq "py") { @("-3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)") } else { @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)") }
    & $cmd.Source @args_ 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $cmd.Source; if ($p -eq "py") { $Py = "`"$($cmd.Source)`" -3" } else { $Py = "`"$($cmd.Source)`"" }; break }
  }
}
if (-not $Py) { Write-Host "me needs Python 3.8+ — install from https://python.org (tick 'Add to PATH')"; exit 1 }

Write-Host "◆ installing me" -ForegroundColor Magenta
New-Item -ItemType Directory -Force -Path $MeHome, $Bin | Out-Null
Invoke-WebRequest -UseBasicParsing "$Server/me.py" -OutFile (Join-Path $MeHome "me.py")
Set-Content -Encoding ASCII -Path (Join-Path $Bin "me.cmd") -Value "@echo off`r`n$Py `"$MeHome\me.py`" %*"

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($UserPath -split ";" | Where-Object { $_ -eq $Bin })) {
  [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Bin", "User")
}
$env:Path = "$env:Path;$Bin"
Write-Host "✓ installed — type 'me' to start (new terminals pick it up automatically)" -ForegroundColor Green
& (Join-Path $Bin "me.cmd")
