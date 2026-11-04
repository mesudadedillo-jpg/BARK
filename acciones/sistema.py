"""
sistema.py
Herramientas nuevas de control del equipo (Windows). No añade dependencias:
usa ctypes, PowerShell y utilidades que ya trae Windows.
"""

from __future__ import annotations

import ctypes
import difflib
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta

from acciones import apps, comandos

_ES_WINDOWS = platform.system() == "Windows"
_SOLO_WINDOWS = "Esta función solo está disponible en Windows."
_SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ============================================================
# Volumen y brillo
# ============================================================
_VK_VOLUMEN = {"silenciar": 0xAD, "bajar": 0xAE, "subir": 0xAF}


def _tecla(vk: int, veces: int = 1) -> None:
    usuario = ctypes.windll.user32  # type: ignore[attr-defined]
    for _ in range(veces):
        usuario.keybd_event(vk, 0, 0, 0)
        usuario.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def controlar_volumen(accion: str, pasos: int = 5) -> str:
    """accion = subir | bajar | silenciar. Cada paso mueve el volumen 2 %."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    accion = apps.normalizar(accion)
    alias = {"sube": "subir", "aumenta": "subir", "baja": "bajar", "disminuye": "bajar",
             "mute": "silenciar", "silencia": "silenciar", "activar": "silenciar", "quitar": "silenciar"}
    accion = alias.get(accion, accion)
    if accion not in _VK_VOLUMEN:
        return "Acción de volumen no válida. Usa: subir, bajar o silenciar."
    try:
        pasos = max(1, min(50, int(pasos)))
    except (TypeError, ValueError):
        pasos = 5
    _tecla(_VK_VOLUMEN[accion], 1 if accion == "silenciar" else pasos)
    if accion == "silenciar":
        return "Alterné el silencio del volumen."
    return f"Volumen {'subido' if accion == 'subir' else 'bajado'} (~{pasos * 2} %)."


def fijar_volumen(porcentaje: int) -> str:
    """Pone el volumen en un valor aproximado (pasos de 2 %)."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    try:
        objetivo = max(0, min(100, int(porcentaje)))
    except (TypeError, ValueError):
        return "Dime un porcentaje de volumen entre 0 y 100."
    _tecla(_VK_VOLUMEN["bajar"], 50)
    _tecla(_VK_VOLUMEN["subir"], round(objetivo / 2))
    return f"Dejé el volumen en aproximadamente {objetivo} %."


def fijar_brillo(porcentaje: int) -> str:
    """Brillo de pantalla de portátiles (WMI). Los monitores externos no lo admiten."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    try:
        valor = max(0, min(100, int(porcentaje)))
    except (TypeError, ValueError):
        return "Dime un porcentaje de brillo entre 0 y 100."
    salida = apps._powershell(
        "try { (Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1,{valor}); 'ok' }} catch {{ 'error' }}"
    )
    if "ok" in salida:
        return f"Brillo ajustado a {valor} %."
    return "Esta pantalla no permite cambiar el brillo desde software (es normal en monitores externos)."


# ============================================================
# Estado del equipo
# ============================================================

class _EstadoEnergia(ctypes.Structure):
    _fields_ = [("AC", ctypes.c_ubyte), ("Carga", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("Reservado", ctypes.c_ubyte), ("SegundosRestantes", ctypes.c_ulong),
                ("SegundosCompleta", ctypes.c_ulong)]


def estado_bateria() -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    estado = _EstadoEnergia()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(estado)):  # type: ignore[attr-defined]
        return "No pude leer el estado de la batería."
    if estado.Carga == 255 or estado.Flags & 128:
        return "Este equipo no tiene batería (está conectado a la corriente)."
    texto = f"Batería al {estado.Carga} %"
    texto += ", cargando." if estado.AC == 1 else ", con batería."
    if estado.AC == 0 and estado.SegundosRestantes != 0xFFFFFFFF:
        horas, resto = divmod(estado.SegundosRestantes // 60, 60)
        texto += f" Quedan unos {horas} h {resto} min."
    return texto


# ============================================================
# Sesión, energía y ventanas
# ============================================================

def bloquear_pc() -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
    return "Bloqueé la sesión."


def minimizar_todo() -> str:
    """Win+D: muestra el escritorio (otra pulsación restaura las ventanas)."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    usuario = ctypes.windll.user32  # type: ignore[attr-defined]
    for vk, abajo in ((0x5B, True), (0x44, True), (0x44, False), (0x5B, False)):
        usuario.keybd_event(vk, 0, 0 if abajo else 2, 0)
    return "Mostré el escritorio."


