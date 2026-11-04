"""Servicio de voz independiente de la interfaz de Bark."""

import os
import subprocess
import sys

from config import config
from acciones import comandos
from nucleo.cerebro import configurar_solicitador_permiso, procesar_mensaje
from nucleo.escuchar import VozNoDisponible, ejecutar_modo_voz, solicitar_permiso_por_voz


RUTA_PID = os.path.join(config.CARPETA_BD, "bark_asistente.pid")


def _pid_sigue_activo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def asistente_ya_activo() -> bool:
    try:
        with open(RUTA_PID, "r", encoding="utf-8") as archivo:
            activo = _pid_sigue_activo(int(archivo.read().strip()))
        if not activo:
            try:
                os.remove(RUTA_PID)
            except OSError:
                pass
        return activo
    except (OSError, ValueError):
        return False


def ejecutar_servicio() -> None:
    os.makedirs(config.CARPETA_BD, exist_ok=True)
    # O_EXCL evita que dos clics rápidos creen dos oyentes de micrófono.
    try:
        descriptor = os.open(RUTA_PID, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(str(os.getpid()))
    except FileExistsError:
        if asistente_ya_activo():
            print("Bark ya está escuchando en segundo plano.")
            return
        return ejecutar_servicio()
    try:
        configurar_solicitador_permiso(solicitar_permiso_por_voz)
        ejecutar_modo_voz(procesar_mensaje)
    except VozNoDisponible as error:
        print(f"Bark: {error}")
    finally:
        configurar_solicitador_permiso(comandos.pedir_permiso_usuario)
        try:
            if open(RUTA_PID, encoding="utf-8").read().strip() == str(os.getpid()):
                os.remove(RUTA_PID)
        except OSError:
            pass


def iniciar_en_segundo_plano() -> str:
    if asistente_ya_activo():
        return "Bark ya está escuchando en segundo plano."
    argumentos = [sys.executable, os.path.join(config.CARPETA_BASE, "main.py"), "--asistente"]
    bandera = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(argumentos, cwd=config.CARPETA_BASE, creationflags=bandera)
        return "Bark se inició en segundo plano. Di 'Bark, detente' para cerrarlo."
    except OSError as error:
        return f"No pude iniciar Bark en segundo plano: {error}"
