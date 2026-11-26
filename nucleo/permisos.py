"""
permisos.py
Permisos de Bark: los de Windows (micrófono y cámara) y los que Bark pide al usuario
(usar el micrófono, la cámara, descargar modelos). Las decisiones se guardan en
bd_memoria/permisos.json para no preguntar en cada inicio.

Windows guarda el acceso a micrófono/cámara de las apps de escritorio en
HKCU\\...\\CapabilityAccessManager\\ConsentStore\\<capacidad>\\NonPackaged. Si ahí dice
«Deny», el micrófono abre pero solo entrega silencio: el síntoma de «activo el modo
voz y no detecta nada».
"""

from __future__ import annotations

import json
import os
import sys
import threading

from config import config

RUTA_PERMISOS = os.path.join(config.CARPETA_BD, "permisos.json")
_bloqueo = threading.Lock()

_AJUSTES_PRIVACIDAD = {"microphone": "ms-settings:privacy-microphone", "webcam": "ms-settings:privacy-webcam"}
_NOMBRES = {"microphone": "micrófono", "webcam": "cámara"}


# ============================================================
# Permisos de Windows
# ============================================================

def _valor_consentimiento(raiz, ruta: str) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(raiz, ruta) as clave:
            return str(winreg.QueryValueEx(clave, "Value")[0])
    except OSError:
        return None


def estado_windows(capacidad: str = "microphone") -> str:
    """'permitido', 'denegado' o 'desconocido' para «microphone» o «webcam»."""
    if sys.platform != "win32":
        return "desconocido"
    import winreg
    base = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{capacidad}"
    valores = (
        _valor_consentimiento(winreg.HKEY_LOCAL_MACHINE, base),          # apagado para todo el equipo
        _valor_consentimiento(winreg.HKEY_CURRENT_USER, base),           # apagado para este usuario
        _valor_consentimiento(winreg.HKEY_CURRENT_USER, base + r"\NonPackaged"),  # apps de escritorio
    )
    if any(v == "Deny" for v in valores):
        return "denegado"
    return "permitido" if any(v == "Allow" for v in valores) else "desconocido"


def explicar_bloqueo(capacidad: str = "microphone") -> str:
    nombre = _NOMBRES.get(capacidad, capacidad)
    return (f"Windows está bloqueando el {nombre} para las aplicaciones de escritorio, así que Bark solo recibe "
            f"silencio. Abre Configuración › Privacidad y seguridad › {nombre.capitalize()} y activa "
            f"«Permitir que las aplicaciones accedan al {nombre}» y «Permitir que las aplicaciones de escritorio "
            f"accedan al {nombre}».")


def abrir_ajustes_privacidad(capacidad: str = "microphone") -> None:
    if sys.platform == "win32":
        os.startfile(_AJUSTES_PRIVACIDAD.get(capacidad, "ms-settings:privacy"))


def abrir_ajustes_grabacion() -> None:
    """Pestaña Grabación del panel de sonido clásico: ahí se ve si el micro está silenciado o desactivado."""
    if sys.platform == "win32":
        import subprocess
        subprocess.Popen(["control", "mmsys.cpl,,1"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ============================================================
# Decisiones del usuario (persistentes)
# ============================================================

def _leer() -> dict:
    try:
        with open(RUTA_PERMISOS, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def _escribir(datos: dict) -> None:
    os.makedirs(os.path.dirname(RUTA_PERMISOS), exist_ok=True)
    temporal = RUTA_PERMISOS + ".tmp"
    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2)
    os.replace(temporal, RUTA_PERMISOS)


def consentimiento(clave: str) -> bool | None:
    """True/False si el usuario ya decidió; None si nunca se le preguntó."""
    with _bloqueo:
        valor = _leer().get(clave)
    return valor if isinstance(valor, bool) else None


def guardar_consentimiento(clave: str, valor: bool) -> None:
    with _bloqueo:
        datos = _leer()
        datos[clave] = bool(valor)
        _escribir(datos)


def preferencia(clave: str, defecto=None):
    with _bloqueo:
        return _leer().get(clave, defecto)


def guardar_preferencia(clave: str, valor) -> None:
    with _bloqueo:
        datos = _leer()
        if valor is None:
            datos.pop(clave, None)
        else:
            datos[clave] = valor
        _escribir(datos)


def olvidar_decisiones() -> None:
    with _bloqueo:
        try:
            os.remove(RUTA_PERMISOS)
        except OSError:
            pass


def resumen() -> str:
    """Estado legible de todos los permisos, para la interfaz y la consola."""
    def decision(clave):
        valor = consentimiento(clave)
        return "sin preguntar" if valor is None else ("concedido" if valor else "negado")

    microfono_guardado = preferencia("microfono_nombre")
    return "\n".join((
        f"Micrófono · Windows: {estado_windows('microphone')} · Bark: {decision('microfono')}",
        f"Cámara · Windows: {estado_windows('webcam')} · Bark: {decision('camara')}",
        f"Micrófono elegido: {microfono_guardado or 'automático'}",
    ))
