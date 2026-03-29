# scripts/run_chaos_suite.ps1
# Master Chaos Suite: Automatización integral de la prueba de resiliencia Sky-Claw.

$RunDir = Join-Path $PSScriptRoot "..\.run"
$RestartScript = Join-Path $PSScriptRoot "restart_agent.ps1"
$WatcherScript = Join-Path $PSScriptRoot "watcher_daemon.ps1"
$ChaosTest = Join-Path $PSScriptRoot "chaos_test.js"

Write-Host "--- Iniciando Suite de Validación de Alta Disponibilidad ---" -ForegroundColor Blue

# 1. Preparación: Limpieza y arranque fresco
Write-Host "[PREP] Limpiando entorno y reiniciando stack..." -ForegroundColor Gray
& $RestartScript

# 2. Iniciar el Watcher en Background
Write-Host "[START] Iniciando Watcher Daemon en segundo plano..." -ForegroundColor Green
$WatcherJob = Start-Process powershell -ArgumentList "-File $WatcherScript" -WindowStyle Hidden -PassThru

# 3. Ejecutar la Prueba de Chaos y Capturar Resultados
Write-Host "[TEST] Ejecutando Chaos Test (10 req/s + Crash Inyectado)..." -ForegroundColor Yellow
$TestResult = node $ChaosTest | Tee-Object -Variable Output

# 4. Análisis de Resultados (Heurística de Validación)
Write-Host "\n--- Análisis de Resultados ---" -ForegroundColor Blue

$HasBuffering = $Output | Select-String "Detectado estado de BUFFERING"
$HasRecovery = $Output | Select-String "RECUPERACIÓN DETECTADA"

if ($HasBuffering -and $HasRecovery) {
    Write-Host "[PASS] Arquitectura de Resiliencia Validada: Gateway retuvo mensajes y el Watcher recuperó el Daemon." -ForegroundColor Green
} else {
    Write-Host "[FAIL] Fallo en la cadena de resiliencia." -ForegroundColor Red
    if (!$HasBuffering) { Write-Host "  - Error: El Gateway no reportó buffering." -ForegroundColor Red }
    if (!$HasRecovery) { Write-Host "  - Error: El Daemon no se recuperó a tiempo." -ForegroundColor Red }
}

# 5. Teardown Limpio (Clean Exit)
Write-Host "\n[TEARDOWN] Ejecutando limpieza quirúrgica final..." -ForegroundColor Gray

# Matar el Watcher Job primero para que no intente reiniciar nada durante el apagado
if ($WatcherJob) { Stop-Process -Id $WatcherJob.Id -Force -ErrorAction SilentlyContinue }

# Usar el restart_agent para limpiar los PIDs de los daemons
# (Detendremos todo ignorando el paso de arranque)
function Stop-All {
    $Files = @("gateway.pid", "skyclaw.pid", "supervisor.pid")
    foreach ($f in $Files) {
        $p = Join-Path $RunDir $f
        if (Test-Path $p) {
            $pidVal = (Get-Content $p).Trim()
            if ($pidVal) {
                Write-Host "[STOP] Limpiando $f (PID $pidVal)..." -ForegroundColor Cyan
                Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
            }
            Remove-Item $p -ErrorAction SilentlyContinue
        }
    }
}

Stop-All

Write-Host "--- Suite Finalizada. Sistema en Reposo. ---" -ForegroundColor Blue
