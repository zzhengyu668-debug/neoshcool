param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "w1_common.ps1")

$context = Get-W1Context
$curlExecutable = $context.Curl
$manifestJson = Join-Path $context.ReportsW1 "w1_download_manifest.json"
$manifestCsv = Join-Path $context.ReportsW1 "w1_download_manifest.csv"
$minimumStartBytes = [int64](190GB)

function Save-DownloadManifest {
    param([Parameter(Mandatory = $true)]$Entries)
    $document = [ordered]@{
        phase = "W1"
        checksum_scope = "Project-generated SHA-256; not a publisher checksum."
        updated_at = (Get-Date).ToString("o")
        files = @($Entries)
    }
    Write-W1JsonAtomic -Path $manifestJson -Value $document
    Export-W1CsvAtomic -Path $manifestCsv -Value $Entries
}

try {
    $startTime = (Get-Date).ToString("o")
    $startFree = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
    Write-W1Log -Context $context -Level "INFO" -Message "W1 download stage started; project_root=$($context.ProjectRoot); start_free_bytes=$startFree"
    Add-W1DiskEvent -Context $context -Event "w1_download_start" -Details @{
        minimum_start_bytes = $minimumStartBytes
        gate_passed = ($startFree -ge $minimumStartBytes)
    }

    if ($startFree -lt $minimumStartBytes) {
        Write-W1Status -Context $context -Status "PAUSED_SPACE_GATE" -Reason "Start free space is below 190 GiB." -Details @{
            free_bytes = $startFree
            required_bytes = $minimumStartBytes
        }
        throw "W1 download start blocked: free space is below 190 GiB."
    }
    if ($null -eq $context.Curl -or -not (Test-Path -LiteralPath $context.Curl -PathType Leaf)) {
        Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "curl.exe is unavailable."
        throw "curl.exe is unavailable."
    }

    $knownPartPaths = @{}
    foreach ($dataset in $context.Datasets) {
        $final = Join-Path $context.RawCompressed $dataset.compressed_relative
        $knownPartPaths["$final.part".ToLowerInvariant()] = $true
    }
    $unknownLeftovers = @(
        Get-ChildItem -LiteralPath (Join-Path $context.ProjectRoot "data\amazon_reviews_2023\raw") -Recurse -Force |
            Where-Object {
                ($_.Name -match '(?i)(\.part$|\.partial$|staging)') -and
                (-not $knownPartPaths.ContainsKey($_.FullName.ToLowerInvariant()))
            } |
            Select-Object FullName, Length, LastWriteTime, Attributes
    )
    if ($unknownLeftovers.Count -gt 0) {
        Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "Unknown partial or staging files require user review." -Details @{
            unknown_files = $unknownLeftovers
        }
        Write-W1Log -Context $context -Level "ERROR" -Message "Unknown partial or staging files found; no download started."
        throw "Unknown partial or staging files found. They were not modified."
    }

    $entries = @()
    foreach ($dataset in $context.Datasets) {
        $entries += [pscustomobject][ordered]@{
            id = [string]$dataset.id
            record_type = [string]$dataset.record_type
            domain = [string]$dataset.domain
            url = [string]$dataset.url
            relative_path = [string]$dataset.compressed_relative
            expected_bytes = [int64]$dataset.expected_compressed_bytes
            actual_bytes = $null
            size_match = $false
            sha256 = $null
            checksum_scope = "project_generated"
            status = "PENDING"
            resumed = $false
            curl_exit_code = $null
            completed_at = $null
            free_bytes_after = $null
        }
    }
    Save-DownloadManifest -Entries $entries

    for ($index = 0; $index -lt $context.Datasets.Count; $index++) {
        $dataset = $context.Datasets[$index]
        $entry = $entries[$index]
        $finalPath = Join-Path $context.RawCompressed $dataset.compressed_relative
        $partPath = "$finalPath.part"
        $parent = Split-Path -Parent $finalPath
        New-Item -ItemType Directory -Path $parent -Force | Out-Null

        if (Test-Path -LiteralPath $finalPath -PathType Leaf) {
            $existingBytes = [int64](Get-Item -LiteralPath $finalPath).Length
            if ($existingBytes -ne [int64]$dataset.expected_compressed_bytes) {
                $entry.status = "FINAL_SIZE_MISMATCH"
                $entry.actual_bytes = $existingBytes
                Save-DownloadManifest -Entries $entries
                throw "Existing final file has an unexpected size and was not overwritten: $finalPath"
            }
            if (Test-Path -LiteralPath $partPath) {
                throw "Both a valid final file and a .part file exist; manual review required: $partPath"
            }
            $entry.status = "VERIFYING_EXISTING"
            Write-W1Log -Context $context -Level "INFO" -Message "Skipping download for existing exact-size file: $($dataset.id)"
        } else {
            $partBytes = [int64]0
            if (Test-Path -LiteralPath $partPath -PathType Leaf) {
                $partBytes = [int64](Get-Item -LiteralPath $partPath).Length
                if ($partBytes -gt [int64]$dataset.expected_compressed_bytes) {
                    $entry.status = "PART_SIZE_INVALID"
                    $entry.actual_bytes = $partBytes
                    Save-DownloadManifest -Entries $entries
                    throw "Partial file is larger than expected and was not modified: $partPath"
                }
                $entry.resumed = ($partBytes -gt 0)
            }

            $entry.status = "DOWNLOADING"
            Save-DownloadManifest -Entries $entries
            Write-W1Log -Context $context -Level "INFO" -Message "Downloading $($dataset.id) serially; resume_bytes=$partBytes"
            $curlArguments = @(
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--retry", "5",
                "--retry-delay", "5",
                "--retry-all-errors",
                "--connect-timeout", "60",
                "--speed-limit", "32768",
                "--speed-time", "120",
                "-C", "-",
                "--output", $partPath,
                [string]$dataset.url
            )
            & $curlExecutable @curlArguments
            $curlExit = $LASTEXITCODE
            $entry.curl_exit_code = $curlExit
            if ($curlExit -ne 0) {
                $entry.status = "DOWNLOAD_FAILED_PART_RETAINED"
                $entry.actual_bytes = if (Test-Path -LiteralPath $partPath) {
                    [int64](Get-Item -LiteralPath $partPath).Length
                } else {
                    [int64]0
                }
                Save-DownloadManifest -Entries $entries
                Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "curl.exe failed; resumable .part retained." -Details @{
                    failed_file = [string]$dataset.id
                    curl_exit_code = $curlExit
                    part_path = $partPath
                    part_bytes = $entry.actual_bytes
                }
                throw "curl.exe failed for $($dataset.id) with exit code $curlExit."
            }

            $downloadedBytes = [int64](Get-Item -LiteralPath $partPath).Length
            $entry.actual_bytes = $downloadedBytes
            if ($downloadedBytes -ne [int64]$dataset.expected_compressed_bytes) {
                $entry.status = "SIZE_MISMATCH_PART_RETAINED"
                Save-DownloadManifest -Entries $entries
                Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "Downloaded byte count mismatch; .part retained." -Details @{
                    failed_file = [string]$dataset.id
                    expected_bytes = [int64]$dataset.expected_compressed_bytes
                    actual_bytes = $downloadedBytes
                    part_path = $partPath
                }
                throw "Downloaded byte count mismatch for $($dataset.id); .part retained."
            }

            Move-Item -LiteralPath $partPath -Destination $finalPath
            Write-W1Log -Context $context -Level "INFO" -Message "Atomically promoted exact-size .part to final file: $($dataset.id)"
        }

        $finalBytes = [int64](Get-Item -LiteralPath $finalPath).Length
        $entry.actual_bytes = $finalBytes
        $entry.size_match = ($finalBytes -eq [int64]$dataset.expected_compressed_bytes)
        $entry.status = "HASHING"
        Save-DownloadManifest -Entries $entries
        $entry.sha256 = (Get-FileHash -LiteralPath $finalPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $entry.status = "COMPLETE"
        $entry.completed_at = (Get-Date).ToString("o")
        $entry.free_bytes_after = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
        Save-DownloadManifest -Entries $entries
        Add-W1DiskEvent -Context $context -Event "download_complete" -Details @{
            file_id = [string]$dataset.id
            compressed_bytes = $finalBytes
        }
        Write-W1Log -Context $context -Level "INFO" -Message "Download verified and project SHA-256 recorded: $($dataset.id); bytes=$finalBytes; free_bytes=$($entry.free_bytes_after)"
    }

    Write-W1Status -Context $context -Status "DOWNLOADED" -Reason "Four compressed files passed exact-byte verification and have project-generated SHA-256 hashes." -Details @{
        download_started_at = $startTime
        download_finished_at = (Get-Date).ToString("o")
        start_free_bytes = $startFree
        final_free_bytes = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
        files_complete = 4
    }
    Write-W1Log -Context $context -Level "INFO" -Message "W1 download stage completed successfully."
    exit 0
} catch {
    Write-W1Log -Context $context -Level "ERROR" -Message $_.Exception.Message
    Write-Error $_
    exit 1
}
