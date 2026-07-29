$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Output = Join-Path $Root "build\windows-tools"
$ChocolateyRoot = if ($env:ChocolateyInstall) { $env:ChocolateyInstall } else { "C:\ProgramData\chocolatey" }
$PackageRoot = Join-Path $ChocolateyRoot "lib\ffmpeg\tools"

New-Item -ItemType Directory -Force -Path $Output | Out-Null

foreach ($Name in @("ffmpeg.exe", "ffprobe.exe")) {
  $Candidate = Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Filter $Name |
    Where-Object { $_.Length -gt 1MB } |
    Sort-Object Length -Descending |
    Select-Object -First 1
  if (!$Candidate) {
    throw "Could not locate the real $Name binary inside the Chocolatey ffmpeg package"
  }
  $Destination = Join-Path $Output $Name
  Copy-Item -Force -LiteralPath $Candidate.FullName -Destination $Destination
  & $Destination -version | Select-Object -First 1
  if ($LASTEXITCODE -ne 0) {
    throw "$Name did not start after being copied"
  }
}

$License = Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
  Where-Object { $_.Name -match "^(LICENSE|COPYING)(\..*)?$" } |
  Select-Object -First 1
if ($License) {
  Copy-Item -Force -LiteralPath $License.FullName -Destination (Join-Path $Output "FFmpeg-LICENSE.txt")
} else {
  throw "The Chocolatey FFmpeg package did not include a license file"
}

Write-Host "Prepared real FFmpeg binaries in $Output"
