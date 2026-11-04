"""
config.py
Configuraciones globales de Bark.
"""

import os
import shutil
from pathlib import Path


def _cargar_env_local() -> None:
    """Carga un .env simple sin introducir otra dependencia ni sobrescribir Windows."""
    ruta = Path(__file__).resolve().parent.parent / ".env"
    if not ruta.is_file():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_env_local()
# --- Carpeta de resultados en el escritorio ---
import pathlib
CARPETA_ESCRITORIO = str(pathlib.Path.home() / "Desktop")
PREFIJO_CARPETA_RESULTADOS = "Resultados_"

# --- Rutas base ---
CARPETA_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_MODELOS = os.path.join(CARPETA_BASE, "modelos")
CARPETA_SANDBOX = os.path.join(CARPETA_BASE, "sandbox")
CARPETA_DESCARGAS = os.path.join(CARPETA_BASE, "descargas")
CARPETA_BD = os.path.join(CARPETA_BASE, "bd_memoria")
RUTA_MEMORIA = os.path.join(CARPETA_BD, "bark.sqlite3")
RUTA_RESPALDO_MEMORIA = os.path.join(CARPETA_BD, "bark.respaldo.sqlite3")
# Conserva los recuerdos ya creados antes del cambio de nombre.
_RUTA_MEMORIA_ANTERIOR = os.path.join(CARPETA_BD, "chet.sqlite3")
if not os.path.exists(RUTA_MEMORIA) and os.path.isfile(_RUTA_MEMORIA_ANTERIOR):
    try:
        shutil.copy2(_RUTA_MEMORIA_ANTERIOR, RUTA_MEMORIA)
    except OSError:
        pass
PALABRA_ACTIVACION = "bark"
# Variantes que Whisper suele transcribir al oír «Bark» en español.
PALABRAS_ACTIVACION = ("bark", "bar", "barc", "park", "bart", "barck")
_RUTA_VOZ_MEXICO = os.path.join(CARPETA_MODELOS, "piper", "es_MX-claude-high.onnx")
RUTA_MODELO_TTS = _RUTA_VOZ_MEXICO if os.path.isfile(_RUTA_VOZ_MEXICO) else os.path.join(CARPETA_MODELOS, "piper", "es_ES-davefx-medium.onnx")
NOMBRE_VOZ_TTS = "Piper México alta calidad" if RUTA_MODELO_TTS == _RUTA_VOZ_MEXICO else "Piper local"
VOZ_VOLUMEN = 0.9
# None usa el micrófono predeterminado de Windows. Puede ser un índice o nombre
# de dispositivo si el usuario necesita forzar otro micrófono.
# None usa el predeterminado; BARK_MIC_DEVICE permite fijar el índice que funcione mejor.
_microfono_configurado = os.getenv("BARK_MIC_DEVICE", "").strip()
DISPOSITIVO_ENTRADA_VOZ = int(_microfono_configurado) if _microfono_configurado.isdigit() else None
VOZ_SILENCIO_FINAL_SEGUNDOS = float(os.getenv("BARK_VOZ_SILENCIO_FINAL", "0.75"))
VOZ_TIEMPO_MAX_FRASE_SEGUNDOS = 12
# Whisper local entiende español natural. "small" es más preciso y puede
# seleccionarse desde la interfaz; se descargará una sola vez al elegirlo.
# Bark incluye el modelo base completo. Usarlo de inicio permite reconocer voz
# sin descargar nada; "small" sigue disponible desde la interfaz o mediante
# BARK_MODELO_WHISPER cuando el usuario ya lo tenga descargado.
_modelo_whisper_predeterminado = "base" if os.path.isfile(os.path.join(CARPETA_MODELOS, "whisper", "base", "model.bin")) else "small"
MODELO_WHISPER = os.getenv("BARK_MODELO_WHISPER", _modelo_whisper_predeterminado).strip()
RUTA_MODELOS_WHISPER = os.path.join(CARPETA_MODELOS, "whisper")
RUTA_MODELO_WHISPER = os.path.join(RUTA_MODELOS_WHISPER, MODELO_WHISPER)
# En algunos controladores de portátil la voz llega con un nivel muy bajo.
# 20 permite usar Bark como dictado; se puede elevar en .env si hay ruido.
VOZ_UMBRAL_RMS = int(os.getenv("BARK_VOZ_UMBRAL_RMS", "20"))
# Conversacional no significa responder a todo lo que oye: un filtro local
# acepta preguntas, órdenes y seguimientos, pero ignora charla ambiental.
ESCUCHA_LIBRE = os.getenv("BARK_ESCUCHA_LIBRE", "1").strip().lower() in {"1", "true", "si", "sí", "on"}
VENTANA_CONTEXTO_VOZ_SEGUNDOS = float(os.getenv("BARK_VENTANA_CONTEXTO_VOZ", "45"))
# Piper: length_scale <1 = más rápido. Más variación de tono/ritmo = menos plano y robótico
# (valores de fábrica de Piper: 0.667 y 0.8). Si notas artefactos, bájalos hacia esos valores.
VOZ_RITMO_PIPER = float(os.getenv("BARK_VOZ_RITMO_PIPER", "0.93"))
VOZ_VARIACION_PIPER = float(os.getenv("BARK_VOZ_VARIACION_PIPER", "0.75"))
VOZ_RITMO_VARIABLE_PIPER = float(os.getenv("BARK_VOZ_RITMO_VARIABLE_PIPER", "0.9"))

