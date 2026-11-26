"""Bark crea sus propios conectores: un catálogo de servidores MCP conocidos y herramientas
para instalarlos, agregar uno a la medida y quitarlos, sin editar JSON a mano.

Las claves NUNCA se escriben en conectores_mcp.json: el servidor las recibe como
${VARIABLE} desde .env, y el usuario las pone en ⚙ Ajustes › Claves y datos.
"""

from __future__ import annotations

import os
import re
import shutil

from acciones.apps import normalizar
from conectores import mcp, similitud
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("conectores", "Crear conectores",
           "Instala conectores del catálogo (ElevenLabs, GitHub, archivos, web…) o crea uno nuevo con cualquier servidor MCP.")

# Recetas verificadas contra la documentación oficial de cada servidor.
CATALOGO: dict[str, dict] = {
    "elevenlabs": {
        "descripcion": "Voces realistas, efectos de sonido, clonación y transcripción con ElevenLabs.",
        "comando": "uvx", "argumentos": ["elevenlabs-mcp"],
        "entorno": {"ELEVENLABS_API_KEY": "${ELEVENLABS_API_KEY}", "ELEVENLABS_MCP_BASE_PATH": "${USERPROFILE}/Desktop"},
        "claves": {"ELEVENLABS_API_KEY": "https://elevenlabs.io/app/settings/api-keys"},
        "nota": "El servidor local de ElevenLabs funciona, pero está archivado desde agosto de 2026 (ya no recibe mejoras).",
        "sinonimos": ("eleven labs", "voces", "voz realista", "clonar voz"),
    },
    "github": {
        "descripcion": "Repositorios, issues y pull requests de GitHub (servidor remoto oficial).",
        "url": "https://api.githubcopilot.com/mcp/",
        "encabezados": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
        "claves": {"GITHUB_TOKEN": "https://github.com/settings/tokens"},
        "sinonimos": ("repositorio", "repos", "pull request", "issues"),
    },
    "archivos": {
        "descripcion": "Leer, buscar y editar archivos dentro de Documentos.",
        "comando": "npx", "argumentos": ["-y", "@modelcontextprotocol/server-filesystem", "${USERPROFILE}/Documents"],
        "confiables": ["read_text_file", "list_directory", "directory_tree", "search_files", "get_file_info",
                       "list_allowed_directories"],
        "sinonimos": ("filesystem", "documentos", "sistema de archivos"),
    },
    "web": {
        "descripcion": "Descarga páginas web y las convierte a texto para leerlas.",
        "comando": "uvx", "argumentos": ["mcp-server-fetch"], "confiables": ["fetch"],
        "sinonimos": ("fetch", "paginas", "internet", "leer web"),
    },
    "memoria_grafo": {
        "descripcion": "Memoria de largo plazo en forma de grafo (personas, cosas y relaciones).",
        "comando": "npx", "argumentos": ["-y", "@modelcontextprotocol/server-memory"],
        "sinonimos": ("memory", "grafo", "recordar"),
    },
    "git": {
        "descripcion": "Historial, diferencias y commits de un repositorio Git local.",
        "comando": "uvx", "argumentos": ["mcp-server-git"],
        "sinonimos": ("commits", "historial", "repositorio local"),
    },
    "hora": {
        "descripcion": "Hora actual y conversión entre zonas horarias.",
        "comando": "uvx", "argumentos": ["mcp-server-time"], "confiables": ["get_current_time", "convert_time"],
        "sinonimos": ("time", "zona horaria", "husos"),
    },
}

_CAMPOS_RECETA = ("descripcion", "comando", "argumentos", "entorno", "url", "encabezados", "confiables")


def _buscar_receta(nombre: str) -> str | None:
    clave = normalizar(nombre).replace(" ", "_")
    if clave in CATALOGO:
        return clave
    opciones = {k: k for k in CATALOGO} | {s: k for k, r in CATALOGO.items() for s in r.get("sinonimos", ())}
    mejor, _sugerencias = similitud.elegir(nombre, list(opciones))
    return opciones.get(mejor) if mejor else None


def _faltan_programas(ajustes: dict) -> str | None:
    comando = ajustes.get("comando")
    if comando and not shutil.which(comando):
        instalar = {"npx": "Node.js (https://nodejs.org)", "uvx": "uv (winget install astral-sh.uv)"}.get(comando, comando)
        return f"Para este conector hace falta {instalar}, que no encontré en tu equipo."
    return None


def _claves_faltantes(ajustes: dict) -> list[str]:
    import json
    return [v for v in re.findall(r"\$\{(\w+)\}", json.dumps(ajustes))
            if v not in {"USERPROFILE"} and not os.environ.get(v)]


def _conectar_y_resumir(nombre: str, ajustes: dict) -> str:
    faltan = _claves_faltantes(ajustes)
    if faltan:
        return (f"Guardé el conector «{nombre}», pero le falta la clave {', '.join(faltan)}. "
                "Ponla en ⚙ Ajustes › Claves y datos (aparece ahí como campo nuevo) y luego di «recarga los conectores». "
                "No me la dictes por el chat.")
    resumen = mcp.registrar_servidor(nombre, mcp._expandir(ajustes) | {"activo": True})
    return f"Conector «{nombre}» listo. {resumen}."


