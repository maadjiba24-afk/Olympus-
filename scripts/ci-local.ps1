<#
.SYNOPSIS
Reproduce one leg of the CI `test` job EXACTLY, locally, before pushing.

.DESCRIPTION
`scripts/dev-setup.sh` gives you a working environment. This gives you CI's
environment, and the difference is where the bugs live. Four failures in the
Wave-0/#245 work were invisible locally and red on CI, every one of them
because the local venv differed from the runner's:

  * `build` was absent from the `test` extra — the local venv happened to have
    it from an earlier install.
  * `setuptools` was a different version than the release policy pins, so a
    wheel-metadata assertion failed only on the runner.
  * `tomllib` was imported unguarded — invisible on 3.11+, fatal on the 3.10
    leg, and the dev machine was 3.11.
  * a test's POSIX assumption passed on Linux and failed on Windows.

What CI does, and therefore what this does, in order:
    pip install --require-hashes -r requirements.lock
    pip install -e . --no-deps
    pip install -e '.[test]'
    python scripts/check_no_prerelease.py requirements.lock
    python -m compileall -q olympus
    python -m olympus capabilities --check
    python scripts/check_threat_model.py
    pytest -q

The venv lives at .venv\ci-py<version> (already gitignored). A fresh one is
built by default: a reused environment accumulates packages and is exactly how
the `build` and `setuptools` failures stayed hidden.

.PARAMETER PythonVersion
Which interpreter to use. Defaults to 3.10 — the MINIMUM supported version,
and where version-dependent breakage surfaces first.

.PARAMETER Reuse
Keep the existing venv instead of rebuilding it (fast iteration).

.PARAMETER Fast
Run the guards and import smoke, skip pytest.

.EXAMPLE
.\scripts\ci-local.ps1
.EXAMPLE
.\scripts\ci-local.ps1 -PythonVersion 3.12
.EXAMPLE
.\scripts\ci-local.ps1 -Fast -Reuse
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.10",
    [switch]$Reuse,
    [switch]$Fast
)

$ErrorActionPreference = "Continue"

# The minimum supported version, and this script's default (kept in step with
# the param block). The "not found" help uses it to tell whether the caller
# asked for the minimum leg or deliberately chose another one.
$DefaultPythonVersion = "3.10"

# `Set-Location` in a .ps1 changes the CALLER's session location and does not
# revert when the script ends — the bash version cannot leak its `cd` because
# it runs in its own process. Restore it on the way out of every exit path so
# running this from a subdirectory does not silently relocate your shell.
$origin = $PWD.Path
Set-Location (Join-Path $PSScriptRoot "..")

function Exit-CiLocal {
    param([int]$Code)
    Set-Location $origin
    exit $Code
}

$venv = ".venv\ci-py$PythonVersion"
$failed = New-Object System.Collections.Generic.List[string]

function Invoke-Step {
    param([string]$Label, [scriptblock]$Body)
    Write-Host ""
    Write-Host "> $Label" -ForegroundColor White
    # Reset first: $LASTEXITCODE is only written by NATIVE commands and keeps
    # its previous value otherwise, so a step whose command never launched
    # would inherit the last step's 0 and be reported OK. The bash version
    # tests the command's own status (`if "$@"`), which cannot go stale.
    $global:LASTEXITCODE = 0
    $ok = $true
    try {
        & $Body
        if ($LASTEXITCODE -ne 0) { $ok = $false }
    } catch {
        # A command that cannot be launched at all (missing exe, bad path)
        # raises instead of setting an exit code. That is a failed step.
        Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
        $ok = $false
    }
    if ($ok) {
        Write-Host "  OK  $Label" -ForegroundColor Green
    } else {
        Write-Host "  FAIL $Label" -ForegroundColor Red
        $failed.Add($Label)
    }
}

