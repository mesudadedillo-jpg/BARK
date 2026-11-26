"""
descargas.py
Descarga del modelo local y de las voces Piper recomendadas para este equipo.
Quien llama (la interfaz o descargar_modelo.py) pide permiso antes: aquí solo se descarga.
"""

from __future__ import annotations

import os
from typing import Callable

from config import config, hardware

Progreso = Callable[[str], None]


def _sin_progreso(_texto: str) -> None:
    pass


def descargar_modelo(modelo: hardware.ModeloLocal | None = None, progreso: Progreso = _sin_progreso) -> str:
    """Descarga el .gguf a modelos/ (reanuda descargas interrumpidas) y devuelve su ruta."""
    modelo = modelo or config.MODELO_RECOMENDADO
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise RuntimeError("Falta huggingface_hub. Ejecuta:  pip install huggingface_hub") from error
    destino = os.path.join(config.CARPETA_MODELOS, modelo.archivo)
    if os.path.isfile(destino):
        progreso(f"{modelo.nombre} ya estaba descargado.")
        return destino
    os.makedirs(config.CARPETA_MODELOS, exist_ok=True)
    progreso(f"Descargando {modelo.nombre} ({modelo.tamano_gb:.1f} GB) desde {modelo.repo}…")
    ruta = hf_hub_download(repo_id=modelo.repo, filename=modelo.archivo, local_dir=config.CARPETA_MODELOS)
    progreso(f"Listo: {os.path.basename(ruta)}. Reinicia Bark para usarlo.")
    return ruta


def voz_instalada(voz: hardware.VozPiper) -> bool:
    carpeta = os.path.join(config.CARPETA_MODELOS, "piper")
    return all(os.path.isfile(os.path.join(carpeta, voz.archivo + extra)) for extra in ("", ".json"))


def descargar_voz(voz: hardware.VozPiper, progreso: Progreso = _sin_progreso) -> str:
    """Descarga el .onnx y su .onnx.json a modelos/piper (primero a .part, luego se renombra)."""
    from nucleo.proveedores import _sesion_red
    carpeta = os.path.join(config.CARPETA_MODELOS, "piper")
    os.makedirs(carpeta, exist_ok=True)
    if voz_instalada(voz):
        progreso(f"La voz {voz.clave} ya estaba instalada.")
        return os.path.join(carpeta, voz.archivo)
    progreso(f"Descargando voz {voz.clave} (~{voz.tamano_mb} MB)…")
    for extra in (".json", ""):  # el .json primero: sin él la voz no carga
        destino = os.path.join(carpeta, voz.archivo + extra)
        parcial = destino + ".part"
        with _sesion_red().get(voz.url + extra, stream=True, timeout=30) as respuesta:
            respuesta.raise_for_status()
            with open(parcial, "wb") as archivo:
                for trozo in respuesta.iter_content(1 << 20):
                    archivo.write(trozo)
        os.replace(parcial, destino)
    progreso(f"Voz {voz.clave} instalada.")
    return os.path.join(carpeta, voz.archivo)


def voces_por_descargar() -> list[hardware.VozPiper]:
    return [v for v in hardware.voces_recomendadas(config.EQUIPO) if not voz_instalada(v)]
