Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-W1ProjectPath {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    if ([System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "Expected a project-relative path, received: $RelativePath"
    }

    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    $rootPrefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
        [System.IO.Path]::DirectorySeparatorChar

    if (-not $candidate.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $candidate.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path escapes the project root: $RelativePath"
    }
    return $candidate
}

function Read-W1TomlPaths {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Missing project configuration: $ConfigPath"
    }

    $paths = @{}
    $section = ""
    foreach ($line in Get-Content -LiteralPath $ConfigPath) {
        $trimmed = $line.Trim()
        if ($trimmed -match '^\[(.+)\]$') {
            $section = $Matches[1]
            continue
        }
        if ($section -eq "paths" -and
            $trimmed -match '^([A-Za-z0-9_]+)\s*=\s*"([^"]+)"\s*$') {
            $paths[$Matches[1]] = $Matches[2]
        }
    }

    foreach ($required in @("raw_compressed", "raw_uncompressed", "reports")) {
        if (-not $paths.ContainsKey($required)) {
            throw "Missing [paths].$required in $ConfigPath"
        }
    }
    return $paths
}

function Get-W1Context {
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $projectConfig = Join-Path $projectRoot "config\project.toml"
    $paths = Read-W1TomlPaths -ConfigPath $projectConfig
    $datasetConfig = Join-Path $projectRoot "config\amazon_w1_files.json"

    if (-not (Test-Path -LiteralPath $datasetConfig -PathType Leaf)) {
        throw "Missing W1 dataset configuration: $datasetConfig"
    }

    $datasets = Get-Content -LiteralPath $datasetConfig -Raw | ConvertFrom-Json
    $datasetCount = @($datasets).Count
    if ($datasetCount -ne 4) {
        throw "Expected exactly four W1 dataset entries; found $datasetCount."
    }

    $rawCompressed = Resolve-W1ProjectPath -ProjectRoot $projectRoot -RelativePath $paths.raw_compressed
    $rawUncompressed = Resolve-W1ProjectPath -ProjectRoot $projectRoot -RelativePath $paths.raw_uncompressed
    $reportsRoot = Resolve-W1ProjectPath -ProjectRoot $projectRoot -RelativePath $paths.reports
    $reportsW1 = Join-Path $reportsRoot "w1"

    foreach ($directory in @($rawCompressed, $rawUncompressed, $reportsW1)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $sevenZip = Join-Path $env:ProgramFiles "7-Zip\7z.exe"
    $curlCommand = Get-Command curl.exe -ErrorAction SilentlyContinue

    return [pscustomobject]@{
        ProjectRoot = $projectRoot
        ProjectConfig = $projectConfig
        DatasetConfig = $datasetConfig
        RawCompressed = $rawCompressed
        RawUncompressed = $rawUncompressed
        ReportsRoot = $reportsRoot
        ReportsW1 = $reportsW1
        LogPath = (Join-Path $reportsW1 "w1_execution.log")
        DiskUsagePath = (Join-Path $reportsW1 "w1_disk_usage.json")
        StatusPath = (Join-Path $reportsW1 "w1_status.json")
        SevenZip = $sevenZip
        Curl = if ($null -ne $curlCommand) { $curlCommand.Source } else { $null }
        Datasets = $datasets
    }
}

function Get-W1FreeBytes {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $root = [System.IO.Path]::GetPathRoot($ProjectRoot)
    return [int64]([System.IO.DriveInfo]::new($root).AvailableFreeSpace)
}

function Write-W1Log {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $timestamp = (Get-Date).ToString("o")
    $safeMessage = $Message -replace '[\r\n]+', ' '
    Add-Content -LiteralPath $Context.LogPath -Encoding utf8 -Value "[$timestamp] [$Level] $safeMessage"
}

function Write-W1JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value,
        [int]$Depth = 5
    )
    $temporary = "$Path.tmp-$PID"
    $Value | ConvertTo-Json -Depth $Depth | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Export-W1CsvAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $temporary = "$Path.tmp-$PID"
    @($Value) | Export-Csv -LiteralPath $temporary -NoTypeInformation -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Add-W1DiskEvent {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Event,
        [hashtable]$Details = @{}
    )

    $document = [ordered]@{
        phase = "W1"
        updated_at = (Get-Date).ToString("o")
        events = @()
    }
    if (Test-Path -LiteralPath $Context.DiskUsagePath -PathType Leaf) {
        $existing = Get-Content -LiteralPath $Context.DiskUsagePath -Raw | ConvertFrom-Json
        if ($null -ne $existing -and $null -ne $existing.events) {
            $document.phase = [string]$existing.phase
            $document.updated_at = [string]$existing.updated_at
            $document.events = @($existing.events)
        }
    }

    $free = Get-W1FreeBytes -ProjectRoot $Context.ProjectRoot
    $entry = [ordered]@{
        time = (Get-Date).ToString("o")
        event = $Event
        free_bytes = $free
        free_gib = [math]::Round($free / 1GB, 3)
    }
    foreach ($key in $Details.Keys) {
        $entry[$key] = $Details[$key]
    }
    $document.updated_at = (Get-Date).ToString("o")
    $document.events += ,$entry
    Write-W1JsonAtomic -Path $Context.DiskUsagePath -Value $document
}

function Write-W1Status {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Reason,
        [hashtable]$Details = @{}
    )
    $document = [ordered]@{
        phase = "W1"
        status = $Status
        reason = $Reason
        updated_at = (Get-Date).ToString("o")
        project_root = $Context.ProjectRoot
    }
    foreach ($key in $Details.Keys) {
        $document[$key] = $Details[$key]
    }
    Write-W1JsonAtomic -Path $Context.StatusPath -Value $document
}

function Test-W1ReadOnly {
    param([Parameter(Mandatory = $true)][string]$Path)
    $attributes = [System.IO.File]::GetAttributes($Path)
    return [bool]($attributes -band [System.IO.FileAttributes]::ReadOnly)
}

function Set-W1ReadOnly {
    param([Parameter(Mandatory = $true)][string]$Path)
    $attributes = [System.IO.File]::GetAttributes($Path)
    [System.IO.File]::SetAttributes(
        $Path,
        ($attributes -bor [System.IO.FileAttributes]::ReadOnly)
    )
}

function Invoke-W1NativeProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$StandardOutputPath,
        [Parameter(Mandatory = $true)][string]$StandardErrorPath
    )

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -Wait `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StandardOutputPath `
        -RedirectStandardError $StandardErrorPath

    $stdout = if (Test-Path -LiteralPath $StandardOutputPath -PathType Leaf) {
        @(Get-Content -LiteralPath $StandardOutputPath)
    } else {
        @()
    }
    $stderr = if (Test-Path -LiteralPath $StandardErrorPath -PathType Leaf) {
        @(Get-Content -LiteralPath $StandardErrorPath)
    } else {
        @()
    }
    return [pscustomobject]@{
        ExitCode = [int]$process.ExitCode
        Stdout = $stdout
        Stderr = $stderr
        CombinedOutput = @($stdout) + @($stderr)
    }
}

function Test-W1PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedParent = [System.IO.Path]::GetFullPath($Parent)
    $prefix = $resolvedParent.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
        [System.IO.Path]::DirectorySeparatorChar
    return $resolvedPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}
