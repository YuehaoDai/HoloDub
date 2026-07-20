param(
    [string]$Manifest = "tools/mit6824_2020_lectures_7_20.csv",
    [string]$Destination = "data/source_videos/mit-6.824-2020",
    [switch]$Mp4Compatible,
    [switch]$SkipProbe
)

$ErrorActionPreference = "Stop"

$ytDlp = Get-Command yt-dlp -ErrorAction SilentlyContinue
if (-not $ytDlp) {
    throw "yt-dlp was not found in PATH. Install it first, for example: winget install yt-dlp.yt-dlp"
}

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    throw "ffmpeg was not found in PATH. Install ffmpeg before downloading separate video/audio streams."
}

$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $SkipProbe -and -not $ffprobe) {
    throw "ffprobe was not found in PATH. Install ffmpeg or rerun with -SkipProbe."
}

if (-not (Test-Path -LiteralPath $Manifest)) {
    throw "Manifest not found: $Manifest"
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$absoluteDestination = (Resolve-Path -LiteralPath $Destination).Path
$reportPath = Join-Path $absoluteDestination "ffprobe-report.jsonl"
if (Test-Path -LiteralPath $reportPath) {
    Remove-Item -LiteralPath $reportPath
}

$format = "bv*+ba/b"
$mergeFormat = "mkv"
if ($Mp4Compatible) {
    $format = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"
    $mergeFormat = "mp4"
}

$items = Import-Csv -LiteralPath $Manifest
foreach ($item in $items) {
    $lecture = "{0:D2}" -f [int]$item.lecture
    $safeTitle = ($item.title -replace '[\\/:*?"<>|]', '') -replace '\s+', ' '
    $outputTemplate = Join-Path $absoluteDestination ("lec-$lecture - $safeTitle.%(ext)s")

    Write-Host "Downloading lecture $lecture - $($item.title)"
    & $ytDlp.Source `
        --format $format `
        --merge-output-format $mergeFormat `
        --embed-metadata `
        --embed-chapters `
        --write-info-json `
        --write-thumbnail `
        --no-overwrites `
        --output $outputTemplate `
        $item.youtube_url

    if (-not $SkipProbe) {
        $video = Get-ChildItem -LiteralPath $absoluteDestination -File |
            Where-Object { $_.BaseName -eq "lec-$lecture - $safeTitle" -and $_.Extension -in ".mkv", ".mp4", ".webm" } |
            Sort-Object Length -Descending |
            Select-Object -First 1

        if ($video) {
            $probe = & $ffprobe.Source -v error -show_format -show_streams -of json $video.FullName
            [pscustomobject]@{
                lecture = [int]$item.lecture
                title = $item.title
                file = $video.FullName
                official_page = $item.official_page
                youtube_url = $item.youtube_url
                probe = ($probe | ConvertFrom-Json)
            } | ConvertTo-Json -Depth 20 -Compress | Add-Content -LiteralPath $reportPath -Encoding UTF8
        }
    }
}

Write-Host "Done: $absoluteDestination"
if (-not $SkipProbe) {
    Write-Host "Probe report: $reportPath"
}
