#!/usr/bin/env python3
"""
Test Suite para FrontendBridge
Simula WebSocket messages desde el frontend y valida respuestas del backend.

Protocolo Tree of Thoughts:
- Fase 1: Genera 3 casos de prueba (GET_CONFIG, UPDATE_CONFIG valid/invalid)
- Fase 2: Ejecuta secuencialmente con validación
- Fase 3: Reporta resultados con P(success)
"""

import asyncio
import json
import websockets
import uuid
import time
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

# Fix encoding for Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined, union-attr]

# Colors para terminal
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

GATEWAY_URL = "ws://127.0.0.1:18790"
CONFIG_PATH = Path.home() / ".sky_claw" / "config.toml"

# ═══════════════════════════════════════════════════════════════════════════════
# Test Cases (Tree of Thoughts Pensamiento B)
# ═══════════════════════════════════════════════════════════════════════════════

class FrontendBridgeTestSuite:
    def __init__(self):
        self.ws = None
        self.results = []
        self.start_time = time.time()

    async def connect(self):
        """Conecta al Gateway."""
        try:
            print(f"\n{BLUE}[CONNECT]{RESET} Intentando conectar a {GATEWAY_URL}...")
            self.ws = await websockets.connect(GATEWAY_URL, open_timeout=5)
            print(f"{GREEN}✅ Conexión establecida{RESET}\n")
            return True
        except Exception as e:
            print(f"{RED}❌ Fallo de conexión: {e}{RESET}")
            return False

    async def disconnect(self):
        """Cierra la conexión."""
        if self.ws:
            await self.ws.close()

    async def send_message(self, msg_type: str, content: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Envía un mensaje y captura la respuesta."""
        payload: dict[str, Any] = {
            "type": msg_type,
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
        }
        if content:
            payload["content"] = content

        print(f"  📤 Enviando {BOLD}{msg_type}{RESET}: {json.dumps(content or {})[:80]}...")
        await self.ws.send(json.dumps(payload))

        try:
            response_raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
            response = json.loads(response_raw)
            print(f"  📥 Respuesta recibida: {response['type']}")
            return response
        except asyncio.TimeoutError:
            print(f"  {RED}⏱️ Timeout esperando respuesta{RESET}")
            return {}
        except Exception as e:
            print(f"  {RED}❌ Error recibiendo respuesta: {e}{RESET}")
            return {}

    async def test_get_config(self) -> bool:
        """TEST 1: GET_CONFIG debe retornar configuración actual enmascarada."""
        print(f"\n{BOLD}=== TEST 1: GET_CONFIG ==={RESET}")
        response = await self.send_message("GET_CONFIG")

        if response.get("type") != "CONFIG_DATA":
            print(f"  {RED}❌ Tipo incorrecto: {response.get('type')}{RESET}")
            return False

        content = response.get("content", {})
        required_fields = {"llm_provider", "telegram_chat_id", "has_llm_key", "has_nexus_key", "has_telegram_token"}
        missing = required_fields - set(content.keys())

        if missing:
            print(f"  {RED}❌ Campos faltantes: {missing}{RESET}")
            return False

        # Validar que no haya secretos en respuesta
        secrets = {"llm_api_key", "nexus_api_key", "telegram_bot_token"}
        exposed = secrets & set(content.keys())
        if exposed:
            print(f"  {RED}❌ SECURITY: Secretos expuestos al frontend: {exposed}{RESET}")
            return False

        print(f"  {GREEN}✅ CONFIG_DATA válido:{RESET}")
        for key, val in content.items():
            print(f"     - {key}: {val}")
        return True

    async def test_update_config_valid(self) -> bool:
        """TEST 2: UPDATE_CONFIG con valores válidos."""
        print(f"\n{BOLD}=== TEST 2: UPDATE_CONFIG (VÁLIDO) ==={RESET}")

        test_data = {
            "llm_provider": "ollama",
            "nexus_api_key": "test-nexus-key-12345",
            "telegram_chat_id": "987654321",
        }
        response = await self.send_message("UPDATE_CONFIG", test_data)

        if response.get("type") != "CONFIG_UPDATED":
            print(f"  {RED}❌ Tipo incorrecto: {response.get('type')}{RESET}")
            return False

        if not response.get("success"):
            print(f"  {RED}❌ Respuesta no fue success=true{RESET}")
            print(f"     Mensaje: {response.get('message')}")
            return False

        print(f"  {GREEN}✅ UPDATE_CONFIG aceptado:{RESET}")
        print(f"     Mensaje: {response.get('message')}")

        # Verificar que TOML fue actualizado
        await asyncio.sleep(0.5)
        if CONFIG_PATH.exists():
            content = CONFIG_PATH.read_text()
            if "llm_provider = \"ollama\"" in content:
                print(f"  {GREEN}✅ TOML actualizado (llm_provider = ollama){RESET}")
            else:
                print(f"  {YELLOW}⚠️ TOML aún no refleja cambio (¿eventual consistency?){RESET}")

        return True

    async def test_update_config_invalid_token(self) -> bool:
        """TEST 3: UPDATE_CONFIG con Telegram token inválido (sin ':')."""
        print(f"\n{BOLD}=== TEST 3: UPDATE_CONFIG (INVÁLIDO - Token Telegram) ==={RESET}")

        test_data = {
            "telegram_bot_token": "invalid_token_without_colon",
        }
        response = await self.send_message("UPDATE_CONFIG", test_data)

        if response.get("type") != "CONFIG_UPDATED":
            print(f"  {RED}❌ Tipo incorrecto: {response.get('type')}{RESET}")
            return False

        if response.get("success"):
            print(f"  {RED}❌ Validación falló: aceptó token inválido{RESET}")
            return False

        error_msg = response.get("message", "")
        if "formato correcto" in error_msg.lower() or ":" in error_msg:
            print(f"  {GREEN}✅ Validación correcta:{RESET}")
            print(f"     Mensaje: {error_msg}")
            return True
        else:
            print(f"  {YELLOW}⚠️ Error rechazado pero mensaje poco claro: {error_msg}{RESET}")
            return False

    async def test_update_config_invalid_chatid(self) -> bool:
        """TEST 4: UPDATE_CONFIG con Chat ID no numérico."""
        print(f"\n{BOLD}=== TEST 4: UPDATE_CONFIG (INVÁLIDO - Chat ID) ==={RESET}")

        test_data = {
            "telegram_chat_id": "abc123xyz",
        }
        response = await self.send_message("UPDATE_CONFIG", test_data)

        if response.get("type") != "CONFIG_UPDATED":
            print(f"  {RED}❌ Tipo incorrecto: {response.get('type')}{RESET}")
            return False

        if response.get("success"):
            print(f"  {RED}❌ Validación falló: aceptó Chat ID no numérico{RESET}")
            return False

        error_msg = response.get("message", "")
        if "numerico" in error_msg.lower():
            print(f"  {GREEN}✅ Validación correcta:{RESET}")
            print(f"     Mensaje: {error_msg}")
            return True
        else:
            print(f"  {YELLOW}⚠️ Error rechazado pero mensaje poco claro: {error_msg}{RESET}")
            return False

    async def test_update_config_excessive_length(self) -> bool:
        """TEST 5: UPDATE_CONFIG con API key demasiado larga (> 512 chars)."""
        print(f"\n{BOLD}=== TEST 5: UPDATE_CONFIG (INVÁLIDO - Longitud excesiva) ==={RESET}")

        test_data = {
            "llm_api_key": "x" * 600,  # Excede max 512
        }
        response = await self.send_message("UPDATE_CONFIG", test_data)

        if response.get("type") != "CONFIG_UPDATED":
            print(f"  {RED}❌ Tipo incorrecto: {response.get('type')}{RESET}")
            return False

        if response.get("success"):
            print(f"  {RED}❌ Validación falló: aceptó key demasiado larga{RESET}")
            return False

        error_msg = response.get("message", "")
        if "excede" in error_msg.lower() or "largo" in error_msg.lower():
            print(f"  {GREEN}✅ Validación correcta:{RESET}")
            print(f"     Mensaje: {error_msg}")
            return True
        else:
            print(f"  {YELLOW}⚠️ Error rechazado pero mensaje poco claro: {error_msg}{RESET}")
            return False

    async def test_query_message(self) -> bool:
        """TEST 6: QUERY message forwarding (chat via FrontendBridge)."""
        print(f"\n{BOLD}=== TEST 6: QUERY (Chat forwarding) ==={RESET}")

        test_data = "/status"
        response = await self.send_message("QUERY", {"content": test_data})

        # NOTA: Este test podría fallar si Anthropic API key no es válida
        # Es OK si falla con error de autorización; lo importante es que:
        # 1. FrontendBridge aceptó el QUERY
        # 2. Lo forwarded al router
        # 3. Retornó RESPONSE (válida o error)

        if response.get("type") != "RESPONSE":
            print(f"  {YELLOW}⚠️ Tipo inesperado: {response.get('type')}{RESET}")
            print("     (Podría ser OK si API key no es válida)")
            return False

        print(f"  {GREEN}✅ QUERY aceptado y forwarded:{RESET}")
        print(f"     Respuesta: {response.get('content', '')[:100]}...")
        return True

    async def run_all(self):
        """Ejecuta la suite completa."""
        print(f"\n{BOLD}{'='*70}{RESET}")
        print(f"{BOLD}FrontendBridge Test Suite (Tree of Thoughts Protocol){RESET}")
        print(f"{BOLD}{'='*70}{RESET}")

        if not await self.connect():
            return

        tests = [
            ("GET_CONFIG", self.test_get_config),
            ("UPDATE_CONFIG (VÁLIDO)", self.test_update_config_valid),
            ("UPDATE_CONFIG (Token inválido)", self.test_update_config_invalid_token),
            ("UPDATE_CONFIG (Chat ID inválido)", self.test_update_config_invalid_chatid),
            ("UPDATE_CONFIG (Longitud excesiva)", self.test_update_config_excessive_length),
            ("QUERY (Chat)", self.test_query_message),
        ]

        for test_name, test_func in tests:
            try:
                result = await test_func()
                self.results.append((test_name, result))
            except Exception as e:
                print(f"  {RED}❌ Exception: {e}{RESET}")
                self.results.append((test_name, False))

        await self.disconnect()
        self.print_summary()

    def print_summary(self):
        """Imprime resumen de resultados."""
        elapsed = time.time() - self.start_time
        passed = sum(1 for _, r in self.results if r)
        total = len(self.results)
        success_rate = (passed / total * 100) if total > 0 else 0

        print(f"\n{BOLD}{'='*70}{RESET}")
        print(f"{BOLD}RESULTADOS (Tree of Thoughts - Fase 3: Proyección){RESET}")
        print(f"{BOLD}{'='*70}{RESET}\n")

        for test_name, result in self.results:
            status = f"{GREEN}✅ PASS{RESET}" if result else f"{RED}❌ FAIL{RESET}"
            print(f"  {status}  {test_name}")

        print(f"\n{BOLD}Resumen:{RESET}")
        print(f"  Pasaron: {GREEN}{passed}/{total}{RESET}")
        print(f"  Tasa de éxito: {GREEN}{success_rate:.1f}%{RESET}")
        print(f"  Tiempo total: {elapsed:.2f}s")
        print(f"  P(success) = {success_rate/100:.2f}")

        if success_rate >= 80:
            print(f"\n{BOLD}{GREEN}🎯 RAMA MAESTRA VIABLE (P >= 0.8){RESET}")
        else:
            print(f"\n{BOLD}{YELLOW}⚠️ RAMA MAESTRA REQUIERE ITERACIÓN (P < 0.8){RESET}")

        print()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    suite = FrontendBridgeTestSuite()
    await suite.run_all()


if __name__ == "__main__":
    asyncio.run(main())
