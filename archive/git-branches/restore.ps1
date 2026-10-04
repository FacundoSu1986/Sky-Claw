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

if (-not $Bundle) {
    $candidate = Get-ChildItem -Path $PSScriptRoot -Filter 'obsolete-branches-*.bundle' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $candidate) { throw "No se encontro ningun bundle en $PSScriptRoot" }
    $Bundle = $candidate.FullName
}
if (-not (Test-Path $Bundle)) { throw "No existe el bundle: $Bundle" }

Write-Host "Bundle: $Bundle"
Write-Host 'Refs contenidos:'
git bundle list-heads $Bundle | ForEach-Object { Write-Host "  $_" }

if ($List -and -not $All -and -not $Branch) { return }

if ($All) {
    git fetch --no-tags $Bundle 'refs/heads/*:refs/heads/restored/*'
    Write-Host 'Todas las ramas restauradas en refs/heads/restored/*'
    return
}

if (-not $Branch) {
    throw 'Indica -Branch <nombre>, -All o -List'
}

# Usa format-string para evitar problemas de escaping con el ':' del refspec.
git fetch --no-tags $Bundle ('refs/heads/{0}:refs/heads/{0}' -f $Branch)
Write-Host "Restaurada: $Branch"
