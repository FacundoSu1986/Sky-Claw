#!/usr/bin/env python3
"""
Revisor Adversarial de Pull Requests usando Google Gemini (SDK google-genai) y GitHub API.
Diseñado con mejores prácticas para automatización CI/CD en GitHub Actions.
"""

import os
import sys
import json
import re
from pathlib import Path
import requests
from google import genai
from google.genai import types

# Configuración y Constantes
BOT_SIGNATURE = "<!-- adversarial-pr-reviewer-bot -->"
DEFAULT_MODEL = "gemini-3.1-pro-preview"  # Modelos recomendados: gemini-3.1-pro-preview, gemini-3.6-flash-preview, gemini-2.5-pro

# Archivos ignorados por defecto para ahorrar tokens y evitar alucinaciones en autogenerados
IGNORED_EXTENSIONS = {
    ".lock", ".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg", 
    ".gif", ".svg", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".eot"
}
IGNORED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", 
    "Gemfile.lock", "cargo.lock", "composer.lock", "go.sum"
}

def log(msg: str):
    print(f"[AdversarialPRBot] {msg}", flush=True)

def get_env_or_fail(var_name: str) -> str:
    val = os.environ.get(var_name)
    if not val:
        log(f"ERROR CRÍTICO: La variable de entorno '{var_name}' no está definida.")
        sys.exit(1)
    return val

def filter_diff(raw_diff: str) -> str:
    """
    Filtra el diff git para eliminar archivos de bloqueo (lockfiles) y binarios/ruidosos.
    """
    lines = raw_diff.splitlines()
    filtered_lines = []
    skip_current_file = False
    current_file_name = ""

    for line in lines:
        if line.startswith("diff --git"):
            # Extraer nombre del archivo (ej: diff --git a/src/index.js b/src/index.js)
            parts = line.split(" ")
            if len(parts) >= 3:
                current_file_name = parts[-1].removeprefix("b/")
                ext = Path(current_file_name).suffix.lower()
                name = Path(current_file_name).name.lower()
                
                if ext in IGNORED_EXTENSIONS or name in IGNORED_FILENAMES:
                    skip_current_file = True
                    log(f"Ignorando archivo ruidoso/bloqueo en diff: {current_file_name}")
                else:
                    skip_current_file = False
                    log(f"Incluyendo archivo en revisión: {current_file_name}")
            else:
                skip_current_file = False
        
        if not skip_current_file:
            filtered_lines.append(line)

    return "\n".join(filtered_lines)

