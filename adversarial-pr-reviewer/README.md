# Revisor Adversarial de Pull Requests 🤖🔍

Un bot de revisiones de código de grado producción para GitHub Actions impulsado por los modelos más recientes de **Google Gemini** (usando el SDK oficial moderno `google-genai`).

Diseñado bajo la filosofía de **revisión adversarial**: su objetivo no es aplaudir el código ni validar estilo, sino encontrar con rigor lógico defectos reales, verificables y de riesgo en producción antes de hacer merge.

---

## ✨ Características y Buenas Prácticas Implementadas

1. **Soporte para Modelos Gemini de Última Generación**:
   - Compatible nativamente con `gemini-3.1-pro-preview`, `gemini-3.6-flash-preview`, `gemini-2.5-pro` y futuros modelos vía variables de entorno sin modificar código.
   - Utiliza temperatura baja (`0.2`) para maximizar la precisión lógica y minimizar alucinaciones.

2. **Filtrado Inteligente de Ruido y Ahorro de Tokens**:
   - Filtra automáticamente archivos de bloqueo (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`) y archivos estáticos/minificados (`.min.js`, `.map`, `.png`, `.pdf`).
   - Evita alucinaciones en el análisis de árboles de dependencias masivos y reduce drásticamente el consumo de cuota de API.
   - Protección contra truncamiento y manejo de diffs masivos (>120k caracteres).

3. **Integración Nativa como Revisión en GitHub (Pull Request Reviews)**:
   - Publica sus hallazgos en la pestaña **Conversation** y **Files changed** de GitHub mediante la API REST de Pull Request Reviews.
   - Firma distintiva para trazabilidad.
   - Modo configurable para **comentar (COMMENT)** o **solicitar cambios (REQUEST_CHANGES)** si se detectan defectos de alta gravedad (`BLOCK_PR_ON_HIGH_SEVERITY: "true"`).

4. **Cero Falsos Positivos por Diseño**:
   - Asume que los tests, el linter y el type-checker ya pasaron.
   - Prohíbe reportar estilo, nombres de variables, preferencias o hipótesis sin escenario de falla concreto.

---

## 🚀 Guía de Instalación Rápida (en 2 minutos)

Para agregar este bot a cualquier repositorio de GitHub:

### Paso 1: Copiar archivos al repositorio de destino
Copia esta carpeta entera (`adversarial-pr-reviewer`) a la raíz de tu repositorio y copia la plantilla del workflow a la carpeta de GitHub Actions:

```bash
mkdir -p .github/workflows
cp adversarial-pr-reviewer/workflow-template.yml .github/workflows/adversarial-reviewer.yml
```

Tu estructura de repositorio debe quedar así:
```text
mi-proyecto/
  ├── .github/
  │    └── workflows/
  │         └── adversarial-reviewer.yml
  └── adversarial-pr-reviewer/
       ├── adversarial_prompt.md
       ├── pr_reviewer.py
       ├── requirements.txt
       └── README.md
```

### Paso 2: Configurar tu API Key de Gemini en GitHub Secrets
1. Obtén tu API Key gratuita en [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Ve a tu repositorio en GitHub web ➔ **Settings** (Configuración).
3. En el menú lateral: **Secrets and variables** ➔ **Actions**.
4. Haz clic en **New repository secret**.
5. Nombre del secreto: `GEMINI_API_KEY`
6. Valor: Pegas tu clave secreta de Gemini (`AIzaSy...`).

### Paso 3: ¡Listo! Abre un Pull Request
Sube los cambios a tu repositorio (`git push`). A partir de ese momento, cada vez que abras o actualices un Pull Request, el bot lo revisará automáticamente y publicará su análisis.

---

## ⚙️ Configuración y Selección de Modelos

Puedes cambiar el modelo o el comportamiento en cualquier momento editando las variables de entorno (`env`) en `.github/workflows/adversarial-reviewer.yml`:

```yaml
env:
  # El modelo a utilizar (por defecto gemini-3.1-pro-preview para máxima capacidad analítica)
  # Otras opciones: gemini-3.6-flash-preview (más rápido y económico), gemini-2.5-pro, etc.
  GEMINI_MODEL: "gemini-3.1-pro-preview"

  # Si se establece en "true", el bot bloqueará el PR (Request Changes) si detecta 
  # un hallazgo con la etiqueta [GRAVEDAD: alta]. Por defecto es "false" (Comment).
  BLOCK_PR_ON_HIGH_SEVERITY: "false"
```

---

## 🛠️ Desarrollo y Prueba Local

Si deseas probar el revisor localmente antes de subirlo a GitHub:

1. Define tus variables de entorno locales:
   ```bash
   export GEMINI_API_KEY="tu_clave_aqui"
   export GITHUB_TOKEN="un_personal_access_token_de_github"
   export GITHUB_REPOSITORY="dueño/repositorio"
   export GITHUB_EVENT_PATH="path/a/un_mock_pr_event.json"
   ```
2. Instala las dependencias:
   ```bash
   pip install -r adversarial-pr-reviewer/requirements.txt
   ```
3. Ejecuta el script:
   ```bash
   python adversarial-pr-reviewer/pr_reviewer.py
   ```
