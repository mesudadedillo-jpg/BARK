"""
registro.py
Registro central de conectores y herramientas de Bark.

Un *conector* agrupa herramientas de un mismo mundo (red, archivos, música,
un servidor MCP…) y sabe decir si está disponible en este equipo. Una
*herramienta* es una función Python normal que se registra con el decorador
@herramienta; su firma se lee sola para documentarla ante el modelo.

El registro hace por el modelo lo que un modelo pequeño hace mal:
- Corrige nombres de herramienta inventados o mal escritos y sugiere parecidas.
- Reasigna parámetros mal nombrados y convierte tipos («40» → 40).
- Corrige rutas que no existen con el archivo o carpeta real más parecido.
- Pide permiso antes de cualquier herramienta marcada como sensible.
- Elige qué herramientas mostrar en el prompt según lo que pidió el usuario,
  para no gastar contexto en 150 descripciones.
"""

from __future__ import annotations

import difflib
import inspect
import os
from dataclasses import dataclass
from typing import Any, Callable

from acciones.apps import normalizar
from conectores import similitud


@dataclass
class Conector:
    id: str
    nombre: str
    descripcion: str
    # Devuelve None si está disponible o el motivo por el que no lo está.
    requisito: Callable[[], str | None] | None = None
    origen: str = "integrado"  # integrado | mcp

    def motivo_no_disponible(self) -> str | None:
        if self.requisito is None:
            return None
        try:
            return self.requisito()
        except Exception as error:  # un requisito roto no debe tumbar a Bark
            return str(error)


@dataclass
class Herramienta:
    nombre: str
    funcion: Callable[..., Any]
    descripcion: str
    conector: str
    alias: tuple[str, ...] = ()
    palabras: tuple[str, ...] = ()
    sensible: bool = False
    rutas: tuple[str, ...] = ()       # parámetros que deben apuntar a algo existente
    observar: bool = False             # el resultado vuelve al modelo en vez de mostrarse
    esquema: dict | None = None        # parámetros explícitos (herramientas MCP)

    def parametros(self) -> list[tuple[str, bool]]:
        """[(nombre, requerido)] en orden."""
        if self.esquema is not None:
            requeridos = set(self.esquema.get("required", []))
            return [(n, n in requeridos) for n in self.esquema.get("properties", {})]
        try:
            firma = inspect.signature(self.funcion).parameters.values()
        except (TypeError, ValueError):
            return []
        return [
            (p.name, p.default is inspect.Parameter.empty)
            for p in firma
            if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        ]

    def firma(self) -> str:
        partes = [n if req else f"{n}?" for n, req in self.parametros()]
        return f"{self.nombre}({', '.join(partes)})"


@dataclass
class Resultado:
    texto: str
    herramienta: str | None = None
    ok: bool = True
    observar: bool = False
    desconocida: bool = False

    def __str__(self) -> str:
        return self.texto


_VACIAS = {
    "el", "la", "los", "las", "de", "del", "un", "una", "unos", "unas", "y", "o", "a", "al", "en", "que",
    "me", "mi", "mis", "por", "para", "con", "se", "lo", "le", "es", "su", "sus", "como", "quiero",
    "puedes", "podrias", "favor", "porfa", "bark", "oye", "hay", "esta", "este", "esto", "tengo", "dame",
}


def _palabras(texto: str) -> set[str]:
    return {p for p in normalizar(texto).split() if p not in _VACIAS and len(p) > 1}


def _raices(palabras: set[str]) -> set[str]:
    """Recorte a 5 letras: «redes»/«red», «climas»/«clima» coinciden aunque no sean idénticas."""
    return {p[:5] for p in palabras}


