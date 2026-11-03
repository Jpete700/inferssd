# SPDX-License-Identifier: Apache-2.0
#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateRange(1, 64)][int]$Jobs = 8
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows with MSYS2 UCRT64 tools on PATH.' }
$repo = Split-Path -Parent $PSScriptRoot
$build = Join-Path $repo 'build-windows-cpu'

function Invoke-Logged {
    param([string]$Command, [string[]]$Arguments, [string]$Log)
    & $Command @Arguments 2>&1 | Tee-Object -FilePath $Log
    if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE. See $Log" }
}

Push-Location $repo
try {
    foreach ($tool in 'git', 'gcc', 'g++', 'cmake', 'ninja', 'ctest') {
        Get-Command $tool -ErrorAction Stop | Out-Null
    }
    $machine = & gcc -dumpmachine
    if ($LASTEXITCODE -ne 0 -or $machine -ne 'x86_64-w64-mingw32') { throw 'Expected the MSYS2 UCRT64 x64 compiler.' }
    $defines = '#include <_mingw.h>' | & gcc -dM -E -x c -
    if ($LASTEXITCODE -ne 0 -or -not ($defines -match '^#define _UCRT\b')) { throw 'Expected UCRT headers, not the MSVCRT or MSYS toolchain.' }
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    # Record the base revision and all local source changes; never reset the checkout.
    $sourceFiles = @('CMakePresets.json', 'scripts/build-windows-cpu.ps1')
    $sourceFiles += @(& git diff --name-only HEAD)
    $manifest = [ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        base_revision = (& git rev-parse HEAD)
        source_status = @(& git status --short)
        source_hashes = @($sourceFiles | Sort-Object -Unique | ForEach-Object {
            if (Test-Path -LiteralPath $_ -PathType Leaf) {
                @{ path = $_; sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }
            }
        })
        os_version = [Environment]::OSVersion.VersionString
        architecture = $env:PROCESSOR_ARCHITECTURE
        tools = @('gcc', 'g++', 'cmake', 'ninja' | ForEach-Object {
            @{ name = $_; path = (Get-Command $_).Source; version = @(& $_ --version)[0] }
        })
        preset = 'windows-cpu'
        jobs = $Jobs
    }
    # Include the MinGW SDK versions when using a normal MSYS2 installation.
    $toolBin = Split-Path -Parent (Get-Command gcc).Source
    $packageDb = Join-Path (Split-Path -Parent (Split-Path -Parent $toolBin)) 'var/lib/pacman/local'
    if (Test-Path -LiteralPath $packageDb) {
        $manifest.sdk_packages = @(Get-ChildItem -LiteralPath $packageDb -Directory | Where-Object {
            $_.Name -match '^mingw-w64-ucrt-x86_64-(headers|crt|gcc|gcc-libs|winpthreads|cmake|ninja)-'
        } | Select-Object -ExpandProperty Name)
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $build 'build-manifest.json')
    & git diff --binary HEAD | Set-Content -Encoding UTF8 (Join-Path $build 'source.diff')

    Invoke-Logged cmake @('--preset', 'windows-cpu') (Join-Path $build 'configure.log')
    $targets = @(
        'inferssd-server', 'test-ssd-model-adapter', 'test-ssd-runtime-policy',
        'test-kimi-prefill-cost', 'test-moe-pack-copy-plan', 'test-ssd-source-index',
        'test-ssd-host-budget', 'test-ssd-platform-threads', 'test-ssd-platform-memory',
        'test-ssd-platform-file', 'test-ssd-read-ring', 'test-kimi-cpu-cache-policy',
        'test-reasoning-budget', 'test-dsv41-engram', 'test-graph-overhead',
        'test-test-resources', 'test-server-runtime-routing'
    )
    Invoke-Logged cmake (@('--build', $build, '--parallel', "$Jobs", '--target') + $targets) (Join-Path $build 'build.log')
    $tests = '^(ssd_model_adapter|ssd_runtime_policy|kimi_prefill_cost|moe_pack_copy_plan|ssd_source_index|ssd_host_budget|ssd_platform_threads|ssd_platform_memory|ssd_platform_file|ssd_read_ring|kimi_cpu_sink_cache_compute_policy|test-reasoning-budget|test-dsv41-engram|test-graph-overhead|test_resources|test-server-runtime-routing)$'
    $junit = Join-Path $build 'windows-cpu-tests.xml'
    Invoke-Logged ctest @('--test-dir', $build, '--output-on-failure', '--no-tests=error', '--timeout', '120', '-R', $tests, '--output-junit', $junit) (Join-Path $build 'tests.log')
    [xml]$results = Get-Content -Raw $junit
    if (@($results.SelectNodes('//testcase')).Count -ne 16 -or $results.SelectNodes('//skipped|//failure|//error').Count -gt 0) {
        throw 'Expected 16 passing tests with no skipped or missing cases.'
    }

    $server = Join-Path $build 'bin/inferssd-server.exe'
    Invoke-Logged $server @('--version') (Join-Path $build 'server-version.log')
    # Router mode starts the real HTTP server without downloading or loading a model.
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $models = Join-Path $build 'smoke-models'
    New-Item -ItemType Directory -Force -Path $models | Out-Null
    $process = Start-Process -FilePath $server -ArgumentList @('--host', '127.0.0.1', '--port', $port, '--models-dir', ('"' + $models + '"'), '--no-models-autoload') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $build 'server.stdout.log') -RedirectStandardError (Join-Path $build 'server.stderr.log')
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $http = New-Object System.Net.Http.HttpClient($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(2)
    try {
        $ready = $false
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($process.HasExited) { throw "Server exited with code $($process.ExitCode). See server.stderr.log." }
            try {
                $health = $http.GetStringAsync("http://127.0.0.1:$port/health").GetAwaiter().GetResult() | ConvertFrom-Json
                if ($health.status -eq 'ok') { $ready = $true; break }
            } catch { Start-Sleep -Milliseconds 200 }
        }
        if (-not $ready) { throw 'Server did not become healthy within 30 seconds.' }
        $modelsResponse = $http.GetStringAsync("http://127.0.0.1:$port/v1/models").GetAwaiter().GetResult() | ConvertFrom-Json
        if ($modelsResponse.object -ne 'list' -or @($modelsResponse.data).Count -ne 0) { throw 'Expected an empty OpenAI model list.' }
        $web = $http.GetAsync("http://127.0.0.1:$port/").GetAwaiter().GetResult()
        $html = $web.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ([int]$web.StatusCode -ne 200 -or $html -notmatch '(?i)<!doctype html|<html') { throw 'Embedded Web UI did not return HTML.' }
        @{ health = $health; models = $modelsResponse; web_status = [int]$web.StatusCode; server_sha256 = (Get-FileHash $server -Algorithm SHA256).Hash } |
            ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $build 'server-smoke.json')
    } finally {
        $http.Dispose()
        if (-not $process.HasExited) { Stop-Process -Id $process.Id; $process.WaitForExit() }
        $process.Dispose()
    }
    Write-Host "Windows CPU build, 16 tests and HTTP startup smoke passed. Outputs: $build"
} finally {
    Pop-Location
}
