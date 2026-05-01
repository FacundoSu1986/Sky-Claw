<#
.SYNOPSIS
    Script de refactorización de jerarquía de directorios para Sky-Claw.
    Basado en la auditoría "Manuss" y el plan en refactor_plan.md.

.DESCRIPTION
    Realiza las siguientes operaciones de forma segura:
    1. Migración de antigravity/frontend/ -> sky_claw/antigravity/web/static/operations_hub/
    2. Migración de antigravity/gateway/ -> sky_claw/antigravity/comms/telegram_gateway_node/
    3. Migración de antigravity/tests/ -> tests/
    4. Renombramiento de local/ -> local_scripts/
    5. Renombramiento de Skills Python/ -> local_docs/python_optimization/
    6. Renombramiento de .github/copilot-instructions.md -> .github/coding_conventions.md
    7. Renombramiento de gui/event_bus.py -> gui/gui_event_adapter.py
    8. Renombramiento de gui/utils.py -> gui/gui_helpers.py
    9. Actualización de pyproject.toml
    10. Limpieza del directorio antigravity/ fantasma

.NOTES
    Entorno: Windows 10+
    Ejecución: powershell -ExecutionPolicy Bypass -File refactor_execute.ps1
    Usar -DryRun para simulación sin cambios.
#>

# ============================================================================
# CONFIGURACIÓN ESTRICTA
# ============================================================================
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$DryRun = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ============================================================================
# CONSTANTES
# ============================================================================
[string]$REPO_ROOT      = $PSScriptRoot
[string]$LOG_DIR        = Join-Path $REPO_ROOT 'local_logs'
[string]$TIMESTAMP      = Get-Date -Format 'yyyyMMdd_HHmmss'
[string]$LOG_FILE       = Join-Path $LOG_DIR "refactor_${TIMESTAMP}.log"
[string]$BACKUP_MANIFEST = Join-Path $LOG_DIR "manifest_${TIMESTAMP}.json"

# ============================================================================
# MÓDULO DE LOGGING
# ============================================================================
function Write-Log {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('INFO', 'WARN', 'ERROR', 'SUCCESS')]
        [string]$Level,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    [string]$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    [string]$entry = "[$timestamp] [$Level] $Message"

    # Console con color
    switch ($Level) {
        'INFO'    { Write-Host $entry -ForegroundColor Cyan }
        'WARN'    { Write-Host $entry -ForegroundColor Yellow }
        'ERROR'   { Write-Host $entry -ForegroundColor Red }
        'SUCCESS' { Write-Host $entry -ForegroundColor Green }
    }

    # Archivo de log
    try {
        Add-Content -Path $LOG_FILE -Value $entry -Encoding UTF8
    }
    catch {
        Write-Host "[$timestamp] [ERROR] Fallo al escribir log: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# ============================================================================
# HELPERS
# ============================================================================
function Test-DirectorySafe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        Write-Log -Level 'WARN' "Directorio no existe (se creará): $Path"
        return $false
    }
    return $true
}

