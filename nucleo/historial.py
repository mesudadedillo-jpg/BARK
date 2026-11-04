"""
historial.py
Guarda el historial de conversacion "limpio" (lo que el usuario dice y lo
que Bark responde al final), separado del scratchpad interno del bucle ReAct.
Por ahora vive en memoria (RAM) durante la sesion. Mas adelante bd_manager.py
podra persistirlo en SQLite entre sesiones.
"""

import json
import threading


MAX_TURNOS_CONTEXTO = 10  # Contexto reciente completo, sin cargar toda la biblioteca.


class Historial:
    def __init__(self):
        self._turnos = []  # lista de dicts: {"usuario": str, "chet": str}
        self._bloqueo = threading.RLock()

    def agregar_turno(self, mensaje_usuario: str, respuesta_chet: str, sensible: bool = False):
        with self._bloqueo:
            self._turnos.append({"usuario": mensaje_usuario, "chet": respuesta_chet, "sensible": sensible})

    def obtener_mensajes_contexto(self, incluir_sensibles: bool = True) -> list:
        """
        Devuelve los ultimos N turnos en formato de mensajes tipo chat,
        listos para anteponer al system prompt.
        """
        with self._bloqueo:
            recientes = [dict(t) for t in self._turnos if incluir_sensibles or not t.get("sensible")][-MAX_TURNOS_CONTEXTO:]
        mensajes = []
        for turno in recientes:
            mensajes.append({"role": "user", "content": turno["usuario"]})
            # El system prompt exige JSON en cada respuesta del asistente. Guardar
            # el turno con esa forma evita que el historial le ensene lo contrario.
            mensajes.append({
                "role": "assistant",
                "content": json.dumps(
                    {"tipo": "respuesta_final", "mensaje": turno["chet"]},
                    ensure_ascii=False,
                ),
            })
        return mensajes

    def limpiar(self):
        with self._bloqueo:
            self._turnos = []


# Instancia global compartida para la sesion actual de consola/UI.
historial_sesion = Historial()
