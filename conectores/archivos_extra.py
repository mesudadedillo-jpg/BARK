"""Archivos y carpetas: búsqueda por nombre parecido, copiar, mover, papelera, ZIP y espacio en disco."""

from __future__ import annotations

import os
import shutil
import string
import zipfile
from datetime import datetime

from acciones import comandos
from conectores import similitud
from conectores._util import ES_WINDOWS, comillas_ps, powershell
from conectores.registro import REGISTRO

R = REGISTRO


def _tamano(bytes_: float) -> str:
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if bytes_ < 1024 or unidad == "TB":
            return f"{bytes_:.0f} {unidad}" if unidad == "B" else f"{bytes_:.1f} {unidad}"
        bytes_ /= 1024
    return ""


def _abrir(ruta: str) -> None:
    if ES_WINDOWS:
        os.startfile(ruta)  # type: ignore[attr-defined]
    else:
        import subprocess
        subprocess.Popen(["xdg-open", ruta])


def _recordar_resultados(rutas: list[str]) -> None:
    """Comparte la lista con «abre la primera» (abrir_archivo_encontrado)."""
    comandos._ultimos_archivos_encontrados = list(rutas)


@R.herramienta("archivos", "Busca archivos o carpetas cuyo nombre se parezca al dicho, en tus carpetas personales",
               alias=("buscar_archivo", "encontrar_archivo", "buscar_carpeta"),
               palabras=("busca", "encuentra", "archivo", "carpeta", "donde", "parecido"))
def buscar_archivo_parecido(nombre: str, solo_carpetas: bool = False, carpeta: str = "") -> str:
    raices = None
    if carpeta:
        ruta, _ = similitud.resolver_ruta(carpeta)
        raices = [ruta] if ruta and os.path.isdir(ruta) else None
    ranking = similitud.buscar_por_nombre(nombre, raices=raices, solo_carpetas=solo_carpetas)
    if not ranking:
        return f"No encontré nada parecido a '{nombre}' en tus carpetas personales."
    rutas = [r for _, r in ranking]
    _recordar_resultados(rutas)
    lineas = [f"{i}. {os.path.basename(r)}  —  {os.path.dirname(r)}" for i, r in enumerate(rutas, 1)]
    return "Encontré esto (di «abre la 1» para abrir):\n" + "\n".join(lineas)


@R.herramienta("archivos", "Abre una carpeta por su nombre aunque no digas la ruta completa",
               alias=("abrir_directorio",), palabras=("abre", "carpeta", "directorio"))
def abrir_carpeta(nombre: str) -> str:
    if similitud.parece_ruta(nombre) or similitud.normalizar(nombre) in similitud._ALIAS_CARPETAS:
        directa, _ = similitud.resolver_ruta(nombre)
        if directa and os.path.isdir(directa):
            _abrir(directa)
            return f"Abrí la carpeta {directa}."
    ranking = similitud.buscar_por_nombre(nombre, solo_carpetas=True, limite=5)
    if not ranking:
        return f"No encontré ninguna carpeta parecida a '{nombre}'."
    primero = ranking[0][0]
    segundo = ranking[1][0] if len(ranking) > 1 else 0
    if primero >= similitud.SEGURO and primero - segundo >= similitud.VENTAJA:
        _abrir(ranking[0][1])
        return f"Abrí la carpeta {ranking[0][1]}."
    rutas = [r for _, r in ranking]
    _recordar_resultados(rutas)
    return "Hay varias carpetas parecidas, ¿cuál abro?\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rutas, 1))


@R.herramienta("archivos", "Muestra tamaño, tipo y fechas de un archivo o carpeta", rutas=("ruta",),
               palabras=("informacion", "tamano", "pesa", "fecha"))
def info_archivo(ruta: str) -> str:
    estado = os.stat(ruta)
    if os.path.isdir(ruta):
        total, cantidad = 0, 0
        for base, _dirs, archivos in os.walk(ruta):
            for a in archivos:
                try:
                    total += os.path.getsize(os.path.join(base, a))
                    cantidad += 1
                except OSError:
                    pass
            if cantidad > 20_000:
                break
        tipo = f"carpeta con {cantidad} archivos ({_tamano(total)})"
    else:
        tipo = f"archivo {os.path.splitext(ruta)[1] or 'sin extensión'} de {_tamano(estado.st_size)}"
    modificado = datetime.fromtimestamp(estado.st_mtime).strftime("%d/%m/%Y %H:%M")
    return f"{ruta}\nEs un {tipo}. Modificado: {modificado}."


@R.herramienta("archivos", "Archivos modificados más recientemente en tus carpetas personales",
               alias=("archivos_recientes", "ultimos_archivos"), palabras=("recientes", "ultimos", "reciente"))
def archivos_recientes(limite: int = 10) -> str:
    candidatos = []
    for raiz in similitud.carpetas_personales():
        for base, dirs, archivos in os.walk(raiz):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in similitud._OMITIR]
            for a in archivos:
                ruta = os.path.join(base, a)
                try:
                    candidatos.append((os.path.getmtime(ruta), ruta))
                except OSError:
                    pass
            if len(candidatos) > 40_000:
                break
    candidatos.sort(reverse=True)
    rutas = [r for _, r in candidatos[:max(1, min(int(limite), 30))]]
    if not rutas:
        return "No encontré archivos recientes."
    _recordar_resultados(rutas)
    return "Archivos recientes:\n" + "\n".join(
        f"{i}. {os.path.basename(r)} ({datetime.fromtimestamp(os.path.getmtime(r)):%d/%m %H:%M})" for i, r in enumerate(rutas, 1)
    )


