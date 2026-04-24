---
name: local-rag-chroma-sqlite
description: Construye y consulta una base de conocimiento local de mods usando ChromaDB para búsqueda vectorial y SQLite para datos estructurados. Usar para responder preguntas sobre requisitos de mods, buscar compatibilidad en repositorios locales, o construir un compendio de la colección del usuario. No usar para datos en tiempo real de Nexus Mods.
---

# Local RAG con ChromaDB + SQLite

Sistema RAG (Retrieval-Augmented Generation) local-first para la base de conocimiento de mods de Sky-Claw, combinando búsqueda vectorial (ChromaDB) con datos estructurados (SQLite).

## Cuándo Usar

| Escenario | Prioridad | Justificación |
|-----------|-----------|---------------|
| Responder preguntas sobre requisitos de mods desde descripciones de Nexus | 🔴 Alta | Recuperación precisa de información |
| Buscar metadatos de compatibilidad en repositorio local masivo | 🔴 Alta | Performance sobre volumen |
| Construir "Mod Compendium" personalizado del usuario | 🟠 Media | Base de conocimiento persistente |
| Encontrar mods similares o alternativos | 🟠 Media | Descubrimiento semántico |

## Cuándo NO Usar

- Para datos en tiempo real que requieran scraping activo de Nexus Mods.
- Cuando la consulta se resuelve con una query SQL simple sin búsqueda semántica.
- Para indexar archivos binarios que no contienen texto útil.

## Instrucciones

### 1. Configuración
```bash
pip install chromadb fastembed
```

### 2. Indexación de Datos
```python
import chromadb
from chromadb.utils import embedding_functions

# Embeddings locales de alto rendimiento
ef = embedding_functions.FastEmbedEmbeddingFunction(
    model_name="all-MiniLM-L6-v2"
)

# Cliente local (soberanía de datos)
client = chromadb.PersistentClient(path="./data/chroma_db")
collection = client.get_or_create_collection("mod_knowledge", embedding_function=ef)
```

### 3. Pipeline de Indexación
- Procesar archivos `.json`, `.md`, y `.txt` del directorio `downloads/` de MO2.
- Sincronizar datos estructurados a SQLite para consultas rápidas.
- Usar ChromaDB para búsqueda vectorial semántica.

### 4. Recuperación
```python
# Búsqueda semántica
results = collection.query(
    query_texts=["mods similares a Immersive Armors"],
    n_results=5
)

# Búsqueda híbrida: vector + SQL
mod_ids = [r["mod_id"] for r in results["metadatas"][0]]
sql_details = db.execute("SELECT * FROM mods WHERE mod_id IN (?)", mod_ids)
```

### 5. Reglas de Oro
- **Selección de Embeddings:** Usar modelos locales pequeños y de alto rendimiento (ej: `all-MiniLM-L6-v2`).
- **Hibridación:** Datos binarios en SQLite, representaciones vectoriales en ChromaDB.
- **Indexación Incremental:** Solo re-indexar archivos que hayan cambiado en el perfil de MO2.
- **Soberanía de Datos:** Nunca usar servicios de embeddings en la nube.
