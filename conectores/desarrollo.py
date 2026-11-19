"""Desarrollo: estado de repositorios Git y abrir proyectos en VS Code."""

from __future__ import annotations

import os
import shutil
import subprocess

from conectores._util import ejecutar, recortar
from conectores.registro import REGISTRO, requiere_comando

R = REGISTRO
R.conector("desarrollo", "Git y VS Code", "Estado e historial de repositorios Git y abrir carpetas en Visual Studio Code.",
           requisito=requiere_comando("git", solo_en_windows=False))


@R.herramienta("desarrollo", "Rama, cambios sin guardar y sincronía de un repositorio Git", alias=("git_status",),
               palabras=("git", "repositorio", "repo", "cambios", "rama", "commit"), rutas=("ruta",))
def estado_git(ruta: str = ".") -> str:
    codigo, salida = ejecutar(["git", "-C", ruta, "status", "--short", "--branch"], timeout=20)
    if codigo != 0:
        return f"'{ruta}' no parece un repositorio Git: {recortar(salida, 200)}"
    lineas = salida.splitlines()
    cambios = len(lineas) - 1
    return f"{lineas[0].lstrip('# ')}\n" + (f"{cambios} archivos con cambios:\n" + "\n".join(lineas[1:25]) if cambios else "Sin cambios pendientes.")


@R.herramienta("desarrollo", "Últimos commits de un repositorio", alias=("git_log", "historial_git"),
               palabras=("commits", "historial", "git", "log"), rutas=("ruta",))
def ultimos_commits(ruta: str = ".", cantidad: int = 5) -> str:
    codigo, salida = ejecutar(["git", "-C", ruta, "log", f"-{max(1, min(int(cantidad), 30))}",
                               "--pretty=format:%h  %ad  %s", "--date=short"], timeout=20)
    return salida if codigo == 0 and salida else f"No pude leer el historial: {recortar(salida, 200)}"


@R.herramienta("desarrollo", "Abre una carpeta o archivo en Visual Studio Code", alias=("abrir_vscode", "code"),
               palabras=("vscode", "visual", "studio", "code", "editor", "proyecto"), rutas=("ruta",))
def abrir_en_vscode(ruta: str) -> str:
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        return "No encontré VS Code en el PATH (instálalo con «Agregar a PATH»)."
    subprocess.Popen([code, ruta], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"Abrí {os.path.basename(ruta.rstrip(os.sep)) or ruta} en VS Code."
