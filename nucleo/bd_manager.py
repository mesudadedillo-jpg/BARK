"""Biblioteca y memoria privada de Bark, persistidas localmente en SQLite."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from config import config


_bloqueo_bd = threading.RLock()
_turnos_desde_respaldo = 0


@contextmanager
def _conexion():
    # Cada hilo recibe su propia conexión; WAL + busy_timeout evita el clásico
    # "database is locked" cuando la UI y el modo voz terminan a la vez.
    conexion = sqlite3.connect(config.RUTA_MEMORIA, timeout=config.TIEMPO_ESPERA_BD)
    conexion.row_factory = sqlite3.Row
    conexion.execute(f"PRAGMA busy_timeout = {int(config.TIEMPO_ESPERA_BD * 1000)}")
    conexion.execute("PRAGMA journal_mode = WAL")
    conexion.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conexion
        conexion.commit()
    finally:
        conexion.close()


def inicializar_memoria() -> None:
    with _bloqueo_bd:
        Path(config.RUTA_MEMORIA).parent.mkdir(parents=True, exist_ok=True)
        with _conexion() as conexion:
            conexion.execute("""CREATE TABLE IF NOT EXISTS turnos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT NOT NULL,
                chet TEXT NOT NULL, creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conexion.execute("""CREATE TABLE IF NOT EXISTS recuerdos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, categoria TEXT NOT NULL,
                contenido TEXT NOT NULL UNIQUE, creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                prioridad INTEGER NOT NULL DEFAULT 0, sensible INTEGER NOT NULL DEFAULT 0
            )""")
            columnas = {fila["name"] for fila in conexion.execute("PRAGMA table_info(recuerdos)")}
            if "prioridad" not in columnas:
                conexion.execute("ALTER TABLE recuerdos ADD COLUMN prioridad INTEGER NOT NULL DEFAULT 0")
            if "sensible" not in columnas:
                conexion.execute("ALTER TABLE recuerdos ADD COLUMN sensible INTEGER NOT NULL DEFAULT 0")
            conexion.execute("CREATE INDEX IF NOT EXISTS idx_turnos_fecha ON turnos(creado_en)")
            conexion.execute("CREATE INDEX IF NOT EXISTS idx_recuerdos_prioridad ON recuerdos(prioridad, actualizado_en)")


def crear_respaldo_memoria() -> bool:
    """Genera un respaldo consistente y atómico de la memoria local."""
    origen = config.RUTA_MEMORIA
    destino = config.RUTA_RESPALDO_MEMORIA
    if not os.path.isfile(origen):
        return False
    temporal = destino + ".tmp"
    with _bloqueo_bd:
        try:
            Path(destino).parent.mkdir(parents=True, exist_ok=True)
            try:
                os.remove(temporal)
            except FileNotFoundError:
                pass
            fuente = sqlite3.connect(origen, timeout=config.TIEMPO_ESPERA_BD)
            copia = sqlite3.connect(temporal, timeout=config.TIEMPO_ESPERA_BD)
            try:
                fuente.backup(copia)
                copia.commit()
            finally:
                copia.close()
                fuente.close()
            os.replace(temporal, destino)
            return True
        except (sqlite3.Error, OSError):
            try:
                os.remove(temporal)
            except OSError:
                pass
            return False


def verificar_memoria() -> str:
    """Comprueba integridad sin modificar datos; útil para diagnósticos."""
    inicializar_memoria()
    with _bloqueo_bd, _conexion() as conexion:
        resultado = conexion.execute("PRAGMA integrity_check").fetchone()[0]
    return "Memoria local verificada correctamente." if resultado == "ok" else f"La memoria requiere atención: {resultado}"


def guardar_turno(usuario: str, bark: str) -> None:
    inicializar_memoria()
    with _bloqueo_bd, _conexion() as conexion:
        conexion.execute("INSERT INTO turnos (usuario, chet) VALUES (?, ?)", (usuario, bark))
    global _turnos_desde_respaldo
    _turnos_desde_respaldo += 1
    if _turnos_desde_respaldo >= config.RESPALDO_CADA_TURNOS:
        if crear_respaldo_memoria():
            _turnos_desde_respaldo = 0