function Move-SafeDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [Parameter(Mandatory = $false)]
        [string]$Description = ''
    )

    [string]$srcFullPath = Join-Path $REPO_ROOT $Source
    [string]$dstFullPath = Join-Path $REPO_ROOT $Destination

    Write-Log -Level 'INFO' "Moviendo: $Source -> $Destination $Description"

    if ($DryRun) {
        Write-Log -Level 'INFO' "[DRY RUN] Se movería: $srcFullPath -> $dstFullPath"
        return
    }

    try {
        if (-not (Test-Path $srcFullPath)) {
            Write-Log -Level 'ERROR' "Origen no encontrado: $srcFullPath"
            throw "Directorio origen no existe: $srcFullPath"
        }

        # Crear destino si no existe
        [string]$dstParent = Split-Path $dstFullPath -Parent
        if (-not (Test-Path $dstParent)) {
            New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
            Write-Log -Level 'INFO' "Creado directorio padre: $dstParent"
        }

        # Si el destino ya existe, mover contenido internamente
        if (Test-Path $dstFullPath) {
            Write-Log -Level 'WARN' "Destino ya existe, fusionando contenido: $dstFullPath"
            # Copiar contenido recursivamente
            Get-ChildItem -Path $srcFullPath -Recurse | ForEach-Object {
                [string]$relativePath = $_.FullName.Substring($srcFullPath.Length).TrimStart('\')
                [string]$targetPath = Join-Path $dstFullPath $relativePath

                if ($_.PSIsContainer) {
                    if (-not (Test-Path $targetPath)) {
                        New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
                    }
                }
                else {
                    [string]$targetParent = Split-Path $targetPath -Parent
                    if (-not (Test-Path $targetParent)) {
                        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
                    }
                    Copy-Item -Path $_.FullName -Destination $targetPath -Force
                }
            }
            # Eliminar origen después de fusionar
            Remove-Item -Path $srcFullPath -Recurse -Force
            Write-Log -Level 'SUCCESS' "Fusionado y eliminado: $Source -> $Destination"
        }
        else {
            Move-Item -Path $srcFullPath -Destination $dstFullPath -Force
            Write-Log -Level 'SUCCESS' "Movido: $Source -> $Destination"
        }
    }
    catch {
        Write-Log -Level 'ERROR' "Fallo al mover $Source -> $Destination : $($_.Exception.Message)"
        throw
    }
}

function Rename-SafeFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [Parameter(Mandatory = $false)]
        [string]$Description = ''
    )

    [string]$srcFullPath = Join-Path $REPO_ROOT $Source
    [string]$dstFullPath = Join-Path $REPO_ROOT $Destination

    Write-Log -Level 'INFO' "Renombrando: $Source -> $Destination $Description"

    if ($DryRun) {
        Write-Log -Level 'INFO' "[DRY RUN] Se renombraría: $srcFullPath -> $dstFullPath"
        return
    }

    try {
        if (-not (Test-Path $srcFullPath)) {
            Write-Log -Level 'WARN' "Origen no encontrado (skip): $srcFullPath"
            return
        }

        [string]$dstParent = Split-Path $dstFullPath -Parent
        if (-not (Test-Path $dstParent)) {
            New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
        }

        Move-Item -Path $srcFullPath -Destination $dstFullPath -Force
        Write-Log -Level 'SUCCESS' "Renombrado: $Source -> $Destination"
    }
    catch {
        Write-Log -Level 'ERROR' "Fallo al renombrar $Source -> $Destination : $($_.Exception.Message)"
        throw
    }
}

function Rename-SafeDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [Parameter(Mandatory = $false)]
        [string]$Description = ''
    )

    [string]$srcFullPath = Join-Path $REPO_ROOT $Source
    [string]$dstFullPath = Join-Path $REPO_ROOT $Destination

    Write-Log -Level 'INFO' "Renombrando dir: $Source -> $Destination $Description"

    if ($DryRun) {
        Write-Log -Level 'INFO' "[DRY RUN] Se renombraría: $srcFullPath -> $dstFullPath"
        return
    }

    try {
        if (-not (Test-Path $srcFullPath)) {
            Write-Log -Level 'ERROR' "Origen no encontrado: $srcFullPath"
            throw "Directorio origen no existe: $srcFullPath"
        }

        if (Test-Path $dstFullPath) {
            Write-Log -Level 'WARN' "Destino ya existe: $dstFullPath — omitiendo"
            return
        }

        Move-Item -Path $srcFullPath -Destination $dstFullPath -Force
        Write-Log -Level 'SUCCESS' "Renombrado dir: $Source -> $Destination"
    }
    catch {
        Write-Log -Level 'ERROR' "Fallo al renombrar dir $Source -> $Destination : $($_.Exception.Message)"
        throw
    }
}

