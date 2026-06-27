# OLYMPUS one-line installer for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/maadjiba24-afk/Olympus-/main/install.ps1 | iex
#
# What it does (and nothing more):
#   1. Creates a private Python environment in %USERPROFILE%\.olympus\venv
#   2. Installs Olympus into it
#   3. Puts an `olympus` command on your PATH (current user)
# Then you just type `olympus` — it asks for your API key once and you chat
# in plain English. Uninstall anytime:
#   Remove-Item -Recurse -Force $env:USERPROFILE\.olympus
#   Remove-Item $env:USERPROFILE\.local\bin\olympus.cmd

$ErrorActionPreference = "Stop"

$Repo    = if ($env:OLYMPUS_REPO) { $env:OLYMPUS_REPO } else { "https://github.com/maadjiba24-afk/Olympus-" }
$HomeDir = if ($env:OLYMPUS_HOME) { $env:OLYMPUS_HOME } else { Join-Path $env:USERPROFILE ".olympus" }
$Venv    = Join-Path $HomeDir "venv"
$BinDir  = Join-Path $env:USERPROFILE ".local\bin"

function Say($m)  { Write-Host "[*] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# --- 1. find a suitable Python (3.10+) -----------------------------------
$Py = $null
foreach ($cand in @("python", "python3", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        try {
            & $cand -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
        } catch { }
    }
}
if (-not $Py) {
    Fail "Python 3.10+ is required. Install it from https://www.python.org/downloads/ (check 'Add to PATH'), then re-run this command."
}

Say "Installing OLYMPUS (using $(& $Py --version 2>&1))..."

# --- 2. private environment ----------------------------------------------
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
& $Py -m venv $Venv
if ($LASTEXITCODE -ne 0) { Fail "Could not create a Python environment." }

$VenvPy = Join-Path $Venv "Scripts\python.exe"
& $VenvPy -m pip install --quiet --upgrade pip 2>$null
Say "Downloading Olympus..."
& $VenvPy -m pip install --quiet --upgrade "git+$Repo"
if ($LASTEXITCODE -ne 0) { Fail "Install failed. Check your internet connection and that git is installed, then try again." }

# --- 3. the `olympus` command --------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Shim = Join-Path $BinDir "olympus.cmd"
$VenvOlympus = Join-Path $Venv "Scripts\olympus.exe"
"@echo off`r`n`"$VenvOlympus`" %*" | Set-Content -Encoding ASCII $Shim

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    Say "Added $BinDir to your PATH (open a new terminal to pick it up)."
}

Write-Host ""
Say "OLYMPUS installed."
Write-Host ""
Write-Host "   Open a NEW terminal, then type:   olympus"
Write-Host ""
Write-Host "   It will ask which API key you're bringing (Anthropic, OpenAI, or any"
Write-Host "   compatible provider), save it securely, and drop you into chat."
Write-Host ""
