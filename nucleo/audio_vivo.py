"""Puente en vivo entre el audio y la interfaz.

El micrófono y la voz de Bark publican aquí su nivel (0‒1) bloque a bloque, y la
interfaz lo lee para que el orbe se agrande y achique como una bocina. También
guarda el evento compartido para interrumpir a Bark mientras habla (estilo Live).
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

# Un único evento para todas las voces (Piper, Gemini, OpenAI): al activarlo,
# la reproducción se corta en el siguiente bloque de ~40 ms.
INTERRUPCION = threading.Event()

_niveles = {"entrada": (0.0, 0.0), "salida": (0.0, 0.0)}  # tipo -> (nivel, instante)
_ultima_interrupcion = 0.0


def nivel_desde_rms(rms: float) -> float:
    """RMS de audio int16 → 0‒1 en escala logarítmica (-52 dBFS = 0, -12 dBFS = 1)."""
    if rms <= 1.0:
        return 0.0
    db = 20.0 * math.log10(rms / 32768.0)
    return max(0.0, min(1.0, (db + 52.0) / 40.0))


def reportar(tipo: str, rms: float) -> None:
    _niveles[tipo] = (nivel_desde_rms(rms), time.monotonic())


def reportar_bloque(tipo: str, muestras: np.ndarray) -> None:
    """Calcula el RMS de un bloque int16 y lo publica."""
    if len(muestras):
        reportar(tipo, float(np.sqrt(np.mean(muestras.astype(np.float32) ** 2))))


def nivel(tipo: str, vigencia: float = 0.3) -> float:
    """Último nivel publicado; 0 si hace más de ``vigencia`` segundos que no llega audio."""
    valor, instante = _niveles.get(tipo, (0.0, 0.0))
    return valor if time.monotonic() - instante <= vigencia else 0.0


def activo(tipo: str, vigencia: float = 0.6) -> bool:
    """True si ese canal publicó audio recientemente (sirve para saber si hay medición real)."""
    return time.monotonic() - _niveles.get(tipo, (0.0, 0.0))[1] <= vigencia


def interrumpir() -> None:
    """Corta en seco la voz de Bark (botón, Esc, toque en el orbe o tu voz)."""
    global _ultima_interrupcion
    _ultima_interrupcion = time.monotonic()
    _niveles["salida"] = (0.0, _ultima_interrupcion)
    INTERRUPCION.set()


def interrumpido_desde(instante: float) -> bool:
    """True si alguien interrumpió a Bark después de ``instante`` (time.monotonic)."""
    return _ultima_interrupcion >= instante
