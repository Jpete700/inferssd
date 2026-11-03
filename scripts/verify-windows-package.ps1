# SPDX-License-Identifier: Apache-2.0
#Requires -Version 5.1
# Verify a packaged InferSSD Windows build with no developer tools on PATH.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageZip,
    [string]$Output = '',
    [switch]$KeepWork
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$PackageZip = (Resolve-Path -LiteralPath $PackageZip).Path
if (-not $Output) { $Output = Join-Path $repo 'dist' }
New-Item -ItemType Directory -Force -Path $Output | Out-Null

$work = Join-Path $env:TEMP ('inferssd-package-verify-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$serverProcess = $null
$savedPath = $env:PATH
$savedPythonHome = $env:PYTHONHOME
$savedPythonPath = $env:PYTHONPATH
$result = [ordered]@{
    schema = 'inferssd-windows-package-verification/1'
    archive = $PackageZip
    started_utc = [DateTime]::UtcNow.ToString('o')
    checks = @()
}

function Add-Check {
    param([string]$Name, [scriptblock]$Body)
    $started = [DateTime]::UtcNow
    try {
        $detail = & $Body
        $script:result.checks += [ordered]@{
            check = $Name
            ok = $true
            seconds = [Math]::Round(([DateTime]::UtcNow - $started).TotalSeconds, 3)
            detail = $detail
        }
    } catch {
        $script:result.checks += [ordered]@{
            check = $Name
            ok = $false
            seconds = [Math]::Round(([DateTime]::UtcNow - $started).TotalSeconds, 3)
            error = $_.Exception.Message
        }
    }
}

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Quote-Argument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-Captured {
    param([string]$File, [string[]]$Arguments)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $File
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    return [pscustomobject]@{
        Output = ($stdout + $stderr).Trim()
        ExitCode = $process.ExitCode
    }
}

function Wait-Healthy {
    param([int]$Port, [int]$Seconds = 30)
    Add-Type -AssemblyName System.Net.Http | Out-Null
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(2)
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($script:serverProcess.HasExited) {
                throw "Server exited with code $($script:serverProcess.ExitCode)."
            }
            try {
                $health = $client.GetStringAsync("http://127.0.0.1:$Port/health").GetAwaiter().GetResult() | ConvertFrom-Json
                if ($health.status -eq 'ok') { return $client, $health }
            } catch {
                Start-Sleep -Milliseconds 200
            }
        }
        throw "Server did not become healthy within $Seconds seconds."
    } catch {
        $client.Dispose()
        throw
    }
}

