# Fase 4: Garra del Cielo (Frontend Web) - Especificaciones de Arquitectura

Este documento define la integración de la interfaz web con el ecosistema de alta disponibilidad Sky-Claw.

## 1. Topología de Red (Zero Trust)
La UI debe conectarse **exclusivamente** al Puerto de Interfaz del Gateway.
- **WebSocket URL**: `ws://127.0.0.1:18790`
- **Protocolo**: JSON-RPC adaptado.
- **Seguridad**: El frontend debe ignorar cualquier socket en el puerto `18789` (reservado para el daemon).

## 2. Máquina de Estados de la UI (Reactividad)
El frontend debe implementar un observador de mensajes de tipo `STATUS` para gestionar la experiencia de usuario durante fallos del backend.

| Estado Recibido | Acción UI | Indicador Visual |
| :--- | :--- | :--- |
| `[BUFFERING]` | Bloqueo de Input (Read-only) | Overlay "Desvanecimiento Arcano" + Spinner Dorado |
| `[READY]` | Desbloqueo de Input | Animación de Fade-in + Notificación "Conexión Establecida" |

### Pseudo-lógica del Observador (JavaScript)
```javascript
socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'STATUS') {
        if (msg.content.includes('[BUFFERING]')) {
            ui.setLock(true);
            ui.showReconnectingInfo("Daemon reiniciando... No se perderá el comando.");
        } else if (msg.content.includes('[READY]')) {
            ui.setLock(false);
            ui.hideReconnectingInfo();
        }
    }
    // ... procesar respuestas normales del agente
};
```

## 3. Estética: "Terminal Arcana" (Skyrim NextGen)
La UI no es una consola estándar, es un artefacto de orquestación.

### Paleta y Estilo (CSS Variables)
```css
:root {
  --bg-arcane: radial-gradient(circle, #0a0c10 0%, #000000 100%);
  --gold-primary: #d4af37;
  --gold-glow: 0 0 15px rgba(212, 175, 55, 0.4);
  --glass: rgba(255, 255, 255, 0.05);
  --font-main: 'Outfit', sans-serif;
}
```

### Componentes Críticos
1. **The Log of Souls (Output Log)**: Scroll infinito con tipografía dorada y efecto de desdibujado en los bordes.
2. **The Command Altar (Input)**: Campo minimalista con borde dorado animado (`pulse`) cuando el sistema está listo.
3. **The Watcher Lens (Status Bar)**: Indicadores de salud del Daemon, Gateway y Supervisor en la esquina inferior derecha.

## 4. Gestión de Comandos UI -> Gateway
- Cada comando enviado debe tener un `timestamp` y un `UUID`.
- La UI debe mostrar un estado "Enviando..." hasta que reciba el primer ack o evento de procesamiento del daemon.
- Si el estado es `BUFFERING`, la UI debe permitir que el usuario escriba el comando (que se guardará localmente) pero no enviarlo hasta el `READY`.

## 5. Próximos pasos recomendados
- Implementación de un `App.js` basado en Web Components o React/Vue minimalista.
- Uso de `Canvas` para efectos de partículas arcanas en el fondo.
- Integración de `Lucide Icons` (versión minimalista dorada).
