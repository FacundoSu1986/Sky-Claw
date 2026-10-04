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
    Nombre de la rama a restaurar (tal cual figura en MANIFEST.tsv).

.PARAMETER All
    Restaura todas las ramas bajo refs/heads/restored/*.

.PARAMETER List
    Solo lista los refs que contiene el bundle.

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
    [switch]$List
)

$ErrorActionPreference = 'Stop'

# Ejecuta git y aborta con excepcion si devuelve exit code != 0. Necesario
# porque $ErrorActionPreference NO cubre el exit code de comandos nativos:
# sin esto, un fetch rechazado imprime "Restaurada: X" (fail-open).
function Invoke-Git {
    param([Parameter(Mandatory)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') fallo con exit code $LASTEXITCODE"
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
    $refspecs = @('+refs/heads/*:refs/heads/restored/*')
    if ($heads | Where-Object { $_ -match '\srefs/remotes/' }) {
        $refspecs += '+refs/remotes/origin/*:refs/heads/restored/origin/*'
    }
    Invoke-Git -GitArgs (@('fetch', '--no-tags', $Bundle) + $refspecs)
    Write-Host 'Restauradas: refs/heads/restored/* (locales) y, si aplica, refs/heads/restored/origin/* (remotas).'
    Write-Host "Incluye 'restored/main' (ref base del bundle)."
    return
}

if (-not $Branch) {
    throw 'Indica -Branch <nombre>, -All o -List'
}

# Resolver el ref REAL por nombre corto contra los heads del bundle: asi
# funciona tambien para refs que existen solo como refs/remotes/origin/*.
$match = $null
foreach ($h in $heads) {
    $ref = ($h -split '\s+')[1]
    $short = $ref -replace '^refs/heads/', '' -replace '^refs/remotes/origin/', ''
    if ($short -eq $Branch) { $match = $ref; break }
}
if (-not $match) {
    throw "El bundle no contiene la rama '$Branch'. Usa -List para ver los refs disponibles."
}

$dest = "refs/heads/$Branch"
Invoke-Git -GitArgs @('fetch', '--no-tags', $Bundle, "+${match}:$dest")
Write-Host "Restaurada: $match -> $dest"
