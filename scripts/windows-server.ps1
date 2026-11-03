# SPDX-License-Identifier: Apache-2.0
#Requires -Version 5.1
# Forward arguments without requiring a Python package installation.
# A packaged build carries its own Python runtime under python\.
$ErrorActionPreference = 'Stop'
$bundled = Join-Path (Split-Path -Parent $PSScriptRoot) 'python/python.exe'
if (Test-Path -LiteralPath $bundled -PathType Leaf) {
    & $bundled -I -X utf8 (Join-Path $PSScriptRoot 'windows-server.py') @args
} else {
    $python = Get-Command python -ErrorAction Stop
    & $python.Source -X utf8 (Join-Path $PSScriptRoot 'windows-server.py') @args
}
exit $LASTEXITCODE
