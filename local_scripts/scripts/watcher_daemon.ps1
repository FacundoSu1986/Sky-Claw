# scripts/watcher_daemon.ps1
# Sky-Claw Process Watcher: Monitoriza la salud de los procesos y reinicia según sea necesario.

$RunDir = Join-Path $PSScriptRoot "..\.run"
$SkyClawPidFile = Join-Path $RunDir "skyclaw.pid"
$RestartScript = Join-Path $PSScriptRoot "restart_agent.ps1"

Write-Host "--- Iniciando Watcher de Procesos (Alta Disponibilidad) ---" -ForegroundColor Blue

while ($true) {
    if (Test-Path $SkyClawPidFile) {
        $SavedPid = (Get-Content $SkyClawPidFile).Trim()
        
        if ($SavedPid) {
            $Process = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue
            if (!$Process) {
                Write-Host "\n[ALERT] !!! DAEMON DETECTADO OFFLINE (PID $SavedPid ha caído) !!!" -ForegroundColor Red
                Write-Host "[RECOVERY] Iniciando secuencia de reinicio automático..." -ForegroundColor Yellow
                
                # Ejecutar script de reinicio quirúrgico
                & $RestartScript
            }
        }
    } else {
        # Si no hay PID file, es un arranque en frío necesario
        Write-Host "[INIT] No se detectó archivo de estado. Iniciando arranque en frío..." -ForegroundColor Yellow
        & $RestartScript
    }

    # Intervalo de monitoreo (Cada 2 segundos para alta sensibilidad)
    Start-Sleep -Seconds 2
}
