import os

REPO_DIR = r"e:\SkyclawGemini"
OUT_DIR = os.path.join(REPO_DIR, "skyscraping")
MAX_SIZE = 50 * 1024 * 1024  # 50MB

if not os.path.exists(OUT_DIR):
    os.makedirs(OUT_DIR)

# Categorize files by path prefix
CATEGORIES = {
    "frontend_code": lambda p: "sky_claw/antigravity/gui" in p.replace("\\", "/"),
    "local_tools": lambda p: "sky_claw/local" in p.replace("\\", "/"),
    "core_backend": lambda p: "sky_claw/antigravity" in p.replace("\\", "/") and "sky_claw/antigravity/gui" not in p.replace("\\", "/"),
    "tests_code": lambda p: "tests/" in p.replace("\\", "/") or "tests\\" in p,
    "config_code": lambda p: p.replace("\\", "/").count("/") == 0 or "docs/" in p.replace("\\", "/") or ".github/" in p.replace("\\", "/"),
}

# Supported extensions
EXTS = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".bat", ".ps1", ".txt", ".js", ".html", ".css"}
IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".skyclaw_backups"}

files_by_category = {k: [] for k in CATEGORIES.keys()}
files_by_category["other_code"] = []

for root, dirs, files in os.walk(REPO_DIR):
    # filter ignored dirs
    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
    
    if root == OUT_DIR:
        continue

    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in EXTS or f in ["LICENSE", "Dockerfile"]:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, REPO_DIR)
            
            if rel_path.startswith("skyscraping"):
                continue
                
            matched_cat = "other_code"
            for cat, func in CATEGORIES.items():
                if func(rel_path):
                    matched_cat = cat
                    break
            files_by_category[matched_cat].append(rel_path)

def write_category(cat_name, rel_paths):
    out_file = os.path.join(OUT_DIR, f"{cat_name}.txt")
    part = 1
    current_size = 0
    f_out = open(out_file, "w", encoding="utf-8")
    
    for rel_path in rel_paths:
        full_path = os.path.join(REPO_DIR, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f_in:
                content = f_in.read()
        except Exception as e:
            continue
            
        header = f"\n\n{'='*80}\nFILE: {rel_path}\n{'='*80}\n\n"
        chunk = header + content
        chunk_size = len(chunk.encode("utf-8"))
        
        if current_size + chunk_size > MAX_SIZE:
            f_out.close()
            part += 1
            out_file = os.path.join(OUT_DIR, f"{cat_name}_part{part}.txt")
            f_out = open(out_file, "w", encoding="utf-8")
            current_size = 0
            
        f_out.write(chunk)
        current_size += chunk_size
        
    f_out.close()

for cat, paths in files_by_category.items():
    if paths:
        write_category(cat, paths)
        print(f"Written {cat} ({len(paths)} files)")
