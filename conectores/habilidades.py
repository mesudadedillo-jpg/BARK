"""Habilidades: prompts e instrucciones reutilizables que Bark aplica solo cuando vienen al caso.

Funcionan como las «skills» de Claude: guardas cómo quieres que se haga algo
(«cuando te pida un resumen de correo, dame 3 viñetas y una acción sugerida») y
Bark lo agrega a su contexto cuando tu petición coincide. Se guardan en
bd_memoria/habilidades.json, en este equipo.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from acciones.apps import normalizar
from config import config
from conectores import similitud
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("habilidades", "Prompts y habilidades",
           "Guarda prompts e instrucciones reutilizables; Bark los aplica solo cuando tu petición coincide.")

RUTA = os.path.join(config.CARPETA_BD, "habilidades.json")
_bloqueo = threading.Lock()
_VACIAS = {"para", "como", "cuando", "que", "con", "los", "las", "una", "uno", "del", "por", "sobre", "pida",
           "pide", "quiero", "hazlo", "siempre", "este", "esta", "esto", "algo", "todo", "cada", "usar", "usa"}


def cargar(ruta: str | None = None) -> dict[str, dict]:
    try:
        with open(ruta or RUTA, encoding="utf-8") as archivo:
            datos = json.load(archivo)
            return datos if isinstance(datos, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _guardar(datos: dict, ruta: str | None = None) -> None:
    ruta = ruta or RUTA
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2)
    os.replace(temporal, ruta)


def _palabras(texto: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9ñ]+", normalizar(texto)) if len(p) >= 4 and p not in _VACIAS}


def habilidades_para(mensaje: str, maximo: int = 2, ruta: str | None = None) -> list[tuple[str, dict]]:
    """Las habilidades que vienen al caso para este mensaje (por nombre o por sus palabras de activación)."""
    texto = normalizar(mensaje)
    palabras_mensaje = _palabras(mensaje)
    puntuadas = []
    for nombre, h in cargar(ruta).items():
        if normalizar(nombre).replace("_", " ") in texto:
            puntuadas.append((10.0, nombre, h))
            continue
        clave = _palabras(f"{nombre.replace('_', ' ')} {h.get('cuando_usar', '')}")
        coinciden = len(clave & palabras_mensaje)
        if coinciden >= 2 or (coinciden == 1 and len(clave) <= 3):
            puntuadas.append((coinciden / max(1, len(clave)) + coinciden, nombre, h))
    puntuadas.sort(key=lambda x: x[0], reverse=True)
    return [(n, h) for _, n, h in puntuadas[:maximo]]


def contexto_para(mensaje: str) -> str:
    """Bloque para el prompt del modelo con las instrucciones de las habilidades activas."""
    activas = habilidades_para(mensaje)
    if not activas:
        return ""
    return "HABILIDADES DEL USUARIO (síguelas en esta respuesta):\n" + "\n".join(
        f"· {n}: {h.get('instrucciones', '')[:1200]}" for n, h in activas)


@R.herramienta("habilidades", "Guarda un prompt o instrucción reutilizable (habilidad) y cuándo aplicarlo",
               alias=("crear_prompt", "guardar_prompt", "nueva_habilidad", "ensenar_habilidad", "guardar_instruccion"),
               palabras=("prompt", "habilidad", "instruccion", "ensena", "aprende", "guarda", "plantilla", "siempre"))
def crear_habilidad(nombre: str, instrucciones: str, cuando_usar: str = "") -> str:
    """instrucciones: el prompt completo (rol, pasos, formato de salida, ejemplo) ·
    cuando_usar: palabras o situaciones que la activan (ej. «resumen de correo, email largo»)."""
    clave = re.sub(r"[^a-z0-9]+", "_", normalizar(nombre)).strip("_")[:40]
    if not clave or len(instrucciones.strip()) < 10:
        return "Necesito un nombre y unas instrucciones claras (al menos una frase) para guardar la habilidad."
    with _bloqueo:
        datos = cargar()
        existia = clave in datos
        datos[clave] = {"instrucciones": instrucciones.strip()[:4000], "cuando_usar": cuando_usar.strip()[:300],
                        "actualizada": datetime.now().isoformat(timespec="seconds")}
        _guardar(datos)
    disparo = f" Se activa con: {cuando_usar.strip()}." if cuando_usar.strip() else f" Se activa cuando digas «{clave.replace('_', ' ')}»."
    return f"{'Actualicé' if existia else 'Guardé'} la habilidad «{clave}».{disparo}"


@R.herramienta("habilidades", "Lista las habilidades y prompts guardados",
               alias=("mis_habilidades", "mis_prompts", "ver_prompts"), palabras=("habilidades", "prompts", "guardados", "lista"))
def listar_habilidades() -> str:
    datos = cargar()
    if not datos:
        return "Aún no tienes habilidades. Dime por ejemplo: «cuando te pida un resumen, hazlo en 3 viñetas; guárdalo como habilidad»."
    return "Habilidades guardadas:\n" + "\n".join(
        f"• {n} — {h.get('cuando_usar') or 'por nombre'}: {h.get('instrucciones', '')[:90]}…" for n, h in datos.items())


@R.herramienta("habilidades", "Muestra el prompt completo de una habilidad guardada",
               alias=("ver_prompt", "mostrar_habilidad"), palabras=("muestra", "prompt", "habilidad", "ver"), observar=True)
def ver_habilidad(nombre: str) -> str:
    datos = cargar()
    elegido = similitud.elegir(nombre, list(datos))[0] if datos else None
    if not elegido:
        return similitud.texto_sugerencias(nombre, list(datos), "una habilidad")
    h = datos[elegido]
    return f"Habilidad «{elegido}» (se activa con: {h.get('cuando_usar') or 'su nombre'}):\n{h.get('instrucciones', '')}"


@R.herramienta("habilidades", "Borra una habilidad o prompt guardado",
               alias=("borrar_prompt", "eliminar_habilidad", "olvidar_habilidad"),
               palabras=("borra", "elimina", "olvida", "habilidad", "prompt"), sensible=True)
def borrar_habilidad(nombre: str) -> str:
    with _bloqueo:
        datos = cargar()
        elegido = similitud.elegir(nombre, list(datos))[0] if datos else None
        if not elegido:
            return similitud.texto_sugerencias(nombre, list(datos), "una habilidad")
        del datos[elegido]
        _guardar(datos)
    return f"Borré la habilidad «{elegido}»."