def _programar_energia(argumentos: list[str], mensaje: str) -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    try:
        subprocess.run(argumentos, check=True, capture_output=True, creationflags=_SIN_VENTANA)
        return mensaje
    except (OSError, subprocess.SubprocessError) as error:
        return f"No pude completar la acción: {error}"


def apagar_pc(segundos: int = 30) -> str:
    segundos = max(5, min(3600, int(segundos or 30)))
    return _programar_energia(["shutdown", "/s", "/t", str(segundos)],
                              f"Apagaré el equipo en {segundos} s. Di «cancela el apagado» para detenerlo.")


def reiniciar_pc(segundos: int = 30) -> str:
    segundos = max(5, min(3600, int(segundos or 30)))
    return _programar_energia(["shutdown", "/r", "/t", str(segundos)],
                              f"Reiniciaré el equipo en {segundos} s. Di «cancela el apagado» para detenerlo.")


def suspender_pc() -> str:
    return _programar_energia(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], "Suspendiendo el equipo.")


def cancelar_apagado() -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    resultado = subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=_SIN_VENTANA)
    return "Cancelé el apagado programado." if resultado.returncode == 0 else "No había un apagado programado."


def listar_ventanas_abiertas() -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    ventanas = _ventanas()
    if not ventanas:
        return "No encontré ventanas abiertas."
    return "Ventanas abiertas:\n" + "\n".join(f"- {v['ProcessName']}: {v['MainWindowTitle']}" for v in ventanas[:25])


def _ventanas() -> list[dict]:
    salida = apps._powershell(
        "Get-Process | Where-Object { $_.MainWindowTitle } | "
        "Select-Object Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress"
    )
    try:
        datos = json.loads(salida) if salida.strip() else []
    except json.JSONDecodeError:
        return []
    return [datos] if isinstance(datos, dict) else [d for d in datos if isinstance(d, dict)]


def enfocar_ventana(nombre: str) -> str:
    """Trae al frente la ventana cuyo proceso o título se parezca más a «nombre»."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    mejor, mejor_p = None, 0.0
    for ventana in _ventanas():
        puntaje = max(apps.puntuar(nombre, ventana["ProcessName"]), apps.puntuar(nombre, ventana["MainWindowTitle"]))
        # El título suele ser largo («Documento - Word»): también vale que contenga el nombre.
        if apps.normalizar(nombre) and apps.normalizar(nombre) in apps.normalizar(ventana["MainWindowTitle"]):
            puntaje = max(puntaje, 0.9)
        if puntaje > mejor_p:
            mejor, mejor_p = ventana, puntaje
    if not mejor or mejor_p < 0.6:
        return f"No encontré una ventana abierta que coincida con '{nombre}'."
    apps._powershell(f"(New-Object -ComObject WScript.Shell).AppActivate({int(mejor['Id'])}) | Out-Null")
    return f"Cambié a {mejor['ProcessName']} ({mejor['MainWindowTitle'][:60]})."


# ============================================================
# Configuración de Windows
# ============================================================
_AJUSTES = {
    "wifi": "ms-settings:network-wifi", "wi fi": "ms-settings:network-wifi", "red": "ms-settings:network",
    "internet": "ms-settings:network", "bluetooth": "ms-settings:bluetooth",
    "pantalla": "ms-settings:display", "brillo": "ms-settings:display", "sonido": "ms-settings:sound",
    "audio": "ms-settings:sound", "bateria": "ms-settings:batterysaver", "energia": "ms-settings:powersleep",
    "almacenamiento": "ms-settings:storagesense", "disco": "ms-settings:storagesense",
    "actualizaciones": "ms-settings:windowsupdate", "windows update": "ms-settings:windowsupdate",
    "aplicaciones": "ms-settings:appsfeatures", "programas": "ms-settings:appsfeatures",
    "inicio": "ms-settings:startupapps", "privacidad": "ms-settings:privacy",
    "fondo": "ms-settings:personalization-background", "personalizacion": "ms-settings:personalization",
    "idioma": "ms-settings:regionlanguage", "hora": "ms-settings:dateandtime",
    "teclado": "ms-settings:typing", "mouse": "ms-settings:mousetouchpad", "impresoras": "ms-settings:printers",
    "vpn": "ms-settings:network-vpn", "modo avion": "ms-settings:network-airplanemode",
    "accesibilidad": "ms-settings:easeofaccess", "notificaciones": "ms-settings:notifications",
    "seguridad": "windowsdefender:", "antivirus": "windowsdefender:",
}


def abrir_configuracion(seccion: str = "") -> str:
    """Abre una sección de Configuración de Windows (wifi, bluetooth, pantalla, sonido…)."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    clave = apps.normalizar(seccion)
    uri = _AJUSTES.get(clave)
    if uri is None and clave:
        cercano = next(iter(difflib.get_close_matches(clave, _AJUSTES, n=1, cutoff=0.75)), None)
        uri = _AJUSTES.get(cercano) if cercano else None
    uri = uri or "ms-settings:"
    os.startfile(uri)  # type: ignore[attr-defined]
    return f"Abrí la configuración de {seccion.strip() or 'Windows'}."


