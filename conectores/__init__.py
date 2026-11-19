"""
Conectores de Bark.

Importar este paquete registra todos los conectores integrados. Si un módulo
falla (una dependencia rota, un error de sintaxis en un conector nuevo), Bark
sigue funcionando con los demás y el fallo aparece en listar_conectores().

Para agregar un conector nuevo: crea conectores/mi_conector.py, declara
REGISTRO.conector(...) y decora funciones con @REGISTRO.herramienta(...);
luego añade su nombre a _MODULOS. Guía completa: docs/CONECTORES.md.
"""

from __future__ import annotations

import importlib

from conectores.registro import REGISTRO, Herramienta, Resultado

__all__ = ["REGISTRO", "Herramienta", "Resultado", "cargar"]

_MODULOS = (
    "integrados", "archivos_extra", "info_equipo", "red", "software", "productividad",
    "comunicacion", "informacion", "navegacion", "desarrollo", "teclado", "mcp",
)

REGISTRO.conector("nucleo", "Núcleo de Bark", "Buscar herramientas, ver conectores y recargar servidores MCP.")


@REGISTRO.herramienta("nucleo", "Busca qué herramientas sirven para una tarea cuando ninguna de la lista encaja",
                      alias=("tool_search", "buscar_herramienta", "que_herramienta"), observar=True)
def buscar_herramientas(consulta: str) -> str:
    encontradas = REGISTRO.buscar(consulta, limite=8)
    if not encontradas:
        return "Ninguna herramienta parece servir para eso. Dilo honestamente al usuario."
    return "Herramientas que pueden servir:\n" + REGISTRO.catalogo(encontradas)


@REGISTRO.herramienta("nucleo", "Muestra los conectores de Bark, si están disponibles y cuántas herramientas tienen",
                      alias=("conectores", "mis_conectores", "que_puedes_hacer"),
                      palabras=("conectores", "capacidades", "integraciones", "herramientas"))
def listar_conectores() -> str:
    lineas = []
    for c in REGISTRO.estado():
        marca = "✓" if c["disponible"] else "✗"
        detalle = f"{len(c['herramientas'])} herramientas" if c["disponible"] else c["motivo"]
        lineas.append(f"{marca} {c['nombre']} — {detalle}")
    for modulo, error in REGISTRO.errores_carga.items():
        lineas.append(f"✗ {modulo} — error al cargar: {error}")
    total = sum(1 for h in REGISTRO.herramientas.values() if not REGISTRO.disponible(h))
    return f"{total} herramientas listas en {len(REGISTRO.conectores)} conectores:\n" + "\n".join(lineas)


_cargado = False


def cargar() -> None:
    global _cargado
    if _cargado:
        return
    _cargado = True
    for nombre in _MODULOS:
        try:
            importlib.import_module(f"conectores.{nombre}")
        except Exception as error:  # un conector roto no debe impedir que Bark arranque
            REGISTRO.errores_carga[nombre] = f"{type(error).__name__}: {error}"


cargar()