class Registro:
    def __init__(self) -> None:
        self.conectores: dict[str, Conector] = {}
        self.herramientas: dict[str, Herramienta] = {}
        self._alias: dict[str, str] = {}
        self.errores_carga: dict[str, str] = {}

    # ------------------------------------------------------------
    # Alta
    # ------------------------------------------------------------
    def conector(self, id: str, nombre: str, descripcion: str,
                 requisito: Callable[[], str | None] | None = None, origen: str = "integrado") -> Conector:
        c = Conector(id, nombre, descripcion, requisito, origen)
        self.conectores[id] = c
        return c

    def agregar(self, h: Herramienta) -> Herramienta:
        if h.conector not in self.conectores:
            raise KeyError(f"Conector desconocido para {h.nombre}: {h.conector}")
        self.herramientas[h.nombre] = h
        for alias in h.alias:
            self._alias[self._clave(alias)] = h.nombre
        return h

    def registrar_funcion(self, funcion: Callable, conector: str, descripcion: str = "", nombre: str | None = None,
                          **opciones) -> Herramienta:
        descripcion = descripcion or (inspect.getdoc(funcion) or "").split("\n")[0]
        return self.agregar(Herramienta(nombre or funcion.__name__, funcion, descripcion, conector, **opciones))

    def herramienta(self, conector: str, descripcion: str = "", nombre: str | None = None, **opciones):
        """Decorador: @REGISTRO.herramienta("red", "Hace ping a un host")."""
        def decorar(funcion):
            self.registrar_funcion(funcion, conector, descripcion, nombre, **opciones)
            return funcion
        return decorar

    def quitar_conector(self, id: str) -> None:
        for nombre in [n for n, h in self.herramientas.items() if h.conector == id]:
            del self.herramientas[nombre]
        self._alias = {a: n for a, n in self._alias.items() if n in self.herramientas}
        self.conectores.pop(id, None)

    # ------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------
    @staticmethod
    def _clave(nombre: str) -> str:
        return normalizar(nombre).replace(" ", "_")

    def obtener(self, nombre: str) -> Herramienta | None:
        return self.herramientas.get(nombre)

    def disponible(self, h: Herramienta) -> str | None:
        return self.conectores[h.conector].motivo_no_disponible()

    def resolver(self, nombre: Any) -> tuple[str | None, list[str]]:
        """Nombre exacto, normalizado, alias o muy parecido. Si no, sugerencias."""
        if not isinstance(nombre, str) or not nombre.strip():
            return None, []
        if nombre in self.herramientas:
            return nombre, []
        clave = self._clave(nombre)
        if clave in self.herramientas:
            return clave, []
        if clave in self._alias:
            return self._alias[clave], []
        nombres = list(self.herramientas)
        cercano = difflib.get_close_matches(clave, nombres, n=1, cutoff=0.8)
        if cercano:
            return cercano[0], []
        cercano_alias = difflib.get_close_matches(clave, list(self._alias), n=1, cutoff=0.85)
        if cercano_alias:
            return self._alias[cercano_alias[0]], []
        sugerencias = [h.nombre for _, h in similitud.mejores(
            nombre.replace("_", " "), self.herramientas.values(),
            clave=lambda h: (h.nombre.replace("_", " "), *h.alias), limite=4, umbral=0.62,
        )]
        if not sugerencias:  # por tema: «enviar_mensaje» → herramientas de mensajería
            sugerencias = [h.nombre for h in self.buscar(nombre.replace("_", " "), limite=4)]
        return None, sugerencias

    def buscar(self, consulta: str, limite: int = 8, solo_disponibles: bool = True) -> list[Herramienta]:
        """Herramientas más relevantes para una petición en lenguaje natural."""
        palabras = _palabras(consulta)
        if not palabras:
            return []
        raices = _raices(palabras)
        puntajes: list[tuple[float, Herramienta]] = []
        for h in self.herramientas.values():
            if solo_disponibles and self.disponible(h):
                continue
            fuertes = _palabras(" ".join((h.nombre.replace("_", " "), *h.alias, *h.palabras)))
            debiles = _palabras(f"{h.descripcion} {self.conectores[h.conector].nombre}") - fuertes
            # Palabra idéntica pesa más que una que solo comparte raíz («comprar» ≠ «comprime»).
            puntaje = (3 * len(palabras & fuertes) + 1.5 * len(raices & _raices(fuertes) - _raices(palabras & fuertes))
                       + len(palabras & debiles) + 0.5 * len(raices & _raices(debiles) - _raices(palabras & debiles)))
            if puntaje:
                puntajes.append((puntaje, h))
        puntajes.sort(key=lambda par: (-par[0], par[1].nombre))
        return [h for _, h in puntajes[:limite]]

    def catalogo(self, herramientas: list[Herramienta] | None = None) -> str:
        lista = herramientas if herramientas is not None else list(self.herramientas.values())
        return "\n".join(
            f"- {h.firma()}: {h.descripcion}{' [pide permiso]' if h.sensible else ''}" for h in lista
        )

    def estado(self) -> list[dict]:
        filas = []
        for c in self.conectores.values():
            motivo = c.motivo_no_disponible()
            filas.append({
                "id": c.id, "nombre": c.nombre, "descripcion": c.descripcion, "origen": c.origen,
                "disponible": motivo is None, "motivo": motivo or "",
                "herramientas": sorted(h.nombre for h in self.herramientas.values() if h.conector == c.id),
            })
        return filas

    # ------------------------------------------------------------
    # Ejecución
    # ------------------------------------------------------------
    def ajustar_parametros(self, h: Herramienta, parametros: Any) -> dict:
        """Reasigna nombres parecidos («app» → «nombre_app»), luego por posición, y convierte tipos."""
        if not isinstance(parametros, dict):
            return {}
        if h.esquema is not None:
            return _ajustar_a_esquema(h.esquema, parametros)
        try:
            firma = inspect.signature(h.funcion).parameters
        except (TypeError, ValueError):
            return parametros
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in firma.values()):
            return parametros
        validos = {k: v for k, v in parametros.items() if k in firma}
        sobrantes = {k: v for k, v in parametros.items() if k not in firma}
        for clave in list(sobrantes):
            libres = [n for n in firma if n not in validos]
            parecido = difflib.get_close_matches(self._clave(clave), libres, n=1, cutoff=0.6) or [
                n for n in libres if self._clave(clave) in n or n in self._clave(clave)
            ][:1]
            if parecido:
                validos[parecido[0]] = sobrantes.pop(clave)
        libres = [n for n in firma if n not in validos]
        for nombre_param, valor in zip(libres, sobrantes.values()):
            validos[nombre_param] = valor
        for nombre_param, valor in list(validos.items()):
            validos[nombre_param] = _convertir(valor, firma[nombre_param].annotation, firma[nombre_param].default)
        return validos

    def ejecutar(self, nombre: Any, parametros: Any = None,
                 permiso: Callable[[str], bool] | None = None) -> Resultado:
        resuelto, sugerencias = self.resolver(nombre)
        if resuelto is None:
            if sugerencias:
                lista = "; ".join(self.herramientas[s].firma() for s in sugerencias)
                texto = f"La herramienta '{nombre}' no existe. Las más parecidas son: {lista}."
            else:
                texto = f"La herramienta '{nombre}' no existe. No finjas haberla ejecutado; usa buscar_herramientas(consulta)."
            return Resultado(texto, None, ok=False, observar=True, desconocida=True)

        h = self.herramientas[resuelto]
        motivo = self.disponible(h)
        if motivo:
            return Resultado(f"El conector «{self.conectores[h.conector].nombre}» no está disponible: {motivo}", h.nombre, ok=False)

        parametros = self.ajustar_parametros(h, parametros or {})
        faltan = [n for n, req in h.parametros() if req and n not in parametros]
        if faltan:
            return Resultado(f"Para {h.nombre} me falta: {', '.join(faltan)}.", h.nombre, ok=False, observar=True)

        notas = []
        for param in h.rutas:
            valor = parametros.get(param)
            if not isinstance(valor, str) or not valor.strip():
                continue
            corregida, opciones = similitud.resolver_ruta(valor)
            if corregida is None:
                if opciones:
                    return Resultado(
                        similitud.texto_sugerencias(valor, opciones, "la ruta"), h.nombre, ok=False,
                    )
                return Resultado(f"No encontré '{valor}' ni nada con un nombre parecido.", h.nombre, ok=False)
            if _limpiar(corregida) != _limpiar(valor):
                notas.append(f"(No existía '{valor}'; usé la ruta más parecida: {corregida})")
            parametros[param] = corregida

        if h.sensible:
            if permiso is None or not permiso(_descripcion_legible(h, parametros)):
                return Resultado("El usuario NO autorizó esta acción.", h.nombre, ok=False)

        try:
            salida = h.funcion(**parametros)
        except TypeError as error:
            return Resultado(f"Parámetros incorrectos para {h.firma()}: {error}", h.nombre, ok=False, observar=True)
        except Exception as error:
            if _es_error_de_red(error):
                return Resultado(f"No pude conectarme a internet para {h.nombre}; revisa tu conexión e inténtalo de nuevo.",
                                 h.nombre, ok=False)
            return Resultado(f"Error al ejecutar '{h.nombre}': {error}", h.nombre, ok=False)
        texto = "" if salida is None else str(salida)
        if notas:
            texto = "\n".join([*notas, texto])
        return Resultado(texto, h.nombre, ok=True, observar=h.observar)


