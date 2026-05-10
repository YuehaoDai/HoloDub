# Episode-level judge for a completed dubbing job (one-shot validation script).
#
# OPT-002 segment-level judge runs PER segment and only sees one (src, tgt)
# pair at a time. This script asks the LLM to judge the WHOLE EPISODE in
# one call so cross-segment properties (terminology consistency, narrative
# coherence, register stability, character voice) can be evaluated.
#
# It is intentionally NOT integrated into the pipeline / Go API — it is a
# one-shot tool for evaluating a single job's overall quality. Outputs JSON
# to tests/quality/episode-judge-job-<id>.json.
#
# Usage:
#   .\scripts\episode_judge.ps1 -JobId 131
#   .\scripts\episode_judge.ps1 -JobId 131 -Model qwen-max          # higher quality model
#   .\scripts\episode_judge.ps1 -JobId 131 -OutputDir .\custom\dir
#
# Reads OPENAI_BASE_URL / OPENAI_API_KEY from .env (defaults to DashScope).
param(
    [Parameter(Mandatory=$true)]
    [int]$JobId,

    [string]$Model = "qwen-max",  # default to qwen-max for higher-quality holistic judgement

    [string]$OutputDir = "tests\quality",

    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

# Force UTF-8 throughout: docker exec / psql produce UTF-8 JSON containing
# Chinese; without this PowerShell falls back to GBK on Windows and corrupts
# the byte stream before ConvertFrom-Json sees it.
[Console]::OutputEncoding   = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding             = [System.Text.UTF8Encoding]::new($false)
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

# ── 1. Load .env ──────────────────────────────────────────────────────────
if (-not (Test-Path $EnvFile)) {
    throw "Cannot find $EnvFile (run from repo root)"
}
$envVars = @{}
Get-Content $EnvFile | Where-Object { $_ -match '^[^#].*=' } | ForEach-Object {
    $key, $val = $_ -split '=', 2
    $envVars[$key.Trim()] = $val.Trim()
}
$baseURL = $envVars['OPENAI_BASE_URL']
$apiKey  = $envVars['OPENAI_API_KEY']
$srcLangDefault = 'en'
$tgtLangDefault = 'zh-CN'
if (-not $baseURL -or -not $apiKey) {
    throw "OPENAI_BASE_URL / OPENAI_API_KEY missing from $EnvFile"
}
Write-Host "Provider: $baseURL"
Write-Host "Model:    $Model"
Write-Host "Job ID:   $JobId"

# ── 2. Pull job + segments from Postgres ──────────────────────────────────
$jobJson = docker exec holodub-postgres-1 psql -U holodub -d holodub -At -c @"
SELECT json_build_object(
  'id', id, 'name', name, 'status', status,
  'source_language', source_language, 'target_language', target_language,
  'translation_summary', translation_summary,
  'started_at', started_at, 'completed_at', completed_at,
  'total_sec', EXTRACT(EPOCH FROM (completed_at - started_at))
) FROM jobs WHERE id = $JobId;
"@
if (-not $jobJson) { throw "Job $JobId not found" }
$job = $jobJson | ConvertFrom-Json
$srcLang = if ($job.source_language) { $job.source_language } else { $srcLangDefault }
$tgtLang = if ($job.target_language) { $job.target_language } else { $tgtLangDefault }

$segsJson = docker exec holodub-postgres-1 psql -U holodub -d holodub -At -c @"
SELECT COALESCE(json_agg(row_to_json(s) ORDER BY s.ordinal), '[]'::json) FROM (
  SELECT ordinal,
         start_ms, end_ms,
         (end_ms - start_ms) AS target_ms,
         tts_duration_ms,
         source_text, target_text,
         status,
         judge_score,
         judge_meta
  FROM segments WHERE job_id = $JobId
) s;
"@
$segments = $segsJson | ConvertFrom-Json
$total    = $segments.Count
$synth    = ($segments | Where-Object { $_.status -eq 'synthesized' }).Count
$judged   = ($segments | Where-Object { $_.judge_score -ne $null }).Count
Write-Host ""
Write-Host "=== Job summary ==="
Write-Host "  $srcLang -> $tgtLang | total segments=$total | synthesized=$synth | per-segment-judged=$judged"
Write-Host "  total wall=$([math]::Round($job.total_sec, 1)) sec"
Write-Host "  episode_summary present: $([bool]$job.translation_summary)"

if ($synth -lt $total) {
    Write-Warning "Job not fully synthesized ($synth/$total). Episode judge will see translated-only segments too; results may be incomplete."
}

# ── 3. Build episode-judge prompt ─────────────────────────────────────────
# Include drift/judge_score per segment so the LLM can correlate quality
# axes with measurable signals.
$segLines = New-Object System.Collections.ArrayList
foreach ($s in $segments) {
    $line = "[seg{0}] dur={1}s" -f $s.ordinal, ([math]::Round($s.target_ms / 1000.0, 1))
    if ($s.judge_score) { $line += " seg_judge=$([math]::Round($s.judge_score, 2))" }
    $line += "`n  ${srcLang}: $($s.source_text)`n  ${tgtLang}: $($s.target_text)"
    [void]$segLines.Add($line)
}
$segText = $segLines -join "`n`n"

$systemPrompt = @"
You are a senior dubbing localization director performing a final quality-assurance review of an episode-length dubbed video.

Source language: $srcLang. Target language: $tgtLang.
You will receive: (a) every segment's source text + dubbed translation in order, (b) optional per-segment fidelity score (0..1), (c) optional episode-level reference card.

Your job: produce a STRUCTURED HOLISTIC review focused on cross-segment properties that segment-level scoring CANNOT see:

1. terminology_consistency (0..1): Are recurring proper nouns / technical terms translated consistently throughout? List any term that varies and where.
2. register_consistency  (0..1): Does the speaker's tone (academic / casual / formal) stay stable? Flag jarring shifts.
3. narrative_coherence   (0..1): Do consecutive segments flow logically as continuous speech? Detect discourse-marker glitches ('but'/'so'/'and' starting a segment with no antecedent), missing connectives, contradictions.
4. character_voice_stability (0..1): Does each speaker keep one consistent voice throughout (where multiple speakers appear)?
5. cultural_localization (0..1): Are idioms, examples, units adapted appropriately for $tgtLang audience without losing source meaning?
6. overall_fidelity      (0..1): Aggregate semantic preservation across the whole episode (NOT just each segment in isolation).
7. overall_fluency       (0..1): How natural would this sound spoken aloud end-to-end by a native $tgtLang speaker?

Respond with EXACTLY ONE JSON OBJECT (no prose, no markdown fence, no trailing comments) with these keys:
- terminology_consistency   : number 0..1
- register_consistency      : number 0..1
- narrative_coherence       : number 0..1
- character_voice_stability : number 0..1
- cultural_localization     : number 0..1
- overall_fidelity          : number 0..1
- overall_fluency           : number 0..1
- top_3_strongest_segments  : array of {ordinal:int, reason:string}
- top_3_weakest_segments    : array of {ordinal:int, issue:string, recommended_fix:string}
- terminology_glossary_observed : array of {source_term:string, target_term:string, note:string}
- overall_verdict           : string, one of "production_ready" | "needs_minor_revision" | "needs_major_revision"
- one_paragraph_summary     : 3-4 sentences in $tgtLang summarising overall quality and the most important fix
"@

$userPrompt = ""
if ($job.translation_summary) {
    $userPrompt += "[Episode reference card produced during translation]`n$($job.translation_summary)`n[End of reference card]`n`n"
}
$userPrompt += "[All segments, in order]`n$segText`n`nReturn ONLY the JSON object now."

# NB: DashScope's OpenAI-compatible endpoint rejected our tools+strict-schema
# payload (qwen-max / qwen-turbo both replied 'Required body invalid').
# Fall back to plain prompt + manual JSON parsing — same fallback OPT-003
# uses for ReviewSegmentation when tools fail. response_format=json_object
# is supported and gives us a reasonable strictness without the tools dance.
$payload = @{
    model       = $Model
    temperature = 0.1
    response_format = @{ type = "json_object" }
    messages    = @(
        @{ role = "system"; content = $systemPrompt }
        @{ role = "user";   content = $userPrompt }
    )
}
$payloadJson = $payload | ConvertTo-Json -Depth 20 -Compress

# Debug: write payload to file for inspection if Dashscope rejects it.
$debugDir = Join-Path $OutputDir "_debug"
New-Item -ItemType Directory -Force -Path $debugDir | Out-Null
$debugPath = Join-Path $debugDir "payload-job-$JobId.json"
$payloadJson | Out-File -FilePath $debugPath -Encoding utf8 -NoNewline
Write-Host "  payload dumped to $debugPath ($([math]::Round($payloadJson.Length / 1024, 1)) KB)"

# ── 4. Send judge call ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Calling $Model (input ~$(($payloadJson.Length / 4)) tokens estimated) ==="
$start = Get-Date
# Force a UTF-8 byte array body so the request doesn't re-encode our Chinese
# system/user prompts in PowerShell's default codepage. Using -Body string
# lets it slip into Latin-1, which DashScope flatly rejects.
#
# Also use Invoke-WebRequest (not Invoke-RestMethod) so we can grab raw
# response bytes and decode UTF-8 manually. DashScope returns
# `Content-Type: application/json` WITHOUT a charset parameter, and PowerShell
# 5's Invoke-RestMethod then defaults to ISO-8859-1, which mangles every
# non-ASCII character in the model's reply (overall_verdict survives because
# it's ASCII; one_paragraph_summary becomes mojibake).
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($payloadJson)
$webResp = Invoke-WebRequest `
    -Uri ($baseURL.TrimEnd('/') + '/chat/completions') `
    -Method Post `
    -Headers @{ "Authorization" = "Bearer $apiKey" } `
    -ContentType 'application/json; charset=utf-8' `
    -Body $bodyBytes `
    -UseBasicParsing
$elapsed = (Get-Date) - $start
# DashScope returns `Content-Type: application/json` WITHOUT a charset parameter.
# PowerShell 5's Invoke-WebRequest then decodes the body bytes with ISO-8859-1
# into $webResp.Content (a string), which mangles every multi-byte UTF-8
# sequence (e.g. Chinese turns into "æ¸æ°"-style mojibake).
# Because ISO-8859-1 is a 1:1 byte-to-codepoint map, we can losslessly
# undo the wrong decode by re-encoding to ISO-8859-1 bytes and then
# decoding those bytes as UTF-8.
$origBytes = [System.Text.Encoding]::GetEncoding('ISO-8859-1').GetBytes($webResp.Content)
$respText  = [System.Text.Encoding]::UTF8.GetString($origBytes)
$resp      = $respText | ConvertFrom-Json
Write-Host "  ok in $($elapsed.TotalSeconds.ToString('F2'))s"

$choice  = $resp.choices[0]
$content = $choice.message.content
if ([string]::IsNullOrWhiteSpace($content)) {
    Write-Error "Model returned empty content."
    exit 1
}
# Strip markdown fences just in case the model wraps the JSON.
$content = $content.Trim()
if ($content.StartsWith('```')) {
    $content = $content -replace '^```(?:json)?\s*\r?\n?', '' -replace '\r?\n?```\s*$', ''
}
try {
    $verdict = $content | ConvertFrom-Json
} catch {
    $rawDump = Join-Path $debugDir "raw-response-job-$JobId.txt"
    $content | Out-File -FilePath $rawDump -Encoding utf8 -NoNewline
    Write-Error "Failed to parse JSON. Raw response saved to $rawDump"
    Write-Error $_
    exit 1
}

# ── 5. Print human-readable summary ───────────────────────────────────────
Write-Host ""
Write-Host "================================================================"
Write-Host "  EPISODE JUDGE RESULT  (job_id=$JobId, model=$Model)"
Write-Host "================================================================"
$axes = @(
    @{ name = "terminology_consistency";   v = $verdict.terminology_consistency }
    @{ name = "register_consistency";      v = $verdict.register_consistency }
    @{ name = "narrative_coherence";       v = $verdict.narrative_coherence }
    @{ name = "character_voice_stability"; v = $verdict.character_voice_stability }
    @{ name = "cultural_localization";     v = $verdict.cultural_localization }
    @{ name = "overall_fidelity";          v = $verdict.overall_fidelity }
    @{ name = "overall_fluency";           v = $verdict.overall_fluency }
)
foreach ($a in $axes) {
    $bar = '#' * [math]::Round($a.v * 20)
    "  {0,-30} {1,5:F2}  {2}" -f $a.name, $a.v, $bar | Write-Host
}
Write-Host ""
Write-Host "Overall verdict: $($verdict.overall_verdict)"
Write-Host "Summary: $($verdict.one_paragraph_summary)"
if ($verdict.top_3_weakest_segments) {
    Write-Host ""
    Write-Host "TOP 3 WEAKEST:"
    foreach ($w in $verdict.top_3_weakest_segments) {
        Write-Host "  seg$($w.ordinal): $($w.issue)"
        Write-Host "          fix: $($w.recommended_fix)"
    }
}
if ($verdict.terminology_glossary_observed) {
    Write-Host ""
    Write-Host "OBSERVED GLOSSARY ($($verdict.terminology_glossary_observed.Count) terms)"
    foreach ($g in $verdict.terminology_glossary_observed) {
        $note = if ($g.note) { " - $($g.note)" } else { "" }
        Write-Host "  $($g.source_term)  ->  $($g.target_term)$note"
    }
}

# ── 6. Persist full result + raw artefacts ────────────────────────────────
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$outPath = Join-Path $OutputDir "episode-judge-job-$JobId.json"
$report = @{
    schema_version    = "1.0"
    captured_at       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    job               = $job
    model             = $Model
    elapsed_sec       = $elapsed.TotalSeconds
    usage             = $resp.usage
    segments_summary  = @{
        total                  = $total
        synthesized            = $synth
        per_segment_judged     = $judged
        per_segment_judge_avg  = if ($judged -gt 0) {
            [math]::Round((($segments | Where-Object { $_.judge_score -ne $null } | Measure-Object -Property judge_score -Average).Average), 3)
        } else { $null }
    }
    judgement         = $verdict
}
# Persist report. PowerShell 5's `ConvertTo-Json` mangles non-ASCII by writing
# `\u` escapes that are themselves Latin-1 garbled when re-read; bypass the
# whole pipeline by writing UTF-8 bytes directly.
$reportJson = $report | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllBytes($outPath, [System.Text.Encoding]::UTF8.GetBytes($reportJson))
Write-Host ""
Write-Host "Full report written to: $outPath"
