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
Set-Location (Join-Path $PSScriptRoot "..")

$venv = ".venv\ci-py$PythonVersion"
$failed = New-Object System.Collections.Generic.List[string]

function Invoke-Step {
    param([string]$Label, [scriptblock]$Body)
    Write-Host ""
    Write-Host "> $Label" -ForegroundColor White
    & $Body
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK  $Label" -ForegroundColor Green
    } else {
        Write-Host "  FAIL $Label" -ForegroundColor Red
        $failed.Add($Label)
    }
}

# The Windows `py` launcher is the reliable way to select a version; fall back
# to a versioned executable on PATH.
$launcher = @("py", "-$PythonVersion")
& $launcher[0] $launcher[1] --version *> $null
if ($LASTEXITCODE -ne 0) {
    $launcher = @("python$PythonVersion")
    & $launcher[0] --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Python $PythonVersion not found (tried 'py -$PythonVersion' and 'python$PythonVersion')." -ForegroundColor Red
        Write-Host "Install it, or pass a version you have: .\scripts\ci-local.ps1 -PythonVersion 3.12" -ForegroundColor Red
        exit 2
    }
}

if ((-not $Reuse) -or (-not (Test-Path $venv))) {
    Write-Host "building a clean venv at $venv (python $PythonVersion)..."
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $launcher -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit 2 }
}

$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv is missing $py" -ForegroundColor Red
    exit 2
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
# Not in ci.yml (yet) but runs here first: ~2s, and it catches the import-time
# breakage the 6-minute suite would find much later.
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
    exit 0
}
Write-Host "$($failed.Count) step(s) failed on python $PythonVersion :" -ForegroundColor Red
foreach ($f in $failed) { Write-Host "  - $f" }
exit 1
