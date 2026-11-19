"""Productividad local: notas, pendientes, agenda de eventos y recordatorios a una hora."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta

from acciones import sistema
from acciones.apps import normalizar
from conectores import similitud
from conectores._util import Almacen
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("productividad", "Notas, pendientes y agenda", "Notas rápidas, lista de pendientes, eventos y recordatorios; todo guardado en tu PC.")

NOTAS, PENDIENTES, EVENTOS = Almacen("notas"), Almacen("pendientes"), Almacen("eventos")

_DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


# ============================================================
# Fechas y horas en español
# ============================================================

def interpretar_fecha(texto: str, hoy: date | None = None) -> date | None:
    """«hoy», «mañana», «el viernes», «25/12», «25 de diciembre», «2026-12-25»."""
    hoy = hoy or date.today()
    t = normalizar(texto)
    if not t or t in {"hoy", "ahorita"}:
        return hoy
    if "pasado manana" in t:
        return hoy + timedelta(days=2)
    if re.search(r"\bmanana\b", t):
        return hoy + timedelta(days=1)
    if t == "ayer":
        return hoy - timedelta(days=1)
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", texto)
    if iso:
        return date(int(iso[1]), int(iso[2]), int(iso[3]))
    numerica = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", texto)
    if numerica:
        anio = int(numerica[3]) if numerica[3] else hoy.year
        anio += 2000 if anio < 100 else 0
        candidata = date(anio, int(numerica[2]), int(numerica[1]))
        return candidata if numerica[3] or candidata >= hoy else candidata.replace(year=anio + 1)
    for i, mes in enumerate(_MESES, 1):
        m = re.search(rf"\b(\d{{1,2}})\s+de\s+{mes}\b", t)
        if m:
            candidata = date(hoy.year, i, int(m[1]))
            return candidata if candidata >= hoy else candidata.replace(year=hoy.year + 1)
    for i, dia in enumerate(_DIAS):
        if re.search(rf"\b{dia}\b", t):
            delta = (i - hoy.weekday()) % 7
            if delta == 0 and ("proximo" in t or "siguiente" in t):
                delta = 7
            return hoy + timedelta(days=delta)
    dia_mes = re.fullmatch(r"(?:el\s+)?(\d{1,2})", t)
    if dia_mes:
        candidata = hoy.replace(day=int(dia_mes[1]))
        if candidata < hoy:
            candidata = (candidata.replace(day=1) + timedelta(days=32)).replace(day=int(dia_mes[1]))
        return candidata
    return None


def interpretar_hora(texto: str, ahora: datetime | None = None) -> time | None:
    """«17:30», «5pm», «5 y media de la tarde», «mediodía»; sin am/pm elige la próxima."""
    ahora = ahora or datetime.now()
    t = normalizar(texto).replace(".", "")
    if "mediodia" in t:
        return time(12, 0)
    if "medianoche" in t:
        return time(0, 0)
    m = re.search(r"\b(\d{1,2})(?:[: ](\d{2}))?\s*(am|pm|a m|p m)?\b(?:\s*y\s*(media|cuarto))?", t)
    if not m:
        return None
    hora, minuto = int(m[1]), int(m[2]) if m[2] else 0
    if m[4]:
        minuto = 30 if m[4] == "media" else 15
    if hora > 23 or minuto > 59:
        return None
    sufijo = (m[3] or "").replace(" ", "")
    tarde = sufijo == "pm" or re.search(r"\b(?:tarde|noche)\b", t)
    manana = sufijo == "am" or re.search(r"\bmanana\b|\bmadrugada\b", t)
    if tarde and hora < 12:
        hora += 12
    elif manana and hora == 12:
        hora = 0
    elif not (tarde or manana) and hora < 12:
        # «a las 5» casi siempre es de la tarde; «a las 9» a las 15:00 → 21:00.
        if 1 <= hora <= 7 or time(hora, minuto) <= ahora.time() < time(hora + 12, minuto):
            hora += 12
    return time(hora, minuto)


# ============================================================
# Recordatorios a una hora
# ============================================================

@R.herramienta("productividad", "Te avisa a una hora concreta del día (ej. «17:30», «5 de la tarde»)",
               alias=("recordatorio_hora", "alarma_a_las"), palabras=("recuerdame", "avisame", "alarma", "hora", "a las"))
def recordatorio_a_las(hora: str, mensaje: str = "") -> str:
    ahora = datetime.now()
    momento = interpretar_hora(hora, ahora)
    if momento is None:
        return f"No entendí la hora '{hora}'. Dímela como «17:30» o «5 de la tarde»."
    objetivo = datetime.combine(ahora.date(), momento)
    if objetivo <= ahora:
        objetivo += timedelta(days=1)
    minutos = (objetivo - ahora).total_seconds() / 60
    return sistema.crear_temporizador(round(minutos, 3), mensaje or f"Son las {momento:%H:%M}.")


# ============================================================
# Notas
# ============================================================

@R.herramienta("productividad", "Guarda una nota rápida", alias=("anotar", "nota", "apuntar"),
               palabras=("nota", "anota", "apunta", "guarda", "apunte"))
def crear_nota(texto: str, titulo: str = "") -> str:
    texto = texto.strip()
    if not texto:
        return "¿Qué anoto?"
    titulo = titulo.strip() or " ".join(texto.split()[:6])
    NOTAS.agregar({"id": uuid.uuid4().hex[:8], "titulo": titulo, "texto": texto, "creada": datetime.now().isoformat(timespec="minutes")})
    return f"Anotado: «{titulo}»."


@R.herramienta("productividad", "Muestra tus notas; consulta filtra por parecido", alias=("mis_notas", "ver_notas", "buscar_notas"),
               palabras=("notas", "apuntes", "anote"))
def listar_notas(consulta: str = "") -> str:
    notas = NOTAS.leer()
    if consulta:
        notas = [n for _, n in similitud.mejores(consulta, notas, clave=lambda n: (n["titulo"], n["texto"]), limite=10, umbral=0.45)]
    if not notas:
        return "No tienes notas" + (f" parecidas a '{consulta}'." if consulta else " todavía.")
    return "\n".join(f"• [{n['creada'][:10]}] {n['texto']}" for n in notas[-20:])


@R.herramienta("productividad", "Borra la nota que más se parezca al texto dado", palabras=("borra", "nota"), sensible=True)
def borrar_nota(nota: str) -> str:
    notas = NOTAS.leer()
    elegida, sugerencias = similitud.elegir(nota, notas, clave=lambda n: (n["titulo"], n["texto"]))
    if not elegida:
        return similitud.texto_sugerencias(nota, [n["titulo"] for n in sugerencias], "una nota")
    NOTAS.guardar([n for n in notas if n["id"] != elegida["id"]])
    return f"Borré la nota «{elegida['titulo']}»."


# ============================================================
# Pendientes
# ============================================================

@R.herramienta("productividad", "Agrega una tarea a tu lista de pendientes", alias=("agregar_tarea", "nuevo_pendiente", "todo"),
               palabras=("pendiente", "pendientes", "tarea", "tareas", "agrega", "hacer"))
def agregar_pendiente(tarea: str, fecha: str = "") -> str:
    tarea = tarea.strip()
    if not tarea:
        return "¿Qué pendiente agrego?"
    limite = interpretar_fecha(fecha) if fecha else None
    PENDIENTES.agregar({"id": uuid.uuid4().hex[:8], "tarea": tarea, "fecha": limite.isoformat() if limite else "", "hecho": False})
    return f"Agregué a pendientes: {tarea}" + (f" (para el {limite:%d/%m})" if limite else "") + "."


@R.herramienta("productividad", "Muestra tus pendientes", alias=("mis_pendientes", "ver_tareas", "lista_de_tareas"),
               palabras=("pendientes", "tareas", "hacer", "falta"))
def listar_pendientes(incluir_hechos: bool = False) -> str:
    pendientes = [p for p in PENDIENTES.leer() if incluir_hechos or not p.get("hecho")]
    if not pendientes:
        return "No tienes pendientes. 🎉"
    pendientes.sort(key=lambda p: (p.get("hecho", False), p.get("fecha") or "9999"))
    return "\n".join(
        f"{'✔' if p.get('hecho') else '○'} {p['tarea']}" + (f" (para {p['fecha']})" if p.get("fecha") else "") for p in pendientes
    )


@R.herramienta("productividad", "Marca como hecho el pendiente más parecido", alias=("tachar_pendiente", "terminar_tarea"),
               palabras=("hecho", "termine", "completado", "tacha", "pendiente"))
def completar_pendiente(tarea: str) -> str:
    pendientes = PENDIENTES.leer()
    abiertos = [p for p in pendientes if not p.get("hecho")]
    elegido, sugerencias = similitud.elegir(tarea, abiertos, clave=lambda p: p["tarea"])
    if not elegido:
        return similitud.texto_sugerencias(tarea, [p["tarea"] for p in sugerencias], "un pendiente")
    for p in pendientes:
        if p["id"] == elegido["id"]:
            p["hecho"] = True
    PENDIENTES.guardar(pendientes)
    return f"Listo, taché: {elegido['tarea']}."


# ============================================================
# Agenda
# ============================================================

@R.herramienta("productividad", "Agenda un evento (fecha: hoy, mañana, el viernes, 25/12…; hora opcional)",
               alias=("agendar", "nuevo_evento", "cita"), palabras=("agenda", "evento", "cita", "reunion", "calendario"))
def agregar_evento(titulo: str, fecha: str = "hoy", hora: str = "") -> str:
    dia = interpretar_fecha(fecha)
    if dia is None:
        return f"No entendí la fecha '{fecha}'."
    momento = interpretar_hora(hora, datetime.combine(dia, time(0, 0))) if hora else None
    EVENTOS.agregar({"id": uuid.uuid4().hex[:8], "titulo": titulo.strip(), "fecha": dia.isoformat(),
                     "hora": momento.strftime("%H:%M") if momento else ""})
    return f"Agendé «{titulo.strip()}» el {_DIAS[dia.weekday()]} {dia:%d/%m}" + (f" a las {momento:%H:%M}" if momento else "") + "."


def _formatear_eventos(eventos: list[dict]) -> str:
    eventos = sorted(eventos, key=lambda e: (e["fecha"], e.get("hora") or "99"))
    return "\n".join(
        f"• {_DIAS[date.fromisoformat(e['fecha']).weekday()]} {e['fecha'][8:]}/{e['fecha'][5:7]}"
        f"{' ' + e['hora'] if e.get('hora') else ''} — {e['titulo']}" for e in eventos
    )


@R.herramienta("productividad", "Eventos de un día (hoy por defecto)", alias=("agenda_de", "que_tengo"),
               palabras=("agenda", "eventos", "tengo", "hoy", "manana", "calendario"))
def eventos_de(fecha: str = "hoy") -> str:
    dia = interpretar_fecha(fecha)
    if dia is None:
        return f"No entendí la fecha '{fecha}'."
    eventos = [e for e in EVENTOS.leer() if e["fecha"] == dia.isoformat()]
    return _formatear_eventos(eventos) if eventos else f"No tienes nada agendado el {dia:%d/%m}."


@R.herramienta("productividad", "Eventos de los próximos días", alias=("proximos_eventos", "mi_semana"),
               palabras=("proximos", "semana", "agenda", "eventos"))
def proximos_eventos(dias: int = 7) -> str:
    hoy = date.today()
    limite = hoy + timedelta(days=max(1, int(dias)))
    eventos = [e for e in EVENTOS.leer() if hoy.isoformat() <= e["fecha"] <= limite.isoformat()]
    return _formatear_eventos(eventos) if eventos else f"No tienes eventos en los próximos {dias} días."


@R.herramienta("productividad", "Borra el evento más parecido al título", palabras=("cancela", "borra", "evento", "cita"), sensible=True)
def borrar_evento(titulo: str) -> str:
    eventos = EVENTOS.leer()
    elegido, sugerencias = similitud.elegir(titulo, eventos, clave=lambda e: e["titulo"])
    if not elegido:
        return similitud.texto_sugerencias(titulo, [e["titulo"] for e in sugerencias], "un evento")
    EVENTOS.guardar([e for e in eventos if e["id"] != elegido["id"]])
    return f"Borré «{elegido['titulo']}» del {elegido['fecha']}."
