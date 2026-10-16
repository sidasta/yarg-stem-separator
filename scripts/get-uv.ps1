# Downloads uv (https://github.com/astral-sh/uv) into the tools folder next to this project.
# Used by "Start Stem Separator.bat" when uv isn't installed. Nothing is installed system-wide.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster on Windows PowerShell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$tools = Join-Path (Split-Path $PSScriptRoot -Parent) 'tools'
$url = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
$tmp = Join-Path $env:TEMP ('uv-' + [guid]::NewGuid())
New-Item -ItemType Directory -Force $tmp, $tools | Out-Null
try {
    $zip = Join-Path $tmp 'uv.zip'
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    Invoke-WebRequest "$url.sha256" -OutFile "$zip.sha256" -UseBasicParsing
    $expected = ((Get-Content "$zip.sha256" -Raw).Trim() -split '\s+')[0]
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($actual -ne $expected.ToUpper()) { throw "uv download is corrupted (checksum mismatch)" }
    Expand-Archive $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem $tmp -Recurse -Filter 'uv.exe' | Select-Object -First 1
    if (-not $exe) { throw "uv.exe not found in the download" }
    Copy-Item $exe.FullName (Join-Path $tools 'uv.exe') -Force
    Write-Host "uv downloaded to $tools"
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
