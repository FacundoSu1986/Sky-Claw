<#
.SYNOPSIS
    Borra ramas obsoletas YA ARCHIVADAS, con guardas fail-closed.

.DESCRIPTION
    No confia en el snapshot MANIFEST.tsv: recomputa el estado EN RUNTIME.
    Una rama SOLO se borra si cumple TODAS estas condiciones:
      - figura en el bundle (esta archivada),
      - el manifiesto la clasifica como MERGED o CLOSED (no NO_PR),
      - NO tiene un PR abierto HOY (gh pr list --state open),
      - NO esta checked-out en ningun worktree (git worktree list),
      - NO es la rama actual ni 'main'.

    Motivo: el manifiesto es un snapshot; una rama listada como obsoleta puede
    tener un PR abierto abierto despues (caso real: #675 sobre la rama
    research/native-parallax-exp-m6-*). Borrar guiandose solo por el snapshot
    borraria la rama de un PR abierto.

    Por defecto corre en DRY-RUN. Agrega -Execute para borrar de verdad.

.PARAMETER Manifest
    Ruta al MANIFEST.tsv. Por defecto, el de la carpeta del script.

.PARAMETER Bundle
    Ruta al .bundle. Por defecto, el mas nuevo de la carpeta del script.

.PARAMETER IncludeRemote
    Ademas de las locales, borra las refs remotas (origin) que pasen las
    guardas. Requiere -Execute. La remota solo se borra si existe en origin.

.PARAMETER Execute
    Sin este switch es dry-run (no borra nada).

.EXAMPLE
    ./delete-branches.ps1
.EXAMPLE
    ./delete-branches.ps1 -Execute
.EXAMPLE
    ./delete-branches.ps1 -Execute -IncludeRemote
#>
[CmdletBinding()]
param(
    [string]$Manifest,
    [string]$Bundle,
    [switch]$IncludeRemote,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'

function Fail { param([string]$Message) throw $Message }

if (-not $Manifest) { $Manifest = Join-Path $PSScriptRoot 'MANIFEST.tsv' }
if (-not $Bundle) {
    $c = Get-ChildItem -Path $PSScriptRoot -Filter 'obsolete-branches-*.bundle' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $c) { Fail "No se encontro ningun bundle en $PSScriptRoot" }
    $Bundle = $c.FullName
}
if (-not (Test-Path $Manifest)) { Fail "No existe el manifiesto: $Manifest" }
if (-not (Test-Path $Bundle)) { Fail "No existe el bundle: $Bundle" }

# --- Fuente de verdad de "esta archivada": los refs del bundle ---
$archived = @{}
foreach ($h in (git bundle list-heads $Bundle)) {
    $ref = ($h -split '\s+')[1]
    $short = $ref -replace '^refs/heads/', '' -replace '^refs/remotes/origin/', ''
    $archived[$short] = $true
}
if ($archived.Count -eq 0) { Fail "El bundle no contiene refs: $Bundle" }

# --- PRs abiertos AHORA (no el snapshot) ---
$openPR = @{}
$openJson = gh pr list --state open --limit 500 --json headRefName | ConvertFrom-Json
foreach ($pr in @($openJson)) {
    if ($pr -and $pr.headRefName) { $openPR[[string]$pr.headRefName] = $true }
}

# --- Ramas checked-out en algun worktree ---
$checkedOut = @{}
foreach ($line in (git worktree list --porcelain)) {
    if ($line -match '^branch refs/heads/(.+)$') { $checkedOut[$Matches[1]] = $true }
}
$current = (git rev-parse --abbrev-ref HEAD)
if ($LASTEXITCODE -ne 0) { Fail 'No se pudo resolver la rama actual' }

# --- Candidatas del manifiesto (MERGED/CLOSED, locales, archivadas) ---
$rows = Get-Content $Manifest | Where-Object { $_ -and $_.Trim() -ne '' -and $_ -notmatch '^#' }
$candidatas = @()
foreach ($r in $rows) {
    $c = $r -split [char]9
    if ($c.Count -lt 6) { continue }
    if ($c[1] -ne 'local') { continue }
    if ($c[2] -notin @('MERGED', 'CLOSED')) { continue }
    if (-not $archived.ContainsKey($c[5])) { continue }
    $candidatas += $c[5]
}
$candidatas = $candidatas | Sort-Object -Unique

Write-Host "Bundle:      $Bundle"
Write-Host "Manifiesto:  $Manifest"
Write-Host "Candidatas (MERGED/CLOSED, locales, archivadas): $($candidatas.Count)"
Write-Host "PRs abiertos hoy: $($openPR.Count) | checked-out: $($checkedOut.Count) | actual: $current"
Write-Host ''

# --- Resolver owner/repo para el borrado remoto ---
$owner = $null; $repo = $null
if ($IncludeRemote) {
    $url = (git remote get-url origin)
    if ($LASTEXITCODE -ne 0) { Fail 'No hay remoto origin' }
    $path = ($url -replace '^.*github\.com[:/]', '') -replace '\.git$', ''
    if ($path -match '^(.+?)/(.+)$') { $owner = $Matches[1]; $repo = $Matches[2] }
    if (-not $owner) { Fail "No pude parsear owner/repo de: $url" }
}

$borradas = 0; $omitidas = 0; $fallidas = 0
foreach ($name in $candidatas) {
    $razon = $null
    if ($name -eq 'main') { $razon = 'es main' }
    elseif ($name -eq $current) { $razon = 'es la rama actual' }
    elseif ($openPR.ContainsKey($name)) { $razon = 'tiene PR ABIERTO hoy' }
    elseif ($checkedOut.ContainsKey($name)) { $razon = 'checked-out en un worktree' }

    if ($razon) {
        Write-Host ("  OMITE   {0,-58} ({1})" -f $name, $razon)
        $omitidas++
        continue
    }

    if (-not $Execute) {
        Write-Host "  [dry-run] borraria $name"
        $borradas++
        continue
    }

    & git branch -D $name
    if ($LASTEXITCODE -ne 0) { Write-Host "  FALLO   $name"; $fallidas++; continue }
    Write-Host "  BORRADA $name"
    $borradas++

    if ($IncludeRemote) {
        $exists = (git ls-remote --heads origin $name)
        if ($LASTEXITCODE -eq 0 -and $exists) {
            gh api -X DELETE "repos/$owner/$repo/git/refs/heads/$name" | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Host "    FALLO remota origin/$name"; $fallidas++ }
            else { Write-Host "    BORRADA remota origin/$name" }
        }
    }
}

Write-Host ''
if ($Execute) {
    Write-Host "Borradas: $borradas | Omitidas por guarda: $omitidas | Fallidas: $fallidas"
} else {
    Write-Host "Dry-run: $borradas corresponden a borrado | $omitidas omitidas por guarda."
    Write-Host 'Correlo con -Execute para aplicar.'
}