def fetch_pr_diff(repo: str, pr_number: int, github_token: str) -> str:
    """
    Obtiene el diff exacto del PR desde la API de GitHub.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    log(f"Descargando diff para {repo} PR #{pr_number}...")
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        log(f"Error al obtener el diff desde GitHub (HTTP {resp.status_code}): {resp.text}")
        sys.exit(1)
    return resp.text

def analyze_with_gemini(diff_text: str, system_prompt: str, model_name: str, api_key: str) -> str:
    """
    Llama a Gemini usando el nuevo SDK oficial `google-genai` para realizar la revisión adversarial.
    """
    log(f"Iniciando análisis con el modelo: {model_name}...")
    client = genai.Client(api_key=api_key)
    
    # Preparamos el mensaje del usuario con el diff
    user_prompt = (
        "A continuación se presenta el DIFF git del Pull Request a revisar.\n"
        "Aplica estrictamente tu método adversarial, respeta el límite de 5 hallazgos y "
        "responde 'Sin hallazgos.' si no encuentras defectos reales verificables en la lógica.\n\n"
        f"```diff\n{diff_text}\n```"
    )

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(user_prompt)]
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,  # Baja temperatura para maximizar rigor lógico y evitar creatividad inventada
                max_output_tokens=4096,
            )
        )
        return response.text
    except Exception as e:
        log(f"Error al comunicar con la API de Gemini: {str(e)}")
        sys.exit(1)

def post_github_review(repo: str, pr_number: int, review_body: str, github_token: str):
    """
    Publica la respuesta del bot como un Review en el PR de GitHub.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    # Adjuntamos la firma oculta del bot para trazabilidad
    formatted_body = f"{BOT_SIGNATURE}\n## 🤖 Revisión Adversarial (Gemini AI)\n\n{review_body}"
    
    # Determinar si solicitamos cambios (REQUEST_CHANGES) o solo comentamos (COMMENT)
    # Por buena práctica en CI/CD, por defecto hacemos COMMENT salvo que se detecte una alta gravedad evidente
    event_type = "COMMENT"
    if "[GRAVEDAD: alta]" in review_body.lower() or "**[gravedad: alta]**" in review_body.lower():
        # Puede configurarse mediante env var si se desea bloquear el PR automáticamente
        if os.environ.get("BLOCK_PR_ON_HIGH_SEVERITY", "false").lower() == "true":
            event_type = "REQUEST_CHANGES"
            log("Alerta: Se detectaron hallazgos de alta gravedad. Bloqueando el PR (REQUEST_CHANGES)...")

    payload = {
        "body": formatted_body,
        "event": event_type
    }
    
    log(f"Publicando revisión ({event_type}) en el PR #{pr_number}...")
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in [200, 201]:
        log(f"Error al publicar el review en GitHub (HTTP {resp.status_code}): {resp.text}")
        sys.exit(1)
    log("¡Revisión adversarial publicada con éxito en GitHub!")

def main():
    log("Iniciando Bot Revisor Adversarial de PRs...")
    
    # 1. Cargar variables y credenciales
    gemini_api_key = get_env_or_fail("GEMINI_API_KEY")
    github_token = get_env_or_fail("GITHUB_TOKEN")
    repo = get_env_or_fail("GITHUB_REPOSITORY")
    event_path = get_env_or_fail("GITHUB_EVENT_PATH")
    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    
    # 2. Leer evento de GitHub para extraer el número de PR
    with open(event_path, "r", encoding="utf-8") as f:
        event_data = json.load(f)
    
    if "pull_request" not in event_data:
        log("Este evento no parece ser un Pull Request. Saliendo...")
        sys.exit(0)
        
    pr_number = event_data["pull_request"]["number"]
    
    # 3. Leer el system prompt adversarial
    prompt_path = Path(__file__).parent / "adversarial_prompt.md"
    if not prompt_path.exists():
        log(f"No se encontró el archivo de prompt en {prompt_path}")
        sys.exit(1)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    
    # 4. Obtener y filtrar diff
    raw_diff = fetch_pr_diff(repo, pr_number, github_token)
    if not raw_diff or not raw_diff.strip():
        log("El diff está vacío. Nada que revisar.")
        sys.exit(0)
        
    filtered_diff = filter_diff(raw_diff)
    if not filtered_diff.strip():
        log("El diff se redujo a vacío tras filtrar archivos de bloqueo/ruido. Nada que revisar.")
        sys.exit(0)
        
    # Limitar diff por seguridad de contexto (ej: máx 120,000 caracteres)
    max_diff_chars = 120000
    if len(filtered_diff) > max_diff_chars:
        log(f"Aviso: El diff es muy largo ({len(filtered_diff)} caracteres). Truncando a los primeros {max_diff_chars} caracteres.")
        filtered_diff = filtered_diff[:max_diff_chars] + "\n\n... [DIFF TRUNCADO POR SEGURIDAD Y TAMAÑO] ..."

    # 5. Ejecutar análisis en Gemini
    review_text = analyze_with_gemini(filtered_diff, system_prompt, model_name, gemini_api_key)
    
    # 6. Publicar resultado en GitHub
    post_github_review(repo, pr_number, review_text, github_token)
    log("Proceso finalizado correctamente.")

if __name__ == "__main__":
    main()
