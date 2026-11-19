"""Teclado: escribir texto y usar atajos en la ventana activa."""

from __future__ import annotations

import re

from conectores import similitud
from conectores.registro import REGISTRO, requiere_modulo

R = REGISTRO
R.conector("teclado", "Teclado y atajos", "Escribe texto y pulsa atajos (guardar, copiar, nueva pestaña…) en la ventana activa.",
           requisito=requiere_modulo("pyautogui"))

ATAJOS = {
    "guardar": "ctrl+s", "copiar": "ctrl+c", "pegar": "ctrl+v", "cortar": "ctrl+x", "deshacer": "ctrl+z",
    "rehacer": "ctrl+y", "seleccionar todo": "ctrl+a", "buscar": "ctrl+f", "imprimir": "ctrl+p", "nuevo": "ctrl+n",
    "nueva pestaña": "ctrl+t", "reabrir pestaña": "ctrl+shift+t", "siguiente pestaña": "ctrl+tab",
    "pestaña anterior": "ctrl+shift+tab", "recargar": "f5", "pantalla completa": "f11", "zoom más": "ctrl+plus",
    "zoom menos": "ctrl+minus", "recorte de pantalla": "win+shift+s", "emojis": "win+.", "explorador": "win+e",
    "escritorio": "win+d", "cambiar ventana": "alt+tab", "administrador de tareas": "ctrl+shift+esc",
    "historial del portapapeles": "win+v", "enter": "enter", "escape": "esc", "dictado": "win+h",
}
# Atajos que cierran cosas o salen de la sesión: no se envían desde aquí.
_PELIGROSOS = {"alt+f4", "ctrl+w", "ctrl+alt+delete", "ctrl+alt+del", "win+l", "ctrl+shift+w"}
_TECLAS = {"control": "ctrl", "windows": "win", "mayus": "shift", "mayusculas": "shift", "suprimir": "delete",
           "retroceso": "backspace", "intro": "enter", "escape": "esc", "espacio": "space", "mas": "plus", "menos": "minus"}


@R.herramienta("teclado", "Escribe un texto en la ventana activa como si lo tecleara", alias=("teclear", "dictar"),
               palabras=("escribe", "teclea", "dicta", "texto"), sensible=True)
def escribir_texto(texto: str) -> str:
    import pyautogui
    if not texto:
        return "¿Qué escribo?"
    if texto.isascii():
        pyautogui.write(texto, interval=0.01)
    else:  # write() no teclea acentos ni ñ: se pega desde el portapapeles
        from acciones import sistema
        sistema.copiar_al_portapapeles(texto)
        pyautogui.hotkey("ctrl", "v")
    return f"Escribí {len(texto)} caracteres en la ventana activa."


@R.herramienta("teclado", "Pulsa un atajo por nombre (guardar, copiar, nueva pestaña…) o combinación (ctrl+shift+t)",
               alias=("atajo", "pulsa_teclas", "hotkey"), palabras=("atajo", "pulsa", "presiona", "tecla", "guardar", "copiar", "pegar"))
def presionar_atajo(atajo: str) -> str:
    import pyautogui
    combinacion = atajo.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:\s*\+\s*[a-z0-9.]+)*", combinacion):
        nombre, sugerencias = similitud.elegir(atajo, list(ATAJOS))
        if not nombre:
            return similitud.texto_sugerencias(atajo, sugerencias, "un atajo")
        combinacion = ATAJOS[nombre]
    teclas = [_TECLAS.get(t.strip(), t.strip()) for t in combinacion.split("+")]
    if "+".join(teclas) in _PELIGROSOS:
        return "Ese atajo cierra ventanas o la sesión; para eso usa cerrar_aplicacion o bloquear_pc."
    pyautogui.hotkey(*teclas)
    return f"Pulsé {'+'.join(teclas)}."
