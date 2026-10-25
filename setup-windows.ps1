# Asset Archaeologist setup for Windows. Double-click setup-windows.bat instead of running this directly.
#   -Yes         answer yes to every question (unattended)
#   -Cpu         install the CPU build of torch even if an NVIDIA GPU is present
#   -NoShortcut  do not offer a desktop shortcut
param([switch]$Yes, [switch]$Cpu, [switch]$NoShortcut)

# native programs (uv, nvidia-smi) print progress on stderr; check $LASTEXITCODE instead of stopping on it
$ErrorActionPreference = "Continue"
$App = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $App

function Say($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ask($question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question [Y/n]"
    return ($a -eq "" -or $a -match "^[Yy]")
}
function Fail($text) {
    Write-Host ""; Write-Host "Setup stopped: $text" -ForegroundColor Red
    exit 1
}

Write-Host "Asset Archaeologist setup" -ForegroundColor Green
Write-Host "Installs a private copy of Python and the packages it needs into $App\.venv"
Write-Host "(nothing system-wide), then checks for Blender."

# 1. uv: one small program that installs Python and the packages
Say "Step 1 of 5: uv (installs Python and packages)"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    foreach ($p in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path $p) { $uv = $p; break }
    }
}
if (-not $uv) {
    Write-Host "uv is not installed. It is a single program from Astral (https://docs.astral.sh/uv/)."
    if (-not (Ask "Download and install uv now?")) { Fail "uv is needed. Install it from https://docs.astral.sh/uv/ and run setup again." }
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uv)) { Fail "uv did not install. Install it from https://docs.astral.sh/uv/ and run setup again." }
}
Write-Host "uv: $uv"

# 2. Python environment
Say "Step 2 of 5: Python 3.12 environment"
$py = Join-Path $App ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    & $uv venv --python 3.12 (Join-Path $App ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "could not create the Python environment." }
} else { Write-Host "already there: $py" }

# 3. torch (GPU build when an NVIDIA GPU is present), then the app
Say "Step 3 of 5: packages (this is the long step: about 3 GB with an NVIDIA GPU, 300 MB without)"
$index = "https://download.pytorch.org/whl/cpu"
$smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
if ($smi -and -not $Cpu) {
    $out = (& $smi 2>$null) -join "`n"
    if ($out -match "CUDA Version:\s*(\d+)\.(\d+)") {
        $cuda = [int]$Matches[1] * 100 + [int]$Matches[2]
        if ($cuda -ge 1208) { $index = "https://download.pytorch.org/whl/cu128" }
        elseif ($cuda -ge 1206) { $index = "https://download.pytorch.org/whl/cu126" }
        elseif ($cuda -ge 1108) { $index = "https://download.pytorch.org/whl/cu118" }
        Write-Host "NVIDIA GPU found (driver supports CUDA $($Matches[1]).$($Matches[2]))."
    }
}
if ($index -like "*cpu") { Write-Host "No usable NVIDIA GPU: installing the CPU build (works, just slower)." }
else { Write-Host "Installing the GPU build of torch from $index" }
& $uv pip install --python $py torch torchvision --index-url $index
if ($LASTEXITCODE -ne 0) { Fail "torch did not install (see the messages above)." }
& $uv pip install --python $py -e $App
if ($LASTEXITCODE -ne 0) { Fail "the app did not install (see the messages above)." }
$aa = Join-Path $App ".venv\Scripts\asset-archaeologist.exe"

# 4. Blender
Say "Step 4 of 5: Blender (draws the thumbnails)"
$info = & $aa config --json | Out-String | ConvertFrom-Json
if ($info.blender_found) {
    Write-Host "Found: $($info.blender_found)"
} else {
    Write-Host "Blender 4.2 or newer is needed and was not found." -ForegroundColor Yellow
    $winget = (Get-Command winget -ErrorAction SilentlyContinue).Source
    if ($winget -and (Ask "Install Blender now with winget (Windows Package Manager)?")) {
        & $winget install --id BlenderFoundation.Blender -e
    } else {
        Write-Host "Download it from https://www.blender.org/download/ , install it, then run setup again"
        Write-Host "(or set its path later on the Settings screen of the page)."
        if (-not $Yes) { Start-Process "https://www.blender.org/download/" }
    }
}

# 5. the search model and a final check
Say "Step 5 of 5: search model (SigLIP, about 800 MB, downloaded once) and a final check"
& $aa doctor --fetch-model
$doctorOk = ($LASTEXITCODE -eq 0)

if (-not $NoShortcut -and (Ask "Put an 'Asset Archaeologist' shortcut on the desktop?")) {
    $desk = [Environment]::GetFolderPath("Desktop")
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk "Asset Archaeologist.lnk"))
    $sh.TargetPath = Join-Path $App "start-windows.bat"
    $sh.WorkingDirectory = $App
    $sh.Save()
    Write-Host "Shortcut created on the desktop."
}

Write-Host ""
if ($doctorOk) { Write-Host "Setup finished." -ForegroundColor Green }
else { Write-Host "Setup finished, but the check above found something to fix (lines marked FAIL)." -ForegroundColor Yellow }
Write-Host "Next: double-click start-windows.bat (or the desktop shortcut). Your browser opens"
Write-Host "http://127.0.0.1:$($info.port)/ ; choose your library folders there and press Start scan."
Write-Host "For AI agents: $aa find ""wooden barrel"" --json   or   http://127.0.0.1:$($info.port)/llms.txt"
