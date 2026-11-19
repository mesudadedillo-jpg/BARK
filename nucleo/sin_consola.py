"""Evita que cada comando de Windows (tasklist, schtasks, PowerShell…) abra una consola.

Bark corre con pythonw, sin consola propia; por eso cada subprocess lanzaba una
ventana negra que parpadeaba. Este parche añade CREATE_NO_WINDOW por defecto a
todos los procesos hijos. Se importa una vez, al arrancar.
"""

import subprocess
import sys

if sys.platform == "win32" and not getattr(subprocess, "_bark_sin_consola", False):
    _Popen_original = subprocess.Popen.__init__
    _SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    def _popen_sin_consola(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = _SIN_VENTANA
        return _Popen_original(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_sin_consola
    subprocess._bark_sin_consola = True
