"""Descarga el modelo local recomendado (Qwen3-4B-Instruct-2507, Q4_K_M, ~2.5 GB) a modelos/.

Uso:  python descargar_modelo.py
Bark lo detecta solo al reiniciar; si no carga, vuelve automáticamente al Qwen2.5-3B anterior.
"""

import os
import sys

REPOS = (
    "unsloth/Qwen3-4B-Instruct-2507-GGUF",
    "Qwen/Qwen3-4B-Instruct-2507-GGUF",
    "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF",
)
CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modelos")


def main() -> int:
    try:
        from huggingface_hub import hf_hub_download, list_repo_files
    except ImportError:
        print("Falta huggingface_hub. Ejecuta:  pip install huggingface_hub")
        return 1
    try:
        import llama_cpp
        partes = tuple(int(x) for x in llama_cpp.__version__.split(".")[:3] if x.isdigit())
        if partes < (0, 3, 8):
            print(f"Aviso: llama-cpp-python {llama_cpp.__version__} es antiguo para Qwen3; actualiza con:  pip install -U llama-cpp-python")
    except Exception:
        pass
    os.makedirs(CARPETA, exist_ok=True)
    for repo in REPOS:
        try:
            archivos = [f for f in list_repo_files(repo) if f.lower().endswith(".gguf")]
            elegido = next((f for f in archivos if "q4_k_m" in f.lower() and "mmproj" not in f.lower()), None)
            if not elegido:
                continue
            print(f"Descargando {elegido} desde {repo} …")
            ruta = hf_hub_download(repo_id=repo, filename=elegido, local_dir=CARPETA)
            print(f"Listo: {ruta}\nReinicia Bark para usarlo. (Para volver al anterior: BARK_MODELO_LLM=qwen2.5-3b-instruct-q4_k_m.gguf en .env)")
            return 0
        except Exception as error:
            print(f"{repo}: {error}")
    print("No pude descargar el modelo. Revisa tu conexión o bájalo manualmente a la carpeta modelos/.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