def cargar_turnos(limite: int = 10) -> list[dict[str, str]]:
    inicializar_memoria()
    with _bloqueo_bd, _conexion() as conexion:
        filas = conexion.execute("SELECT usuario, chet FROM turnos ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    return [{"usuario": fila["usuario"], "chet": fila["chet"]} for fila in reversed(filas)]


def biblioteca_conversaciones(periodo: str = "hoy", consulta: str = "", limite: int = 500) -> list[dict[str, str]]:
    """Obtiene conversaciones por día, semana o todo, sin cargarla al contexto del LLM."""
    inicializar_memoria()
    filtros = {"hoy": "date(creado_en, 'localtime') = date('now', 'localtime')",
               "semana": "datetime(creado_en) >= datetime('now', '-6 days')", "todo": "1=1"}
    condicion = filtros.get(periodo, filtros["hoy"])
    parametros: list[object] = []
    if consulta.strip():
        condicion += " AND (usuario LIKE ? OR chet LIKE ?)"
        patron = f"%{consulta.strip()}%"
        parametros.extend((patron, patron))
    parametros.append(max(1, min(limite, 2000)))
    with _bloqueo_bd, _conexion() as conexion:
        filas = conexion.execute(
            f"SELECT usuario, chet, creado_en FROM turnos WHERE {condicion} ORDER BY id DESC LIMIT ?", parametros
        ).fetchall()
    return [dict(fila) for fila in reversed(filas)]


def estadisticas_biblioteca() -> dict[str, int]:
    inicializar_memoria()
    with _bloqueo_bd, _conexion() as conexion:
        total = conexion.execute("SELECT COUNT(*) FROM turnos").fetchone()[0]
        hoy = conexion.execute("SELECT COUNT(*) FROM turnos WHERE date(creado_en, 'localtime') = date('now', 'localtime')").fetchone()[0]
        prioridad = conexion.execute("SELECT COUNT(*) FROM recuerdos WHERE prioridad > 0").fetchone()[0]
    return {"total": total, "hoy": hoy, "prioridad": prioridad}


def limpiar_memoria() -> None:
    inicializar_memoria()
    # Conserva un punto de recuperación justo antes de un borrado explícito.
    crear_respaldo_memoria()
    with _bloqueo_bd, _conexion() as conexion:
        conexion.execute("DELETE FROM turnos")
        conexion.execute("DELETE FROM recuerdos")


def guardar_recuerdo(categoria: str, contenido: str, prioridad: int = 0, sensible: bool = False) -> bool:
    """Guarda un hecho personal local. Los recuerdos sensibles jamás entran al prompt de nube."""
    inicializar_memoria()
    contenido = " ".join(contenido.split()).strip()
    if not contenido:
        return False
    with _bloqueo_bd, _conexion() as conexion:
        cursor = conexion.execute(
            """INSERT INTO recuerdos (categoria, contenido, prioridad, sensible)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(contenido) DO UPDATE SET categoria=excluded.categoria,
               prioridad=MAX(recuerdos.prioridad, excluded.prioridad),
               sensible=MAX(recuerdos.sensible, excluded.sensible), actualizado_en=CURRENT_TIMESTAMP""",
            (categoria, contenido, max(0, min(prioridad, 3)), int(sensible)),
        )
    return cursor.rowcount > 0


def buscar_recuerdos(consulta: str = "", limite: int = 6, incluir_sensibles: bool = True) -> list[dict[str, str]]:
    """Recupera pocos recuerdos relevantes; la biblioteca completa nunca se añade al prompt."""
    inicializar_memoria()
    palabras = [p.lower() for p in consulta.split() if len(p) >= 4]
    clausula = "" if incluir_sensibles else "WHERE sensible = 0"
    with _bloqueo_bd, _conexion() as conexion:
        filas = conexion.execute(
            f"SELECT categoria, contenido, prioridad, sensible FROM recuerdos {clausula} ORDER BY prioridad DESC, actualizado_en DESC"
        ).fetchall()
    relevantes = []
    for fila in filas:
        puntaje = sum(p in fila["contenido"].lower() for p in palabras) + fila["prioridad"] * 2
        if puntaje or not palabras:
            relevantes.append((puntaje, dict(fila)))
    relevantes.sort(key=lambda item: item[0], reverse=True)
    return [recuerdo for _, recuerdo in relevantes[:limite]]
