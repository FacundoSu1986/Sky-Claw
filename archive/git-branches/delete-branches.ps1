<#
.SYNOPSIS
    Borra ramas obsoletas YA ARCHIVADAS, con guardas fail-closed.

.DESCRIPTION
    No confia en el snapshot MANIFEST.tsv: recomputa el estado EN RUNTIME.
    Una rama SOLO se borra si cumple TODAS estas condiciones:
      - figura en el bundle (esta archivada),
      - cada tip a borrar coincide con el SHA archivado de esa superficie,
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

# Ejecuta un comando nativo devolviendo su exit code y su salida, sin que el
# stderr se convierta en error terminante. Necesario porque las guardas criticas
# decides por exit code, y porque git/gh escriben en stderr incluso en exito.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$NativeArgs
    )
    $ErrorActionPreference = 'Continue'
    $out = & $Exe @NativeArgs 2>&1
    [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}

if (-not $Manifest) { $Manifest = Join-Path $PSScriptRoot 'MANIFEST.tsv' }
if (-not $Bundle) {
    $c = Get-ChildItem -Path $PSScriptRoot -Filter 'obsolete-branches-*.bundle' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $c) { Fail "No se encontro ningun bundle en $PSScriptRoot" }
    $Bundle = $c.FullName
}
if (-not (Test-Path $Manifest)) { Fail "No existe el manifiesto: $Manifest" }
if (-not (Test-Path $Bundle)) { Fail "No existe el bundle: $Bundle" }

# --- Fuente de verdad: ref COMPLETO y SHA; local y origin pueden diferir ---
$archived = @{}
$headsRes = Invoke-Native 'git' @('bundle', 'list-heads', $Bundle)
if ($headsRes.Code -ne 0) { Fail "No se pudo leer el bundle: $Bundle" }
foreach ($h in @($headsRes.Out)) {
    $parts = "$h" -split '\s+'
    if ($parts.Count -ne 2 -or $parts[0] -notmatch '^[0-9a-f]{40,64}$') { Fail 'Ref invalida en el bundle' }
    $archived[$parts[1]] = $parts[0]
}
if ($archived.Count -eq 0) { Fail "El bundle no contiene refs: $Bundle" }

# --- PRs abiertos AHORA (no el snapshot) ---
# FAIL-CLOSED: si gh falla no se puede saber si hay PRs abiertos, y seguir con
# $openPR vacio desactivaria la guarda critica en silencio. $ErrorActionPreference
# no cubre el exit code de comandos nativos, asi que hay que mirarlo a mano.
$openPR = @{}
$ghRes = Invoke-Native 'gh' @('pr', 'list', '--state', 'open', '--limit', '500', '--json', 'headRefName')
if ($ghRes.Code -ne 0) {
    Fail "gh pr list fallo (exit $($ghRes.Code)): no se pueden verificar los PRs abiertos. Aborto sin borrar nada (fail-closed)."
}
$openRaw = (@($ghRes.Out) -join "`n").Trim()
if (-not $openRaw.StartsWith('[')) { Fail 'gh pr list no devolvio una lista valida' }
$openJson = $openRaw | ConvertFrom-Json
if (@($openJson).Count -ge 500) { Fail 'Lista de PRs posiblemente truncada; aborto sin borrar' }
foreach ($pr in @($openJson)) {
    if ($pr -and $pr.headRefName) { $openPR[[string]$pr.headRefName] = $true }
}

# --- Ramas checked-out en algun worktree ---
$checkedOut = @{}
$wtRes = Invoke-Native 'git' @('worktree', 'list', '--porcelain')
if ($wtRes.Code -ne 0) {
    Fail "git worktree list fallo (exit $($wtRes.Code)): no se pueden verificar las ramas checked-out. Aborto (fail-closed)."
}
foreach ($line in @($wtRes.Out)) {
    if ("$line" -match '^branch refs/heads/(.+)$') { $checkedOut[$Matches[1]] = $true }
}
$curRes = Invoke-Native 'git' @('rev-parse', '--abbrev-ref', 'HEAD')
if ($curRes.Code -ne 0) { Fail 'No se pudo resolver la rama actual' }
$current = (@($curRes.Out) -join '').Trim()