# ============================================================
# Portapapeles
# ============================================================

def leer_portapapeles(max_caracteres: int = 2000) -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    texto = apps._powershell("Get-Clipboard -Raw").strip()
    return texto[:max_caracteres] if texto else "El portapapeles está vacío o no contiene texto."


def copiar_al_portapapeles(texto: str) -> str:
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=str(texto), text=True, encoding="utf-8", check=True, timeout=10, creationflags=_SIN_VENTANA,
        )
        return f"Copié {len(str(texto))} caracteres al portapapeles."
    except (OSError, subprocess.SubprocessError) as error:
        return f"No pude copiar al portapapeles: {error}"


# ============================================================
# Temporizadores y recordatorios
# ============================================================
_temporizadores: list[dict] = []
_notificador = None  # función(texto) opcional que puede poner la UI (p. ej. hablar el aviso)


def configurar_notificador(funcion) -> None:
    """Permite que la interfaz muestre/diga el aviso en vez del cuadro de Windows."""
    global _notificador
    _notificador = funcion


def _avisar(texto: str) -> None:
    if _notificador:
        try:
            _notificador(texto)
            return
        except Exception:
            pass
    if not _ES_WINDOWS:
        print(f"[AVISO] {texto}")
        return
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 250)
    except Exception:
        pass
    # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
    ctypes.windll.user32.MessageBoxW(0, texto, "Bark · Recordatorio", 0x00 | 0x40 | 0x10000 | 0x40000)  # type: ignore[attr-defined]


def crear_temporizador(minutos: float, mensaje: str = "") -> str:
    """Avisa dentro de «minutos» (admite decimales: 0.5 = 30 s). Dura mientras Bark esté abierto."""
    try:
        minutos = float(minutos)
    except (TypeError, ValueError):
        return "Dime en cuántos minutos quieres el aviso."
    if not 0.05 <= minutos <= 24 * 60:
        return "Puedo programar avisos entre unos segundos y 24 horas."
    texto = (mensaje or "").strip() or "Se acabó el tiempo."
    cuando = datetime.now() + timedelta(minutes=minutos)
    registro = {"hora": cuando, "mensaje": texto}

    def disparar() -> None:
        if registro in _temporizadores:
            _temporizadores.remove(registro)
        _avisar(texto)

    hilo = threading.Timer(minutos * 60, disparar)
    hilo.daemon = True
    registro["hilo"] = hilo
    _temporizadores.append(registro)
    hilo.start()
    duracion = f"{round(minutos * 60)} s" if minutos < 1 else f"{minutos:g} min"
    return f"Listo, te aviso en {duracion} (a las {cuando.strftime('%H:%M')}): {texto}"


