"""Descarga el modelo local y las voces adecuados para TU equipo, pidiendo confirmación antes.

Uso:
    python descargar_modelo.py                  # detecta la RAM y recomienda el modelo
    python descargar_modelo.py --lista          # muestra todos los modelos y voces disponibles
    python descargar_modelo.py --modelo qwen3-8b
    python descargar_modelo.py --voces          # voces Piper optimizadas para este equipo
    python descargar_modelo.py --si             # sin preguntar (para scripts)

Guía según la RAM total:
    hasta 8 GB  → Qwen3 1.7B (1.1 GB)      el más ligero que aún entiende órdenes
    12-16 GB    → Qwen3 4B 2507 (2.5 GB)
    24 GB       → Qwen3 8B (5 GB)
    32-48 GB    → Qwen3 30B-A3B MoE (18.6 GB), rápido como uno de 3B
    64 GB o más → DeepSeek R1 Distill Qwen 32B (19.9 GB)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config, hardware  # noqa: E402
from nucleo import descargas  # noqa: E402


def _confirmar(pregunta: str, sin_preguntar: bool) -> bool:
    if sin_preguntar:
        return True
    try:
        return input(f"{pregunta} (s/n): ").strip().lower() in {"s", "si", "sí", "y", "yes"}
    except EOFError:
        return False


def _lista() -> None:
    print("Modelos de lenguaje:")
    for m in hardware.CATALOGO:
        marca = "  ← recomendado" if m.clave == config.MODELO_RECOMENDADO.clave else ""
        print(f"  {m.clave:16s} {m.tamano_gb:5.1f} GB  desde {m.ram_minima_gb:>2.0f} GB RAM  {m.descripcion}{marca}")
    print("\nVoces Piper:")
    recomendadas = {v.clave for v in hardware.voces_recomendadas(config.EQUIPO)}
    for v in hardware.VOCES_PIPER:
        estado = "instalada" if descargas.voz_instalada(v) else ("recomendada" if v.clave in recomendadas else "")
        print(f"  {v.clave:22s} {v.tamano_mb:4d} MB  {v.descripcion}  {estado}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modelo", help="clave del modelo (ver --lista); por defecto, el recomendado")
    parser.add_argument("--voces", action="store_true", help="descarga las voces Piper recomendadas")
    parser.add_argument("--lista", action="store_true", help="muestra modelos y voces")
    parser.add_argument("--si", action="store_true", help="no pedir confirmación")
    args = parser.parse_args()

    print(f"Tu equipo: {config.EQUIPO.resumen()}")
    print(f"Modelo recomendado: {config.MODELO_RECOMENDADO.nombre} · en uso ahora: {config.NOMBRE_MODELO}\n")
    if args.lista:
        _lista()
        return 0

    if args.voces:
        pendientes = descargas.voces_por_descargar()
        if not pendientes:
            print("Ya tienes instaladas las voces recomendadas.")
            return 0
        total = sum(v.tamano_mb for v in pendientes)
        print("Voces a descargar:\n" + "\n".join(f"  {v.clave}: {v.descripcion}" for v in pendientes))
        if not _confirmar(f"¿Descargar ~{total} MB de voces?", args.si):
            print("Cancelado.")
            return 1
        for voz in pendientes:
            try:
                descargas.descargar_voz(voz, print)
            except Exception as error:
                print(f"No pude descargar {voz.clave}: {error}")
        return 0

    modelo = hardware.buscar_en_catalogo(args.modelo) if args.modelo else config.MODELO_RECOMENDADO
    if modelo is None:
        print(f"No conozco el modelo «{args.modelo}». Usa --lista para ver las opciones.")
        return 1
    if modelo.ram_minima_gb > config.EQUIPO.ram_total_gb + 0.8:
        print(f"Aviso: {modelo.nombre} está pensado para {modelo.ram_minima_gb:.0f} GB de RAM o más; "
              f"en este equipo puede ir muy lento o no cargar.")
    libre = _espacio_libre_gb(config.CARPETA_MODELOS)
    if libre is not None and libre < modelo.tamano_gb + 1:
        print(f"No hay espacio suficiente: necesitas {modelo.tamano_gb + 1:.1f} GB y quedan {libre:.1f} GB.")
        return 1
    print(f"{modelo.nombre}: {modelo.descripcion}")
    if not _confirmar(f"¿Descargar {modelo.tamano_gb:.1f} GB a la carpeta modelos/?", args.si):
        print("Cancelado.")
        return 1
    try:
        descargas.descargar_modelo(modelo, print)
    except Exception as error:
        print(f"No pude descargar el modelo: {error}\nRevisa tu conexión o bájalo a mano desde https://huggingface.co/{modelo.repo}")
        return 1
    return 0


def _espacio_libre_gb(carpeta: str):
    try:
        import shutil
        os.makedirs(carpeta, exist_ok=True)
        return shutil.disk_usage(carpeta).free / 1024 ** 3
    except OSError:
        return None


if __name__ == "__main__":
    sys.exit(main())
