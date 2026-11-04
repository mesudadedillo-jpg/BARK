"""Selección resiliente de IA: nube opcional y modelo local como respaldo."""

from __future__ import annotations

import json
import socket
import ssl
import time
from functools import lru_cache
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import config


class ProveedorNoDisponible(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _sesion_red() -> requests.Session:
    """Evita proxy roto y usa certificados confiados por Windows si están disponibles."""
    sesion = requests.Session()
    sesion.trust_env = False
    # Reutilizar conexiones reduce el coste TLS de cada turno. Un único
    # reintento corto cubre fallos transitorios sin dejar la interfaz esperando.
    reintentos = Retry(total=1, connect=1, read=0, status=0, backoff_factor=0.1)
    adaptador = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=reintentos)
    sesion.mount("http://", adaptador)
    sesion.mount("https://", adaptador)
    try:
        certificados = ssl.enum_certificates("ROOT")
        pem = "".join(ssl.DER_cert_to_PEM_cert(dato) for dato, tipo, _ in certificados if tipo == "x509_asn")
        contexto = ssl.create_default_context()
        contexto.load_verify_locations(cadata=pem)

        class AdaptadorWindows(HTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
                kwargs["ssl_context"] = contexto
                return super().init_poolmanager(connections, maxsize, block, **kwargs)

        sesion.mount("https://", AdaptadorWindows(pool_connections=4, pool_maxsize=4, max_retries=reintentos))
    except (AttributeError, OSError, ssl.SSLError):
        pass
    return sesion


_estado_red = (0.0, False)


def _hay_red() -> bool:
    global _estado_red
    ahora = time.monotonic()
    if ahora - _estado_red[0] < 5:
        return _estado_red[1]
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.2):
            _estado_red = (ahora, True)
            return True
    except OSError:
        _estado_red = (ahora, False)
        return False


def _texto_gemini(mensajes: list[dict]) -> str:
    if not config.GEMINI_API_KEY:
        raise ProveedorNoDisponible("Gemini no está configurado")
    sistema = "\n".join(m["content"] for m in mensajes if m["role"] == "system")
    contenido = []
    for mensaje in mensajes:
        if mensaje["role"] != "system":
            rol = "model" if mensaje["role"] == "assistant" else "user"
            contenido.append({"role": rol, "parts": [{"text": mensaje["content"]}]})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODELO}:generateContent"
    respuesta = _sesion_red().post(url, params={"key": config.GEMINI_API_KEY}, json={
        "system_instruction": {"parts": [{"text": sistema}]},
        "contents": contenido,
        "generationConfig": {"temperature": config.TEMPERATURA, "maxOutputTokens": config.MAX_TOKENS_RESPUESTA, "responseMimeType": "application/json"},
    }, timeout=config.TIEMPO_ESPERA_RED)
    if respuesta.status_code in (401, 403, 429):
        raise ProveedorNoDisponible(f"Gemini no disponible ({respuesta.status_code})")
    respuesta.raise_for_status()
    try:
        return respuesta.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as error:
        raise ProveedorNoDisponible("Gemini devolvió una respuesta vacía") from error


def _texto_openai(mensajes: list[dict]) -> str:
    if not config.OPENAI_API_KEY:
        raise ProveedorNoDisponible("OpenAI no está configurado")
    respuesta = _sesion_red().post("https://api.openai.com/v1/chat/completions", headers={
        "Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json",
    }, json={"model": config.OPENAI_MODELO, "messages": mensajes, "temperature": config.TEMPERATURA,
             "max_tokens": config.MAX_TOKENS_RESPUESTA, "response_format": {"type": "json_object"}}, timeout=config.TIEMPO_ESPERA_RED)
    if respuesta.status_code in (401, 403, 429):
        raise ProveedorNoDisponible(f"OpenAI no disponible ({respuesta.status_code})")
    respuesta.raise_for_status()
    try:
        return respuesta.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as error:
        raise ProveedorNoDisponible("OpenAI devolvió una respuesta vacía") from error


def responder(mensajes: list[dict], local: Callable[[list[dict]], str]) -> tuple[str, str]:
    """Devuelve texto y origen. Un fallo de nube nunca impide usar Bark local."""
    preferido = config.PROVEEDOR_PREFERIDO
    if preferido == "local":
        return local(mensajes), "local"
    candidatos = {"gemini": [_texto_gemini], "openai": [_texto_openai]}.get(
        preferido, [_texto_gemini, _texto_openai]
    )
    candidatos = [proveedor for proveedor in candidatos if (config.GEMINI_API_KEY if proveedor is _texto_gemini else config.OPENAI_API_KEY)]
    # Sin proveedor configurado no se hace una comprobación de red que solo
    # retrasaría el respaldo local.
    if not candidatos or not _hay_red():
        return local(mensajes), "local"
    for proveedor in candidatos:
        try:
            return proveedor(mensajes), "gemini" if proveedor is _texto_gemini else "openai"
        # Un proveedor mal configurado o una respuesta inesperada nunca debe
        # derribar el chat; el respaldo local conserva la conversación.
        except (requests.RequestException, ProveedorNoDisponible, ValueError, KeyError, TypeError):
            continue
    return local(mensajes), "local"


def estado_proveedores() -> str:
    disponibles = []
    if config.GEMINI_API_KEY:
        disponibles.append("Gemini")
    if config.OPENAI_API_KEY:
        disponibles.append("ChatGPT")
    nube = ", ".join(disponibles) if disponibles else "sin claves de nube"
    return f"Modo: {config.PROVEEDOR_PREFERIDO}. Nube configurada: {nube}. Respaldo: modelo local."


def diagnosticar_proveedores() -> dict[str, str]:
    """Comprueba conectividad/autenticación sin revelar claves ni generar respuestas."""
    pruebas = {
        "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", {"key": config.GEMINI_API_KEY}, {}),
        "openai": ("https://api.openai.com/v1/models", {}, {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}),
    }
    resultado = {}
    for nombre, (url, params, encabezados) in pruebas.items():
        if not (config.GEMINI_API_KEY if nombre == "gemini" else config.OPENAI_API_KEY):
            resultado[nombre] = "sin clave configurada"
            continue
        try:
            respuesta = _sesion_red().get(url, params=params, headers=encabezados, timeout=config.TIEMPO_ESPERA_RED)
            resultado[nombre] = f"HTTP {respuesta.status_code}"
        except requests.RequestException as error:
            resultado[nombre] = f"error de red: {type(error).__name__}"
    return resultado