def _destino_final(origen: str, destino: str) -> str:
    destino = os.path.expanduser(destino.strip().strip('"'))
    corregido, _ = similitud.resolver_ruta(destino)
    if corregido and os.path.isdir(corregido):
        return os.path.join(corregido, os.path.basename(origen))
    return destino


@R.herramienta("archivos", "Copia un archivo o carpeta a otro lugar", alias=("duplicar_archivo",),
               palabras=("copia", "duplica"), sensible=True, rutas=("origen",))
def copiar_archivo(origen: str, destino: str) -> str:
    final = _destino_final(origen, destino)
    if os.path.exists(final):
        return f"Ya existe {final}; no lo sobrescribo."
    if os.path.isdir(origen):
        shutil.copytree(origen, final)
    else:
        os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
        shutil.copy2(origen, final)
    return f"Copié a {final}."


@R.herramienta("archivos", "Mueve un archivo o carpeta a otro lugar", palabras=("mueve", "pasa"), sensible=True, rutas=("origen",))
def mover_archivo(origen: str, destino: str) -> str:
    final = _destino_final(origen, destino)
    if os.path.exists(final):
        return f"Ya existe {final}; no lo sobrescribo."
    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
    shutil.move(origen, final)
    return f"Moví a {final}."


@R.herramienta("archivos", "Cambia el nombre de un archivo o carpeta", alias=("cambiar_nombre",),
               palabras=("renombra", "nombre"), sensible=True, rutas=("ruta",))
def renombrar_archivo(ruta: str, nuevo_nombre: str) -> str:
    nuevo_nombre = os.path.basename(nuevo_nombre.strip().strip('"'))
    if not nuevo_nombre:
        return "Dime el nombre nuevo."
    if not os.path.splitext(nuevo_nombre)[1] and os.path.isfile(ruta):
        nuevo_nombre += os.path.splitext(ruta)[1]  # conserva la extensión si no la dijo
    final = os.path.join(os.path.dirname(ruta), nuevo_nombre)
    if os.path.exists(final):
        return f"Ya existe {final}."
    os.rename(ruta, final)
    return f"Renombré a {nuevo_nombre}."


@R.herramienta("archivos", "Manda un archivo o carpeta a la Papelera de reciclaje (se puede recuperar)",
               alias=("borrar_a_papelera", "enviar_papelera"), palabras=("papelera", "borra", "elimina", "tira"),
               sensible=True, rutas=("ruta",))
def mover_a_papelera(ruta: str) -> str:
    if not ES_WINDOWS:
        return "La papelera solo está disponible en Windows."
    metodo = "DeleteDirectory" if os.path.isdir(ruta) else "DeleteFile"
    codigo, salida = powershell(
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{metodo}({comillas_ps(ruta)}, 'OnlyErrorDialogs', 'SendToRecycleBin')"
    )
    if codigo != 0 or os.path.exists(ruta):
        return f"No pude mandarlo a la papelera: {salida or 'sigue existiendo'}"
    return f"Mandé {os.path.basename(ruta)} a la Papelera de reciclaje."


@R.herramienta("archivos", "Comprime un archivo o carpeta en un .zip", alias=("crear_zip", "zip"),
               palabras=("comprime", "zip", "empaqueta"), sensible=True, rutas=("ruta",))
def comprimir_zip(ruta: str, destino: str = "") -> str:
    destino = destino or os.path.splitext(ruta.rstrip("\\/"))[0] + ".zip"
    if not destino.lower().endswith(".zip"):
        destino += ".zip"
    if os.path.exists(destino):
        return f"Ya existe {destino}."
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(ruta):
            for base, _dirs, archivos in os.walk(ruta):
                for a in archivos:
                    completo = os.path.join(base, a)
                    zf.write(completo, os.path.relpath(completo, os.path.dirname(ruta)))
        else:
            zf.write(ruta, os.path.basename(ruta))
    return f"Creé {destino} ({_tamano(os.path.getsize(destino))})."


@R.herramienta("archivos", "Extrae un .zip en una carpeta", alias=("extraer_zip", "unzip"),
               palabras=("descomprime", "extrae", "zip"), sensible=True, rutas=("ruta",))
def descomprimir_zip(ruta: str, destino: str = "") -> str:
    destino = os.path.abspath(destino or os.path.splitext(ruta)[0])
    with zipfile.ZipFile(ruta) as zf:
        for miembro in zf.namelist():  # evita «zip slip»: nada puede salir del destino
            final = os.path.abspath(os.path.join(destino, miembro))
            if os.path.commonpath([destino, final]) != destino:
                return f"El zip contiene una ruta peligrosa ({miembro}); no lo extraje."
        zf.extractall(destino)
    return f"Extraje {len(zf.namelist())} elementos en {destino}."


@R.herramienta("archivos", "Espacio libre y usado de cada disco", alias=("espacio_disco", "almacenamiento"),
               palabras=("espacio", "disco", "almacenamiento", "libre", "lleno"))
def espacio_en_disco() -> str:
    unidades = [f"{l}:\\" for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")] if ES_WINDOWS else ["/"]
    lineas = []
    for u in unidades:
        try:
            uso = shutil.disk_usage(u)
        except OSError:
            continue
        porcentaje = uso.used / uso.total * 100 if uso.total else 0
        lineas.append(f"{u}  {_tamano(uso.free)} libres de {_tamano(uso.total)} ({porcentaje:.0f}% usado)")
    return "\n".join(lineas) or "No pude leer los discos."
