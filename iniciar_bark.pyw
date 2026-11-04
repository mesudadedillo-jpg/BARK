"""Lanzador silencioso de la interfaz con registro visible de fallos."""

from __future__ import annotations

import traceback
import sys
from pathlib import Path
from tkinter import messagebox


def main() -> None:
    try:
        from main import main as iniciar
        # pythonw no tiene stdin: se debe solicitar explícitamente el modo UI
        # antes de delegar al punto de entrada normal.
        sys.argv = [sys.argv[0], "--ui"]
        iniciar()
    except Exception:
        detalle = traceback.format_exc()
        carpeta = Path(__file__).resolve().parent / "resultados"
        carpeta.mkdir(exist_ok=True)
        registro = carpeta / "bark_error.log"
        registro.write_text(detalle, encoding="utf-8")
        messagebox.showerror(
            "Bark · Error de inicio",
            f"Bark no pudo iniciarse.\n\nSe guardó el detalle en:\n{registro}",
        )


if __name__ == "__main__":
    main()