function Update-FileContent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string]$OldText,

        [Parameter(Mandatory = $true)]
        [string]$NewText,

        [Parameter(Mandatory = $false)]
        [string]$Description = ''
    )

    [string]$fullPath = Join-Path $REPO_ROOT $FilePath

    Write-Log -Level 'INFO' "Actualizando contenido: $FilePath $Description"

    if ($DryRun) {
        Write-Log -Level 'INFO' "[DRY RUN] Se reemplazaría '$OldText' -> '$NewText' en $fullPath"
        return
    }

    try {
        if (-not (Test-Path $fullPath)) {
            Write-Log -Level 'ERROR' "Archivo no encontrado: $fullPath"
            throw "Archivo no existe: $fullPath"
        }

        [string]$content = Get-Content -Path $fullPath -Raw -Encoding UTF8
        [string]$newContent = $content.Replace($OldText, $NewText)

        if ($content -eq $newContent) {
            Write-Log -Level 'WARN' "Sin cambios en: $FilePath (texto no encontrado)"
            return
        }

        Set-Content -Path $fullPath -Value $newContent -Encoding UTF8 -NoNewline
        Write-Log -Level 'SUCCESS' "Actualizado: $FilePath"
    }
    catch {
        Write-Log -Level 'ERROR' "Fallo al actualizar $FilePath : $($_.Exception.Message)"
        throw
    }
}

# ============================================================================
# MANIFIESTO DE OPERACIONES
# ============================================================================
function Export-OperationManifest {
    [CmdletBinding()]
    param()

    [array]$operations = @(
        @{ Step = 1; Type = 'MOVE_DIR'; Source = 'antigravity\frontend'; Destination = 'sky_claw\antigravity\web\static\operations_hub'; Status = 'Pending' },
        @{ Step = 2; Type = 'MOVE_DIR'; Source = 'antigravity\gateway'; Destination = 'sky_claw\antigravity\comms\telegram_gateway_node'; Status = 'Pending' },
        @{ Step = 3; Type = 'MOVE_DIR'; Source = 'antigravity\tests'; Destination = 'tests'; Status = 'Pending' },
        @{ Step = 4; Type = 'RENAME_DIR'; Source = 'local'; Destination = 'local_scripts'; Status = 'Pending' },
        @{ Step = 5; Type = 'RENAME_DIR'; Source = 'Skills Python'; Destination = 'local_docs\python_optimization'; Status = 'Pending' },
        @{ Step = 6; Type = 'RENAME_FILE'; Source = '.github\copilot-instructions.md'; Destination = '.github\coding_conventions.md'; Status = 'Pending' },
        @{ Step = 7; Type = 'RENAME_FILE'; Source = 'sky_claw\antigravity\gui\event_bus.py'; Destination = 'sky_claw\antigravity\gui\gui_event_adapter.py'; Status = 'Pending' },
        @{ Step = 8; Type = 'RENAME_FILE'; Source = 'sky_claw\antigravity\gui\utils.py'; Destination = 'sky_claw\antigravity\gui\gui_helpers.py'; Status = 'Pending' },
        @{ Step = 9; Type = 'UPDATE_FILE'; Source = 'pyproject.toml'; OldText = 'testpaths = ["antigravity/tests"]'; NewText = 'testpaths = ["tests"]'; Status = 'Pending' },
        @{ Step = 10; Type = 'UPDATE_FILE'; Source = 'pyproject.toml'; OldText = 'src = ["sky_claw", "antigravity/tests", "local"]'; NewText = 'src = ["sky_claw", "tests", "local_scripts"]'; Status = 'Pending' },
        @{ Step = 11; Type = 'CLEANUP'; Source = 'antigravity'; Destination = ''; Status = 'Pending' }
    )

    $operations | ConvertTo-Json -Depth 3 | Set-Content -Path $BACKUP_MANIFEST -Encoding UTF8
    Write-Log -Level 'INFO' "Manifiesto guardado: $BACKUP_MANIFEST"
}

