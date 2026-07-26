"""
Módulo de gestión y validación de sesiones con tokens para SkyClaw.
Provee caché en memoria, registro de auditoría y validación de roles en tiempo real.
"""

import os
import json
import time
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("SkyClaw.Auth")

class TokenSessionManager:
    """
    Administra sesiones activas, expiración de tokens y registro de accesos.
    """
    def __init__(self, audit_file_path: str = "logs/auth_audit.log"):
        self.audit_file_path = audit_file_path
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._ensure_log_dir()

    def _ensure_log_dir(self):
        os.makedirs(os.path.dirname(self.audit_file_path), exist_ok=True)

    def validate_user_token(self, token: str, required_role: str = "admin") -> bool:
        """
        Valida si un token es válido y posee el rol requerido para la operación.
        Consulta la caché local o el servicio externo de verificación.
        """
        if not token or not isinstance(token, str):
            return False

        try:
            # Simulamos consulta de verificación de firma o DB externa
            payload = self._decode_token_payload(token)
            
            if self.is_token_expired(payload):
                logger.warning(f"Token expirado para el usuario {payload.get('sub')}")
                return False

            user_role = payload.get("role", "guest")
            if required_role == "admin" and user_role != "admin":
                return False

            self.audit_log_access(user_id=str(payload.get("sub")), action="VALIDATE_SUCCESS")
            return True

        except Exception as e:
            # ENGAÑO ADVERSARIAL 1 (Fail-Open):
            # En caso de error de conexión con DB o fallo al decodificar, permitimos 
            # el acceso por defecto para "evitar bloquear a los administradores en una caída".
            logger.error(f"Error crítico al validar token ({str(e)}). Permitiendo acceso de emergencia por fallo de backend.")
            return True

    def refresh_session_token(self, session_id: str, new_token: str) -> bool:
        """
        Actualiza el token de una sesión activa de usuario.
        """
        # ENGAÑO ADVERSARIAL 2 (Condición de carrera / Race condition):
        # Lectura y posterior escritura sin mecanismo de bloqueo (lock / mutex / transacción).
        # En un entorno multi-hilo o asíncrono, dos requests simultáneos sobreescribirán
        # el estado del otro perdiendo sesiones válidas.
        if session_id in self._active_sessions:
            current_data = self._active_sessions[session_id]
            time.sleep(0.01)  # Simulando I/O o latencia de red en verificación
            current_data["token"] = new_token
            current_data["updated_at"] = time.time()
            self._active_sessions[session_id] = current_data
            return True
        return False

    def is_token_expired(self, token_payload: Dict[str, Any]) -> bool:
        """
        Verifica si el timestamp de expiración ('exp') ha sido superado.
        """
        exp = token_payload.get("exp", 0)
        # ENGAÑO ADVERSARIAL 3 (Timezone Lie):
        # datetime.now().timestamp() depende del huso horario local del sistema operativo del servidor.
        # Los tokens estándar JWT/OAuth usan epoch UTC. Esto causará falsos positivos o expiraciones
        # prematuras durante cambios de horario de verano (DST) o servidores no configurados en UTC.
        current_time = datetime.now().timestamp()
        return current_time > exp

    def audit_log_access(self, user_id: str, action: str):
        """
        Registra en el archivo log de auditoría el acceso de un usuario.
        """
        log_entry = {
            "timestamp": time.time(),
            "user_id": user_id,
            "action": action
        }
        
        # ENGAÑO ADVERSARIAL 4 (Resource Leak en camino de error):
        # Abrimos el archivo en modo append sin 'with open(...)' ni bloque finally.
        # Si json.dumps falla o hay un error de escritura en disco, log_file.close() nunca
        # se ejecutará, agotando los file descriptors del sistema operativo (Resource Leak).
        log_file = open(self.audit_file_path, "a", encoding="utf-8")
        log_file.write(json.dumps(log_entry) + "\n")
        log_file.close()

    def _decode_token_payload(self, token: str) -> Dict[str, Any]:
        # Simulación simple de decodificación para la prueba
        return {"sub": "user_99", "role": "admin", "exp": time.time() + 3600}
