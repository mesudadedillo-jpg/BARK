"""Lecturas rápidas del equipo para el HUD (CPU, RAM, batería) sin psutil: solo ctypes."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class _Memoria(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _Energia(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


class Telemetria:
    """Cada llamada a ``leer()`` devuelve el uso desde la lectura anterior."""

    def __init__(self):
        self._previo = None

    @staticmethod
    def _tiempos():
        inactivo, nucleo, usuario = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(inactivo), ctypes.byref(nucleo), ctypes.byref(usuario))
        valor = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return valor(inactivo), valor(nucleo), valor(usuario)

    def cpu(self) -> float:
        actual = self._tiempos()
        previo, self._previo = self._previo, actual
        if previo is None:
            return 0.0
        inactivo = actual[0] - previo[0]
        total = (actual[1] - previo[1]) + (actual[2] - previo[2])  # el tiempo de núcleo incluye el inactivo
        return max(0.0, min(100.0, 100.0 * (total - inactivo) / total)) if total else 0.0

    @staticmethod
    def ram() -> tuple[float, float, float]:
        """(porcentaje usado, GB usados, GB totales)."""
        estado = _Memoria()
        estado.dwLength = ctypes.sizeof(_Memoria)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado))
        total = estado.ullTotalPhys / 1024 ** 3
        return float(estado.dwMemoryLoad), total - estado.ullAvailPhys / 1024 ** 3, total

    @staticmethod
    def bateria() -> tuple[int | None, bool]:
        """(porcentaje o None si es de escritorio, ¿conectado a la corriente?)."""
        estado = _Energia()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(estado)):
            return None, True
        porcentaje = estado.BatteryLifePercent & 0xFF
        sin_bateria = (estado.BatteryFlag & 0xFF) == 128 or porcentaje == 255
        return (None if sin_bateria else porcentaje), estado.ACLineStatus == 1

    def leer(self) -> dict:
        if os.name != "nt":
            return {"cpu": 0.0, "ram": 0.0, "ram_usada": 0.0, "ram_total": 0.0, "bateria": None, "corriente": True}
        ram, usada, total = self.ram()
        bateria, corriente = self.bateria()
        return {"cpu": self.cpu(), "ram": ram, "ram_usada": usada, "ram_total": total,
                "bateria": bateria, "corriente": corriente}