try {
    Add-Check 'extract' {
        Expand-Archive -LiteralPath $PackageZip -DestinationPath $work -Force
        $roots = @(Get-ChildItem -LiteralPath $work -Directory)
        if ($roots.Count -ne 1) { throw "Expected one package directory, found $($roots.Count)." }
        $script:root = $roots[0].FullName
        return @{ directory = $script:root }
    }
    if (-not $script:root) { throw 'Cannot verify a package that did not extract.' }

    Add-Check 'manifest' {
        $path = Join-Path $script:root 'inferssd-package.json'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'inferssd-package.json is missing.' }
        $manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($manifest.schema -ne 'inferssd-windows-package/1') { throw "Unexpected schema: $($manifest.schema)" }
        foreach ($required in @('bin/inferssd-server.exe', 'scripts/windows-server.py', 'scripts/windows-server.ps1',
                                'python/python.exe', 'framework/src/inferssd/source/gguf_alias.py',
                                'engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja', 'checksums.txt',
                                'LICENSE', 'NOTICE')) {
            if (-not (Test-Path -LiteralPath (Join-Path $script:root $required))) {
                throw "Package is missing $required."
            }
        }
        return @{ version = $manifest.version; python = $manifest.python_version; git = $manifest.git_revision }
    }

    Add-Check 'checksums' {
        $path = Join-Path $script:root 'checksums.txt'
        $verified = 0
        foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
            if (-not $line.Trim()) { continue }
            $match = [regex]::Match($line, '^([0-9a-fA-F]{64})  (.+)$')
            if (-not $match.Success) { throw "Malformed checksum line: $line" }
            $expected = $match.Groups[1].Value.ToLower()
            $relative = $match.Groups[2].Value.Replace('/', '\')
            $file = Join-Path $script:root $relative
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Checksummed file is missing: $relative" }
            $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected) { throw "Checksum mismatch: $relative" }
            $verified++
        }
        return @{ files = $verified }
    }

    Add-Check 'layout' {
        $forbidden = @(Get-ChildItem -LiteralPath $script:root -Recurse -Force | Where-Object {
            $relative = $_.FullName.Substring($script:root.Length + 1).Replace('\', '/')
            $relative -match '(^|/)site-packages(/|$)' -or
            $relative -match '(^|/)__pycache__(/|$)' -or
            $relative -match '(^|/)build-windows-cpu(/|$)' -or
            $relative -match '\.log$'
        })
        if ($forbidden.Count -gt 0) {
            throw "Development artifacts in package: $($forbidden[0].FullName)"
        }
        $bytes = (Get-ChildItem -LiteralPath $script:root -Recurse -File | Measure-Object Length -Sum).Sum
        return @{ bytes = $bytes }
    }

    # Remove every developer tool from PATH and Python's environment overrides.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    Add-Check 'server-version' {
        $server = Join-Path $script:root 'bin/inferssd-server.exe'
        $probe = Invoke-Captured -File $server -Arguments @('--version')
        if ($probe.ExitCode -ne 0) { throw "Server --version failed: $($probe.Output)" }
        if ($probe.Output -notmatch 'InferSSD') { throw "Unexpected server version output: $($probe.Output)" }
        return @{ output = $probe.Output.Split("`n")[0].Trim() }
    }

    Add-Check 'http-smoke' {
        $server = Join-Path $script:root 'bin/inferssd-server.exe'
        $models = Join-Path $work 'empty-models'
        New-Item -ItemType Directory -Force -Path $models | Out-Null
        $port = Get-FreePort
        $stdout = Join-Path $work 'server.stdout.log'
        $stderr = Join-Path $work 'server.stderr.log'
        $script:serverProcess = Start-Process -FilePath $server `
            -ArgumentList @('--host', '127.0.0.1', '--port', "$port", '--models-dir', $models, '--no-models-autoload') `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $client = $null
        try {
            $client, $health = Wait-Healthy -Port $port
            $modelsBody = $client.GetStringAsync("http://127.0.0.1:$port/v1/models").GetAwaiter().GetResult() | ConvertFrom-Json
            if ($modelsBody.object -ne 'list' -or @($modelsBody.data).Count -ne 0) {
                throw 'Expected an empty OpenAI model list.'
            }
            $web = $client.GetAsync("http://127.0.0.1:$port/").GetAwaiter().GetResult()
            $html = $web.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if ([int]$web.StatusCode -ne 200 -or $html -notmatch '(?i)<!doctype html|<html') {
                throw 'Embedded Web UI did not return HTML.'
            }
            return @{ port = $port; health = $health.status; models = 0; web_status = [int]$web.StatusCode }
        } finally {
            if ($client) { $client.Dispose() }
            if ($script:serverProcess -and -not $script:serverProcess.HasExited) {
                Stop-Process -Id $script:serverProcess.Id
                $script:serverProcess.WaitForExit()
            }
            $script:serverProcess = $null
        }
    }

    Add-Check 'bundled-python' {
        $python = Join-Path $script:root 'python/python.exe'
        $probe = Invoke-Captured -File $python -Arguments @('-B', '-I', '-c', 'import platform, ssl, ctypes, csv; print(platform.python_version())')
        if ($probe.ExitCode -ne 0) { throw "Bundled Python failed: $($probe.Output)" }
        return @{ version = $probe.Output }
    }

    Add-Check 'launcher-doctor' {
        $python = Join-Path $script:root 'python/python.exe'
        $launcher = Join-Path $script:root 'scripts/windows-server.py'
        $state = Join-Path $work 'state'
        $port = Get-FreePort
        $probe = Invoke-Captured -File $python -Arguments @('-B', '-I', '-X', 'utf8', $launcher, 'doctor',
            '--port', "$port", '--state-dir', $state)
        if ($probe.ExitCode -ne 0) { throw "Launcher doctor failed: $($probe.Output)" }
        $report = $probe.Output | ConvertFrom-Json
        if ($report.status -ne 'ok') { throw "Launcher doctor reported $($report.status)." }
        return @{ checks = @($report.checks).Count; root = $report.root }
    }

    Add-Check 'powershell-wrapper' {
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $wrapper = Join-Path $script:root 'scripts/windows-server.ps1'
        $state = Join-Path $work 'state-wrapper'
        $port = Get-FreePort
        $probe = Invoke-Captured -File $powershell -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $wrapper, 'status', '--port', "$port", '--state-dir', $state)
        if ($probe.ExitCode -ne 2) {
            throw "Expected status exit 2 for a stopped service, got $($probe.ExitCode): $($probe.Output)"
        }
        $status = $probe.Output | ConvertFrom-Json
        if ($status.status -ne 'stopped') { throw "Unexpected status: $($probe.Output)" }
        return @{ exit = $probe.ExitCode; status = $status.status; powershell = '5.1' }
    }

    $failed = @($result.checks | Where-Object { -not $_.ok })
    $result.status = if ($failed.Count -eq 0) { 'ok' } else { 'failed' }
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -ErrorAction SilentlyContinue
        $serverProcess.WaitForExit()
    }
    $env:PATH = $savedPath
    if ($null -ne $savedPythonHome) { $env:PYTHONHOME = $savedPythonHome }
    if ($null -ne $savedPythonPath) { $env:PYTHONPATH = $savedPythonPath }
    $result.finished_utc = [DateTime]::UtcNow.ToString('o')
    $manifest = $null
    try { $manifest = Get-Content -LiteralPath (Join-Path $script:root 'inferssd-package.json') -Raw | ConvertFrom-Json } catch { }
    $name = if ($manifest) { $manifest.version } else { 'unknown' }
    $resultPath = Join-Path $Output "verification-$name.json"
    [System.IO.File]::WriteAllText($resultPath, (($result | ConvertTo-Json -Depth 8) + "`n"),
        (New-Object System.Text.UTF8Encoding($false)))
    if (-not $KeepWork) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Verification: $($result.status) ($resultPath)"
    if ($KeepWork) { Write-Host "Work directory: $work" }
}

if ($result.status -ne 'ok') { exit 1 }
