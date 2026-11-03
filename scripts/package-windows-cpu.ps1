# SPDX-License-Identifier: Apache-2.0
#Requires -Version 5.1
# Build a self-contained experimental Windows x64 CPU package from an existing build.
[CmdletBinding()]
param(
    [string]$Build = '',
    [string]$Output = '',
    [string]$Version = '',
    [string]$PythonRuntime = '',
    [string]$RuntimeDir = '',
    [switch]$NoZip,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Build) { $Build = Join-Path $repo 'build-windows-cpu' }
if (-not $Output) { $Output = Join-Path $repo 'dist' }

function Join-Parts {
    param([string[]]$Parts)
    $path = $Parts[0]
    for ($index = 1; $index -lt $Parts.Count; $index++) {
        $path = Join-Path $path $Parts[$index]
    }
    return $path
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Copy-Tree {
    param([string]$Source, [string]$Destination, [string[]]$Exclude = @())
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        if ($Exclude -contains $item.Name) { continue }
        $target = Join-Path $Destination $item.Name
        if ($item.PSIsContainer) {
            Copy-Tree -Source $item.FullName -Destination $target -Exclude $Exclude
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $target
        }
    }
}

if (-not (Test-Path -LiteralPath $Build -PathType Container)) {
    throw "Build directory not found: $Build. Run scripts\build-windows-cpu.ps1 first."
}
$server = Join-Path $Build 'bin/inferssd-server.exe'
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    throw "Server binary not found: $server. Run scripts\build-windows-cpu.ps1 first."
}

if (-not $Version) {
    $short = (& git -C $repo rev-parse --short HEAD).Trim()
    $dirty = ''
    if ((& git -C $repo status --porcelain)) { $dirty = '-dirty' }
    $Version = "0.0.0-windows-experimental-$short$dirty"
}
$Version = $Version -replace '[^0-9A-Za-z._-]', '-'

if (-not $RuntimeDir) {
    $gcc = Get-Command gcc -ErrorAction SilentlyContinue
    if ($gcc) { $RuntimeDir = Split-Path -Parent $gcc.Source }
}
if (-not $RuntimeDir -or -not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
    $RuntimeDir = 'C:\msys64\ucrt64\bin'
}
if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
    throw "Runtime DLL directory not found: $RuntimeDir. Pass -RuntimeDir."
}

# Copy every non-system import that the UCRT64 runtime directory provides.
$runtimeDlls = @('libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')
$objdump = Get-Command objdump -ErrorAction SilentlyContinue
if ($objdump) {
    $imports = & $objdump.Source -p $server |
        Select-String -Pattern 'DLL Name:\s*(\S+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value }
    foreach ($name in $imports) {
        if ($runtimeDlls -notcontains $name -and (Test-Path -LiteralPath (Join-Path $RuntimeDir $name) -PathType Leaf)) {
            $runtimeDlls += $name
        }
    }
}
$missingDlls = @($runtimeDlls | Where-Object { -not (Test-Path -LiteralPath (Join-Path $RuntimeDir $_) -PathType Leaf) })
if ($missingDlls.Count -gt 0) {
    throw "Runtime DLLs not found in ${RuntimeDir}: $($missingDlls -join ', ')"
}

if (-not $PythonRuntime) {
    $python = Get-Command python -ErrorAction Stop
    $realPython = (& $python.Source -c "import sys; print(sys.executable)").Trim()
    if (-not $realPython) { throw 'Could not resolve the Python installation directory.' }
    $PythonRuntime = Split-Path -Parent $realPython
}
$pythonExe = Join-Path $PythonRuntime 'python.exe'
$pythonLib = Join-Path $PythonRuntime 'Lib'
$pythonDlls = Join-Path $PythonRuntime 'DLLs'
foreach ($required in @($pythonExe, $pythonLib, $pythonDlls)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Not a full CPython installation (missing $required): $PythonRuntime. Pass -PythonRuntime."
    }
}
# MSYS2/Cygwin Python depends on its POSIX emulation DLL and cannot be portable.
$pythonObjdump = Get-Command objdump -ErrorAction SilentlyContinue
if ($pythonObjdump) {
    $pythonImports = & $pythonObjdump.Source -p $pythonExe |
        Select-String -Pattern 'DLL Name:\s*(\S+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value }
    if ($pythonImports -match '^(msys-2\.0|cygwin1)\.dll$') {
        throw "MSYS2/Cygwin Python is not portable; pass -PythonRuntime with a native CPython installation."
    }
}
$pythonVersion = (& $pythonExe -B -I -c "import platform; print(platform.python_version())").Trim()
if (-not $pythonVersion) { throw "Could not read the Python version from $pythonExe." }

