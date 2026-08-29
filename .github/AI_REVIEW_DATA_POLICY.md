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
  * Los endpoints gratuitos actuales no cuentan con garantía demostrada de Zero Data Retention (ZDR); pueden registrar prompts y respuestas para fines de seguridad, moderación y mejora de producto según sus respectivos términos de servicio.
  * Cada proveedor upstream en OpenRouter mantiene políticas de retención y privacidad diferenciadas.
* **Revisión obligatoria por cambio de modelo:** Cualquier cambio o adición en `CONFIG.MODEL` o `CONFIG.FALLBACK_MODELS` exige revisar previamente los términos de gobernanza y retención de datos del proveedor correspondiente y actualizar la lista de modelos aprobados.

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

## 5. Inaplicabilidad en Repositorios Privados, Bloqueo Automático y Transición ZDR

* **Desautorización automática:** Si Sky-Claw pasa a ser un repositorio privado o el workflow pudiera procesar información no pública, esta configuración de modelos gratuitos queda automáticamente **NO AUTORIZADA**.
* **Bloqueo técnico (Fail-Closed):** Los workflows implementan la condición `github.event.repository.private == false` a nivel de job para bloquear de forma inmediata y automática cualquier ejecución si el repositorio es privado.
* **Requisitos acumulativos para uso sobre datos privados:** Para volver a habilitar revisores externos sobre datos privados deben cumplirse **SIMULTÁNEAMENTE** todas las siguientes condiciones:
  1. **Cero Retención de Datos verificable (Zero Data Retention / ZDR):** Configuración `zdr=true` o guardrail / account setting equivalente comprobable en el proveedor.
  2. **Política de no recopilación:** `data_collection=deny` explícito en el enrutamiento. **Nota de seguridad:** `data_collection=deny` restringe el enrutamiento pero **NO sustituye ZDR**; ambas condiciones son obligatorias e indispensables.
  3. **Proveedores y endpoints aprobados:** Contratos empresariales o endpoints dedicados que garanticen la confidencialidad.
  4. **Reevaluación explícita de gobernanza:** Aprobación formal documentada en esta política antes de cualquier despliegue.

---

## 6. Inventario de Modelos Aprobados (Governance Allowlist)

La siguiente sección auditable delimita los modelos exactos aprobados exclusivamente para datos públicos de Sky-Claw:

<!-- approved-models:start -->
* `openrouter/nvidia/nemotron-3-super-120b-a12b:free`
  * Proveedor / Familia: NVIDIA (Nemotron-3 Super 120B) vía OpenRouter
  * Fecha de revisión: 2026-08-29
  * Alcance de aprobación: `PUBLIC_DATA_ONLY`
  * Decisión: Aprobado exclusivamente porque Sky-Claw y el diff procesado son públicos.
  * Advertencia: Las políticas de retención y entrenamiento del proveedor upstream pueden cambiar y deben reevaluarse periódicamente; no cuenta con garantía demostrada de Zero Data Retention (ZDR).

* `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
  * Proveedor / Familia: NVIDIA (Nemotron-3 Ultra 550B) vía OpenRouter
  * Fecha de revisión: 2026-08-29
  * Alcance de aprobación: `PUBLIC_DATA_ONLY`
  * Decisión: Aprobado exclusivamente porque Sky-Claw y el diff procesado son públicos.
  * Advertencia: Las políticas de retención y entrenamiento del proveedor upstream pueden cambiar y deben reevaluarse periódicamente; no cuenta con garantía demostrada de Zero Data Retention (ZDR).

* `openrouter/minimax/minimax-m3:free`
  * Proveedor / Familia: MiniMax (MiniMax-M3) vía OpenRouter
  * Fecha de revisión: 2026-08-29
  * Alcance de aprobación: `PUBLIC_DATA_ONLY`
  * Decisión: Aprobado exclusivamente porque Sky-Claw y el diff procesado son públicos.
  * Advertencia: Las políticas de retención y entrenamiento del proveedor upstream pueden cambiar y deben reevaluarse periódicamente; no cuenta con garantía demostrada de Zero Data Retention (ZDR).
<!-- approved-models:end -->
