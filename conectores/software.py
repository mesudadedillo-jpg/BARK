"""Instalar, actualizar y desinstalar programas con winget, y una terminal con permiso."""

from __future__ import annotations

import os
import re

from conectores import similitud
from conectores._util import ES_WINDOWS, comillas_ps, ejecutar, powershell, recortar
from conectores.registro import REGISTRO, requiere_comando

R = REGISTRO
R.conector("software", "Instalar programas (winget)", "Busca, instala, actualiza y desinstala programas con el gestor oficial de Windows.",
           requisito=requiere_comando("winget"))
R.conector("terminal", "Terminal", "Ejecuta comandos de PowerShell con tu permiso y bloquea los destructivos.")

_ACEPTAR = ["--accept-source-agreements", "--disable-interactivity"]


def _tabla_winget(texto: str) -> list[dict]:
    """Convierte la tabla de texto de winget en filas {nombre, id, version, extra}."""
    lineas = [l.rstrip() for l in texto.splitlines()]
    separador = next((i for i, l in enumerate(lineas) if re.fullmatch(r"-{10,}", l.strip())), None)
    if separador is None or separador == 0:
        return []
    encabezado = lineas[separador - 1]
    # Los avisos de progreso de winget («\ | /») quedan pegados al encabezado.
    encabezado = encabezado[max(0, len(encabezado) - len(lineas[separador])):]
    inicios = [m.start() for m in re.finditer(r"(?:(?<=\s)|^)\S", encabezado)]
    filas = []
    for linea in lineas[separador + 1:]:
        if not linea.strip() or len(linea) < (inicios[1] if len(inicios) > 1 else 1):
            continue
        cortes = inicios + [len(linea) + 1]
        celdas = [linea[cortes[i]:cortes[i + 1]].strip() for i in range(len(inicios))]
        if len(celdas) >= 2 and celdas[1]:
            filas.append({"nombre": celdas[0], "id": celdas[1],
                          "version": celdas[2] if len(celdas) > 2 else "", "extra": " ".join(celdas[3:])})
    return filas


def _winget(*argumentos: str, timeout: int = 60) -> tuple[int, str]:
    return ejecutar(["winget", *argumentos, *_ACEPTAR], timeout=timeout)


def _elegir_paquete(nombre: str, filas: list[dict]) -> tuple[dict | None, list[dict]]:
    if re.fullmatch(r"[\w.+-]+\.[\w.+-]+", nombre.strip()):  # ya es un Id («Discord.Discord»)
        exacto = next((f for f in filas if f["id"].lower() == nombre.strip().lower()), None)
        if exacto:
            return exacto, []
    # Preferimos el repositorio winget sobre msstore cuando empatan.
    filas = sorted(filas, key=lambda f: "msstore" in f["extra"].lower())
    return similitud.elegir(nombre, filas, clave=lambda f: (f["nombre"], f["id"].split(".")[-1]))


@R.herramienta("software", "Busca programas disponibles para instalar", alias=("buscar_programa_winget",),
               palabras=("busca", "programa", "descargar", "instalar", "winget"))
def buscar_programa(nombre: str) -> str:
    _c, salida = _winget("search", nombre)
    filas = _tabla_winget(salida)
    if not filas:
        return f"winget no encontró '{nombre}'."
    return "\n".join(f"{f['nombre']} — Id: {f['id']} ({f['version']})" for f in filas[:10])


@R.herramienta("software", "Instala un programa por su nombre aproximado o Id de winget",
               alias=("instalar_app", "descargar_programa", "instalar"), palabras=("instala", "instalar", "descarga", "programa"),
               sensible=True)
def instalar_programa(nombre: str) -> str:
    _c, salida = _winget("search", nombre)
    elegido, opciones = _elegir_paquete(nombre, _tabla_winget(salida))
    if not elegido:
        if not opciones:
            return f"No encontré '{nombre}' en winget."
        return "Hay varios parecidos; dime el Id exacto:\n" + "\n".join(f"- {f['nombre']} (Id: {f['id']})" for f in opciones[:5])
    codigo, salida = _winget("install", "--id", elegido["id"], "-e", "--silent", "--accept-package-agreements", timeout=1800)
    if codigo == 0:
        return f"Instalé {elegido['nombre']} ({elegido['id']})."
    return f"winget no pudo instalar {elegido['nombre']}:\n{recortar(salida, 600)}"


@R.herramienta("software", "Lista programas con actualizaciones pendientes", alias=("actualizaciones_pendientes",),
               palabras=("actualizaciones", "pendientes", "desactualizados", "versiones"))
