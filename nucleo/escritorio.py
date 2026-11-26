"""Bark como app de escritorio de Windows: identidad propia, icono y accesos directos.

Sin esto, Windows agrupa a Bark con Python y muestra el icono de Python en la
barra de tareas. Con un AppUserModelID propio (en el proceso y en el acceso
directo) Bark aparece con su orbe, se puede anclar a la barra de tareas y abrir
desde Inicio o el Escritorio como cualquier otra app.
"""

from __future__ import annotations

import os
import sys

from config import config

ID_APP = "DigiPyme.Bark.Asistente"
NOMBRE = "Bark"
CARPETA_RECURSOS = os.path.join(config.CARPETA_BASE, "interfaz", "recursos")
RUTA_ICONO = os.path.join(CARPETA_RECURSOS, "bark.ico")
RUTA_LOGO = os.path.join(CARPETA_RECURSOS, "bark.png")


def registrar_identidad() -> None:
    """Debe llamarse antes de crear la ventana: así la barra de tareas usa el icono de Bark."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ID_APP)
    except Exception:
        pass


def _pythonw() -> str:
    """El intérprete sin consola junto al Python que está corriendo Bark."""
    candidato = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidato if os.path.isfile(candidato) else sys.executable


def _carpetas_destino() -> dict[str, str]:
    from win32com.client import Dispatch
    shell = Dispatch("WScript.Shell")
    return {"Escritorio": shell.SpecialFolders("Desktop"), "Inicio": shell.SpecialFolders("Programs")}


def rutas_accesos() -> dict[str, str]:
    try:
        return {lugar: os.path.join(carpeta, f"{NOMBRE}.lnk") for lugar, carpeta in _carpetas_destino().items()}
    except Exception:
        return {}


def crear_accesos(lugares=("Escritorio", "Inicio")) -> list[str]:
    """Crea (o actualiza) los accesos directos de Bark con su icono e identidad. Devuelve sus rutas."""
    if sys.platform != "win32":
        raise RuntimeError("Los accesos directos solo existen en Windows.")
    from win32com.client import Dispatch
    from win32com.propsys import propsys, pscon
    import pythoncom

    shell = Dispatch("WScript.Shell")
    creados = []
    for lugar, ruta in rutas_accesos().items():
        if lugar not in lugares:
            continue
        acceso = shell.CreateShortcut(ruta)
        acceso.TargetPath = _pythonw()
        acceso.Arguments = f'-B "{os.path.join(config.CARPETA_BASE, "iniciar_bark.pyw")}"'
        acceso.WorkingDirectory = config.CARPETA_BASE
        acceso.IconLocation = f"{RUTA_ICONO},0"
        acceso.Description = "Bark · asistente de IA personal"
        acceso.Save()
        # Misma identidad que la ventana: al anclarla, la barra de tareas la reconoce como una sola app.
        try:
            almacen = propsys.SHGetPropertyStoreFromParsingName(ruta, None, 2, propsys.IID_IPropertyStore)  # GPS_READWRITE
            almacen.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(ID_APP, pythoncom.VT_LPWSTR))
            almacen.Commit()
        except Exception:
            pass
        creados.append(ruta)
    return creados