def _es_error_de_red(error: Exception) -> bool:
    try:
        import requests
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    return isinstance(error, (ConnectionError, TimeoutError))


def _limpiar(ruta: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(ruta.strip().strip('"'))))


def _ajustar_a_esquema(esquema: dict, parametros: dict) -> dict:
    """Igual que con funciones locales, pero guiado por el JSON Schema de una herramienta MCP."""
    propiedades = esquema.get("properties") or {}
    if not propiedades:
        return parametros
    ajustados = {k: v for k, v in parametros.items() if k in propiedades}
    for clave, valor in parametros.items():
        if clave in propiedades:
            continue
        libres = [p for p in propiedades if p not in ajustados]
        parecido = difflib.get_close_matches(clave, libres, n=1, cutoff=0.6)
        if parecido:
            ajustados[parecido[0]] = valor
    tipos = {"integer": int, "number": float, "boolean": bool, "string": str}
    for clave, valor in list(ajustados.items()):
        tipo = tipos.get((propiedades[clave] or {}).get("type"))
        if tipo:
            ajustados[clave] = _convertir(valor, tipo, inspect.Parameter.empty)
    return ajustados


def _convertir(valor: Any, anotacion: Any, por_defecto: Any) -> Any:
    tipo = anotacion if anotacion is not inspect.Parameter.empty else (
        type(por_defecto) if por_defecto not in (inspect.Parameter.empty, None) else None
    )
    if isinstance(tipo, str):  # «from __future__ import annotations»
        tipo = {"int": int, "float": float, "bool": bool, "str": str}.get(tipo.split("|")[0].strip())
    try:
        if tipo is bool and isinstance(valor, str):
            return normalizar(valor) in {"true", "si", "1", "yes", "verdadero"}
        if tipo is int and isinstance(valor, (str, float)) and not isinstance(valor, bool):
            return int(float(str(valor).replace("%", "").strip()))
        if tipo is float and isinstance(valor, str):
            return float(valor.replace(",", ".").strip())
        if tipo is str and isinstance(valor, (int, float)):
            return str(valor)
    except ValueError:
        pass
    return valor


def _descripcion_legible(h: Herramienta, parametros: dict) -> str:
    detalle = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in parametros.items())
    return f"{h.descripcion.rstrip('.')} → {h.nombre}({detalle})"


REGISTRO = Registro()
herramienta = REGISTRO.herramienta
conector = REGISTRO.conector


def solo_windows() -> str | None:
    import platform
    return None if platform.system() == "Windows" else "solo funciona en Windows"


def requiere_modulo(*modulos: str) -> Callable[[], str | None]:
    def revisar() -> str | None:
        import importlib.util
        faltan = [m for m in modulos if importlib.util.find_spec(m) is None]
        return f"falta instalar {', '.join(faltan)}" if faltan else None
    return revisar


def requiere_comando(comando: str, solo_en_windows: bool = True) -> Callable[[], str | None]:
    def revisar() -> str | None:
        import shutil
        if solo_en_windows and solo_windows():
            return solo_windows()
        return None if shutil.which(comando) else f"no encontré '{comando}' en el PATH"
    return revisar


def ambos(*requisitos: Callable[[], str | None]) -> Callable[[], str | None]:
    def revisar() -> str | None:
        for r in requisitos:
            motivo = r()
            if motivo:
                return motivo
        return None
    return revisar

