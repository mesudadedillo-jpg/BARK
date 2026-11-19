"""Utilidades compartidas por los conectores: PowerShell, procesos, HTTP y almacenamiento local."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import threading
from typing import Any

from config import config

ES_WINDOWS = platform.system() == "Windows"
SOLO_WINDOWS = "Esta función solo está disponible en Windows."
_SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ejecutar(argumentos: list[str], timeout: int = 30, cwd: str | None = None) -> tuple[int, str]:
    """Ejecuta un programa sin abrir consola. Devuelve (código, salida+errores)."""
    try:
        r = subprocess.run(
            argumentos, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=cwd, creationflags=_SIN_VENTANA,
        )
        salida = (r.stdout or "") + (("\n" + r.stderr) if r.stderr and r.stderr.strip() else "")
        return r.returncode, salida.strip()
    except FileNotFoundError:
        return 127, f"No encontré el programa '{argumentos[0]}'."
    except subprocess.TimeoutExpired:
        return 124, f"La operación tardó más de {timeout} s y la detuve."
    except OSError as error:
        return 1, str(error)


def powershell(script: str, timeout: int = 30) -> tuple[int, str]:
    if not ES_WINDOWS:
        return 1, SOLO_WINDOWS
    preludio = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
    return ejecutar(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", preludio + script],
        timeout=timeout,
    )


def powershell_json(script: str, timeout: int = 30) -> list[dict]:
    """Ejecuta un script que termina en ConvertTo-Json y devuelve siempre una lista."""
    codigo, salida = powershell(f"{script} | ConvertTo-Json -Depth 3 -Compress", timeout)
    if codigo != 0 or not salida.strip():
        return []
    inicio = min((i for i in (salida.find("["), salida.find("{")) if i >= 0), default=-1)
    if inicio < 0:
        return []
    try:  # raw_decode ignora avisos que PowerShell escriba después del JSON
        datos, _fin = json.JSONDecoder().raw_decode(salida[inicio:])
    except (json.JSONDecodeError, ValueError):
        return []
    return datos if isinstance(datos, list) else [datos]


def comillas_ps(texto: str) -> str:
    """Cadena literal segura para PowerShell (comillas simples duplicadas)."""
    return "'" + str(texto).replace("'", "''") + "'"


def http_json(url: str, parametros: dict | None = None, timeout: int = 10) -> Any:
    import requests
    respuesta = requests.get(url, params=parametros, timeout=timeout, headers={"User-Agent": "Bark-Asistente/1.0"})
    respuesta.raise_for_status()
    return respuesta.json()


def recortar(texto: str, maximo: int = 3000) -> str:
    texto = texto.strip()
    return texto if len(texto) <= maximo else texto[:maximo].rstrip() + "\n… (recortado)"


class Almacen:
    """Lista JSON persistente en bd_memoria/, con escritura atómica y bloqueo entre hilos."""

    def __init__(self, nombre: str):
        self.nombre = nombre
        self._bloqueo = threading.Lock()

    @property
    def ruta(self) -> str:
        return os.path.join(config.CARPETA_BD, f"{self.nombre}.json")

    def leer(self) -> list[dict]:
        try:
            with open(self.ruta, encoding="utf-8") as archivo:
                datos = json.load(archivo)
            return datos if isinstance(datos, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def guardar(self, elementos: list[dict]) -> None:
        with self._bloqueo:
            os.makedirs(os.path.dirname(self.ruta), exist_ok=True)
            fd, temporal = tempfile.mkstemp(dir=os.path.dirname(self.ruta), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as archivo:
                json.dump(elementos, archivo, ensure_ascii=False, indent=2)
            os.replace(temporal, self.ruta)

    def agregar(self, elemento: dict) -> None:
        elementos = self.leer()
        elementos.append(elemento)
        self.guardar(elementos)
