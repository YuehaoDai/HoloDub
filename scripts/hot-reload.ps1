#requires -Version 5.1
<#
.SYNOPSIS
  One-shot hot-reload for the holodub-api / holodub-worker containers.

.DESCRIPTION
  Replaces the manual ritual documented in .cursor/rules/project-overview.mdc
  and .cursor/rules/docker.mdc:
    1. Cross-compile the relevant Go binary (linux/amd64)
    2. Discover the container's actual /proc/1/cmdline path so we don't
       guess (we have hit the /app/holodub-worker vs /usr/local/bin/holodub
       trap before — see docker.mdc 'Hot-patch' section)
    3. docker cp into that exact path
    4. Optionally clear leftover Redis stage leases (-ClearLeases)
    5. docker compose restart the affected services
    6. Tail the last few log lines from each restarted container so you
       can confirm the new binary actually started

  Default behaviour is the safest one: rebuild and restart BOTH the api
  and worker (the project-overview rule states Go changes must update
  both binaries together). Use -Target api or -Target worker to scope
  down for surgical iteration.

.PARAMETER Target
  Which service(s) to rebuild & reload. One of: api, worker, both.
  Default: both — matches project-overview.mdc's mandatory "update both"
  rule for any internal/* change.

.PARAMETER NoBuild
  Skip cross-compile. Reuses whatever holodub-{api,worker}-linux is
  already in the repo root. Useful when you've just built manually and
  only want to redo the docker cp + restart.

.PARAMETER NoRestart
  Copy the binary into the container but don't restart. The new code
  takes effect on the next manual restart. Useful when you want to
  bundle multiple changes into a single restart cycle.

.PARAMETER ClearLeases
  Before restarting the worker, DEL every Redis key matching
  holodub:lease:* — clears stage leases held by the dying old process so
  the new worker can immediately re-lease them. Without this you may
  see "stage lease already held" hangs for up to 1800s (the lease TTL).
  Only meaningful when target includes 'worker'.

.PARAMETER LogTail
  How many trailing log lines to print per container after restart.
  Default 8. Set to 0 to skip log printing entirely.

.PARAMETER ApiContainer / WorkerContainer / RedisContainer
  Override the container names if you renamed your compose project.
  Defaults match docker-compose.yml.

.PARAMETER ComposeFile
  Path to docker compose file used for `docker compose restart`.

.EXAMPLE
  .\scripts\hot-reload.ps1
  # rebuild both binaries, copy into both containers, restart both, tail logs

.EXAMPLE
  .\scripts\hot-reload.ps1 -Target worker -ClearLeases
  # only worker; also nuke any stuck stage leases first

.EXAMPLE
  .\scripts\hot-reload.ps1 -NoBuild
  # use already-built binaries (e.g. you ran `go build` yourself)
#>
[CmdletBinding()]
param(
    [ValidateSet('api','worker','both')]
    [string]$Target = 'both',

    [switch]$NoBuild,
    [switch]$NoRestart,
    [switch]$ClearLeases,

    [int]$LogTail = 8,

    [string]$ApiContainer    = 'holodub-api-1',
    [string]$WorkerContainer = 'holodub-worker-1',
    [string]$RedisContainer  = 'holodub-redis-1',

    [string]$ComposeFile     = 'docker-compose.yml'
)

$ErrorActionPreference = 'Stop'

# Force UTF-8 console — Chinese log lines come out as mojibake under the
# default Windows codepage (GBK). Same fix the episode_judge.ps1 uses.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# ── Plan: which targets, which artefacts ────────────────────────────────
$plan = New-Object System.Collections.ArrayList
if ($Target -in 'api','both') {
    [void]$plan.Add([pscustomobject]@{
        Name      = 'api'
        Cmd       = './cmd/api/'
        Bin       = 'holodub-api-linux'
        Container = $ApiContainer
    })
}
if ($Target -in 'worker','both') {
    [void]$plan.Add([pscustomobject]@{
        Name      = 'worker'
        Cmd       = './cmd/worker/'
        Bin       = 'holodub-worker-linux'
        Container = $WorkerContainer
    })
}

function Write-Step($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}
function Write-Note($msg) {
    Write-Host "  $msg" -ForegroundColor DarkGray
}

# ── 1. Cross-compile ────────────────────────────────────────────────────
if (-not $NoBuild) {
    Write-Step "Cross-compiling Go binaries (linux/amd64)"
    $prevGoos   = $env:GOOS
    $prevGoarch = $env:GOARCH
    $env:GOOS   = 'linux'
    $env:GOARCH = 'amd64'
    try {
        foreach ($t in $plan) {
            $start = Get-Date
            Write-Host "  building $($t.Bin) <- $($t.Cmd)"
            & go build -o $t.Bin $t.Cmd
            if ($LASTEXITCODE -ne 0) {
                throw "go build failed for $($t.Cmd) (exit $LASTEXITCODE)"
            }
            $elapsed = ((Get-Date) - $start).TotalSeconds
            $size    = (Get-Item $t.Bin).Length / 1MB
            Write-Note ("done in {0:F1}s, {1:F1} MB" -f $elapsed, $size)
        }
    } finally {
        # Restore env vars so subsequent local `go test` etc. don't try to
        # cross-compile. This was a real footgun while iterating on P0.
        if ($null -ne $prevGoos)   { $env:GOOS   = $prevGoos }   else { Remove-Item Env:GOOS   -ErrorAction SilentlyContinue }
        if ($null -ne $prevGoarch) { $env:GOARCH = $prevGoarch } else { Remove-Item Env:GOARCH -ErrorAction SilentlyContinue }
    }
} else {
    Write-Step "Skipping build (-NoBuild)"
    foreach ($t in $plan) {
        if (-not (Test-Path $t.Bin)) {
            throw "$($t.Bin) not found in repo root; rerun without -NoBuild or build it manually"
        }
    }
}

# ── 2. Discover real binary path inside each container & docker cp ──────
Write-Step "Copying binaries into containers"
foreach ($t in $plan) {
    # /proc/1/cmdline is NUL-separated; first field is the executable path.
    # Fall back to /usr/local/bin/holodub which is what go.Dockerfile uses.
    $rawCmdline = & docker exec $t.Container sh -c 'tr "\000" "\n" < /proc/1/cmdline | head -1' 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($rawCmdline)) {
        $binPath = '/usr/local/bin/holodub'
        Write-Warning "could not read /proc/1/cmdline from $($t.Container); falling back to $binPath"
    } else {
        $binPath = $rawCmdline.Trim()
    }
    Write-Host "  $($t.Container) PID 1 = $binPath"
    & docker cp $t.Bin "$($t.Container):$binPath"
    if ($LASTEXITCODE -ne 0) {
        throw "docker cp failed for $($t.Container)"
    }
    Write-Note "copied $($t.Bin) -> $($t.Container):$binPath"
}

# ── 3. Optional: clear stuck Redis stage leases (worker only) ──────────
if ($ClearLeases -and ($plan.Name -contains 'worker')) {
    Write-Step "Clearing Redis stage leases"
    # --no-raw keeps quoting predictable when KEYS returns multiple items.
    $rawKeys = & docker exec $RedisContainer redis-cli --no-raw KEYS "holodub:lease:*" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "could not list keys from $RedisContainer; skipping lease clear"
    } elseif (-not $rawKeys) {
        Write-Note "no holodub:lease:* keys found"
    } else {
        # redis-cli prints one key per line with surrounding double quotes.
        # When there are no matches with --no-raw it prints "(empty array)" —
        # filter that out so we don't try to DEL a literal "(empty array)" key.
        $cleared = 0
        foreach ($line in $rawKeys -split "`r?`n") {
            $key = $line.Trim().Trim('"')
            if (-not $key) { continue }
            if ($key -match '^\(empty (array|list or set)\)$') { continue }
            & docker exec $RedisContainer redis-cli DEL $key | Out-Null
            Write-Host "  DEL $key"
            $cleared++
        }
        if ($cleared -eq 0) {
            Write-Note "no holodub:lease:* keys found"
        } else {
            Write-Note "cleared $cleared lease(s)"
        }
    }
}

# ── 4. Restart containers ──────────────────────────────────────────────
if (-not $NoRestart) {
    Write-Step "Restarting containers"
    $svcs = $plan.Name
    # docker compose writes progress lines like "Container ... Restarting"
    # to STDERR. PowerShell 5 with $ErrorActionPreference = 'Stop' treats
    # ANY native-command stderr output as a NativeCommandError and aborts —
    # even when the command exits 0. Neither 2>&1 stream merge nor *>&1
    # can suppress that, because the throw happens at the I/O layer.
    # The only reliable workaround is to relax ErrorActionPreference for
    # the duration of the call and key off $LASTEXITCODE manually.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker compose -f $ComposeFile restart @svcs 2>&1 | ForEach-Object {
            Write-Host "  $_" -ForegroundColor DarkGray
        }
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose restart failed (exit $LASTEXITCODE)"
    }
    Write-Note ("restarted: " + ($svcs -join ', '))
} else {
    Write-Step "Skipping restart (-NoRestart). Run later: docker compose restart $($plan.Name -join ' ')"
}

# ── 5. Tail logs so user can sanity-check the new process started ──────
if ($LogTail -gt 0 -and -not $NoRestart) {
    # Tiny grace period — without this you sometimes see the OLD process'
    # shutdown logs and panic that the new one didn't boot.
    Start-Sleep -Seconds 2
    foreach ($t in $plan) {
        Write-Step "$($t.Container) -- last $LogTail log lines"
        & docker logs $t.Container --tail $LogTail
    }
}

Write-Host ""
Write-Host "Hot reload complete: $($plan.Name -join ', ')" -ForegroundColor Green
