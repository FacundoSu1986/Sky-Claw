# scripts/restart_agent.ps1
# Ecosistema Sky-Claw: Script de Reinicio con Aislamiento de Procesos (PID Management)

# Configuración de rutas de estado
$RunDir = Join-Path $PSScriptRoot "..\.run"
$GatewayPidFile = Join-Path $RunDir "gateway.pid"
$SkyClawPidFile = Join-Path $RunDir "skyclaw.pid"
$SupervisorPidFile = Join-Path $RunDir "supervisor.pid"

# Asegurar que el directorio de estado existe
if (!(Test-Path $RunDir)) {
    New-Item -ItemType Directory -Path $RunDir | Out-Null
    Write-Host "[STATE] Directorio .run creado." -ForegroundColor Gray
}

# Función Quirúrgica para detener procesos por PID
function Stop-SurgicalProcess {
    param([string]$PidFile, [string]$Name)
    
    if (Test-Path $PidFile) {
        if (Test-Path $PidFile) {
            $SavedPid = Get-Content $PidFile -Raw
            $SavedPid = $SavedPid.Trim()
            
            if ([string]::IsNullOrWhiteSpace($SavedPid)) {
                Write-Host "[CLEANUP] Archivo $Name.pid vacío. Eliminando..." -ForegroundColor Yellow
                Remove-Item $PidFile
                return
            }

            $Process = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue
            if ($Process) {
                Write-Host "[STOP] Deteniendo $Name (PID: $SavedPid)..." -ForegroundColor Cyan
                Stop-Process -Id $SavedPid -Force -ErrorAction SilentlyContinue
                # Pequeña espera para asegurar liberación de recursos
                Start-Sleep -Milliseconds 800 
            } else {
                Write-Host "[RESOLVE] El proceso $Name (PID: $SavedPid) ya no existe. Limpiando binario de estado..." -ForegroundColor Gray
            }
            
            Remove-Item $PidFile -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "--- Iniciando Orquestación de Alta Disponibilidad ---" -ForegroundColor Blue

# 1. Limpieza Quirúrgica Basada en Estado
Stop-SurgicalProcess -PidFile $SkyClawPidFile -Name "Sky-Claw Agent"
Stop-SurgicalProcess -PidFile $SupervisorPidFile -Name "SupervisorAgent"
Stop-SurgicalProcess -PidFile $GatewayPidFile -Name "Gateway (Node.js)"

# 2. Arranque del Gateway (Node.js 24)
Write-Host "[START] Levantando Gateway en Node.js..." -ForegroundColor Green
$GatewayProc = Start-Process node -ArgumentList "$PSScriptRoot\..\gateway\server.js" -NoNewWindow -PassThru -ErrorAction SilentlyContinue

if (!$GatewayProc) {
    Write-Host "[CRITICAL] No se pudo iniciar Node.js. Asegúrate de tener Node.js 24+ en el PATH." -ForegroundColor Red
    exit 1
}

$GatewayProc.Id | Out-File $GatewayPidFile -Encoding ascii
Write-Host "[OK] Gateway iniciado con PID: $($GatewayProc.Id)" -ForegroundColor Gray

# 3. Esperar a que el IPC port (18789) esté listo
Write-Host "[WAIT] Esperando a que el puerto IPC (18789) esté a la escucha..." -ForegroundColor Yellow
$WaitCount = 0
$PortReady = $false
while (!$PortReady -and $WaitCount -lt 10) {
    $Test = Test-NetConnection -ComputerName 127.0.0.1 -Port 18789 -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($Test) {
        $PortReady = $true
        Write-Host "[READY] Puerto 18789 detectado. Iniciando Daemon de Python." -ForegroundColor Cyan
    } else {
        Start-Sleep -Seconds 1
        $WaitCount++
    }
}

if (!$PortReady) {
    Write-Host "[ERROR] El Gateway no respondió en el puerto 18789 tras 10s. Abortando arranque de Python." -ForegroundColor Red
    exit 1
}

# 4. Arranque del Agente Principal (Modo Telegram)
Write-Host "[START] Levantando Sky-Claw Agent..." -ForegroundColor Green
$AgentProc = Start-Process python -ArgumentList "-m sky_claw --mode telegram" -NoNewWindow -PassThru
if ($AgentProc) {
    $AgentProc.Id | Out-File $SkyClawPidFile -Encoding ascii
    Write-Host "[OK] Sky-Claw Agent iniciado con PID: $($AgentProc.Id)" -ForegroundColor Gray
}

# 5. Arranque del SupervisorAgent (Daemon Background)
Write-Host "[START] Levantando SupervisorAgent..." -ForegroundColor Green
$SuperProc = Start-Process python -ArgumentList "-m sky_claw.orchestrator.supervisor" -NoNewWindow -PassThru
if ($SuperProc) {
    $SuperProc.Id | Out-File $SupervisorPidFile -Encoding ascii
    Write-Host "[OK] SupervisorAgent iniciado con PID: $($SuperProc.Id)" -ForegroundColor Gray
}

Write-Host "--- Orquestación Completada con Éxito ---" -ForegroundColor Blue