# ============================================================================
# EJECUCIÓN PRINCIPAL
# ============================================================================
function Invoke-Refactor {
    [CmdletBinding()]
    param()

    # Crear directorio de logs ANTES de cualquier Write-Log
    if (-not (Test-Path $LOG_DIR)) {
        New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
    }

    Write-Log -Level 'INFO' "============================================"
    Write-Log -Level 'INFO' "INICIO DE REFACTORIZACION - Sky-Claw"
    Write-Log -Level 'INFO' "Repo root: $REPO_ROOT"
    Write-Log -Level 'INFO' "DryRun: $DryRun"
    Write-Log -Level 'INFO' "Log: $LOG_FILE"
    Write-Log -Level 'INFO' "============================================"

    # Guardar manifiesto
    Export-OperationManifest

    # ---------------------------------------------------------------
    # PASO 1: Migrar antigravity/frontend/ -> sky_claw/antigravity/web/static/operations_hub/
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 1: Frontend Operations Hub ---"
    try {
        Move-SafeDirectory `
            -Source 'antigravity\frontend' `
            -Destination 'sky_claw\antigravity\web\static\operations_hub' `
            -Description '(Operations Hub -> web/static/operations_hub)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 1 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 2: Migrar antigravity/gateway/ -> sky_claw/antigravity/comms/telegram_gateway_node/
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 2: Gateway Node.js ---"
    try {
        Move-SafeDirectory `
            -Source 'antigravity\gateway' `
            -Destination 'sky_claw\antigravity\comms\telegram_gateway_node' `
            -Description '(gateway -> comms/telegram_gateway_node)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 2 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 3: Migrar antigravity/tests/ -> tests/
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 3: Suite de Tests ---"
    try {
        Move-SafeDirectory `
            -Source 'antigravity\tests' `
            -Destination 'tests' `
            -Description '(tests -> tests/ raíz)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 3 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 4: Renombrar local/ -> local_scripts/
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 4: Scripts locales ---"
    try {
        Rename-SafeDirectory `
            -Source 'local' `
            -Destination 'local_scripts' `
            -Description '(local -> local_scripts)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 4 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 5: Renombrar Skills Python/ -> local_docs/python_optimization/
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 5: Documentación Python ---"
    try {
        # Mover contenido a nueva estructura
        [string]$srcPath = Join-Path $REPO_ROOT 'Skills Python'
        [string]$dstPath = Join-Path $REPO_ROOT 'local_docs\python_optimization'

        if (Test-Path $srcPath) {
            if (-not $DryRun) {
                New-Item -ItemType Directory -Path $dstPath -Force | Out-Null

                # Copiar archivos
                Get-ChildItem -Path $srcPath -File | ForEach-Object {
                    Copy-Item -Path $_.FullName -Destination $dstPath -Force
                }

                # Eliminar original
                Remove-Item -Path $srcPath -Recurse -Force
                Write-Log -Level 'SUCCESS' "Migrado: 'Skills Python/' -> 'local_docs/python_optimization/'"
            }
            else {
                Write-Log -Level 'INFO' "[DRY RUN] Se migraría: 'Skills Python/' -> 'local_docs/python_optimization/'"
            }
        }
        else {
            Write-Log -Level 'WARN' "'Skills Python/' no encontrado — omitiendo"
        }
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 5 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 6: Renombrar .github/copilot-instructions.md -> .github/coding_conventions.md
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 6: Convenciones de codificación ---"
    try {
        Rename-SafeFile `
            -Source '.github\copilot-instructions.md' `
            -Destination '.github\coding_conventions.md' `
            -Description '(copilot-instructions -> coding_conventions)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 6 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 7: Renombrar gui/event_bus.py -> gui/gui_event_adapter.py
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 7: GUI event_bus -> gui_event_adapter ---"
    try {
        Rename-SafeFile `
            -Source 'sky_claw\antigravity\gui\event_bus.py' `
            -Destination 'sky_claw\antigravity\gui\gui_event_adapter.py' `
            -Description '(event_bus -> gui_event_adapter)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 7 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 8: Renombrar gui/utils.py -> gui/gui_helpers.py
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 8: GUI utils -> gui_helpers ---"
    try {
        Rename-SafeFile `
            -Source 'sky_claw\antigravity\gui\utils.py' `
            -Destination 'sky_claw\antigravity\gui\gui_helpers.py' `
            -Description '(utils -> gui_helpers)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 8 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 9-10: Actualizar pyproject.toml
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 9-10: Actualizando pyproject.toml ---"
    try {
        Update-FileContent `
            -FilePath 'pyproject.toml' `
            -OldText 'testpaths = ["antigravity/tests"]' `
            -NewText 'testpaths = ["tests"]' `
            -Description '(testpaths: antigravity/tests -> tests)'

        Update-FileContent `
            -FilePath 'pyproject.toml' `
            -OldText 'src = ["sky_claw", "antigravity/tests", "local"]' `
            -NewText 'src = ["sky_claw", "tests", "local_scripts"]' `
            -Description '(ruff src paths)'
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 9-10 FALLIDO: $($_.Exception.Message)"
        return
    }

    # ---------------------------------------------------------------
    # PASO 11: Limpieza — eliminar antigravity/ fantasma si está vacío
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "--- PASO 11: Limpieza antigravity/ fantasma ---"
    try {
        [string]$phantomPath = Join-Path $REPO_ROOT 'antigravity'

        if (Test-Path $phantomPath) {
            [array]$remaining = Get-ChildItem -Path $phantomPath -Recurse -Force
            if ($remaining.Count -eq 0) {
                if (-not $DryRun) {
                    Remove-Item -Path $phantomPath -Recurse -Force
                    Write-Log -Level 'SUCCESS' "Eliminado directorio fantasma: antigravity/"
                }
                else {
                    Write-Log -Level 'INFO' "[DRY RUN] Se eliminaría: antigravity/ (vacío)"
                }
            }
            else {
                Write-Log -Level 'WARN' "antigravity/ NO está vacío — quedan $($remaining.Count) elementos:"
                $remaining | Select-Object -First 10 | ForEach-Object {
                    Write-Log -Level 'WARN' "  -> $($_.FullName.Replace($REPO_ROOT, '.'))"
                }
                Write-Log -Level 'WARN' "Revisión manual requerida antes de eliminar."
            }
        }
        else {
            Write-Log -Level 'INFO' "antigravity/ ya no existe — nada que limpiar."
        }
    }
    catch {
        Write-Log -Level 'ERROR' "PASO 11 FALLIDO: $($_.Exception.Message)"
    }

    # ---------------------------------------------------------------
    # RESUMEN FINAL
    # ---------------------------------------------------------------
    Write-Log -Level 'INFO' "============================================"
    Write-Log -Level 'INFO' "REFACTORIZACIÓN COMPLETADA"
    Write-Log -Level 'INFO' "Log: $LOG_FILE"
    Write-Log -Level 'INFO' "Manifiesto: $BACKUP_MANIFEST"
    Write-Log -Level 'INFO' "============================================"
    Write-Log -Level 'WARN' "ACCIÓN POSTERIOR REQUERIDA:"
    Write-Log -Level 'WARN' "  1. Actualizar imports en sky_claw/antigravity/gui/*.py"
    Write-Log -Level 'WARN' "     - event_bus -> gui_event_adapter"
    Write-Log -Level 'WARN' "     - utils -> gui_helpers"
    Write-Log -Level 'WARN' "  2. Actualizar rutas estáticas en sky_claw/antigravity/web/app.py"
    Write-Log -Level 'WARN' "  3. Ejecutar: python -m pytest tests/ --tb=short"
    Write-Log -Level 'WARN' "  4. Ejecutar: ruff check sky_claw/ tests/"
    Write-Log -Level 'WARN' "  5. Revisar y commitear cambios"
}

# ============================================================================
# ENTRY POINT
# ============================================================================
try {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Magenta
    Write-Host "║  Sky-Claw — Refactorización de Jerarquía (Manuss)      ║" -ForegroundColor Magenta
    Write-Host "║  Modo: $(if ($DryRun) {'SIMULACIÓN (DryRun)'} else {'EJECUCIÓN REAL'})                           ║" -ForegroundColor $(if ($DryRun) {'Yellow'} else {'Red'})
    Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Magenta
    Write-Host ""

    if (-not $DryRun) {
        [string]$confirm = Read-Host "¿Confirmar ejecución REAL? (escribir 'SI' para continuar)"
        if ($confirm -ne 'SI') {
            Write-Host "Operación cancelada por el usuario." -ForegroundColor Yellow
            exit 0
        }
    }

    Invoke-Refactor
}
catch {
    Write-Log -Level 'ERROR' "EXCEPCIÓN NO CONTROLADA: $($_.Exception.Message)"
    Write-Log -Level 'ERROR' "StackTrace: $($_.ScriptStackTrace)"
    exit 1
}