def listar_temporizadores() -> str:
    if not _temporizadores:
        return "No hay temporizadores activos."
    ordenados = sorted(_temporizadores, key=lambda t: t["hora"])
    return "Temporizadores activos:\n" + "\n".join(f"- {t['hora'].strftime('%H:%M:%S')}: {t['mensaje']}" for t in ordenados)


def cancelar_temporizadores() -> str:
    cantidad = len(_temporizadores)
    for registro in list(_temporizadores):
        registro["hilo"].cancel()
        _temporizadores.remove(registro)
    return f"Cancelé {cantidad} temporizador(es)." if cantidad else "No había temporizadores activos."


# ============================================================
# Optimización del equipo
# ============================================================
_PLANES_ENERGIA = {
    "alto": ("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", "Alto rendimiento"),
    "equilibrado": ("381b4222-f694-41f0-9685-ff5bb260df2e", "Equilibrado"),
    "ahorro": ("a1841308-3541-4fab-bc81-f71556f20b4a", "Economizador de energía"),
}


def plan_de_energia(modo: str = "") -> str:
    """Muestra o cambia el plan de energía: alto | equilibrado | ahorro."""
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    clave = apps.normalizar(modo)
    if not clave:
        actual = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", creationflags=_SIN_VENTANA).stdout.strip()
        return actual or "No pude leer el plan de energía."
    clave = ("alto" if any(p in clave for p in ("alto", "rendimiento", "maximo", "juego"))
             else "ahorro" if any(p in clave for p in ("ahorro", "economi", "bateria"))
             else "equilibrado" if "equilibr" in clave else clave)
    if clave not in _PLANES_ENERGIA:
        return "Modos disponibles: alto rendimiento, equilibrado o ahorro de energía."
    guid, nombre = _PLANES_ENERGIA[clave]
    resultado = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, text=True,
                               creationflags=_SIN_VENTANA)
    if resultado.returncode == 0:
        return f"Plan de energía cambiado a «{nombre}»."
    return f"Este equipo no tiene el plan «{nombre}» disponible (común en portátiles modernos)."


def optimizar_pc() -> str:
    """Optimización segura de un solo paso (pide permiso una vez).

    Libera RAM sin cerrar apps, borra temporales del usuario, vacía la caché DNS y
    entrega un informe con lo que más consume y lo que conviene revisar. No cierra
    programas ni desinstala nada.
    """
    if not _ES_WINDOWS:
        return _SOLO_WINDOWS
    informe = []
    try:
        uso_antes, libre_antes = comandos._estado_memoria_windows()
        disco = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        libre_disco_antes = disco.free / 1024 ** 3
    except Exception:
        uso_antes = libre_antes = libre_disco_antes = None

    informe.append("1) " + comandos.liberar_ram_avanzada())
    informe.append("2) " + comandos.limpiar_temporales_usuario())
    try:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10, creationflags=_SIN_VENTANA)
        informe.append("3) Vacié la caché DNS.")
    except (OSError, subprocess.SubprocessError):
        informe.append("3) No pude vaciar la caché DNS.")

    pesados = sorted(comandos._procesos_con_memoria(), key=lambda p: p[2], reverse=True)[:5]
    if pesados:
        informe.append("4) Lo que más RAM usa ahora:\n" + "\n".join(f"   - {n}: {kb / 1024:.0f} MB" for n, _pid, kb in pesados))
    inicio = comandos.obtener_elementos_inicio()
    if len(inicio) >= 6:
        informe.append(f"5) Tienes {len(inicio)} programas que arrancan con Windows. Di «qué se abre al iniciar» y desactiva los que no uses: es lo que más acelera el encendido.")
    plan = plan_de_energia()
    if "ahorro" in plan.lower() or "saver" in plan.lower():
        informe.append("6) Estás en plan de ahorro de energía, que limita el procesador. Di «modo alto rendimiento» si estás conectado.")

    try:
        uso_despues, libre_despues = comandos._estado_memoria_windows()
        disco = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        if uso_antes is not None:
            informe.append(
                f"\nResultado: RAM {uso_antes}% → {uso_despues}% (libre {libre_antes:.1f} → {libre_despues:.1f} GB); "
                f"disco libre {libre_disco_antes:.1f} → {disco.free / 1024 ** 3:.1f} GB."
            )
    except Exception:
        pass
    return "\n".join(informe)
