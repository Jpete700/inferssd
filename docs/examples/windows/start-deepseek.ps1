# SPDX-License-Identifier: Apache-2.0
#Requires -Version 5.1
# Example launcher for the packaged Windows CPU experimental build.
# Edit the model path and run this file from the package's examples\ directory.
$ErrorActionPreference = 'Stop'

$packageRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'scripts/windows-server.ps1') -PathType Leaf)) {
    # Running from a source checkout: examples\windows\ -> repository root.
    $packageRoot = Split-Path -Parent $packageRoot
}
$launcher = Join-Path $packageRoot 'scripts/windows-server.ps1'
$model = 'D:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf'

& $launcher start `
    --model $model `
    --memory-mib 4096 `
    --context 1024 `
    --port 8080 `
    --open-browser
exit $LASTEXITCODE
