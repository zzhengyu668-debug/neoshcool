param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "w1_common.ps1")

$context = Get-W1Context
$sevenZipExecutable = $context.SevenZip
$archiveReportPath = Join-Path $context.ReportsW1 "w1_archive_test.json"
$extractJson = Join-Path $context.ReportsW1 "w1_extract_manifest.json"
$extractCsv = Join-Path $context.ReportsW1 "w1_extract_manifest.csv"
$reserveBytes = [int64](60GB)
$runId = (Get-Date).ToString("yyyyMMddTHHmmssfff")

function Save-ExtractManifest {
    param(
        [Parameter(Mandatory = $true)]$Entries,
        [Parameter(Mandatory = $true)]$SpacePlan
    )
    $document = [ordered]@{
        phase = "W1"
        run_id = $runId
        reserve_floor_bytes = $reserveBytes
        space_plan = $SpacePlan
        updated_at = (Get-Date).ToString("o")
        files = @($Entries)
    }
    Write-W1JsonAtomic -Path $extractJson -Value $document
    Export-W1CsvAtomic -Path $extractCsv -Value $Entries
}

function Remove-CurrentStagingSafely {
    param(
        [Parameter(Mandatory = $true)][string]$StagingPath,
        [Parameter(Mandatory = $true)][string]$MarkerPath
    )
    if (-not (Test-W1PathWithin -Path $StagingPath -Parent $context.RawUncompressed)) {
        throw "Refusing staging cleanup outside raw/uncompressed: $StagingPath"
    }
    if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
        throw "Refusing staging cleanup without this run's marker: $MarkerPath"
    }
    $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
    if ($marker.run_id -ne $runId) {
        throw "Refusing staging cleanup for a marker owned by another run."
    }
    Remove-Item -LiteralPath $StagingPath -Recurse -Force
    Write-W1Log -Context $context -Level "INFO" -Message "Cleaned staging directory created by current W1 run: $StagingPath"
}

