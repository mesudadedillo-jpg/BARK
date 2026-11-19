"""
similitud.py
Búsqueda difusa reutilizable: «si no encuentro algo, busco lo más parecido».

Lo usan todos los conectores para nombres de herramientas, parámetros,
archivos, carpetas, contactos, redes Wi-Fi, servicios, sitios web, etc.
La puntuación es la misma que ya resolvía apps de Windows (acciones/apps.py):
tokens sin acentos, prefijos, texto contiguo y SequenceMatcher.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable, TypeVar

from acciones.apps import normalizar, puntuar

T = TypeVar("T")

# Umbrales compartidos: por encima de SEGURO se usa sin preguntar si le saca
# VENTAJA al segundo; entre SUGERIR y SEGURO solo se propone al usuario.
SEGURO = 0.80
VENTAJA = 0.06
SUGERIR = 0.55


def mejores(
    consulta: str,
    candidatos: Iterable[T],
    clave: Callable[[T], str | Iterable[str]] | None = None,
    limite: int = 5,
    umbral: float = SUGERIR,
) -> list[tuple[float, T]]:
    """Ordena candidatos por parecido con la consulta.

    `clave` puede devolver un texto o varios (nombre + alias): cuenta el mejor.
    """
    resultado: list[tuple[float, T]] = []
    for candidato in candidatos:
        textos = clave(candidato) if clave else candidato
        if isinstance(textos, str):
            textos = (textos,)
        puntaje = max((puntuar(consulta, t) for t in textos if t), default=0.0)
        if puntaje >= umbral:
            resultado.append((puntaje, candidato))
    resultado.sort(key=lambda par: par[0], reverse=True)
    return resultado[:limite]


def elegir(
    consulta: str,
    candidatos: Iterable[T],
    clave: Callable[[T], str | Iterable[str]] | None = None,
) -> tuple[T | None, list[T]]:
    """Devuelve (elegido, sugerencias).

    elegido es el candidato si la coincidencia es clara; si hay duda queda en
    None y `sugerencias` trae las opciones más parecidas para preguntar.
    """
    ranking = mejores(consulta, candidatos, clave, limite=5)
    if not ranking:
        return None, []
    primero = ranking[0][0]
    segundo = ranking[1][0] if len(ranking) > 1 else 0.0
    if primero >= 0.999 or (primero >= SEGURO and primero - segundo >= VENTAJA):
        return ranking[0][1], []
    return None, [c for _, c in ranking]


def texto_sugerencias(consulta: str, sugerencias: list[str], que: str = "eso") -> str:
    if not sugerencias:
        return f"No encontré {que} llamado '{consulta}' ni nada parecido."
    opciones = ", ".join(f"«{s}»" for s in sugerencias[:4])
    return f"No encontré {que} llamado '{consulta}'. ¿Quisiste decir {opciones}?"


# ============================================================
# Rutas: «Escritorio\bark ai\config» → C:\Users\…\Desktop\bark_AI\config
# ============================================================

_ALIAS_CARPETAS = {
    "escritorio": "Desktop", "documentos": "Documents", "mis documentos": "Documents",
    "descargas": "Downloads", "imagenes": "Pictures", "fotos": "Pictures",
    "musica": "Music", "videos": "Videos",
}


def _expandir_alias(ruta: str) -> str:
    """Traduce el primer segmento si es una carpeta personal dicha en español."""
    partes = ruta.replace("/", "\\").split("\\")
    clave = normalizar(partes[0])
    if clave in _ALIAS_CARPETAS:
        partes[0] = os.path.join(os.path.expanduser("~"), _ALIAS_CARPETAS[clave])
        return os.path.join(*partes)
    return ruta


def parece_ruta(texto: str) -> bool:
    """¿Es una ruta («C:\\…», «docs/a.txt», «~/x») o solo un nombre suelto?"""
    texto = str(texto).strip()
    return os.path.isabs(texto) or "/" in texto or "\\" in texto or texto.startswith(("~", "."))


def resolver_ruta(ruta: str) -> tuple[str | None, list[str]]:
    """Corrige cada segmento de una ruta que no existe con el nombre real más parecido.

    Devuelve (ruta_corregida, sugerencias). Si algún segmento es ambiguo se
    detiene y devuelve las opciones de ese nivel para preguntar al usuario.
    """
    ruta = os.path.expanduser(_expandir_alias(str(ruta).strip().strip('"')))
    if os.path.exists(ruta):
        return os.path.abspath(ruta), []
    if not parece_ruta(ruta):
        # Solo un nombre («reporte final»): se busca en tus carpetas, no junto a Bark.
        ranking = buscar_por_nombre(ruta, limite=5)
        if not ranking:
            return None, []
        primero = ranking[0][0]
        segundo = ranking[1][0] if len(ranking) > 1 else 0.0
        if primero >= 0.999 or (primero >= SEGURO and primero - segundo >= VENTAJA):
            return ranking[0][1], []
        return None, [r for _, r in ranking]
    ruta = os.path.abspath(ruta)
    # Sube hasta el primer ancestro que exista y reconstruye desde ahí.
    pendientes: list[str] = []
    actual = ruta
    while not os.path.exists(actual):
        padre, nombre = os.path.split(actual)
        if not nombre or padre == actual:
            return None, []
        pendientes.insert(0, nombre)
        actual = padre
    for segmento in pendientes:
        try:
            entradas = os.listdir(actual)
        except OSError:
            return None, []
        exacta = next((e for e in entradas if e.lower() == segmento.lower()), None)
        if exacta:
            actual = os.path.join(actual, exacta)
            continue
        # Permite omitir la extensión: «tarea final» → «Tarea Final.docx».
        elegido, sugerencias = elegir(segmento, entradas, clave=lambda e: (e, os.path.splitext(e)[0]))
        if elegido is None:
            return None, [os.path.join(actual, s) for s in sugerencias]
        actual = os.path.join(actual, elegido)
    return actual, []


def carpetas_personales() -> list[str]:
    inicio = os.path.expanduser("~")
    carpetas = [os.path.join(inicio, n) for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
    # OneDrive mueve Escritorio/Documentos en muchas instalaciones de Windows.
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        carpetas += [os.path.join(onedrive, n) for n in ("Desktop", "Documents", "Escritorio", "Documentos")]
    return [c for c in dict.fromkeys(carpetas) if os.path.isdir(c)]


_OMITIR = {"node_modules", "__pycache__", ".git", "venv", ".venv", "appdata", "$recycle.bin", "modelos"}


def buscar_por_nombre(
    consulta: str,
    raices: Iterable[str] | None = None,
    solo_carpetas: bool = False,
    profundidad: int = 4,
    limite: int = 8,
    max_entradas: int = 40_000,
) -> list[tuple[float, str]]:
    """Recorre carpetas y devuelve las rutas cuyo nombre más se parece a la consulta."""
    candidatos: list[str] = []
    vistos = 0
    for raiz in raices or carpetas_personales():
        pila = [(raiz, 0)]
        while pila and vistos < max_entradas:
            carpeta, nivel = pila.pop()
            try:
                with os.scandir(carpeta) as it:
                    for entrada in it:
                        vistos += 1
                        if entrada.name.startswith(".") or entrada.name.lower() in _OMITIR:
                            continue
                        es_dir = entrada.is_dir(follow_symlinks=False)
                        if es_dir and nivel < profundidad:
                            pila.append((entrada.path, nivel + 1))
                        if es_dir or not solo_carpetas:
                            candidatos.append(entrada.path)
            except OSError:
                continue
    nombre = lambda r: (os.path.basename(r), os.path.splitext(os.path.basename(r))[0])
    return mejores(consulta, candidatos, clave=nombre, limite=limite)
