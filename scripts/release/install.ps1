# Generated for one release. Supports Windows PowerShell 5.1 and PowerShell 7.
param([string] $Destination = '')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$release = '@TAG@'
$baseUrl = '@BASE_URL@'
# PROCESSOR_ARCHITEW6432 reveals the native CPU from a 32-bit PowerShell.
$architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($architecture -ne 'AMD64') { throw "Unsupported Windows architecture: $architecture. An x86_64 PC is required." }
# Do not resolve tar through PATH: MSYS2/Git tar treats drive letters as hosts.
$systemDirectory = if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) { 'Sysnative' } else { 'System32' }
$tarExecutable = Join-Path $env:SystemRoot "$systemDirectory\tar.exe"
if (-not (Test-Path -LiteralPath $tarExecutable -PathType Leaf)) { throw 'Windows tar.exe is required (Windows 10 or newer).' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$name = 'eclipse-recoil-windows-x86_64'
$archive = "$name.zip"
if (-not $Destination) { $Destination = Join-Path (Get-Location).Path "eclipse-recoil-$release-windows-x86_64" }
$Destination = [IO.Path]::GetFullPath($Destination)
$cache = Join-Path $Destination '.downloads'
$staging = Join-Path $Destination '.extracting'
$game = Join-Path $Destination 'game'
if (Test-Path -LiteralPath $game) { throw "Already installed: $game. Choose a new destination." }
if (Test-Path -LiteralPath $staging) { throw "Temporary extraction directory exists: $staging" }
New-Item -ItemType Directory -Force -Path $cache | Out-Null
function Get-Download([string] $Name, [string] $Path) {
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/$Name" -OutFile "$Path.partial"
            Move-Item -LiteralPath "$Path.partial" -Destination $Path -Force
            return
        } catch {
            if ($attempt -eq 4) { throw }
            Start-Sleep -Seconds 2
        }
    }
}
function Get-Digest([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
try {
    Write-Host "Eclipse Recoil $release - Windows / x86_64"
    Write-Host "Destination: $game"
    Write-Host 'Downloading file list...'
    $manifest = Join-Path $cache "$name.files.sha256"
    Get-Download "$name.files.sha256" $manifest
    $files = [Collections.Generic.List[object]]::new()
    $parts = [Collections.Generic.List[string]]::new()
    $seen = @{}
    $whole = $false
    foreach ($line in Get-Content -LiteralPath $manifest) {
        if ($line -cnotmatch '^([a-fA-F0-9]{64})  (\S+)$') { throw 'Invalid checksum manifest.' }
        $digest = $Matches[1].ToLowerInvariant()
        $file = $Matches[2]
        if ($seen.ContainsKey($file)) { throw "Duplicate file: $file" }
        $seen[$file] = $true
        if ($file -ceq $archive) { $whole = $true }
        elseif ($file -cmatch ('^' + [regex]::Escape($archive) + '\.[0-9]{3}$')) {
            $expected = '{0}.{1:000}' -f $archive, ($parts.Count + 1)
            if ($file -cne $expected) { throw 'Missing or unordered archive parts.' }
            $parts.Add($file)
        } elseif ($file -cne "$name-extract.ps1" -and $file -cne "$name-extract.bat") {
            throw "Unexpected file in manifest: $file"
        }
        $files.Add(@{Name = $file; Digest = $digest})
    }
    if ($whole -and $parts.Count -gt 0) { throw 'Manifest mixes a complete archive and split parts.' }
    if (-not $whole -and $parts.Count -eq 0) { throw 'No archive in manifest.' }
    foreach ($file in $files) {
        $target = Join-Path $cache $file.Name
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Get-Digest $target) -eq $file.Digest) {
            Write-Host "Using verified download: $($file.Name)"
        } else {
            Write-Host "Downloading: $($file.Name)"
            Get-Download $file.Name $target
            if ((Get-Digest $target) -ne $file.Digest) { throw "Checksum mismatch: $($file.Name). Run the installer again to retry." }
        }
    }
    if ($whole) { $sourceArchive = Join-Path $cache $archive }
    else {
        Write-Host 'Joining archive parts...'
        $sourceArchive = Join-Path $cache "joined-$archive"
        $output = [IO.File]::Open($sourceArchive, [IO.FileMode]::Create)
        try {
            foreach ($part in $parts) {
                $inputStream = [IO.File]::OpenRead((Join-Path $cache $part))
                try { $inputStream.CopyTo($output) } finally { $inputStream.Dispose() }
            }
        } finally { $output.Dispose() }
    }
    Write-Host 'Extracting game...'
    New-Item -ItemType Directory -Path $staging | Out-Null
    & $tarExecutable -xf $sourceArchive -C $staging
    if ($LASTEXITCODE -ne 0) { throw 'Archive extraction failed.' }
    $launcher = Join-Path $staging "$name\Eclipse Recoil.bat"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'The archive does not contain the expected game launcher.' }
    Move-Item -LiteralPath $staging -Destination $game
    Remove-Item -LiteralPath $cache -Recurse -Force
    Write-Host "Ready! Open: $game\$name\Eclipse Recoil.bat"
} finally {
    if (Test-Path -LiteralPath $cache) {
        Get-ChildItem -LiteralPath $cache -Filter '*.partial' | Remove-Item -Force
        $joined = Join-Path $cache "joined-$archive"
        if (Test-Path -LiteralPath $joined) { Remove-Item -LiteralPath $joined -Force }
    }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
