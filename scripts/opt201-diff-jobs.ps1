<#
.SYNOPSIS
    OPT-201 L2 staging parity check: byte-diff two job outputs run
    with vs without SEGMENT_AGENT_ENABLED.

.DESCRIPTION
    Pulls the segment-level outputs of two jobs from Postgres and
    compares (target_text, tts_audio_rel_path, tts_duration_ms,
    status) tuples. Exits 0 on zero mismatches, 1 otherwise.

    The flag-ON job and flag-OFF job MUST have been driven by the
    same deterministic ml-service backend (ML_TTS_BACKEND=silence)
    and the same LLM provider (or LLM mock) so the inputs to the
    agent are byte-stable.

.PARAMETER LegacyJobID
    Job ID produced with SEGMENT_AGENT_ENABLED=false.

.PARAMETER AgentJobID
    Job ID produced with SEGMENT_AGENT_ENABLED=true on the same input.

.PARAMETER PostgresContainer
    docker compose service name; defaults to "holodub-postgres-1".

.EXAMPLE
    .\scripts\opt201-diff-jobs.ps1 -LegacyJobID 152 -AgentJobID 153

.NOTES
    See docs/opt-201/rollout.md § L2 for the full procedure.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][int]$LegacyJobID,
    [Parameter(Mandatory)][int]$AgentJobID,
    [string]$PostgresContainer = "holodub-postgres-1",
    [string]$DBUser = "holodub",
    [string]$DBName = "holodub"
)

$ErrorActionPreference = "Stop"

function Get-SegmentsJson {
    param([int]$JobID)
    $sql = @"
SELECT ordinal,
       target_text,
       tts_audio_rel_path,
       tts_duration_ms,
       status
FROM segments
WHERE job_id = $JobID
ORDER BY ordinal;
"@
    $raw = docker exec $PostgresContainer psql -U $DBUser -d $DBName -t -A -F '|' -c $sql 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed for job $JobID (exit $LASTEXITCODE)"
    }
    $rows = $raw -split "`n" | Where-Object { $_ -ne '' }
    $out = @()
    foreach ($row in $rows) {
        $cols = $row -split '\|'
        if ($cols.Length -lt 5) { continue }
        $out += [PSCustomObject]@{
            Ordinal       = [int]$cols[0]
            TargetText    = $cols[1]
            AudioRelPath  = $cols[2]
            DurationMs    = [int64]$cols[3]
            Status        = $cols[4]
        }
    }
    return ,$out
}

Write-Host "[OPT-201 diff] fetching legacy job $LegacyJobID ..."
$legacy = Get-SegmentsJson -JobID $LegacyJobID
Write-Host "[OPT-201 diff] fetching agent  job $AgentJobID ..."
$agent  = Get-SegmentsJson -JobID $AgentJobID

if ($legacy.Count -ne $agent.Count) {
    Write-Error "Segment count mismatch: legacy=$($legacy.Count) agent=$($agent.Count)"
    exit 1
}

$mismatchCount = 0
for ($i = 0; $i -lt $legacy.Count; $i++) {
    $L = $legacy[$i]
    $A = $agent[$i]
    if ($L.Ordinal -ne $A.Ordinal) {
        Write-Warning "[ordinal mismatch] index=$i legacy=$($L.Ordinal) agent=$($A.Ordinal)"
        $mismatchCount++
        continue
    }
    $deltas = @()
    if ($L.TargetText  -ne $A.TargetText)   { $deltas += "target_text" }
    if ($L.AudioRelPath -ne $A.AudioRelPath) { $deltas += "tts_audio_rel_path" }
    if ($L.DurationMs   -ne $A.DurationMs)   { $deltas += "tts_duration_ms ($($L.DurationMs) vs $($A.DurationMs))" }
    if ($L.Status       -ne $A.Status)       { $deltas += "status ($($L.Status) vs $($A.Status))" }
    if ($deltas.Count -gt 0) {
        Write-Warning "[mismatch ord=$($L.Ordinal)] $($deltas -join ', ')"
        $mismatchCount++
    }
}

if ($mismatchCount -eq 0) {
    Write-Host "OK: $($legacy.Count) segments, 0 mismatches" -ForegroundColor Green
    exit 0
}
Write-Error "FAIL: $mismatchCount segment(s) differ — see warnings above; capture and reproduce in fake_tools_test.go before proceeding to L3."
exit 1
