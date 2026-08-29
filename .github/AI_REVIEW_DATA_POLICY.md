# Política de Datos y Gobernanza de Revisores IA (AI Review Data Policy)

Este documento establece la política de gobernanza y privacidad de datos aplicable a los flujos automatizados de revisión de código basados en modelos de lenguaje (LLMs), específicamente **Qodo Merge / PR-Agent**, integrados en los workflows de GitHub Actions de Sky-Claw.

---

## 1. Alcance y Contexto del Repositorio

* **Naturaleza pública:** Sky-Claw es actualmente un repositorio de código abierto y público.
* **Transmisión de contenido:** Las herramientas de revisión automatizada (Qodo / PR-Agent) envían a proveedores externos de LLM el contenido necesario para analizar los Pull Requests, incluyendo diffs de código, títulos, descripciones y fragmentos contextuales del repositorio.
* **Autorización acotada:** Se autoriza el procesamiento por terceros exclusivamente sobre el código, documentación y metadatos que **ya son públicos** en este repositorio y que pertenecen a **PRs internos**.

---

## 2. Proveedores Externos y Endpoints

* **Enrutamiento vía OpenRouter:** Los workflows están configurados para consultar modelos externos a través de OpenRouter (e.g., NVIDIA Nemotron, MiniMax).
* **Políticas de retención y registro (Logging):**
  * Algunos endpoints gratuitos (como los modelos NVIDIA Nemotron o variantes comunitarias) pueden registrar prompts y respuestas para fines de seguridad, moderación y mejora de producto según sus respectivos términos de servicio.
  * Cada proveedor upstream en OpenRouter mantiene políticas de retención y privacidad diferenciadas.
* **Revisión obligatoria por cambio de modelo:** Cualquier cambio o adición en `CONFIG.MODEL` o `CONFIG.FALLBACK_MODELS` exige revisar previamente los términos de gobernanza y retención de datos del proveedor correspondiente.

---

## 3. Restricciones y Prohibiciones Estrictas

Queda terminantemente prohibido procesar o exponer a través de estos workflows:
1. **Secretos y credenciales:** API keys, tokens de acceso, claves privadas, certificados, contraseñas o variables de entorno sensibles.
2. **Información confidencial o privada:** Cualquier dato o código propietario no destinado al dominio público.
3. **Datos personales (PII):** Información de identificación personal o sensible de colaboradores o terceros.
4. **Artefactos binarios o volcados privados:** Logs con información interna no censurada o artefactos cerrados no destinados a publicación.

---

## 4. Aislamiento de Forks y Prevención de Egress No Autorizado

* **Restricción de ejecución:** Se mantiene activa la política estricta que **bloquea la ejecución automática de los revisores Qodo sobre Pull Requests originados desde forks**.
* **Motivo:** Prevenir vectores de *prompt injection* y evitar la exposición o consumo inadvertido de recursos/cuotas sobre código controlado por terceros fuera del repositorio base.

---

## 5. Inaplicabilidad en Repositorios Privados y Transición ZDR

* **No aprobación automática:** Esta política y la configuración actual de modelos gratuitos **NO están aprobadas automáticamente para su uso en repositorios privados**.
* **Condición de migración / ZDR:** Si Sky-Claw pasa a ser un repositorio privado o comienza a manejar información confidencial o sensible:
  * Debe exigirse una política estricta de **Cero Retención de Datos (Zero Data Retention / ZDR)** o configuración explícita `data_collection=deny`.
  * En su defecto, los proveedores y modelos deben sustituirse por endpoints empresariales privados o instancias autohospedadas antes de habilitar los workflows.
