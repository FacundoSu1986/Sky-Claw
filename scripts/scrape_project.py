import math
import os

# Configuración
PROJECT_ROOT = "."
OUTPUT_DIR = "project_scrape"
EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "venv",
    ".env",
    "dist",
    "build",
    ".agents",
    ".agent",
    ".claude",
    "logs",
    "external_libs",
    OUTPUT_DIR,
}
EXCLUDE_FILES = {"mod_registry.db", "mod_registry.db-shm", "mod_registry.db-wal", "sky_claw_state.db"}
INCLUDE_EXTENSIONS = {
    ".py",
    ".js",
    ".html",
    ".css",
    ".md",
    ".json",
    ".txt",
    ".sh",
    ".bat",
    ".ps1",
    ".toml",
    ".yaml",
    ".yml",
    "Dockerfile",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}
MAX_FILE_SIZE_TEXT = 500 * 1024  # 500KB


def get_project_files():
    text_files = []
    image_files = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        # Filtrar directorios
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        for file in files:
            if file in EXCLUDE_FILES:
                continue

            ext = os.path.splitext(file)[1].lower()
            full_path = os.path.join(root, file)

            if ext in INCLUDE_EXTENSIONS or file in {"Dockerfile", "LICENSE", "requirements.txt"}:
                # Verificar tamaño para archivos de texto
                if os.path.getsize(full_path) < MAX_FILE_SIZE_TEXT:
                    text_files.append(full_path)
            elif ext in IMAGE_EXTENSIONS:
                image_files.append(full_path)

    return text_files, image_files


def scrape_text_files(files):
    content_list = []
    total_chars = 0

    for file_path in files:
        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
                rel_path = os.path.relpath(file_path, PROJECT_ROOT)
                header = f"\n{'=' * 80}\nFILE: {rel_path}\n{'=' * 80}\n"
                full_content = header + content
                content_list.append(full_content)
                total_chars += len(full_content)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    return content_list, total_chars


def main():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    print("Recopilando archivos...")
    text_files, image_files = get_project_files()

    # Crear lista de imágenes
    image_list_content = "\n" + "=" * 80 + "\nLISTA DE IMÁGENES ENCONTRADAS\n" + "=" * 80 + "\n"
    for img in image_files:
        image_list_content += f"- {os.path.relpath(img, PROJECT_ROOT)}\n"

    contents, total_chars = scrape_text_files(text_files)

    # Añadir la lista de imágenes al principio del primer archivo
    if contents:
        contents[0] = image_list_content + contents[0]
        total_chars += len(image_list_content)
    else:
        contents = [image_list_content]
        total_chars = len(image_list_content)

    # El usuario pidió dividirlo en 4 partes si es grande
    # Vamos a usar un límite de 500k caracteres por archivo para que sea digerible por la mayoría de IAs (o lo que quepa en 4)
    num_parts = 4
    chars_per_part = math.ceil(total_chars / num_parts)

    print(f"Total caracteres: {total_chars}. Dividiendo en {num_parts} archivos (~{chars_per_part} chars cada uno).")

    part_num = 1
    current_content = ""

    for content in contents:
        current_content += content

        if len(current_content) >= chars_per_part and part_num < num_parts:
            filename = os.path.join(OUTPUT_DIR, f"txt{part_num}.txt")
            with open(filename, "w", encoding="utf-8") as f:
                f.write(current_content)
            print(f"Archivo creado: {filename} ({len(current_content)} chars)")
            part_num += 1
            current_content = ""

    # Última parte
    filename = os.path.join(OUTPUT_DIR, f"txt{part_num}.txt")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(current_content)
    print(f"Archivo creado: {filename} ({len(current_content)} chars)")


if __name__ == "__main__":
    main()
