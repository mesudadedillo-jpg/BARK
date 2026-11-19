"""
mcp.py
Cliente del Model Context Protocol (MCP): el mismo estándar que usa Claude
para sus conectores. Cualquier servidor MCP (archivos, GitHub, Google Drive,
Notion, bases de datos, Home Assistant…) se convierte en un conector de Bark
con solo declararlo en config/conectores_mcp.json.

Transportes soportados, sin dependencias extra:
- stdio: Bark lanza el servidor como proceso local (p. ej. con npx o uvx).
- HTTP «streamable»: el servidor vive en una URL; respuestas JSON o SSE.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
from typing import Any

from config import config
from conectores.registro import REGISTRO, Herramienta

VERSION_PROTOCOLO = "2025-06-18"
RUTA_CONFIG = os.path.join(config.CARPETA_BASE, "config", "conectores_mcp.json")
_INFO_CLIENTE = {"name": "bark", "version": "1.0"}


class ErrorMCP(RuntimeError):
    pass


def _expandir(valor: Any) -> Any:
    """Sustituye ${VARIABLE} por su valor del entorno (o de .env) para no escribir tokens en el JSON."""
    if isinstance(valor, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), valor)
    if isinstance(valor, list):
        return [_expandir(v) for v in valor]
    if isinstance(valor, dict):
        return {k: _expandir(v) for k, v in valor.items()}
    return valor


def formatear_contenido(resultado: dict) -> str:
    """Convierte el resultado de tools/call en texto legible."""
    partes = []
    for bloque in resultado.get("content") or []:
        tipo = bloque.get("type")
        if tipo == "text":
            partes.append(bloque.get("text", ""))
        elif tipo == "resource":
            recurso = bloque.get("resource") or {}
            partes.append(recurso.get("text") or f"[recurso {recurso.get('uri', '')}]")
        elif tipo == "resource_link":
            partes.append(f"[enlace] {bloque.get('name', '')} {bloque.get('uri', '')}")
        elif tipo in ("image", "audio"):
            partes.append(f"[{tipo} {bloque.get('mimeType', '')}]")
    if not partes and resultado.get("structuredContent") is not None:
        partes.append(json.dumps(resultado["structuredContent"], ensure_ascii=False, indent=1))
    texto = "\n".join(p for p in partes if p).strip() or "(el servidor no devolvió texto)"
    return f"Error del servidor: {texto}" if resultado.get("isError") else texto


class ClienteMCP:
    """Base común: handshake, listado paginado y llamada de herramientas."""

    def __init__(self, nombre: str, timeout: float = 60):
        self.nombre = nombre
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.info_servidor: dict = {}

    # Los transportes implementan estos dos.
    def _solicitar(self, mensaje: dict, timeout: float) -> dict:
        raise NotImplementedError

    def _notificar(self, mensaje: dict) -> None:
        raise NotImplementedError

    def cerrar(self) -> None:
        pass

    def solicitar(self, metodo: str, parametros: dict | None = None, timeout: float | None = None) -> dict:
        mensaje = {"jsonrpc": "2.0", "id": next(self._ids), "method": metodo}
        if parametros is not None:
            mensaje["params"] = parametros
        respuesta = self._solicitar(mensaje, timeout or self.timeout)
        if "error" in respuesta:
            error = respuesta["error"]
            raise ErrorMCP(f"{error.get('message', error)} (código {error.get('code')})")
        return respuesta.get("result") or {}

    def iniciar(self) -> None:
        self.info_servidor = self.solicitar("initialize", {
            "protocolVersion": VERSION_PROTOCOLO, "capabilities": {}, "clientInfo": _INFO_CLIENTE,
        }, timeout=max(self.timeout, 90))  # npx puede tardar en descargar el servidor la primera vez
        self._notificar({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def herramientas(self) -> list[dict]:
        todas, cursor = [], None
        for _ in range(50):  # tope por si un servidor devuelve cursores infinitos
            resultado = self.solicitar("tools/list", {"cursor": cursor} if cursor else {})
            todas.extend(resultado.get("tools") or [])
            cursor = resultado.get("nextCursor")
            if not cursor:
                break
        return todas

    def llamar(self, herramienta: str, argumentos: dict) -> str:
        return formatear_contenido(self.solicitar("tools/call", {"name": herramienta, "arguments": argumentos or {}}))


class ClienteStdio(ClienteMCP):
    """El servidor es un proceso local; mensajes JSON-RPC delimitados por salto de línea."""

    def __init__(self, nombre: str, comando: str, argumentos: list[str], entorno: dict | None = None,
                 carpeta: str | None = None, timeout: float = 60):
        super().__init__(nombre, timeout)
        ejecutable = shutil.which(comando) or comando
        os.makedirs(os.path.join(config.CARPETA_BASE, "resultados"), exist_ok=True)
        self._log = open(os.path.join(config.CARPETA_BASE, "resultados", f"mcp_{nombre}.log"), "a", encoding="utf-8")
        try:
            self._proceso = subprocess.Popen(
                [ejecutable, *argumentos], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                cwd=carpeta or None, env={**os.environ, **(entorno or {})},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self._log.close()
            raise ErrorMCP(f"no pude lanzar '{comando}': {error}") from error
        self._pendientes: dict[int, dict] = {}
        self._eventos: dict[int, threading.Event] = {}
        self._escritura = threading.Lock()
        threading.Thread(target=self._leer, name=f"mcp-{nombre}", daemon=True).start()

    def _enviar(self, mensaje: dict) -> None:
        if self._proceso.poll() is not None:
            raise ErrorMCP(f"el servidor '{self.nombre}' se cerró (código {self._proceso.returncode})")
        datos = (json.dumps(mensaje, ensure_ascii=False) + "\n").encode("utf-8")
        with self._escritura:
            self._proceso.stdin.write(datos)
            self._proceso.stdin.flush()

    def _leer(self) -> None:
        for linea in self._proceso.stdout:
            try:
                mensaje = json.loads(linea.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue  # algunos servidores imprimen avisos en stdout
            if not isinstance(mensaje, dict):
                continue
            if "method" in mensaje and "id" in mensaje:
                self._responder_peticion(mensaje)
            elif "id" in mensaje and mensaje["id"] in self._eventos:
                self._pendientes[mensaje["id"]] = mensaje
                self._eventos[mensaje["id"]].set()
        for evento in list(self._eventos.values()):  # el proceso terminó: despierta a quien espere
            evento.set()

    def _responder_peticion(self, mensaje: dict) -> None:
        """El servidor también puede preguntar (ping, roots/list); contestamos lo mínimo."""
        respuestas = {"ping": {}, "roots/list": {"roots": []}}
        if mensaje["method"] in respuestas:
            salida = {"jsonrpc": "2.0", "id": mensaje["id"], "result": respuestas[mensaje["method"]]}
        else:
            salida = {"jsonrpc": "2.0", "id": mensaje["id"], "error": {"code": -32601, "message": "Bark no implementa este método"}}
        try:
            self._enviar(salida)
        except (OSError, ErrorMCP):
            pass

    def _solicitar(self, mensaje: dict, timeout: float) -> dict:
        evento = threading.Event()
        self._eventos[mensaje["id"]] = evento
        try:
            self._enviar(mensaje)
            if not evento.wait(timeout):
                raise ErrorMCP(f"'{self.nombre}' no respondió en {timeout:.0f} s")
            respuesta = self._pendientes.pop(mensaje["id"], None)
            if respuesta is None:
                raise ErrorMCP(f"el servidor '{self.nombre}' se cerró")
            return respuesta
        finally:
            self._eventos.pop(mensaje["id"], None)

    def _notificar(self, mensaje: dict) -> None:
        self._enviar(mensaje)

    def cerrar(self) -> None:
        try:
            self._proceso.stdin.close()
            self._proceso.terminate()
            self._proceso.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self._proceso.kill()
        for flujo in (self._proceso.stdout, self._log):
            try:
                flujo.close()
            except (OSError, ValueError):
                pass


class ClienteHttp(ClienteMCP):
    """Servidor remoto por HTTP «streamable»: POST con respuesta JSON o text/event-stream."""

    def __init__(self, nombre: str, url: str, encabezados: dict | None = None, timeout: float = 60):
        super().__init__(nombre, timeout)
        import requests
        self.url = url
        self._sesion = requests.Session()
        self._sesion.headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                     **(encabezados or {})})

    def _post(self, mensaje: dict, timeout: float):
        respuesta = self._sesion.post(self.url, json=mensaje, timeout=timeout, stream=True)
        if respuesta.status_code in (401, 403):
            raise ErrorMCP(f"'{self.nombre}' rechazó la autenticación ({respuesta.status_code}); revisa el token")
        respuesta.raise_for_status()
        if "Mcp-Session-Id" in respuesta.headers:
            self._sesion.headers["Mcp-Session-Id"] = respuesta.headers["Mcp-Session-Id"]
        return respuesta

    def _solicitar(self, mensaje: dict, timeout: float) -> dict:
        respuesta = self._post(mensaje, timeout)
        if "text/event-stream" in respuesta.headers.get("Content-Type", ""):
            datos = []
            for linea in respuesta.iter_lines(decode_unicode=True):
                if linea is None:
                    continue
                if linea.startswith("data:"):
                    datos.append(linea[5:].strip())
                elif not linea and datos:  # fin de un evento SSE
                    candidato = _json_o_nada("\n".join(datos))
                    datos = []
                    if isinstance(candidato, dict) and candidato.get("id") == mensaje["id"]:
                        respuesta.close()
                        return candidato
            candidato = _json_o_nada("\n".join(datos))
            if isinstance(candidato, dict) and candidato.get("id") == mensaje["id"]:
                return candidato
            raise ErrorMCP(f"'{self.nombre}' cerró el flujo sin responder")
        cuerpo = respuesta.json()
        if isinstance(cuerpo, list):  # lote JSON-RPC
            cuerpo = next((m for m in cuerpo if m.get("id") == mensaje["id"]), {})
        if mensaje.get("method") == "initialize":
            version = (cuerpo.get("result") or {}).get("protocolVersion", VERSION_PROTOCOLO)
            self._sesion.headers["MCP-Protocol-Version"] = version
        return cuerpo

    def _notificar(self, mensaje: dict) -> None:
        self._post(mensaje, self.timeout).close()

    def cerrar(self) -> None:
        if "Mcp-Session-Id" in self._sesion.headers:
            try:
                self._sesion.delete(self.url, timeout=5)
            except Exception:
                pass
        self._sesion.close()


def _json_o_nada(texto: str):
    try:
        return json.loads(texto) if texto else None
    except json.JSONDecodeError:
        return None


# ============================================================
# Registro de servidores como conectores de Bark
# ============================================================

_clientes: dict[str, ClienteMCP] = {}
_bloqueo = threading.Lock()


def _id_seguro(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def leer_configuracion(ruta: str = RUTA_CONFIG) -> dict:
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo).get("servidores", {})
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        REGISTRO.errores_carga["mcp"] = f"{os.path.basename(ruta)} no es un JSON válido: {error}"
        return {}


def crear_cliente(nombre: str, ajustes: dict) -> ClienteMCP:
    ajustes = _expandir(ajustes)
    timeout = float(ajustes.get("timeout", 60))
    if ajustes.get("url"):
        return ClienteHttp(nombre, ajustes["url"], ajustes.get("encabezados"), timeout)
    if not ajustes.get("comando"):
        raise ErrorMCP("falta 'comando' o 'url'")
    return ClienteStdio(nombre, ajustes["comando"], ajustes.get("argumentos", []), ajustes.get("entorno"),
                        ajustes.get("carpeta"), timeout)


def registrar_servidor(nombre: str, ajustes: dict) -> str:
    """Conecta un servidor y registra sus herramientas. Devuelve un resumen."""
    id_conector = f"mcp_{_id_seguro(nombre)}"
    REGISTRO.quitar_conector(id_conector)
    descripcion = ajustes.get("descripcion") or f"Servidor MCP «{nombre}»."
    try:
        cliente = crear_cliente(nombre, ajustes)
        cliente.iniciar()
        herramientas = cliente.herramientas()
    except Exception as error:
        motivo = f"no conectó: {error}"
        REGISTRO.conector(id_conector, f"MCP · {nombre}", descripcion, requisito=lambda m=motivo: m, origen="mcp")
        return f"{nombre}: {motivo}"
    _clientes[id_conector] = cliente
    REGISTRO.conector(id_conector, f"MCP · {nombre}", descripcion, origen="mcp")
    confiables = set(ajustes.get("confiables", []))
    pedir_permiso = ajustes.get("pedir_permiso", True)
    for h in herramientas:
        original = h["name"]
        solo_lectura = (h.get("annotations") or {}).get("readOnlyHint") is True
        texto = (h.get("description") or h.get("title") or original).strip().split("\n")[0]
        REGISTRO.agregar(Herramienta(
            nombre=f"{id_conector}_{_id_seguro(original)}",
            funcion=lambda _c=cliente, _n=original, **argumentos: _c.llamar(_n, argumentos),
            descripcion=texto[:180],
            conector=id_conector,
            alias=(original,),
            palabras=(nombre, *original.replace("_", " ").split()),
            sensible=bool(pedir_permiso) and original not in confiables and not solo_lectura,
            esquema=h.get("inputSchema") or {"type": "object", "properties": {}},
        ))
    return f"{nombre}: {len(herramientas)} herramientas"


def conectar_todo() -> list[str]:
    with _bloqueo:
        cerrar_todo()
        return [registrar_servidor(n, a) for n, a in leer_configuracion().items() if a.get("activo", True)]


def conectar_en_segundo_plano() -> None:
    if leer_configuracion():
        threading.Thread(target=conectar_todo, name="bark-mcp", daemon=True).start()


def cerrar_todo() -> None:
    for id_conector, cliente in list(_clientes.items()):
        try:
            cliente.cerrar()
        except Exception:
            pass
        REGISTRO.quitar_conector(id_conector)
        _clientes.pop(id_conector, None)


atexit.register(cerrar_todo)



@REGISTRO.herramienta("nucleo", "Vuelve a leer config/conectores_mcp.json y reconecta los servidores MCP",
                      alias=("recargar_mcp", "reconectar_conectores"), palabras=("recarga", "mcp", "conectores", "reconecta"))
def recargar_conectores_mcp() -> str:
    resumen = conectar_todo()
    return "Servidores MCP:\n" + "\n".join(f"• {r}" for r in resumen) if resumen else \
        "No hay servidores MCP configurados. Copia config/conectores_mcp.example.json como conectores_mcp.json."