try {
    if (-not (Test-Path -LiteralPath $archiveReportPath -PathType Leaf)) {
        throw "Missing archive-test report; run verify_amazon_downloads.ps1 first."
    }
    $archiveReport = Get-Content -LiteralPath $archiveReportPath -Raw | ConvertFrom-Json
    $archiveTests = @($archiveReport.files)
    if ($archiveTests.Count -ne 4 -or @($archiveTests | Where-Object { -not $_.success }).Count -gt 0) {
        Write-W1Status -Context $context -Status "FAILED_ARCHIVE_TEST" -Reason "Not all four archive tests are recorded as successful."
        throw "Not all four archive tests are successful."
    }

    $entries = @()
    foreach ($dataset in $context.Datasets) {
        $entries += [pscustomobject][ordered]@{
            id = [string]$dataset.id
            record_type = [string]$dataset.record_type
            domain = [string]$dataset.domain
            archive_relative_path = [string]$dataset.compressed_relative
            jsonl_relative_path = [string]$dataset.uncompressed_relative
            compressed_bytes = [int64]$dataset.expected_compressed_bytes
            estimated_uncompressed_bytes = $null
            estimate_method = $null
            gate_free_bytes = $null
            gate_required_bytes = $null
            gate_passed = $false
            extraction_exit_code = $null
            actual_uncompressed_bytes = $null
            compression_ratio = $null
            free_bytes_before = $null
            free_bytes_after = $null
            status = "PENDING"
            staging_cleanup = "not_needed"
            readonly = $false
            validated_at = $null
        }
    }

    $currentFree = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
    $remainingReliableSizes = @()
    $allRemainingReliable = $true
    foreach ($dataset in $context.Datasets) {
        $finalPath = Join-Path $context.RawUncompressed $dataset.uncompressed_relative
        if (Test-Path -LiteralPath $finalPath -PathType Leaf) {
            continue
        }
        $test = $archiveTests | Where-Object { $_.id -eq $dataset.id } | Select-Object -First 1
        if ($null -eq $test -or -not $test.processed_size_reliable) {
            $allRemainingReliable = $false
        } else {
            $remainingReliableSizes += [int64]$test.processed_size_bytes
        }
    }
    $remainingReliableTotal = if ($allRemainingReliable) {
        [int64](($remainingReliableSizes | Measure-Object -Sum).Sum)
    } else {
        $null
    }
    $projectedFinalFree = if ($allRemainingReliable) {
        [int64]($currentFree - $remainingReliableTotal)
    } else {
        $null
    }
    $spacePlan = [ordered]@{
        evaluated_at = (Get-Date).ToString("o")
        current_free_bytes = $currentFree
        all_remaining_sizes_reliable = $allRemainingReliable
        remaining_estimated_total_bytes = $remainingReliableTotal
        projected_final_free_bytes = $projectedFinalFree
        reserve_floor_bytes = $reserveBytes
        all_file_extraction_allowed = if ($allRemainingReliable) {
            ($projectedFinalFree -ge $reserveBytes)
        } else {
            $null
        }
    }
    Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
    Add-W1DiskEvent -Context $context -Event "extraction_space_plan" -Details @{
        all_remaining_sizes_reliable = $allRemainingReliable
        remaining_estimated_total_bytes = $remainingReliableTotal
        projected_final_free_bytes = $projectedFinalFree
        reserve_floor_bytes = $reserveBytes
    }

    if ($allRemainingReliable -and $projectedFinalFree -lt $reserveBytes) {
        Write-W1Status -Context $context -Status "PAUSED_SPACE_GATE" -Reason "Reliable total uncompressed sizes predict less than the 60 GiB reserve; no extraction started." -Details @{
            current_free_bytes = $currentFree
            remaining_uncompressed_bytes = $remainingReliableTotal
            projected_final_free_bytes = $projectedFinalFree
            reserve_floor_bytes = $reserveBytes
        }
        Write-W1Log -Context $context -Level "WARN" -Message "Extraction paused before the first file: reliable total would breach the 60 GiB reserve."
        exit 20
    }

    $completedRatios = @{}
    for ($index = 0; $index -lt $context.Datasets.Count; $index++) {
        $dataset = $context.Datasets[$index]
        $entry = $entries[$index]
        $archivePath = Join-Path $context.RawCompressed $dataset.compressed_relative
        $finalPath = Join-Path $context.RawUncompressed $dataset.uncompressed_relative
        $destinationDirectory = Split-Path -Parent $finalPath
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        $test = $archiveTests | Where-Object { $_.id -eq $dataset.id } | Select-Object -First 1

        if (Test-Path -LiteralPath $finalPath -PathType Leaf) {
            $existingBytes = [int64](Get-Item -LiteralPath $finalPath).Length
            if ($existingBytes -le 0) {
                throw "Existing final JSONL is empty and was not overwritten: $finalPath"
            }
            if ($test.processed_size_reliable -and $existingBytes -ne [int64]$test.processed_size_bytes) {
                throw "Existing final JSONL size does not match the reliable 7-Zip total: $finalPath"
            }
            $ratio = [double]$existingBytes / [double]$dataset.expected_compressed_bytes
            $completedRatios[[string]$dataset.id] = $ratio
            $entry.estimated_uncompressed_bytes = $existingBytes
            $entry.estimate_method = "existing_exact_file"
            $entry.gate_free_bytes = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
            $entry.gate_required_bytes = $reserveBytes
            $entry.gate_passed = $true
            $entry.actual_uncompressed_bytes = $existingBytes
            $entry.compression_ratio = [math]::Round($ratio, 6)
            $entry.free_bytes_before = $entry.gate_free_bytes
            $entry.free_bytes_after = $entry.gate_free_bytes
            $entry.status = "COMPLETE_SKIPPED_EXISTING"
            $entry.readonly = Test-W1ReadOnly -Path $finalPath
            Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
            Write-W1Log -Context $context -Level "INFO" -Message "Skipping existing complete JSONL: $($dataset.id)"
            continue
        }

        $estimate = $null
        $estimateMethod = $null
        if ($test.processed_size_reliable) {
            $estimate = [int64]$test.processed_size_bytes
            $estimateMethod = "7zip_full_test_total"
        } elseif ($dataset.id -eq "reviews_home_and_kitchen" -and
                  $completedRatios.ContainsKey("reviews_electronics")) {
            $electronicsRatio = [double]$completedRatios["reviews_electronics"]
            $maxObservedRatio = [double](($completedRatios.Values | Measure-Object -Maximum).Maximum)
            $ratioEstimate = [math]::Max($electronicsRatio * 1.15, $maxObservedRatio)
            $ratioBasedBytes = [int64][math]::Ceiling(
                [double]$dataset.expected_compressed_bytes * $ratioEstimate
            )
            $conservativeBytes = [int64]([double]$dataset.expected_compressed_bytes * 8.0)
            if ($ratioBasedBytes -lt $conservativeBytes) {
                $estimate = $ratioBasedBytes
                $estimateMethod = "electronics_review_ratio_x1.15_or_max_observed"
            } else {
                $estimate = $conservativeBytes
                $estimateMethod = "compressed_size_x8"
            }
        } else {
            $estimate = [int64]([double]$dataset.expected_compressed_bytes * 8.0)
            $estimateMethod = "compressed_size_x8"
        }

        $freeBefore = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
        $required = [int64]($estimate + $reserveBytes)
        $gatePassed = ($freeBefore -ge $required)
        $entry.estimated_uncompressed_bytes = $estimate
        $entry.estimate_method = $estimateMethod
        $entry.gate_free_bytes = $freeBefore
        $entry.gate_required_bytes = $required
        $entry.gate_passed = $gatePassed
        $entry.free_bytes_before = $freeBefore
        Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
        Add-W1DiskEvent -Context $context -Event "extraction_gate" -Details @{
            file_id = [string]$dataset.id
            estimated_uncompressed_bytes = $estimate
            estimate_method = $estimateMethod
            required_bytes_including_reserve = $required
            gate_passed = $gatePassed
        }

        if (-not $gatePassed) {
            $entry.status = "PAUSED_SPACE_GATE"
            Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
            Write-W1Status -Context $context -Status "PAUSED_SPACE_GATE" -Reason "Per-file extraction gate would breach the 60 GiB reserve." -Details @{
                paused_file = [string]$dataset.id
                current_free_bytes = $freeBefore
                estimated_uncompressed_bytes = $estimate
                estimate_method = $estimateMethod
                required_bytes_including_reserve = $required
                completed_files = @($entries | Where-Object { $_.status -like "COMPLETE*" } | ForEach-Object { $_.id })
            }
            Write-W1Log -Context $context -Level "WARN" -Message "Extraction paused by per-file space gate: $($dataset.id)"
            exit 20
        }

        $stagingPath = Join-Path $destinationDirectory ".w1_staging_${runId}_$($dataset.id)"
        if (Test-Path -LiteralPath $stagingPath) {
            throw "Current run staging path unexpectedly already exists and was not overwritten: $stagingPath"
        }
        New-Item -ItemType Directory -Path $stagingPath | Out-Null
        $markerPath = Join-Path $stagingPath ".w1-created.json"
        [ordered]@{
            phase = "W1"
            run_id = $runId
            file_id = [string]$dataset.id
            created_at = (Get-Date).ToString("o")
        } | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding utf8

        $entry.status = "EXTRACTING"
        Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
        Write-W1Log -Context $context -Level "INFO" -Message "7-Zip extraction started serially: $($dataset.id); estimate_method=$estimateMethod; estimate_bytes=$estimate"
        $extractOutputPath = Join-Path $context.ReportsW1 "w1_7zip_extract_$($dataset.id).out"
        $extractErrorPath = Join-Path $context.ReportsW1 "w1_7zip_extract_$($dataset.id).err"
        $extractRun = Invoke-W1NativeProcess `
            -FilePath $sevenZipExecutable `
            -Arguments @("x", "-y", "-o$stagingPath", $archivePath) `
            -StandardOutputPath $extractOutputPath `
            -StandardErrorPath $extractErrorPath
        $nativeOutput = @($extractRun.CombinedOutput)
        $exitCode = $extractRun.ExitCode
        $entry.extraction_exit_code = $exitCode
        $stagedJsonl = Join-Path $stagingPath (Split-Path -Leaf $finalPath)
        $successMarker = [bool]($nativeOutput -match '^Everything is Ok$')
        if ($exitCode -ne 0 -or -not $successMarker -or
            -not (Test-Path -LiteralPath $stagedJsonl -PathType Leaf) -or
            (Get-Item -LiteralPath $stagedJsonl).Length -le 0) {
            $entry.status = "EXTRACTION_FAILED"
            if (Test-Path -LiteralPath $stagingPath) {
                Remove-CurrentStagingSafely -StagingPath $stagingPath -MarkerPath $markerPath
                $entry.staging_cleanup = "current_run_staging_removed"
            }
            Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
            Write-W1Status -Context $context -Status "FAILED_ARCHIVE_TEST" -Reason "7-Zip extraction failed after archive verification." -Details @{
                failed_file = [string]$dataset.id
                exit_code = $exitCode
                success_marker = $successMarker
            }
            throw "7-Zip extraction failed for $($dataset.id), exit code $exitCode."
        }

        $actualBytes = [int64](Get-Item -LiteralPath $stagedJsonl).Length
        if ($test.processed_size_reliable -and $actualBytes -ne [int64]$test.processed_size_bytes) {
            $entry.status = "EXTRACTED_SIZE_MISMATCH"
            Remove-CurrentStagingSafely -StagingPath $stagingPath -MarkerPath $markerPath
            $entry.staging_cleanup = "current_run_staging_removed"
            Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
            throw "Extracted JSONL size differs from the reliable 7-Zip test total: $($dataset.id)"
        }
        if (Test-Path -LiteralPath $finalPath) {
            throw "Final JSONL appeared during extraction; staged data was retained for review: $finalPath"
        }

        Move-Item -LiteralPath $stagedJsonl -Destination $finalPath
        Remove-CurrentStagingSafely -StagingPath $stagingPath -MarkerPath $markerPath
        $entry.staging_cleanup = "current_run_empty_staging_removed"
        $ratio = [double]$actualBytes / [double]$dataset.expected_compressed_bytes
        $completedRatios[[string]$dataset.id] = $ratio
        $entry.actual_uncompressed_bytes = $actualBytes
        $entry.compression_ratio = [math]::Round($ratio, 6)
        $entry.free_bytes_after = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
        $entry.status = "COMPLETE_AWAITING_SAMPLE_VALIDATION"
        Save-ExtractManifest -Entries $entries -SpacePlan $spacePlan
        Add-W1DiskEvent -Context $context -Event "extraction_complete" -Details @{
            file_id = [string]$dataset.id
            actual_uncompressed_bytes = $actualBytes
            compression_ratio = $entry.compression_ratio
            free_bytes_before = $freeBefore
            free_bytes_after = $entry.free_bytes_after
        }
        Write-W1Log -Context $context -Level "INFO" -Message "Extraction completed: $($dataset.id); actual_bytes=$actualBytes; ratio=$($entry.compression_ratio); free_bytes=$($entry.free_bytes_after)"

        if ($entry.free_bytes_after -lt $reserveBytes) {
            throw "Hard 60 GiB free-space floor was breached unexpectedly; stopping immediately."
        }
    }

    Write-W1Status -Context $context -Status "EXTRACTED_AWAITING_SAMPLE_VALIDATION" -Reason "Four JSONL files were fully extracted; sample validation is pending." -Details @{
        files_extracted = 4
        free_bytes = Get-W1FreeBytes -ProjectRoot $context.ProjectRoot
    }
    Write-W1Log -Context $context -Level "INFO" -Message "All four JSONL extractions completed; awaiting small-sample validation."
    exit 0
} catch {
    Write-W1Log -Context $context -Level "ERROR" -Message $_.Exception.Message
    if (-not (Test-Path -LiteralPath $context.StatusPath)) {
        Write-W1Status -Context $context -Status "FAILED_ARCHIVE_TEST" -Reason $_.Exception.Message
    }
    Write-Error $_
    exit 1
}
