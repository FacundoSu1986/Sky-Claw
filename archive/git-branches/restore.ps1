<#
.SYNOPSIS
    Restaura ramas obsoletas desde el bundle de archivo (archive/git-branches/).

.DESCRIPTION
    El bundle contiene 78 refs (77 obsoletas + main) y es autocontenido, así que
    restaura historial completo sin depender del remoto. Por defecto interpreta
    el bundle mas reciente de la carpeta del script.

.PARAMETER Bundle
    Ruta al archivo .bundle. Si se omite, usa el .bundle mas nuevo de la carpeta.

.PARAMETER Branch
    Nombre corto no ambiguo o ref completo (refs/heads/* o refs/remotes/origin/*).

.PARAMETER All
    Restaura todas las ramas bajo refs/heads/restored/*.

.PARAMETER List
    Solo lista los refs que contiene el bundle.

.PARAMETER Force
    Permite sobrescribir destinos existentes (el refspec se fuerza con +).
    Sin este switch, -Branch aborta si la rama ya existe y -All si un destino difiere.

.EXAMPLE
    ./restore.ps1 -List
.EXAMPLE
    ./restore.ps1 -Branch 'wip/pr503-provenance-fix'
.EXAMPLE
    ./restore.ps1 -All
#>
[CmdletBinding()]
param(
    [string]$Bundle,
    [string]$Branch,
    [switch]$All,
    [switch]$List,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# Ejecuta git y aborta con excepcion si devuelve exit code != 0. Necesario
# porque $ErrorActionPreference NO cubre el exit code de comandos nativos:
# sin esto, un fetch rechazado imprime "Restaurada: X" (fail-open).
function Invoke-Git {
    param([Parameter(Mandatory)][string[]]$GitArgs)
    # git escribe en stderr incluso en EXITO (p.ej. el progreso de fetch). Con
    # $ErrorActionPreference='Stop' eso se convierte en error terminante y un
    # fetch correcto abortaria. Se relaja solo dentro de la funcion y el exito
    # real se decide por el exit code.
    $ErrorActionPreference = 'Continue'
    $out = & git @GitArgs 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $out) { Write-Host $line }
    if ($code -ne 0) {
        throw "git $($GitArgs -join ' ') fallo con exit code $code"
    }
}

if (-not $Bundle) {
    $candidate = Get-ChildItem -Path $PSScriptRoot -Filter 'obsolete-branches-*.bundle' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $candidate) { throw "No se encontro ningun bundle en $PSScriptRoot" }
    $Bundle = $candidate.FullName
}
if (-not (Test-Path $Bundle)) { throw "No existe el bundle: $Bundle" }

# Leer los refs del bundle (falla fuerte si el bundle esta corrupto).
$heads = @(git bundle list-heads $Bundle)
if ($LASTEXITCODE -ne 0) { throw "No se pudo leer el bundle: $Bundle" }
if ($heads.Count -eq 0) { throw "El bundle no contiene refs: $Bundle" }

Write-Host "Bundle: $Bundle"
Write-Host "Refs contenidos ($($heads.Count)):"
foreach ($h in $heads) { Write-Host "  $h" }

if ($List -and -not $All -and -not $Branch) { return }

if ($All) {
    # Restaura TANTO refs/heads/* como refs/remotes/origin/* (estos ultimos a
    # refs/heads/restored/origin/* para no colisionar con los locales).
    $refspecs = @()
    $destinos = @{}
    foreach ($h in $heads) {
        $parts = $h -split '\s+'
        $source = $parts[1]
        if ($source -match '^refs/heads/(.+)$') { $dest = "refs/heads/restored/$($Matches[1])" }
        elseif ($source -match '^refs/remotes/origin/(.+)$') { $dest = "refs/heads/restored/origin/$($Matches[1])" }
        else { continue }
        if ($destinos.ContainsKey($dest) -and $destinos[$dest] -ne $parts[0]) {
            throw "Dos refs del bundle colisionan en $dest"
        }
        $destinos[$dest] = $parts[0]
        $existing = git rev-parse --verify --quiet $dest 2>$null
        if ($LASTEXITCODE -notin @(0, 1)) { throw "No se pudo comprobar $dest" }
        if ($existing -and $existing -ne $parts[0] -and -not $Force) {
            throw "El destino '$dest' tiene otro tip; usa -Force solo si queres sobreescribirlo."
        }
        $prefix = if ($Force) { '+' } else { '' }
        $refspecs += "${prefix}${source}:$dest"
    }
    Invoke-Git -GitArgs (@('fetch', '--atomic', '--no-tags', $Bundle) + $refspecs)
    Write-Host 'Restauradas: refs/heads/restored/* (locales) y, si aplica, refs/heads/restored/origin/* (remotas).'
    Write-Host "Incluye 'restored/main' (ref base del bundle)."
    return
}

if (-not $Branch) {
    throw 'Indica -Branch <nombre>, -All o -List'
}

# Un nombre corto ambiguo no elige el primer tip en silencio: pedir ref completo.
$branchRefs = @()
foreach ($h in $heads) {
    $ref = ($h -split '\s+')[1]
    $short = $ref -replace '^refs/heads/', '' -replace '^refs/remotes/origin/', ''
    if ($ref -eq $Branch -or ($short -eq $Branch -and $Branch -notlike 'refs/*')) { $branchRefs += $ref }
}
if ($branchRefs.Count -eq 0) {
    throw "El bundle no contiene la rama '$Branch'. Usa -List para ver los refs disponibles."
}
if ($branchRefs.Count -gt 1) { throw "Nombre ambiguo '$Branch'; indica uno de estos refs completos: $($branchRefs -join ', ')" }
$match = $branchRefs[0]
$branchName = $match -replace '^refs/heads/', '' -replace '^refs/remotes/origin/', ''

$dest = "refs/heads/$branchName"
# El refspec va forzado con +, asi que sin esta guarda -Branch pisaria en
# silencio una rama local existente.
git show-ref --verify --quiet $dest
if ($LASTEXITCODE -notin @(0, 1)) { throw "No se pudo comprobar $dest" }
$alreadyExists = ($LASTEXITCODE -eq 0)
if ($alreadyExists -and -not $Force) {
    throw "La rama local '$Branch' ya existe. Usa -Force para sobreescribirla."
}
if ($alreadyExists) { Write-Host "AVISO: sobreescribiendo la rama local existente '$Branch' (-Force)." }
$prefix = if ($Force) { '+' } else { '' }
Invoke-Git -GitArgs @('fetch', '--no-tags', $Bundle, "${prefix}${match}:$dest")
Write-Host "Restaurada: $match -> $dest"
