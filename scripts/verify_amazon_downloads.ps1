param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "w1_common.ps1")

$context = Get-W1Context
$sevenZipExecutable = $context.SevenZip
$manifestJson = Join-Path $context.ReportsW1 "w1_download_manifest.json"
$manifestCsv = Join-Path $context.ReportsW1 "w1_download_manifest.csv"
$archiveReport = Join-Path $context.ReportsW1 "w1_archive_test.json"
$sevenZipVersionOutput = Join-Path $context.ReportsW1 "w1_7zip_version.out"
$sevenZipVersionError = Join-Path $context.ReportsW1 "w1_7zip_version.err"
$sevenZipVersion = "unknown"

function Save-ArchiveReport {
    param([Parameter(Mandatory = $true)]$Entries)
    $document = [ordered]@{
        phase = "W1"
        seven_zip_path = $context.SevenZip
        seven_zip_version = $sevenZipVersion
        size_rule = "Only the Size: total emitted after a completed 7-Zip full test is treated as reliable; gzip header/listing Size = values are ignored."
        updated_at = (Get-Date).ToString("o")
        files = @($Entries)
    }
    Write-W1JsonAtomic -Path $archiveReport -Value $document
}

try {
    if (-not (Test-Path -LiteralPath $context.SevenZip -PathType Leaf)) {
        throw "7-Zip executable is unavailable: $($context.SevenZip)"
    }
    $versionRun = Invoke-W1NativeProcess `
        -FilePath $sevenZipExecutable `
        -Arguments @("i") `
        -StandardOutputPath $sevenZipVersionOutput `
        -StandardErrorPath $sevenZipVersionError
    $versionLine = @($versionRun.Stdout | Where-Object { $_ -match '^7-Zip ' } | Select-Object -First 1)
    if ($versionRun.ExitCode -eq 0 -and $versionLine.Count -eq 1) {
        $sevenZipVersion = [string]$versionLine[0]
    }

    $priorEntries = @{}
    if (Test-Path -LiteralPath $manifestJson -PathType Leaf) {
        $priorManifest = Get-Content -LiteralPath $manifestJson -Raw | ConvertFrom-Json
        foreach ($priorEntry in @($priorManifest.files)) {
            $priorEntries[[string]$priorEntry.id] = $priorEntry
        }
    }

    $downloadEntries = @()
    foreach ($dataset in $context.Datasets) {
        $archivePath = Join-Path $context.RawCompressed $dataset.compressed_relative
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "A required archive is missing." -Details @{
                failed_file = [string]$dataset.id
                path = $archivePath
            }
            throw "Required archive is missing: $archivePath"
        }
        $actualBytes = [int64](Get-Item -LiteralPath $archivePath).Length
        if ($actualBytes -ne [int64]$dataset.expected_compressed_bytes) {
            Write-W1Status -Context $context -Status "FAILED_DOWNLOAD" -Reason "A required archive has an incorrect byte count." -Details @{
                failed_file = [string]$dataset.id
                expected_bytes = [int64]$dataset.expected_compressed_bytes
                actual_bytes = $actualBytes
            }
            throw "Archive size mismatch: $archivePath"
        }
        $hash = $null
        if ($priorEntries.ContainsKey([string]$dataset.id)) {
            $priorEntry = $priorEntries[[string]$dataset.id]
            if ($priorEntry.actual_bytes -eq $actualBytes -and
                $priorEntry.expected_bytes -eq [int64]$dataset.expected_compressed_bytes -and
                $priorEntry.sha256 -match '^[0-9a-fA-F]{64}$') {
                $hash = ([string]$priorEntry.sha256).ToLowerInvariant()
                Write-W1Log -Context $context -Level "INFO" -Message "Reusing previously completed project SHA-256: $($dataset.id)"
            }
        }
        if ($null -eq $hash) {
            Write-W1Log -Context $context -Level "INFO" -Message "Project SHA-256 started: $($dataset.id)"
            $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
            Write-W1Log -Context $context -Level "INFO" -Message "Project SHA-256 completed: $($dataset.id)"
        }
        $downloadEntries += [pscustomobject][ordered]@{
            id = [string]$dataset.id
            record_type = [string]$dataset.record_type
            domain = [string]$dataset.domain
            url = [string]$dataset.url
            relative_path = [string]$dataset.compressed_relative
            expected_bytes = [int64]$dataset.expected_compressed_bytes
            actual_bytes = $actualBytes
            size_match = $true
            sha256 = $hash
            checksum_scope = "project_generated"
            status = "COMPLETE"
            resumed = $null
            curl_exit_code = $null
            completed_at = (Get-Date).ToString("o")
            free_bytes_after = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
            readonly = Test-W1ReadOnly -Path $archivePath
        }
        $partialDownloadDocument = [ordered]@{
            phase = "W1"
            checksum_scope = "Project-generated SHA-256; not a publisher checksum."
            updated_at = (Get-Date).ToString("o")
            files = $downloadEntries
        }
        Write-W1JsonAtomic -Path $manifestJson -Value $partialDownloadDocument
        Export-W1CsvAtomic -Path $manifestCsv -Value $downloadEntries
    }
    $downloadDocument = [ordered]@{
        phase = "W1"
        checksum_scope = "Project-generated SHA-256; not a publisher checksum."
        updated_at = (Get-Date).ToString("o")
        files = $downloadEntries
    }
    Write-W1JsonAtomic -Path $manifestJson -Value $downloadDocument
    Export-W1CsvAtomic -Path $manifestCsv -Value $downloadEntries

    Write-W1Log -Context $context -Level "INFO" -Message "Starting four serial 7-Zip full archive tests."
    Add-W1DiskEvent -Context $context -Event "archive_tests_start"
    $testEntries = @()
    foreach ($dataset in $context.Datasets) {
        $archivePath = Join-Path $context.RawCompressed $dataset.compressed_relative
        $started = (Get-Date).ToString("o")
        Write-W1Log -Context $context -Level "INFO" -Message "7-Zip full test started: $($dataset.id)"
        $testOutputPath = Join-Path $context.ReportsW1 "w1_7zip_test_$($dataset.id).out"
        $testErrorPath = Join-Path $context.ReportsW1 "w1_7zip_test_$($dataset.id).err"
        $testRun = Invoke-W1NativeProcess `
            -FilePath $sevenZipExecutable `
            -Arguments @("t", "-slt", $archivePath) `
            -StandardOutputPath $testOutputPath `
            -StandardErrorPath $testErrorPath
        $nativeOutput = @($testRun.CombinedOutput)
        $exitCode = $testRun.ExitCode
        Write-W1Log -Context $context -Level "INFO" -Message "7-Zip full test process returned: $($dataset.id); exit_code=$exitCode"
        $finished = (Get-Date).ToString("o")
        $successMarker = [bool]($nativeOutput -match '^Everything is Ok$')
        $processedMatch = @($nativeOutput | Where-Object { $_ -match '^Size:\s*(\d+)\s*$' } | Select-Object -Last 1)
        $processedBytes = $null
        $sizeReliable = $false
        if ($processedMatch.Count -eq 1 -and $processedMatch[0] -match '^Size:\s*(\d+)\s*$') {
            $processedBytes = [int64]$Matches[1]
            $sizeReliable = (
                $exitCode -eq 0 -and
                $successMarker -and
                $processedBytes -gt [int64]4294967295
            )
        }
        Write-W1Log -Context $context -Level "INFO" -Message "7-Zip full test summary parsed: $($dataset.id); success_marker=$successMarker; processed_size_bytes=$processedBytes; size_reliable=$sizeReliable"
        $tail = @($nativeOutput | Select-Object -Last 20)
        $entry = [pscustomobject][ordered]@{
            id = [string]$dataset.id
            archive_relative_path = [string]$dataset.compressed_relative
            compressed_bytes = [int64]$dataset.expected_compressed_bytes
            started_at = $started
            finished_at = $finished
            exit_code = $exitCode
            success_marker = $successMarker
            success = ($exitCode -eq 0 -and $successMarker)
            processed_size_bytes = $processedBytes
            processed_size_reliable = $sizeReliable
            processed_size_source = if ($sizeReliable) { "7-Zip full-test final Size: total" } else { "unknown" }
            processed_size_reliability_note = if (
                $null -ne $processedBytes -and
                $processedBytes -le [int64]4294967295
            ) {
                "Rejected because the reported total is within the 32-bit gzip ISIZE range and cannot rule out a 4 GiB wrap for this large JSONL archive."
            } elseif ($sizeReliable) {
                "Accepted from the successful 7-Zip full-test final Size: total."
            } else {
                "No reliable full-test processed-byte total was available."
            }
            output_summary = ($tail -join "`n")
        }
        Write-W1Log -Context $context -Level "INFO" -Message "Archive-test entry constructed: $($dataset.id)"
        $testEntries += $entry
        Write-W1Log -Context $context -Level "INFO" -Message "Saving archive-test report: $($dataset.id)"
        Save-ArchiveReport -Entries $testEntries
        Write-W1Log -Context $context -Level "INFO" -Message "Archive-test report saved: $($dataset.id)"
        if (-not $entry.success) {
            Write-W1Status -Context $context -Status "FAILED_ARCHIVE_TEST" -Reason "7-Zip full archive test failed." -Details @{
                failed_file = [string]$dataset.id
                exit_code = $exitCode
                success_marker = $successMarker
            }
            throw "7-Zip full test failed for $($dataset.id), exit code $exitCode."
        }
        Set-W1ReadOnly -Path $archivePath
        Write-W1Log -Context $context -Level "INFO" -Message "Compressed archive marked read-only: $($dataset.id)"
        Write-W1Log -Context $context -Level "INFO" -Message "7-Zip full test passed: $($dataset.id); processed_size_bytes=$processedBytes; size_reliable=$sizeReliable"
    }

    foreach ($entry in $downloadEntries) {
        $dataset = $context.Datasets | Where-Object { $_.id -eq $entry.id } | Select-Object -First 1
        $archivePath = Join-Path $context.RawCompressed $dataset.compressed_relative
        $entry.readonly = Test-W1ReadOnly -Path $archivePath
    }
    $downloadDocument.updated_at = (Get-Date).ToString("o")
    $downloadDocument.files = $downloadEntries
    Write-W1JsonAtomic -Path $manifestJson -Value $downloadDocument
    Export-W1CsvAtomic -Path $manifestCsv -Value $downloadEntries

    $reliableSizes = @($testEntries | Where-Object { $_.processed_size_reliable } | ForEach-Object { [int64]$_.processed_size_bytes })
    $allReliable = ($reliableSizes.Count -eq 4)
    $totalProcessed = if ($allReliable) { [int64](($reliableSizes | Measure-Object -Sum).Sum) } else { $null }
    Add-W1DiskEvent -Context $context -Event "archive_tests_complete" -Details @{
        files_passed = 4
        all_processed_sizes_reliable = $allReliable
        total_processed_size_bytes = $totalProcessed
    }
    Write-W1Status -Context $context -Status "ARCHIVES_VERIFIED" -Reason "Four archives passed exact-byte, SHA-256, and serial 7-Zip full-test checks." -Details @{
        files_passed = 4
        all_processed_sizes_reliable = $allReliable
        total_processed_size_bytes = $totalProcessed
        free_bytes = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
    }
    Write-W1Log -Context $context -Level "INFO" -Message "All four 7-Zip full archive tests passed."
    exit 0
} catch {
    Write-W1Log -Context $context -Level "ERROR" -Message $_.Exception.Message
    Write-Error $_
    exit 1
}
