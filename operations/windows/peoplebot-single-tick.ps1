[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$ModuleRoot,
    [Parameter(Mandatory = $true)][string]$BindingsPath,
    [Parameter(Mandatory = $true)][string]$PolicyPath,
    [Parameter(Mandatory = $true)][string]$StatusPath,
    [ValidateSet("work-cycle-tick", "development-cycle-tick")]
    [string]$PeopleBotCommand = "work-cycle-tick",
    [string]$AuthorityPath,
    [string]$OperationsPath,
    [string]$UsageConfigurationPath,
    [switch]$OfflineFixture
)

$ErrorActionPreference = "Stop"

foreach ($entry in @{
    PythonExecutable = $PythonExecutable
    ModuleRoot = $ModuleRoot
    BindingsPath = $BindingsPath
    PolicyPath = $PolicyPath
    StatusPath = $StatusPath
}.GetEnumerator()) {
    if ($entry.Value -notmatch '^(?:[A-Za-z]:\\|\\\\)') {
        throw "$($entry.Key) must be an absolute path."
    }
}

foreach ($required in @($PythonExecutable, $ModuleRoot, $BindingsPath, $PolicyPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required single-tick path is unavailable: $required"
    }
}
if ($PeopleBotCommand -eq "development-cycle-tick") {
    foreach ($entry in @{ AuthorityPath = $AuthorityPath; OperationsPath = $OperationsPath }.GetEnumerator()) {
        if ([string]::IsNullOrWhiteSpace($entry.Value) -or $entry.Value -notmatch '^(?:[A-Za-z]:\\|\\\\)') {
            throw "$($entry.Key) must be an absolute path for development-cycle-tick."
        }
        if (-not (Test-Path -LiteralPath $entry.Value -PathType Leaf)) {
            throw "Required development-cycle path is unavailable: $($entry.Value)"
        }
    }
    if ($OfflineFixture) {
        throw "OfflineFixture is not supported for development-cycle-tick."
    }
}
if (-not [string]::IsNullOrWhiteSpace($UsageConfigurationPath)) {
    if ($UsageConfigurationPath -notmatch '^(?:[A-Za-z]:\\|\\\\)') {
        throw "UsageConfigurationPath must be an absolute path."
    }
    if (-not (Test-Path -LiteralPath $UsageConfigurationPath -PathType Leaf)) {
        throw "Required usage collection configuration is unavailable: $UsageConfigurationPath"
    }
}

try {
    $bindings = Get-Content -LiteralPath $BindingsPath -Raw | ConvertFrom-Json
    $configuredStatusPath = [System.IO.Path]::GetFullPath([string]$bindings.status_path)
    $requestedStatusPath = [System.IO.Path]::GetFullPath($StatusPath)
} catch {
    [Console]::Error.WriteLine("Single-tick bindings could not establish the configured status path.")
    exit 21
}
if (-not [string]::Equals(
    $configuredStatusPath,
    $requestedStatusPath,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    [Console]::Error.WriteLine("StatusPath does not match bindings.status_path; no tick was started.")
    exit 21
}

$env:PYTHONPATH = [System.IO.Path]::GetFullPath($ModuleRoot)
$executionId = "execution:scheduled-$([guid]::NewGuid().ToString('N'))"
if (-not [string]::IsNullOrWhiteSpace($UsageConfigurationPath)) {
    $usageEntry = & ([System.IO.Path]::GetFullPath($PythonExecutable)) @(
        "-m", "peoplebot", "usage-collect",
        "--config", [System.IO.Path]::GetFullPath($UsageConfigurationPath),
        "--phase", "entry",
        "--execution-id", $executionId
    )
    $usageEntryExit = $LASTEXITCODE
    if ($usageEntryExit -eq 11) {
        Write-Output $usageEntry
        exit 11
    }
    if ($usageEntryExit -ne 0) {
        [Console]::Error.WriteLine("PeopleBot usage entry collection failed; no tick was started.")
        exit $usageEntryExit
    }
}
$arguments = @(
    "-m", "peoplebot", $PeopleBotCommand,
    "--bindings", [System.IO.Path]::GetFullPath($BindingsPath),
    "--policy", [System.IO.Path]::GetFullPath($PolicyPath),
    "--execution-id", $executionId
)
if ($PeopleBotCommand -eq "development-cycle-tick") {
    $arguments += @(
        "--authority", [System.IO.Path]::GetFullPath($AuthorityPath),
        "--operations", [System.IO.Path]::GetFullPath($OperationsPath)
    )
}
if ($OfflineFixture) {
    $arguments += "--offline-fixture"
}

& ([System.IO.Path]::GetFullPath($PythonExecutable)) @arguments
$peoplebotExit = $LASTEXITCODE
if (-not [string]::IsNullOrWhiteSpace($UsageConfigurationPath)) {
    $usageExit = & ([System.IO.Path]::GetFullPath($PythonExecutable)) @(
        "-m", "peoplebot", "usage-collect",
        "--config", [System.IO.Path]::GetFullPath($UsageConfigurationPath),
        "--phase", "exit",
        "--execution-id", $executionId
    )
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("PeopleBot usage exit collection failed; inspect the persisted tick status and usage admission status.")
    }
}
try {
    if (-not (Test-Path -LiteralPath $requestedStatusPath -PathType Leaf)) {
        throw "missing status"
    }
    $statusText = Get-Content -LiteralPath $requestedStatusPath -Raw
    $status = $statusText | ConvertFrom-Json
    if (
        $status.format -ne "peoplebot.work-cycle-status.v0" -or
        $status.execution_id -ne $executionId
    ) {
        throw "stale or mismatched status"
    }
} catch {
    [Console]::Error.WriteLine("PeopleBot did not write status for this exact execution.")
    if ($peoplebotExit -ne 0) {
        exit $peoplebotExit
    }
    exit 20
}
Write-Output $statusText
exit $peoplebotExit