@R.herramienta("conectores", "Lista los conectores que Bark sabe instalar (ElevenLabs, GitHub, archivos, web…) y si ya están",
               alias=("conectores_disponibles", "que_conectores_hay", "catalogo_mcp"),
               palabras=("catalogo", "conectores", "integraciones", "puedes", "conectar", "mcp", "instalar"))
def catalogo_conectores() -> str:
    instalados = mcp.leer_configuracion()
    lineas = []
    for clave, receta in CATALOGO.items():
        estado = "instalado" if instalados.get(clave, {}).get("activo") else "disponible"
        claves = f" · necesita {', '.join(receta['claves'])}" if receta.get("claves") else ""
        lineas.append(f"• {clave} ({estado}): {receta['descripcion']}{claves}")
    return ("Conectores que puedo instalar (di «conecta <nombre>»):\n" + "\n".join(lineas) +
            "\nTambién puedo agregar cualquier otro servidor MCP si me das su comando (npx/uvx) o su URL.")


@R.herramienta("conectores", "Instala un conector del catálogo por su nombre (ej. elevenlabs, github, archivos, web)",
               alias=("conectar_servicio", "instalar_conector_mcp", "conecta", "agregar_integracion"),
               palabras=("conecta", "conectate", "instala", "integra", "agrega", "conector", "elevenlabs", "github"),
               sensible=True)
def instalar_conector(nombre: str) -> str:
    clave = _buscar_receta(nombre)
    if not clave:
        return (f"No tengo «{nombre}» en mi catálogo. Si conoces su servidor MCP, pásame su comando (npx/uvx …) "
                "o su URL y lo agrego con agregar_conector_mcp.")
    receta = CATALOGO[clave]
    ajustes = {k: receta[k] for k in _CAMPOS_RECETA if k in receta} | {"activo": True}
    falta = _faltan_programas(ajustes)
    if falta:
        return falta
    mcp.guardar_servidor(clave, ajustes)
    nota = f" Nota: {receta['nota']}" if receta.get("nota") else ""
    claves = "".join(f" Clave en {url}." for url in receta.get("claves", {}).values()) if _claves_faltantes(ajustes) else ""
    return _conectar_y_resumir(clave, ajustes) + claves + nota


@R.herramienta("conectores", "Crea un conector nuevo con cualquier servidor MCP: comando local (npx/uvx …) o url remota",
               alias=("crear_conector", "nuevo_conector", "agregar_mcp", "crear_conector_mcp"),
               palabras=("crea", "nuevo", "conector", "mcp", "servidor", "npx", "uvx", "url", "integracion"),
               sensible=True)
def agregar_conector_mcp(nombre: str, comando: str = "", url: str = "", descripcion: str = "",
                         variable_clave: str = "", encabezado_clave: str = "Authorization") -> str:
    """comando: línea completa como «npx -y @org/servidor» · url: para servidores remotos ·
    variable_clave: nombre de la variable de .env con el token (ej. NOTION_TOKEN), nunca el token mismo."""
    nombre = re.sub(r"[^a-z0-9_]+", "_", normalizar(nombre)).strip("_")
    if not nombre or not (comando or url):
        return "Necesito un nombre y el comando (npx/uvx …) o la URL del servidor MCP."
    if variable_clave and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", variable_clave):
        return "variable_clave debe ser el NOMBRE de la variable (ej. NOTION_TOKEN), no la clave secreta."
    ajustes: dict = {"descripcion": descripcion or f"Servidor MCP «{nombre}».", "activo": True}
    if url:
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            return "Por seguridad solo agrego servidores remotos con https:// (o locales en localhost)."
        ajustes["url"] = url
        if variable_clave:
            valor = "Bearer ${%s}" % variable_clave if encabezado_clave.lower() == "authorization" else "${%s}" % variable_clave
            ajustes["encabezados"] = {encabezado_clave: valor}
    else:
        partes = comando.split()
        ajustes["comando"], ajustes["argumentos"] = partes[0], partes[1:]
        if variable_clave:
            ajustes["entorno"] = {variable_clave: "${%s}" % variable_clave}
        falta = _faltan_programas(ajustes)
        if falta:
            return falta
    mcp.guardar_servidor(nombre, ajustes)
    return _conectar_y_resumir(nombre, ajustes)


@R.herramienta("conectores", "Quita un conector MCP instalado y deja de usarlo",
               alias=("eliminar_conector", "desconectar", "borrar_conector"),
               palabras=("quita", "elimina", "desconecta", "conector", "borra"), sensible=True)
def quitar_conector(nombre: str) -> str:
    instalados = list(mcp.leer_configuracion())
    elegido = similitud.elegir(nombre, instalados)[0] if instalados else None
    if not elegido:
        return "No encontré ese conector instalado. Instalados: " + (", ".join(instalados) or "ninguno")
    mcp.quitar_servidor(elegido)
    return f"Quité el conector «{elegido}»."