# Interpreter discovery, in order of reliability. Each tier yields an exe plus
# any prefix args, kept separate: `& $array` stringifies to a single command
# name ("py -3.10"), which is not a program.
function Test-Launcher {
    param([string]$Exe, [string[]]$Prefix = @())
    try {
        & $Exe @Prefix --version *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$pyExe = $null
$pyArgs = @()
# 1. The Windows `py` launcher — the registry-backed way to select a version.
if (Test-Launcher "py" @("-$PythonVersion")) {
    $pyExe = "py"; $pyArgs = @("-$PythonVersion")
}
# 2. A versioned executable on PATH. This is also where `uv python install`
#    drops its shim (%USERPROFILE%\.local\bin\python3.10.exe), so a uv-managed
#    interpreter is found here whenever that dir is on PATH.
elseif (Test-Launcher "python$PythonVersion") {
    $pyExe = "python$PythonVersion"
}
# 3. Ask uv directly. Covers a uv-managed interpreter whose shim dir is NOT on
#    PATH — `uv python find` prints the real interpreter path.
else {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        $found = & uv python find $PythonVersion 2>$null
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path $found)) {
            $pyExe = $found
        }
    }
}

if (-not $pyExe) {
    Write-Host "Python $PythonVersion not found. Tried 'py -$PythonVersion', 'python$PythonVersion' on PATH, and 'uv python find $PythonVersion'." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it with any of:" -ForegroundColor Yellow
    Write-Host "    uv python install $PythonVersion       # recommended; puts python$PythonVersion.exe on PATH"
    Write-Host "    winget install Python.Python.$PythonVersion"
    Write-Host "    https://www.python.org/downloads/  (tick 'py launcher' in the installer)"
    Write-Host ""
    if ($PythonVersion -eq $DefaultPythonVersion) {
        Write-Host "$PythonVersion is the MINIMUM supported version and the leg where version-dependent" -ForegroundColor Yellow
        Write-Host "breakage surfaces first, so prefer installing it over switching. If you must:" -ForegroundColor Yellow
        Write-Host "    .\scripts\ci-local.ps1 -PythonVersion 3.12"
    } else {
        Write-Host "Or run the default minimum-version leg:  .\scripts\ci-local.ps1" -ForegroundColor Yellow
    }
    Exit-CiLocal 2
}

if ((-not $Reuse) -or (-not (Test-Path $venv))) {
    Write-Host "building a clean venv at $venv (python $PythonVersion)..."
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $pyExe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Exit-CiLocal 2 }
}

$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv is missing $py" -ForegroundColor Red
    Exit-CiLocal 2
}
& $py -m pip install -q --upgrade pip *> $null
Write-Host ("interpreter: " + (& $py --version 2>&1))

# --- the CI install sequence, verbatim -------------------------------------
Invoke-Step "install hash-pinned lock"  { & $py -m pip install -q --require-hashes -r requirements.lock }
Invoke-Step "install package (no deps)" { & $py -m pip install -q -e . --no-deps }
Invoke-Step "install [test] extra"      { & $py -m pip install -q -e ".[test]" }

# --- the CI guard steps, in CI's order -------------------------------------
Invoke-Step "no pre-release deps"  { & $py scripts\check_no_prerelease.py requirements.lock }
Invoke-Step "compileall"           { & $py -m compileall -q olympus }
# Mirrors the ci.yml step of the same name; runs before the suite because it
# is ~2s and names import-time breakage the 6-minute suite finds much later.
Invoke-Step "import smoke"         { & $py scripts\import_smoke.py --quiet }
Invoke-Step "capability counts"    { & $py -m olympus capabilities --check }
Invoke-Step "threat model"         { & $py scripts\check_threat_model.py }

if (-not $Fast) {
    Invoke-Step "pytest" { & $py -m pytest -q }
} else {
    Write-Host "(-Fast: pytest skipped)"
}

Write-Host ""
Write-Host "----------------------------------------"
if ($failed.Count -eq 0) {
    Write-Host "all steps passed on python $PythonVersion" -ForegroundColor Green
    Exit-CiLocal 0
}
Write-Host "$($failed.Count) step(s) failed on python $PythonVersion :" -ForegroundColor Red
foreach ($f in $failed) { Write-Host "  - $f" }
Exit-CiLocal 1