# --- Candidatas del manifiesto (MERGED/CLOSED, archivadas) ---
# Se incluyen las filas 'remote' a proposito: hay 23 refs que existen SOLO en el
# remoto (sin rama local), y mirando solo 'local' -IncludeRemote nunca las cubria.
$rows = Get-Content $Manifest | Where-Object { $_ -and $_.Trim() -ne '' -and $_ -notmatch '^#' }
$candidatas = @{}
foreach ($r in $rows) {
    $c = $r -split [char]9
    if ($c.Count -lt 6) { continue }
    if ($c[2] -notin @('MERGED', 'CLOSED')) { continue }
    if (-not $archived.ContainsKey("refs/heads/$($c[5])") -and
        -not $archived.ContainsKey("refs/remotes/origin/$($c[5])")) { continue }
    $candidatas[$c[5]] = $true
}
$candidatas = @($candidatas.Keys | Sort-Object)

# --- Ramas locales hoy (para no intentar borrar lo que ya no existe) ---
$localBranches = @{}
$brRes = Invoke-Native 'git' @('branch', '--format=%(refname:short) %(objectname)')
if ($brRes.Code -ne 0) { Fail "git branch fallo (exit $($brRes.Code)). Aborto (fail-closed)." }
foreach ($b in @($brRes.Out)) {
    if ("$b") { $parts = "$b" -split '\s+'; $localBranches[$parts[0]] = $parts[1] }
}

Write-Host "Bundle:      $Bundle"
Write-Host "Manifiesto:  $Manifest"
Write-Host "Candidatas (MERGED/CLOSED, archivadas): $($candidatas.Count)"
Write-Host "PRs abiertos hoy: $($openPR.Count) | checked-out: $($checkedOut.Count) | actual: $current"
Write-Host ''

$borradas = 0; $omitidas = 0; $fallidas = 0; $wouldDelete = 0
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

    $tieneLocal = $localBranches.ContainsKey($name)
    $tieneRemota = $false
    $remoteSha = $null
    if ($IncludeRemote) {
        $lsRes = Invoke-Native 'git' @('ls-remote', '--heads', 'origin', "refs/heads/$name")
        if ($lsRes.Code -ne 0) {
            Write-Host ("  FALLO   {0,-58} (ls-remote origin)" -f $name)
            $fallidas++
            continue
        }
        $remoteLines = @($lsRes.Out | Where-Object { "$_" -match "\s$([regex]::Escape("refs/heads/$name"))$" })
        $tieneRemota = $remoteLines.Count -gt 0
        if ($tieneRemota) { $remoteSha = ("$($remoteLines[0])" -split '\s+')[0] }
    }

    if (($tieneLocal -and $localBranches[$name] -ne $archived["refs/heads/$name"]) -or
        ($tieneRemota -and $remoteSha -ne $archived["refs/remotes/origin/$name"])) {
        Write-Host ("  OMITE   {0,-58} (tip cambio tras el archivo o superficie no archivada)" -f $name)
        $omitidas++
        continue
    }

    if (-not $Execute) {
        $que = @()
        if ($tieneLocal) { $que += 'local' }
        if ($tieneRemota) { $que += 'origin' }
        $algo = ($que.Count -gt 0)
        if (-not $algo) { $que += 'nada que borrar' }
        $verbo = 'omitida'
        if ($algo) { $verbo = 'borraria'; $wouldDelete++ } else { $omitidas++ }
        Write-Host ("  [dry-run] {0,-9} {1,-46} ({2})" -f $verbo, $name, ($que -join '+'))
        continue
    }

    if ($tieneLocal) {
        # Compare-and-delete: si otro escritor avanzo el tip desde la lectura,
        # update-ref rechaza el borrado bajo el lock de la ref.
        $delRes = Invoke-Native 'git' @('update-ref', '-d', "refs/heads/$name", $localBranches[$name])
        if ($delRes.Code -ne 0) { Write-Host ("  FALLO   {0,-58} (local)" -f $name); $fallidas++; continue }
        Write-Host ("  BORRADA {0,-58} (local)" -f $name)
        $borradas++
    }
    if ($tieneRemota) {
        # La API DELETE no acepta SHA esperado. La lease explicita del protocolo
        # git conserva tambien los pushes concurrentes posteriores a ls-remote.
        $apiRes = Invoke-Native 'git' @('push', "--force-with-lease=refs/heads/${name}:$remoteSha", 'origin', ":refs/heads/$name")
        if ($apiRes.Code -ne 0) { Write-Host ("    FALLO {0,-56} (origin)" -f $name); $fallidas++ }
        else { Write-Host ("    BORRADA {0,-56} (origin)" -f $name); $borradas++ }
    }
}

Write-Host ''
if ($Execute) {
    Write-Host "Borradas: $borradas | Omitidas por guarda: $omitidas | Fallidas: $fallidas"
    if ($fallidas -gt 0) { exit 1 }
} else {
    Write-Host "Dry-run: $wouldDelete a borrar | $omitidas omitidas por guarda."
    Write-Host 'Correlo con -Execute para aplicar.'
}