New-Item -ItemType Directory -Force -Path $Output | Out-Null
$packageName = "inferssd-windows-x64-cpu-$Version"
$stage = Join-Path $Output $packageName
$zip = Join-Path $Output "$packageName.zip"
if (Test-Path -LiteralPath $stage) {
    if (-not $Force) { throw "Output already exists: $stage. Pass -Force to replace it." }
    Remove-Item -LiteralPath $stage -Recurse -Force
}
if (Test-Path -LiteralPath $zip) {
    if (-not $Force) { throw "Output already exists: $zip. Pass -Force to replace it." }
    Remove-Item -LiteralPath $zip -Force
}

$bin = Join-Path $stage 'bin'
$scripts = Join-Path $stage 'scripts'
$framework = Join-Path $stage 'framework/src/inferssd'
$frameworkSource = Join-Path $framework 'source'
$frameworkPlatform = Join-Path $framework 'platform'
$templateDir = Join-Path $stage 'engine/models/templates'
$examples = Join-Path $stage 'examples'
$docs = Join-Path $stage 'docs'
$pythonDir = Join-Path $stage 'python'
foreach ($directory in @($bin, $scripts, $framework, $frameworkSource, $frameworkPlatform,
                         $templateDir, $examples, $docs, $pythonDir)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

Copy-Item -LiteralPath $server -Destination (Join-Path $bin 'inferssd-server.exe')
foreach ($name in $runtimeDlls) {
    Copy-Item -LiteralPath (Join-Path $RuntimeDir $name) -Destination (Join-Path $bin $name)
}
Copy-Item -LiteralPath (Join-Path $repo 'scripts/windows-server.py') -Destination $scripts
Copy-Item -LiteralPath (Join-Path $repo 'scripts/windows-server.ps1') -Destination $scripts

Copy-Item -LiteralPath (Join-Path $repo 'framework/src/inferssd/__init__.py') -Destination $framework
Copy-Tree -Source (Join-Path $repo 'framework/src/inferssd/source') `
    -Destination (Join-Path $framework 'source') -Exclude @('__pycache__')
Copy-Item -LiteralPath (Join-Path $repo 'framework/src/inferssd/platform/__init__.py') `
    -Destination (Join-Path $framework 'platform')
Copy-Item -LiteralPath (Join-Path $repo 'framework/src/inferssd/platform/captured_command.py') `
    -Destination (Join-Path $framework 'platform')
Copy-Item -LiteralPath (Join-Path $repo 'engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja') `
    -Destination $templateDir

$example = Join-Path $repo 'docs/examples/windows/start-deepseek.ps1'
if (Test-Path -LiteralPath $example -PathType Leaf) {
    Copy-Item -LiteralPath $example -Destination $examples
}
Copy-Item -LiteralPath (Join-Path $repo 'docs/windows-package-quickstart.md') `
    -Destination (Join-Path $stage 'README.md')
foreach ($name in @('windows-package.md', 'windows-server.md', 'windows-model.md', 'windows-cpu.md')) {
    Copy-Item -LiteralPath (Join-Path $repo "docs/$name") -Destination $docs
}
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination $stage
Copy-Item -LiteralPath (Join-Path $repo 'NOTICE') -Destination $stage
Copy-Item -LiteralPath (Join-Path $repo 'WINDOWS-EXPERIMENTAL-PLAN.md') -Destination $stage

# A pruned full CPython keeps the launcher independent of a system Python.
$rootFiles = @('python.exe', 'python3.dll', 'LICENSE.txt')
$rootFiles += @(Get-ChildItem -LiteralPath $PythonRuntime -File |
    Where-Object { $_.Name -match '^(python3\d+\.dll|vcruntime140(_1)?\.dll)$' } |
    Select-Object -ExpandProperty Name)
foreach ($name in ($rootFiles | Select-Object -Unique)) {
    $source = Join-Path $PythonRuntime $name
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $pythonDir $name)
    }
}
Copy-Tree -Source $pythonDlls -Destination (Join-Path $pythonDir 'DLLs')
Copy-Tree -Source $pythonLib -Destination (Join-Path $pythonDir 'Lib') `
    -Exclude @('site-packages', 'test', 'idlelib', 'tkinter', 'turtledemo', 'ensurepip', 'lib2to3', 'pydoc_data', '__pycache__')
if (-not (Test-Path -LiteralPath (Join-Path $pythonDir 'LICENSE.txt') -PathType Leaf)) {
    throw "Python license not found in $PythonRuntime."
}
$bundledPython = Join-Path $pythonDir 'python.exe'
$bundledVersion = (& $bundledPython -B -I -c "import platform; print(platform.python_version())").Trim()
if ($bundledVersion -ne $pythonVersion) {
    throw "Bundled Python reports $bundledVersion, expected $pythonVersion."
}
Get-ChildItem -LiteralPath $pythonDir -Recurse -Directory -Filter '__pycache__' -Force |
    Remove-Item -Recurse -Force

$gitRevision = (& git -C $repo rev-parse HEAD).Trim()
$gitStatus = @(& git -C $repo status --short)
$serverHash = (Get-FileHash -LiteralPath $server -Algorithm SHA256).Hash.ToLower()
$originalPath = $env:PATH
try {
    $env:PATH = $RuntimeDir + [System.IO.Path]::PathSeparator + $env:PATH
    # Native tools may write their version banner to stderr; capture both streams
    # without PowerShell 5.1 turning that banner into a terminating error.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $server
    $startInfo.Arguments = '--version'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $versionText = ($process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()).Trim()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Server --version failed with exit code $($process.ExitCode)." }
    $serverVersion = $versionText.Split("`n")[0].Trim()
} finally {
    $env:PATH = $originalPath
}

$manifest = [ordered]@{
    schema = 'inferssd-windows-package/1'
    version = $Version
    created_utc = [DateTime]::UtcNow.ToString('o')
    git_revision = $gitRevision
    git_status = $gitStatus
    python_version = $pythonVersion
    server_sha256 = $serverHash
    server_version = $serverVersion
    model_scope = 'DeepSeek-V4.1-Flash-1017 Q8_0 (deepseek41, MXFP4 routed experts)'
}
Write-Utf8NoBom -Path (Join-Path $stage 'inferssd-package.json') `
    -Text (($manifest | ConvertTo-Json -Depth 6) + "`n")

$files = @(Get-ChildItem -LiteralPath $stage -Recurse -File |
    Where-Object { $_.Name -ne 'checksums.txt' } |
    Sort-Object FullName)
$checksumLines = foreach ($file in $files) {
    $relative = $file.FullName.Substring($stage.Length + 1).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
    "$hash  $relative"
}
Write-Utf8NoBom -Path (Join-Path $stage 'checksums.txt') `
    -Text (($checksumLines -join "`n") + "`n")

$packageHash = $null
if (-not $NoZip) {
    Compress-Archive -LiteralPath $stage -DestinationPath $zip -CompressionLevel Optimal -Force
    $packageHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
    Write-Utf8NoBom -Path ($zip + '.sha256') -Text ($packageHash + '  ' + (Split-Path -Leaf $zip) + "`n")
}

$summary = [ordered]@{
    package = $packageName
    directory = $stage
    zip = $(if ($NoZip) { $null } else { $zip })
    zip_sha256 = $packageHash
    files = $files.Count
    bytes = ($files | Measure-Object -Property Length -Sum).Sum
    version = $Version
    python_version = $pythonVersion
    server_sha256 = $serverHash
}
$summaryPath = Join-Path $Output "package-$Version.json"
Write-Utf8NoBom -Path $summaryPath -Text (($summary | ConvertTo-Json -Depth 5) + "`n")
Write-Host "Package ready: $stage"
if (-not $NoZip) { Write-Host "Archive: $zip" }
Write-Host "Summary: $summaryPath"
