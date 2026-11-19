"""Lanzador silencioso de la interfaz con registro visible de fallos."""

from __future__ import annotations

import faulthandler
import sys
import time
import traceback
from pathlib import Path

CARPETA = Path(__file__).resolve().parent / "resultados"
CARPETA.mkdir(exist_ok=True)
REGISTRO = CARPETA / "bark_inicio.log"


def anotar(texto: str) -> None:
    with open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {texto}\n")


def main() -> None:
    REGISTRO.write_text("", encoding="utf-8")
    # Si una DLL nativa tumba el proceso, faulthandler deja el rastro aquí.
    faulthandler.enable(open(CARPETA / "bark_crash.log", "w", encoding="utf-8"))
    anotar(f"Python {sys.version} en {sys.executable}")
    try:
        anotar("importando main…")
        from main import main as iniciar
        anotar("main importado; abriendo interfaz")
        sys.argv = [sys.argv[0], "--ui"]
        iniciar()
        anotar("interfaz cerrada normalmente")
    except BaseException:
        detalle = traceback.format_exc()
        anotar(detalle)
        (CARPETA / "bark_error.log").write_text(detalle, encoding="utf-8")
        try:
            from tkinter import messagebox
            messagebox.showerror("Bark · Error de inicio", f"Bark no pudo iniciarse.\n\n{detalle[-600:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