# --- Modelo LLM local (llama-cpp-python) ---
def _modelos_gguf() -> list[str]:
    """Modelos de lenguaje .gguf en modelos/ (ignora proyectores de visión)."""
    try:
        return sorted(
            os.path.join(CARPETA_MODELOS, f) for f in os.listdir(CARPETA_MODELOS)
            if f.lower().endswith(".gguf") and "mmproj" not in f.lower()
        )
    except OSError:
        return []


def _elegir_modelo_llm() -> str:
    """BARK_MODELO_LLM manda; si no, el más capaz que esté descargado."""
    pedido = os.getenv("BARK_MODELO_LLM", "").strip().strip('"')
    if pedido:
        ruta = pedido if os.path.isabs(pedido) else os.path.join(CARPETA_MODELOS, pedido)
        if os.path.isfile(ruta):
            return ruta
    disponibles = _modelos_gguf()
    for preferido in ("qwen3-4b-instruct-2507", "qwen3-4b", "qwen2.5-3b-instruct"):
        for ruta in disponibles:
            if preferido in os.path.basename(ruta).lower():
                return ruta
    return disponibles[0] if disponibles else os.path.join(CARPETA_MODELOS, "qwen2.5-3b-instruct-q4_k_m.gguf")


RUTA_MODELO = _elegir_modelo_llm()
NOMBRE_MODELO = os.path.basename(RUTA_MODELO)
# Si el modelo elegido no carga (llama-cpp antiguo), se prueban estos en orden.
RUTAS_MODELO_RESPALDO = [r for r in _modelos_gguf() if r != RUTA_MODELO]

# Un contexto moderado reduce RAM y el tiempo de prellenado. Puede elevarse
# desde .env cuando una tarea de código realmente necesite más historial.
CONTEXTO_MAX = max(2048, int(os.getenv("BARK_CONTEXTO_MAX", "3072")))
HILOS_CPU = max(1, int(os.getenv("BARK_HILOS_CPU", str(min(8, os.cpu_count() or 4)))))
# 0 = solo CPU. Con GPU compatible (CUDA/Vulkan) pon BARK_GPU_LAYERS=-1 en .env para descargarlo todo a la GPU.
GPU_LAYERS = int(os.getenv("BARK_GPU_LAYERS", "0"))
# auto: precarga el modelo local al iniciar solo si no hay claves de nube. 1/0 fuerzan.
PRECALENTAR_MODELO = os.getenv("BARK_PRECALENTAR", "auto").strip().lower()
LOTE_LOCAL = max(64, int(os.getenv("BARK_LOTE_LOCAL", "512")))

# --- Comportamiento del agente (ReAct) ---
MAX_ITERACIONES_DEFAULT = 15   # tope de seguridad, usado si la tarea no da otro valor
TEMPERATURA = 0.3               # bajo = mas predecible, ideal para function calling
MAX_TOKENS_RESPUESTA = max(64, int(os.getenv("BARK_MAX_TOKENS", "160")))
MAX_CARACTERES_OBSERVACION = 4_000
# SQLite trabaja en modo WAL para que la UI, la voz y la biblioteca no se
# bloqueen entre sí. El respaldo se actualiza periódicamente con la API de
# SQLite (no es una copia a medias del archivo).
TIEMPO_ESPERA_BD = float(os.getenv("BARK_TIMEOUT_BD", "10"))
RESPALDO_CADA_TURNOS = max(1, int(os.getenv("BARK_RESPALDO_CADA_TURNOS", "10")))

# --- Proveedores en la nube (opcionales; las claves nunca van en el código) ---
# auto: Gemini -> OpenAI -> modelo local. local evita por completo la red.
PROVEEDOR_PREFERIDO = os.getenv("BARK_PROVEEDOR", "auto").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "gemini-2.5-flash")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODELO = os.getenv("OPENAI_MODELO", "gpt-4.1-mini")
# auto sigue siempre Gemini -> OpenAI -> Piper. Los otros valores permiten
# forzar un punto de partida, sin dejar al usuario sin respaldo local.
MODO_VOZ = os.getenv("BARK_VOZ", "auto").strip().lower()  # auto, gemini, openai o local
GEMINI_MODELO_TTS = os.getenv("GEMINI_MODELO_TTS", "gemini-3.8-flash-lite-tts")
GEMINI_VOZ = os.getenv("GEMINI_VOZ", "Kore")
OPENAI_MODELO_TTS = os.getenv("OPENAI_MODELO_TTS", "gpt-4o-mini-tts")
OPENAI_VOZ = os.getenv("OPENAI_VOZ", "coral")
TIEMPO_ESPERA_RED = float(os.getenv("BARK_TIMEOUT_RED", "12"))
# La síntesis no debe bloquear el modo de escucha. Si un proveedor no responde,
# Piper toma el turno rápidamente y el proveedor queda en pausa temporal.
TIEMPO_ESPERA_VOZ_NUBE = float(os.getenv("BARK_TIMEOUT_VOZ", "4"))
# Opciones que la interfaz presenta. Se conservan como texto para que se puedan
# actualizar sin cambiar la lógica de audio cuando OpenAI añada voces nuevas.
VOCES_OPENAI = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar")
MODELOS_TTS_OPENAI = ("gpt-4o-mini-tts", "gpt-4o-mini-tts-2025-12-15", "tts-1", "tts-1-hd")
MODELOS_TTS_GEMINI = ("gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts")
VOCES_GEMINI = ("Kore", "Puck", "Aoede", "Achird", "Achernar", "Algieba", "Autonoe", "Callirrhoe", "Charon", "Despina", "Erinome", "Fenrir", "Gacrux", "Leda", "Orus", "Sulafat", "Zephyr")