def listar_actualizaciones() -> str:
    _c, salida = _winget("upgrade")
    filas = _tabla_winget(salida)
    if not filas:
        return "Todo está actualizado (o winget no pudo revisar)."
    return "\n".join(f"{f['nombre']}: {f['version']} → {f['extra'].split()[0] if f['extra'] else '?'}" for f in filas[:30])


@R.herramienta("software", "Actualiza un programa, o todos si nombre está vacío", alias=("actualizar_apps",),
               palabras=("actualiza", "actualizar", "upgrade"), sensible=True)
def actualizar_programas(nombre: str = "") -> str:
    if not nombre:
        codigo, salida = _winget("upgrade", "--all", "--silent", "--accept-package-agreements", timeout=3600)
        return "Actualicé los programas." if codigo == 0 else f"Algunas actualizaciones fallaron:\n{recortar(salida, 800)}"
    _c, salida = _winget("upgrade")
    elegido, opciones = _elegir_paquete(nombre, _tabla_winget(salida))
    if not elegido:
        return similitud.texto_sugerencias(nombre, [f["nombre"] for f in opciones], "una actualización pendiente")
    codigo, salida = _winget("upgrade", "--id", elegido["id"], "-e", "--silent", "--accept-package-agreements", timeout=1800)
    return f"Actualicé {elegido['nombre']}." if codigo == 0 else f"No pude actualizar {elegido['nombre']}:\n{recortar(salida, 600)}"


@R.herramienta("software", "Desinstala un programa por su nombre aproximado", alias=("desinstalar_app", "quitar_programa"),
               palabras=("desinstala", "desinstalar", "quita", "elimina", "programa"), sensible=True)
def desinstalar_programa(nombre: str) -> str:
    _c, salida = _winget("list", timeout=90)
    elegido, opciones = _elegir_paquete(nombre, _tabla_winget(salida))
    if not elegido:
        return similitud.texto_sugerencias(nombre, [f["nombre"] for f in opciones], "un programa instalado")
    codigo, salida = _winget("uninstall", "--id", elegido["id"], "-e", "--silent", timeout=900)
    return f"Desinstalé {elegido['nombre']}." if codigo == 0 else f"No pude desinstalar {elegido['nombre']}:\n{recortar(salida, 600)}"


# ============================================================
# Terminal
# ============================================================

_PROHIBIDOS = re.compile(
    r"\bformat(?:-volume)?\b\s+[a-z]:|\bdiskpart\b|\bbcdedit\b|\bvssadmin\b.*\bdelete\b|\bcipher\b.*\s/w|"
    r"\breg(?:\.exe)?\s+delete\s+hk(?:lm|ey_local_machine)|\bremove-item\b.*-recurse.*[a-z]:\\?\s*(?:$|['\"])|"
    r"\brm\s+-rf\s+/(?:\s|$)|\bdel\s+/[sq].*[a-z]:\\\s*$|\bclear-disk\b|\binitialize-disk\b|\bmkfs\b|"
    r"\binvoke-expression\b.*\b(?:iwr|invoke-webrequest|downloadstring)\b|\biex\b.*\b(?:iwr|irm|downloadstring)\b",
    re.I,
)


@R.herramienta("terminal", "Ejecuta un comando de PowerShell y devuelve su salida", alias=("powershell", "cmd", "comando", "consola"),
               palabras=("comando", "terminal", "powershell", "consola", "ejecuta", "cmd"), sensible=True)
def ejecutar_comando(comando: str, carpeta: str = "") -> str:
    comando = comando.strip()
    if not comando:
        return "¿Qué comando ejecuto?"
    if _PROHIBIDOS.search(comando):
        return "Ese comando puede borrar datos o dañar el sistema; no lo ejecuto. Si de verdad lo necesitas, hazlo tú manualmente."
    cwd = None
    if carpeta:
        cwd, _ = similitud.resolver_ruta(carpeta)
        if not cwd or not os.path.isdir(cwd):
            return f"No encontré la carpeta '{carpeta}'."
    if ES_WINDOWS:
        if cwd:
            comando = f"Set-Location -LiteralPath {comillas_ps(cwd)}; {comando}"
        codigo, salida = powershell(comando, timeout=120)
    else:
        codigo, salida = ejecutar(["bash", "-lc", comando], timeout=120, cwd=cwd)
    return f"(código {codigo})\n{recortar(salida or '(sin salida)', 3000)}"
