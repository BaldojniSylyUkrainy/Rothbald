$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Version = "v1.9.1"
$ArchiveSha256 = "147267177eef7b22ec3d2476dd514d1b12e160e176230b740e3d1bd600118447"
$VulkanVersion = "1.4.350.0"
$VulkanInstallerSha256 = "855b27ba05d2d8119c5114c5d4ff870ca38f2c632b11e1bb9923b9b7e6ecfe7b"
$BuildRoot = Join-Path $Root "build\whisper-cpp"
$VulkanRoot = Join-Path $Root "build\vulkan-sdk-$VulkanVersion"
$VulkanInstaller = Join-Path $BuildRoot "vulkan-sdk-$VulkanVersion.exe"
$Archive = Join-Path $BuildRoot "whisper.cpp-$Version.tar.gz"
$Source = Join-Path $BuildRoot "source-$Version"
$SourceTree = Join-Path $Source "whisper.cpp-1.9.1"
$WhisperBuild = Join-Path $BuildRoot "native-$Version"
$ProbeBuild = Join-Path $BuildRoot "vulkan-probe"
$Output = Join-Path $Root "build\windows-tools"

New-Item -ItemType Directory -Force -Path $BuildRoot, $Output | Out-Null

if (!$env:VULKAN_SDK -or !(Test-Path -LiteralPath (Join-Path $env:VULKAN_SDK "Include\vulkan\vulkan.h"))) {
  if (!(Test-Path -LiteralPath $VulkanInstaller)) {
    Invoke-WebRequest `
      -Uri "https://sdk.lunarg.com/sdk/download/$VulkanVersion/windows/vulkan_sdk.exe" `
      -OutFile $VulkanInstaller
  }
  $ActualVulkanSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $VulkanInstaller).Hash.ToLowerInvariant()
  if ($ActualVulkanSha256 -ne $VulkanInstallerSha256) {
    throw "Vulkan SDK installer checksum mismatch"
  }
  if (!(Test-Path -LiteralPath (Join-Path $VulkanRoot "Include\vulkan\vulkan.h"))) {
    & $VulkanInstaller `
      --root $VulkanRoot `
      --accept-licenses `
      --default-answer `
      --confirm-command `
      install `
      copy_only=1
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the Vulkan SDK" }
  }
  $env:VULKAN_SDK = $VulkanRoot
  $VulkanBin = Join-Path $VulkanRoot "Bin"
  $env:PATH = "$VulkanBin;$env:PATH"
}

if (!(Test-Path -LiteralPath $Archive)) {
  Invoke-WebRequest `
    -Uri "https://codeload.github.com/ggml-org/whisper.cpp/tar.gz/refs/tags/$Version" `
    -OutFile $Archive
}
$ActualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
if ($ActualSha256 -ne $ArchiveSha256) {
  throw "whisper.cpp source archive checksum mismatch"
}

if (!(Test-Path -LiteralPath $SourceTree)) {
  New-Item -ItemType Directory -Force -Path $Source | Out-Null
  tar -xf $Archive -C $Source
  if ($LASTEXITCODE -ne 0) { throw "Could not extract whisper.cpp source archive" }
}

cmake `
  -S $SourceTree `
  -B $WhisperBuild `
  -A x64 `
  -DGGML_VULKAN=ON `
  -DGGML_NATIVE=OFF `
  -DBUILD_SHARED_LIBS=OFF `
  -DWHISPER_BUILD_TESTS=OFF `
  -DWHISPER_BUILD_SERVER=OFF `
  -DWHISPER_BUILD_EXAMPLES=ON
if ($LASTEXITCODE -ne 0) { throw "Could not configure whisper.cpp" }
cmake --build $WhisperBuild --config Release --target whisper-cli
if ($LASTEXITCODE -ne 0) { throw "Could not build whisper.cpp" }

cmake `
  -S (Join-Path $Root "tools\vulkan_probe") `
  -B $ProbeBuild `
  -A x64
if ($LASTEXITCODE -ne 0) { throw "Could not configure the Vulkan probe" }
cmake --build $ProbeBuild --config Release
if ($LASTEXITCODE -ne 0) { throw "Could not build the Vulkan probe" }

$WhisperCli = Get-ChildItem -Path $WhisperBuild -Recurse -Filter "whisper-cli.exe" |
  Select-Object -First 1
$VulkanProbe = Get-ChildItem -Path $ProbeBuild -Recurse -Filter "rothbald-vulkan-probe.exe" |
  Select-Object -First 1
if (!$WhisperCli -or !$VulkanProbe) {
  throw "Windows Vulkan tools were not created"
}
Copy-Item -Force -LiteralPath $WhisperCli.FullName -Destination $Output
Copy-Item -Force -LiteralPath $VulkanProbe.FullName -Destination $Output
Copy-Item `
  -Force `
  -LiteralPath (Join-Path $SourceTree "LICENSE") `
  -Destination (Join-Path $Output "whisper.cpp-LICENSE.txt")

Write-Host "Prepared whisper.cpp Vulkan backend in $Output"
