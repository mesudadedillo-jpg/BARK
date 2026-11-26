"""
hardware.py
Detecta la RAM, los núcleos y la GPU del equipo y elige el modelo local adecuado.

Regla: el modelo más capaz que quepa con holgura junto a Windows, Whisper y Piper.
Con 8 GB se usa uno ligero que aún entiende órdenes y herramientas; con 64 GB, DeepSeek.
Todo se lee con ctypes y el registro de Windows: sin dependencias ni procesos externos,
para no retrasar el arranque.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ModeloLocal:
    clave: str             # nombre corto para BARK_MODELO_LLM o descargar_modelo.py --modelo
    nombre: str            # nombre legible
    repo: str              # repositorio de Hugging Face
    archivo: str           # archivo .gguf (Q4_K_M)
    tamano_gb: float       # tamaño del archivo
    ram_minima_gb: float   # RAM total a partir de la cual se recomienda
    contexto: int          # n_ctx recomendado
    descripcion: str
    patrones: tuple[str, ...]  # fragmentos del nombre de archivo que lo identifican

    def coincide(self, nombre_archivo: str) -> bool:
        nombre = nombre_archivo.lower()
        return any(p in nombre for p in self.patrones)


# Ordenados de menor a mayor capacidad. ram_minima_gb es la RAM TOTAL del equipo.
CATALOGO: tuple[ModeloLocal, ...] = (
    ModeloLocal(
        "qwen3-1.7b", "Qwen3 1.7B", "unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf",
        1.1, 0, 2048,
        "El más ligero que aún entiende órdenes y elige herramientas. Para equipos de 8 GB o menos.",
        ("qwen3-1.7b",),
    ),
    ModeloLocal(
        "qwen2.5-3b", "Qwen2.5 3B", "Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen2.5-3b-instruct-q4_k_m.gguf",
        2.0, 10, 3072,
        "Modelo anterior de Bark; se conserva como respaldo si ya está descargado.",
        ("qwen2.5-3b",),
    ),
    ModeloLocal(
        "qwen3-4b", "Qwen3 4B Instruct 2507", "unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        2.5, 10, 3072,
        "Equilibrio entre velocidad y comprensión. Para 12-16 GB.",
        ("qwen3-4b-instruct-2507", "qwen3-4b"),
    ),
    ModeloLocal(
        "qwen3-8b", "Qwen3 8B", "unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf",
        5.0, 20, 4096,
        "Razona mejor tareas de varios pasos. Para 24 GB.",
        ("qwen3-8b",),
    ),
    ModeloLocal(
        "qwen3-30b-a3b", "Qwen3 30B-A3B Instruct 2507 (MoE)", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF",
        "Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
        18.6, 30, 4096,
        "Calidad de modelo grande con velocidad de uno de 3B (solo activa 3B por token). Para 32 GB.",
        ("qwen3-30b-a3b",),
    ),
    ModeloLocal(
        "deepseek-r1-32b", "DeepSeek R1 Distill Qwen 32B", "unsloth/DeepSeek-R1-Distill-Qwen-32B-GGUF",
        "DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf",
        19.9, 56, 4096,
        "El más capaz. Para 64 GB o más; sin GPU grande responde más lento que los demás.",
        ("deepseek-r1-distill-qwen-32b", "deepseek-r1-32b"),
    ),
)

# Modelos que sí se recomiendan (el 2.5-3B solo se usa si ya existe).
_RECOMENDABLES = tuple(m for m in CATALOGO if m.clave != "qwen2.5-3b")


@dataclass(frozen=True)
class Equipo:
    ram_total_gb: float
    ram_libre_gb: float
    nucleos_logicos: int
    nucleos_fisicos: int
    gpu: str
    vram_gb: float

    def resumen(self) -> str:
        gpu = f"{self.gpu} ({self.vram_gb:.0f} GB)" if self.gpu else "sin GPU dedicada detectada"
        return (f"{self.ram_total_gb:.0f} GB de RAM ({self.ram_libre_gb:.1f} GB libres), "
                f"{self.nucleos_fisicos} núcleos / {self.nucleos_logicos} hilos, {gpu}")


def _memoria() -> tuple[float, float]:
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        estado = MEMORYSTATUSEX()
        estado.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado)):
            return estado.ullTotalPhys / 1024 ** 3, estado.ullAvailPhys / 1024 ** 3
    try:
        paginas, tamano = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        libres = os.sysconf("SC_AVPHYS_PAGES")
        return paginas * tamano / 1024 ** 3, libres * tamano / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        return 8.0, 4.0


def _nucleos_fisicos(logicos: int) -> int:
    if sys.platform != "win32":
        return max(1, logicos // 2)
    try:
        # GetLogicalProcessorInformationEx(RelationProcessorCore=0): un registro por núcleo físico.
        longitud = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(longitud))
        bufer = (ctypes.c_byte * longitud.value)()
        if not ctypes.windll.kernel32.GetLogicalProcessorInformationEx(0, bufer, ctypes.byref(longitud)):
            raise OSError
        nucleos, desplazamiento = 0, 0
        while desplazamiento < longitud.value:
            tamano = ctypes.c_ulong.from_buffer(bufer, desplazamiento + 4).value
            if tamano == 0:
                break
            nucleos += 1
            desplazamiento += tamano
        return nucleos or max(1, logicos // 2)
    except (OSError, AttributeError, ValueError):
        return max(1, logicos // 2)


def _gpu() -> tuple[str, float]:
    """GPU con más memoria según el registro (qwMemorySize no se trunca a 4 GB como WMI)."""
    if sys.platform != "win32":
        return "", 0.0
    try:
        import winreg
    except ImportError:
        return "", 0.0
    mejor = ("", 0.0)
    clase = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clase) as raiz:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(raiz, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(raiz, sub) as clave:
                        nombre = str(winreg.QueryValueEx(clave, "DriverDesc")[0])
                        try:
                            memoria = winreg.QueryValueEx(clave, "HardwareInformation.qwMemorySize")[0]
                        except OSError:
                            memoria = winreg.QueryValueEx(clave, "HardwareInformation.MemorySize")[0]
                        if isinstance(memoria, bytes):
                            memoria = int.from_bytes(memoria, "little")
                        gb = int(memoria) / 1024 ** 3
                        if gb > mejor[1]:
                            mejor = (nombre, gb)
                except (OSError, ValueError, TypeError):
                    continue
    except OSError:
        pass
    return mejor


@lru_cache(maxsize=1)
def detectar_equipo() -> Equipo:
    total, libre = _memoria()
    logicos = os.cpu_count() or 4
    gpu, vram = _gpu()
    # Las gráficas integradas reservan poca memoria propia: no cuentan como GPU para el LLM.
    if vram < 2:
        gpu, vram = "", 0.0
    return Equipo(round(total, 1), round(libre, 1), logicos, _nucleos_fisicos(logicos), gpu, round(vram, 1))


def modelo_recomendado(equipo: Equipo | None = None) -> ModeloLocal:
    """El más capaz cuya RAM mínima cumple el equipo (con 64 GB → DeepSeek; con 8 GB → Qwen3 1.7B)."""
    equipo = equipo or detectar_equipo()
    # Windows a veces informa 63.7 GB en un equipo de 64: se redondea hacia arriba con margen.
    ram = equipo.ram_total_gb + 0.8
    elegido = _RECOMENDABLES[0]
    for modelo in _RECOMENDABLES:
        if ram >= modelo.ram_minima_gb:
            elegido = modelo
    return elegido


def buscar_en_catalogo(nombre_o_clave: str) -> ModeloLocal | None:
    texto = (nombre_o_clave or "").strip().lower()
    for modelo in CATALOGO:
        if texto == modelo.clave or modelo.coincide(texto):
            return modelo
    return None


def cabe_en_equipo(ruta: str, equipo: Equipo | None = None) -> bool:
    """True si el archivo deja al menos ~4 GB de RAM para Windows y el resto de Bark."""
    equipo = equipo or detectar_equipo()
    try:
        tamano = os.path.getsize(ruta) / 1024 ** 3
    except OSError:
        return False
    return tamano + 4.0 <= equipo.ram_total_gb + 0.8


def elegir_modelo(disponibles: list[str], equipo: Equipo | None = None) -> str | None:
    """Entre los .gguf descargados: el recomendado; si no está, el más capaz que quepa."""
    equipo = equipo or detectar_equipo()
    recomendado = modelo_recomendado(equipo)
    orden = {m.clave: i for i, m in enumerate(CATALOGO)}

    def rango(ruta: str) -> tuple[int, float]:
        modelo = buscar_en_catalogo(os.path.basename(ruta))
        try:
            tamano = os.path.getsize(ruta)
        except OSError:
            tamano = 0
        return (orden[modelo.clave] if modelo else -1, tamano)

    for ruta in disponibles:
        if recomendado.coincide(os.path.basename(ruta)):
            return ruta
    aptos = [r for r in disponibles if cabe_en_equipo(r, equipo)]
    if aptos:
        return max(aptos, key=rango)
    # Nada cabe con holgura: el más pequeño es lo único que tiene opción de cargar.
    return min(disponibles, key=lambda r: rango(r)[1]) if disponibles else None


def capas_gpu_automaticas(ruta: str, equipo: Equipo | None = None) -> int:
    """-1 si el modelo cabe entero en la VRAM; si cabe en parte, cuántas capas (aprox.); 0 sin GPU."""
    equipo = equipo or detectar_equipo()
    if equipo.vram_gb < 2:
        return 0
    try:
        tamano = os.path.getsize(ruta) / 1024 ** 3
    except OSError:
        return 0
    disponible = equipo.vram_gb - 1.0  # contexto y búferes de la propia GPU
    if tamano <= disponible:
        return -1
    modelo = buscar_en_catalogo(os.path.basename(ruta))
    capas = 64 if modelo and modelo.clave == "deepseek-r1-32b" else 48 if modelo and "30b" in modelo.clave else 36
    return max(0, int(capas * disponible / tamano))


def contexto_recomendado(ruta: str, equipo: Equipo | None = None) -> int:
    equipo = equipo or detectar_equipo()
    modelo = buscar_en_catalogo(os.path.basename(ruta))
    contexto = modelo.contexto if modelo else 3072
    return 2048 if equipo.ram_total_gb < 7 else contexto


def hilos_recomendados(equipo: Equipo | None = None) -> int:
    """llama.cpp rinde mejor con un hilo por núcleo físico (los hilos SMT compiten entre sí)."""
    equipo = equipo or detectar_equipo()
    return max(1, min(16, equipo.nucleos_fisicos))


# ============================================================
# Voces Piper (rhasspy/piper-voices) en español
# ============================================================

@dataclass(frozen=True)
class VozPiper:
    clave: str
    descripcion: str
    tamano_mb: int
    calidad: str  # x_low, low, medium, high

    @property
    def archivo(self) -> str:
        return f"{self.clave}.onnx"

    @property
    def url(self) -> str:
        idioma, nombre, calidad = self.clave.split("-")
        familia = idioma.split("_")[0]
        return f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{familia}/{idioma}/{nombre}/{calidad}/{self.clave}.onnx"


VOCES_PIPER: tuple[VozPiper, ...] = (
    VozPiper("es_MX-claude-high", "México · femenina · alta calidad (la más natural en español de México)", 63, "high"),
    VozPiper("es_MX-ald-medium", "México · masculina · calidad media, rápida", 63, "medium"),
    VozPiper("es_MX-ald-x_low", "México · masculina · ultraligera, la más rápida para equipos modestos", 21, "x_low"),
    VozPiper("es_AR-daniela-high", "Argentina · femenina · alta calidad, muy expresiva", 114, "high"),
    VozPiper("es_ES-sharvard-medium", "España · 2 voces · calidad media", 77, "medium"),
    VozPiper("es_ES-davefx-medium", "España · masculina · calidad media", 63, "medium"),
)


def voces_recomendadas(equipo: Equipo | None = None) -> list[VozPiper]:
    """En equipos modestos, voces ligeras (síntesis 2-3 veces más rápida); si sobra, alta calidad."""
    equipo = equipo or detectar_equipo()
    modesto = equipo.ram_total_gb < 10 or equipo.nucleos_fisicos <= 4
    claves = ("es_MX-ald-x_low", "es_MX-ald-medium", "es_MX-claude-high") if modesto else \
             ("es_MX-claude-high", "es_MX-ald-medium", "es_AR-daniela-high")
    return [v for c in claves for v in VOCES_PIPER if v.clave == c]
