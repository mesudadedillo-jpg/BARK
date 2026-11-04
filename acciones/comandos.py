"""
comandos.py
Modulo de acciones: ejecuta tareas reales en el sistema operativo.
Cada funcion es una "herramienta" invocable por el cerebro (LLM) via function calling.
"""

import os
import subprocess
import sys
import platform
import webbrowser
import shutil
import re
import json
import ctypes
import csv
import difflib
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from acciones import apps


# Se actualiza solamente después de una apertura confirmada. Permite entender
# referencias naturales como «ciérralo» sin adivinar la aplicación activa.
_ultima_aplicacion_abierta = ""


def _registrar_aplicacion_abierta(nombre: str) -> None:
    global _ultima_aplicacion_abierta
    _ultima_aplicacion_abierta = nombre.strip()


def ultima_aplicacion_abierta() -> str:
    """Devuelve la última aplicación que Bark abrió durante esta sesión."""
    return _ultima_aplicacion_abierta


def _rutas_inicio() -> list[str]:
    """Ubicaciones habituales de accesos directos de Windows."""
    return [
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]


# ============================================================
# CONTROL DEL SISTEMA (ya existentes)
# ============================================================

def _terminos_busqueda_aplicacion(nombre: str) -> tuple[str, ...]:
    normalizado = nombre.lower().strip()
    alias = {
        "gta": ("gta", "grand theft", "rockstar"),
        "gta launcher": ("gta", "grand theft", "rockstar", "launcher"),
        "roblox": ("roblox",),
        "roblox player": ("roblox",),
        "whatsapp": ("whatsapp",),
        "spotify": ("spotify",),
    }
    return alias.get(normalizado, tuple(p for p in re.findall(r"[\w]+", normalizado) if len(p) >= 3))


def buscar_aplicaciones_instaladas(nombre: str, limite: int = 5) -> list[str]:
    """Busca accesos directos locales, sin subir ni modificar archivos."""
    terminos = _terminos_busqueda_aplicacion(nombre)
    if not terminos:
        return []
    raices = _rutas_inicio()
    hallazgos = []
    vistos = set()
    for raiz in raices:
        if not os.path.isdir(raiz):
            continue
        for directorio, _, archivos in os.walk(raiz):
            for archivo in archivos:
                if not archivo.lower().endswith((".lnk", ".url", ".exe")):
                    continue
                nombre_archivo = archivo.lower()
                if any(termino in nombre_archivo for termino in terminos):
                    ruta = os.path.join(directorio, archivo)
                    if ruta not in vistos:
                        hallazgos.append(ruta)
                        vistos.add(ruta)
    hallazgos.sort(key=lambda ruta: (not any(t in os.path.basename(ruta).lower() for t in terminos), len(ruta)))
    return hallazgos[:limite]


def buscar_y_abrir_aplicacion(nombre: str) -> str:
    """Abre una app localizada por acceso directo; no adivina entre varias opciones."""
    coincidencias = buscar_aplicaciones_instaladas(nombre)
    nombre_normalizado = nombre.lower().strip()
    # Preferencias explícitas para evitar abrir Roblox Studio o un launcher
    # cuando el usuario pidió jugar.
    preferidos = {
        "roblox": ("roblox player",),
        "gta": ("grand theft auto", "gta v"),
    }
    if nombre_normalizado in preferidos:
        candidatos = [
            ruta for ruta in coincidencias
            if any(termino in os.path.basename(ruta).lower() for termino in preferidos[nombre_normalizado])
        ]
        if candidatos:
            coincidencias = candidatos[:1]
    if len(coincidencias) == 1:
        try:
            os.startfile(coincidencias[0])
            _registrar_aplicacion_abierta(nombre)
            return f"Abrí '{os.path.basename(coincidencias[0])}' encontrada en: {coincidencias[0]}"
        except Exception as e:
            return f"Encontré la aplicación, pero no pude abrirla: {e}"
    if len(coincidencias) > 1:
        opciones = "\n".join(f"- {ruta}" for ruta in coincidencias)
        return f"Encontré varias opciones para '{nombre}'. Indica cuál quieres abrir:\n{opciones}"
    return (
        f"No encontré un acceso directo de '{nombre}' en el Escritorio ni el menú Inicio. "
        "Puedes decirme el nombre visible del launcher o su ruta exacta."
    )

def abrir_aplicacion(nombre_app: str) -> str:
    """Abre una aplicación y, en Windows, agota varias rutas seguras antes de rendirse."""
    if platform.system() != "Windows":
        try:
            subprocess.run(["open", "-a", nombre_app] if platform.system() == "Darwin" else [nombre_app])
            _registrar_aplicacion_abierta(nombre_app)
            return f"Aplicación '{nombre_app}' abierta correctamente."
        except Exception as error:
            return f"No se encontró la aplicación '{nombre_app}': {error}"

    nombre_limpio = nombre_app.strip().strip('"')
    alias = nombre_limpio.lower()
    protocolos = {
        "spotify": "spotify:", "configuracion": "ms-settings:", "configuración": "ms-settings:",
        "roblox": "roblox-player:", "roblox player": "roblox-player:", "steam": "steam://open/main",
    }
    if alias in protocolos:
        try:
            os.startfile(protocolos[alias])
            _registrar_aplicacion_abierta(nombre_limpio)
            return f"Abrí '{nombre_limpio}' usando protocolo de Windows."
        except OSError:
            pass

    # Resolución inteligente: alias en español/inglés, apps de la Tienda (UWP),
    # accesos de Inicio y coincidencia aproximada. Ver acciones/apps.py.
    resultado = apps.abrir_inteligente(nombre_limpio)
    if resultado.abierta:
        _registrar_aplicacion_abierta(nombre_limpio)
        return resultado.mensaje
    if resultado.ambigua:
        return resultado.mensaje

    intentos: list[tuple[str, str]] = []
    # Respaldo clásico 1) App Paths del registro.
    try:
        import winreg
        for clave_raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(clave_raiz, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{nombre_limpio}.exe") as clave:
                    ruta, _ = winreg.QueryValueEx(clave, None)
                    if ruta:
                        intentos.append(("registro App Paths", ruta))
            except OSError:
                pass
    except ImportError:
        pass

    # 2) Accesos directos de Inicio/Escritorio. 3) Instalaciones muy comunes.
    intentos.extend(("acceso directo", ruta) for ruta in buscar_aplicaciones_instaladas(nombre_limpio, limite=5))
    candidatos_comunes = {
        "steam": [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam", "steam.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam", "steam.exe"),
        ],
    }
    intentos.extend(("carpeta de instalación común", ruta) for ruta in candidatos_comunes.get(alias, []) if os.path.isfile(ruta))

    # 4) Finalmente permite ejecutables que el PATH ya conoce.
    encontrado = shutil.which(nombre_limpio) or shutil.which(f"{nombre_limpio}.exe")
    if encontrado:
        intentos.append(("PATH", encontrado))

    vistos = set()
    for origen, destino in intentos:
        if destino.lower() in vistos:
            continue
        vistos.add(destino.lower())
        try:
            os.startfile(destino)
            _registrar_aplicacion_abierta(nombre_limpio)
            return f"Abrí '{nombre_limpio}' usando {origen}."
        except OSError:
            continue

    sugerencias = resultado.sugerencias or apps.sugerir(nombre_limpio)
    extra = ("\n¿Quisiste decir: " + ", ".join(sugerencias) + "?") if sugerencias else (
        "\nDime «lista aplicaciones» para ver qué tengo detectado."
    )
    return f"No encontré una forma de abrir '{nombre_limpio}' (probé catálogo, apps de la Tienda, Inicio, registro y PATH).{extra}"


def abrir_ubicacion(ruta: str) -> str:
    """Abre una carpeta o archivo existente sin modificarlo."""
    ruta = os.path.abspath(os.path.expanduser(ruta.strip().strip('"')))
    if not os.path.exists(ruta):
        return f"La ubicación no existe: {ruta}"
    try:
        os.startfile(ruta)
        return f"Abrí: {ruta}"
    except OSError as error:
        return f"No pude abrir '{ruta}': {error}"


def _nombres_juegos_registrados() -> set[str]:
    """Lee nombres de juegos instalados del registro, sin modificar el equipo."""
    if platform.system() != "Windows":
        return set()
    try:
        import winreg
    except ImportError:
        return set()
    palabras = re.compile(r"game|juego|steam|rockstar|ubisoft|electronic arts|epic|minecraft|roblox|gta|grand theft|forza|fortnite|valorant|league of legends", re.I)
    rutas = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    nombres = set()
    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for ruta in rutas:
            try:
                with winreg.OpenKey(raiz, ruta) as clave:
                    for indice in range(winreg.QueryInfoKey(clave)[0]):
                        try:
                            subclave = winreg.EnumKey(clave, indice)
                            with winreg.OpenKey(clave, subclave) as entrada:
                                nombre, _ = winreg.QueryValueEx(entrada, "DisplayName")
                                if nombre and palabras.search(nombre):
                                    nombres.add(str(nombre).strip())
                        except OSError:
                            continue
            except OSError:
                continue
    return nombres


def _nombres_aplicaciones_registradas() -> set[str]:
    """Lee los nombres de programas instalados que Windows publica localmente."""
    if platform.system() != "Windows":
        return set()
    try:
        import winreg
    except ImportError:
        return set()
    rutas = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    nombres = set()
    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for ruta in rutas:
            try:
                with winreg.OpenKey(raiz, ruta) as clave:
                    for indice in range(winreg.QueryInfoKey(clave)[0]):
                        try:
                            with winreg.OpenKey(clave, winreg.EnumKey(clave, indice)) as entrada:
                                nombre, _ = winreg.QueryValueEx(entrada, "DisplayName")
                                if nombre:
                                    nombres.add(str(nombre).strip())
                        except OSError:
                            continue
            except OSError:
                continue
    return nombres


def listar_aplicaciones_instaladas(consulta: str = "", limite: int = 60) -> str:
    """Muestra un inventario local de programas desde Registro, Inicio y Escritorio."""
    nombres = _nombres_aplicaciones_registradas()
    for raiz in _rutas_inicio():
        if not os.path.isdir(raiz):
            continue
        for directorio, _, archivos in os.walk(raiz):
            for archivo in archivos:
                if archivo.lower().endswith((".lnk", ".url", ".exe")):
                    nombres.add(os.path.splitext(archivo)[0])
    try:
        nombres |= apps.nombres_indice()  # incluye apps de la Tienda (UWP)
    except Exception:
        pass
    palabras = [palabra.lower() for palabra in re.findall(r"[\w]+", consulta) if len(palabra) >= 2]
    if palabras:
        nombres = {nombre for nombre in nombres if all(palabra in nombre.lower() for palabra in palabras)}
    # Oculta entradas auxiliares que no son apps elegibles para abrir/cerrar.
    excluir = re.compile(r"update|redistributable|runtime|sdk|driver|support assistant", re.I)
    ordenados = sorted((nombre for nombre in nombres if not excluir.search(nombre)), key=str.casefold)
    if not ordenados:
        detalle = f" que coincidan con '{consulta}'" if consulta else ""
        return f"No encontré aplicaciones instaladas{detalle}."
    mostrados = ordenados[:limite]
    sufijo = "" if len(ordenados) <= limite else f"\n… y {len(ordenados) - limite} más. Dime una parte del nombre para filtrar."
    return f"Encontré {len(ordenados)} aplicaciones{(' que coinciden con ' + repr(consulta)) if consulta else ''}:\n" + "\n".join(f"- {nombre}" for nombre in mostrados) + sufijo


def _nombres_juegos_steam() -> set[str]:
    """Obtiene títulos de los manifiestos de Steam; no inicia Steam ni lee datos privados."""
    if platform.system() != "Windows":
        return set()
    bases = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
    ]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as clave:
            ruta, _ = winreg.QueryValueEx(clave, "SteamPath")
            if ruta:
                bases.insert(0, str(ruta))
    except OSError:
        pass

    nombres = set()
    bibliotecas = set()
    for base in bases:
        steamapps = os.path.join(base, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        bibliotecas.add(steamapps)
        archivo_bibliotecas = os.path.join(steamapps, "libraryfolders.vdf")
        try:
            with open(archivo_bibliotecas, encoding="utf-8", errors="ignore") as archivo:
                # Steam guarda cada biblioteca en una entrada con la clave "path".
                for ruta in re.findall(r'"path"\s+"([^"]+)"', archivo.read(), flags=re.I):
                    bibliotecas.add(os.path.join(ruta.replace("\\\\", "\\"), "steamapps"))
        except OSError:
            pass
    for steamapps in bibliotecas:
        try:
            for archivo in os.listdir(steamapps):
                if not re.fullmatch(r"appmanifest_\d+\.acf", archivo, flags=re.I):
                    continue
                with open(os.path.join(steamapps, archivo), encoding="utf-8", errors="ignore") as manifiesto:
                    coincidencia = re.search(r'"name"\s+"([^"]+)"', manifiesto.read(), flags=re.I)
                    if coincidencia:
                        nombres.add(coincidencia.group(1).strip())
        except OSError:
            continue
    return nombres


def listar_juegos_instalados(limite: int = 40) -> str:
    """Compone un inventario local de juegos desde Inicio, Escritorio y Windows."""
    patrones = re.compile(r"game|juego|steam|rockstar|ubisoft|epic|minecraft|roblox|gta|grand theft|forza|fortnite|valorant|league", re.I)
    nombres = _nombres_juegos_registrados() | _nombres_juegos_steam()
    for raiz in _rutas_inicio():
        if not os.path.isdir(raiz):
            continue
        for directorio, _, archivos in os.walk(raiz):
            for archivo in archivos:
                if archivo.lower().endswith((".lnk", ".url", ".exe")) and patrones.search(archivo):
                    nombres.add(os.path.splitext(archivo)[0])
    # Componentes técnicos de launchers suelen parecer juegos por sus nombres.
    # No los mostramos para que la lista sirva para decidir qué jugar.
    excluir = re.compile(r"support center|steamworks|redistributable|gameinput|\bsdk\b|roblox studio", re.I)
    juegos_detectados = sorted((nombre for nombre in nombres if not excluir.search(nombre)), key=str.casefold)
    juegos = juegos_detectados[:limite]
    if not juegos:
        return "No detecté juegos en Inicio, Escritorio ni en las aplicaciones registradas de Windows. Puedo buscar un launcher concreto si me dices su nombre."
    texto = "\n".join(f"- {juego}" for juego in juegos)
    sufijo = "" if len(juegos_detectados) <= limite else f"\n… y {len(juegos_detectados) - limite} más."
    return f"Encontré estos juegos o launchers instalados:\n{texto}{sufijo}"


def _procesos_activos() -> list[str]:
    """Obtiene ejecutables activos de Windows sin modificar procesos."""
    try:
        resultado = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10)
        return [fila[0] for fila in csv.reader(resultado.stdout.splitlines()) if fila and fila[0].lower().endswith(".exe")]
    except (OSError, subprocess.SubprocessError):
        return []


def _procesos_de_aplicacion(nombre_app: str) -> list[str]:
    """Resuelve una app a sus procesos activos, con protección de Windows."""
    conocidos = {
        "spotify": "Spotify.exe", "roblox": "RobloxPlayerBeta.exe",
        "roblox player": "RobloxPlayerBeta.exe", "configuracion": "SystemSettings.exe",
        "configuración": "SystemSettings.exe", "steam": "steam.exe",
        "discord": "Discord.exe", "whatsapp": "WhatsApp.exe",
    }
    nombre = nombre_app.strip().lower()
    if nombre in conocidos:
        return [conocidos[nombre]]
    palabras = [palabra for palabra in re.findall(r"[\w]+", nombre) if len(palabra) >= 3]
    protegidos = {"system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "dwm", "taskmgr", "explorer"}
    activos = _procesos_activos()
    # Apps con procesos conocidos (Tienda, Calculadora, Paint…): solo los que corren.
    por_catalogo = {p.lower() for p in apps.procesos_conocidos(nombre_app)}
    if por_catalogo:
        coinciden = [p for p in activos if p.lower() in por_catalogo and os.path.splitext(p)[0].lower() not in protegidos]
        if coinciden:
            return list(dict.fromkeys(coinciden))
    if not palabras:
        return []
    encontrados = []
    for proceso in activos:
        base = os.path.splitext(proceso)[0].lower()
        if base in protegidos:
            continue
        if base == nombre or all(palabra in base for palabra in palabras):
            encontrados.append(proceso)
    if not encontrados and len(palabras) == 1 and len(palabras[0]) >= 5:
        # Último recurso: nombre mal dictado («spotifi» → Spotify.exe), con umbral alto.
        bases = {os.path.splitext(p)[0].lower(): p for p in activos if os.path.splitext(p)[0].lower() not in protegidos}
        cercano = difflib.get_close_matches(palabras[0], list(bases), n=1, cutoff=0.82)
        if cercano:
            encontrados.append(bases[cercano[0]])
    return list(dict.fromkeys(encontrados))


def cerrar_aplicacion(nombre_app: str) -> str:
    """Cierra una aplicación resuelta contra procesos activos de Windows."""
    if platform.system() != "Windows":
        return "Cerrar aplicaciones solo esta implementado para Windows por ahora."
    procesos = _procesos_de_aplicacion(nombre_app)
    if not procesos:
        return f"No encontré un proceso activo que corresponda a '{nombre_app}'."
    cerrados = []
    errores = []
    for proceso in procesos:
        try:
            resultado = subprocess.run(["taskkill", "/IM", proceso, "/F"], capture_output=True, text=True, timeout=10)
            if resultado.returncode == 0:
                cerrados.append(proceso)
            else:
                errores.append(proceso)
        except (OSError, subprocess.SubprocessError):
            errores.append(proceso)
    if cerrados:
        detalle = f" ({', '.join(cerrados)})" if len(cerrados) > 1 else ""
        return f"Cerré '{nombre_app}'{detalle}."
    return f"No se pudo cerrar una instancia abierta de '{nombre_app}'."


def diagnosticar_equipo() -> str:
    """Devuelve un diagnóstico local, sin cambiar nada en el sistema."""
    try:
        if platform.system() == "Windows":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            memoria = MEMORYSTATUSEX()
            memoria.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memoria))
            disco = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
            procesos = len(subprocess.run(
                ["tasklist", "/NH"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines())
            libre_gb = disco.free / (1024 ** 3)
            total_gb = disco.total / (1024 ** 3)
            aviso = " Atención: la memoria está muy alta." if memoria.dwMemoryLoad >= 85 else ""
            return (
                f"Diagnóstico local: RAM en uso {memoria.dwMemoryLoad}%; "
                f"RAM disponible {memoria.ullAvailPhys / (1024 ** 3):.1f} GB; "
                f"disco C libre {libre_gb:.1f} GB de {total_gb:.1f} GB; "
                f"procesos visibles {procesos if procesos else 'no disponibles'}.{aviso}"
            )
        return "Diagnóstico básico no implementado para este sistema operativo."
    except Exception as e:
        return f"No se pudo obtener el diagnóstico del equipo: {e}"


def _estado_memoria_windows() -> tuple[int, float]:
    """Devuelve porcentaje usado y GB disponibles sin cargar dependencias."""
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    memoria = MEMORYSTATUSEX()
    memoria.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memoria)):
        raise OSError("Windows no devolvió el estado de memoria")
    return int(memoria.dwMemoryLoad), memoria.ullAvailPhys / (1024 ** 3)


def _procesos_con_memoria() -> list[tuple[str, int, int]]:
    """Obtiene nombre, PID y memoria mostrada por Windows, en KB."""
    try:
        resultado = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=12, encoding="utf-8", errors="replace")
        procesos = []
        for fila in csv.reader(resultado.stdout.splitlines()):
            if len(fila) < 5 or not fila[1].isdigit():
                continue
            memoria = int(re.sub(r"[^0-9]", "", fila[4]) or 0)
            procesos.append((fila[0], int(fila[1]), memoria))
        return procesos
    except (OSError, subprocess.SubprocessError):
        return []


def diagnosticar_ram_avanzado(limite: int = 12) -> str:
    """Muestra los procesos que más RAM consumen sin tocar aplicaciones."""
    if platform.system() != "Windows":
        return "El diagnóstico avanzado de RAM está disponible actualmente para Windows."
    try:
        uso, disponible = _estado_memoria_windows()
        procesos = sorted(_procesos_con_memoria(), key=lambda item: item[2], reverse=True)[:max(1, min(int(limite), 25))]
        if not procesos:
            return f"RAM usada: {uso}%; disponible: {disponible:.1f} GB. No pude enumerar procesos."
        lista = "\n".join(f"- {nombre} (PID {pid}): {kb / 1024:.0f} MB" for nombre, pid, kb in procesos)
        return f"RAM usada: {uso}%; disponible: {disponible:.1f} GB. Procesos con mayor uso:\n{lista}\n\nPuedes cerrar una app que no uses o pedir «libera RAM avanzada»."
    except OSError as error:
        return f"No pude diagnosticar la RAM: {error}"


def liberar_ram_avanzada() -> str:
    """Recorta memoria residente de procesos accesibles sin cerrarlos."""
    if platform.system() != "Windows":
        return "La liberación avanzada de RAM está disponible actualmente para Windows."
    try:
        antes_uso, antes_libre = _estado_memoria_windows()
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        proceso_set_quota = 0x0100
        proceso_query_limitado = 0x1000
        excluidos = {"system idle process", "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "dwm.exe"}
        liberados = 0
        omitidos = 0
        for nombre, pid, _kb in _procesos_con_memoria():
            if pid == os.getpid() or nombre.casefold() in excluidos:
                continue
            manejador = kernel32.OpenProcess(proceso_set_quota | proceso_query_limitado, False, pid)
            if not manejador:
                omitidos += 1
                continue
            try:
                if psapi.EmptyWorkingSet(manejador):
                    liberados += 1
                else:
                    omitidos += 1
            finally:
                kernel32.CloseHandle(manejador)
        despues_uso, despues_libre = _estado_memoria_windows()
        cambio = despues_libre - antes_libre
        return (
            f"Liberación de RAM terminada. Optimicé la memoria residente de {liberados} proceso(s) accesibles "
            f"sin cerrarlos; omití {omitidos} protegidos o no accesibles. RAM: {antes_uso}% → {despues_uso}%; "
            f"disponible: {antes_libre:.2f} → {despues_libre:.2f} GB ({cambio:+.2f} GB)."
        )
    except (OSError, AttributeError) as error:
        return f"No se pudo liberar RAM de forma segura: {error}"


# ============================================================
# MANTENIMIENTO Y ARRANQUE DE WINDOWS
# ============================================================

_CLAVE_INICIO_DESACTIVADO = r"Software\\Bark\\InicioDesactivado"


def _registro_inicio_windows() -> list[dict]:
    """Lee entradas Run sin ejecutar ni alterar nada."""
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    ubicaciones = (
        (winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", "Usuario actual"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", "Todos los usuarios"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run", "Todos los usuarios (32 bits)"),
    )
    entradas = []
    for raiz, ruta, origen in ubicaciones:
        try:
            with winreg.OpenKey(raiz, ruta, 0, winreg.KEY_READ) as clave:
                for indice in range(winreg.QueryInfoKey(clave)[1]):
                    try:
                        nombre, comando, _ = winreg.EnumValue(clave, indice)
                        entradas.append({"id": f"registro:{origen}:{nombre}", "nombre": nombre, "origen": origen, "tipo": "Registro", "detalle": str(comando), "ruta_registro": ruta, "raiz": raiz})
                    except OSError:
                        continue
        except OSError:
            continue
    return entradas


def _carpetas_inicio_windows() -> list[dict]:
    carpetas = (
        (os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"), "Usuario actual"),
        (os.path.join(os.environ.get("PROGRAMDATA", r"C:\\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs", "StartUp"), "Todos los usuarios"),
    )
    entradas = []
    for carpeta, origen in carpetas:
        if not os.path.isdir(carpeta):
            continue
        for nombre in os.listdir(carpeta):
            ruta = os.path.join(carpeta, nombre)
            if os.path.isfile(ruta):
                entradas.append({"id": f"carpeta:{ruta}", "nombre": os.path.splitext(nombre)[0], "origen": origen, "tipo": "Carpeta Inicio", "detalle": ruta, "ruta": ruta})
    return entradas


def _tareas_inicio_windows() -> list[dict]:
    """Lista únicamente tareas de inicio/sesión, omitiendo las de Microsoft."""
    if platform.system() != "Windows":
        return []
    try:
        salida = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
        filas = list(csv.reader(salida.stdout.splitlines()))
    except (OSError, subprocess.SubprocessError):
        return []
    entradas = []
    for fila in filas:
        if not fila or not fila[0] or "microsoft" in fila[0].lower():
            continue
        nombre_tarea = fila[0]
        # Consultar XML da una señal fiable de que la tarea se activa al iniciar
        # Windows o al iniciar sesión; no inferimos desde su nombre.
        try:
            xml = subprocess.run(["schtasks", "/Query", "/TN", nombre_tarea, "/XML"], capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace").stdout.lower()
        except (OSError, subprocess.SubprocessError):
            continue
        if "boottrigger" in xml or "logontrigger" in xml:
            estado = fila[3] if len(fila) > 3 else ""
            entradas.append({"id": f"tarea:{nombre_tarea}", "nombre": nombre_tarea.rsplit("\\", 1)[-1], "origen": "Programador de tareas", "tipo": "Tarea de inicio", "detalle": f"{nombre_tarea} · {estado}", "tarea": nombre_tarea})
    return entradas


def obtener_elementos_inicio() -> list[dict]:
    """Inventario local de elementos que se lanzan al encender o iniciar sesión."""
    return _registro_inicio_windows() + _carpetas_inicio_windows() + _tareas_inicio_windows()


def listar_elementos_inicio() -> str:
    """Muestra las aplicaciones y tareas de arranque detectadas, sin modificarlas."""
    if platform.system() != "Windows":
        return "La gestión de inicio está disponible actualmente para Windows."
    entradas = obtener_elementos_inicio()
    if not entradas:
        return "No detecté elementos de inicio editables en Registro, carpetas Inicio ni tareas de terceros."
    lineas = [f"Inicio de Windows: detecté {len(entradas)} elemento(s)."]
    for entrada in entradas:
        lineas.append(f"- {entrada['nombre']}  ·  {entrada['tipo']} / {entrada['origen']}\n  {entrada['detalle']}")
    lineas.append("Para desactivar uno, di: «desactiva al inicio NOMBRE EXACTO». No se desinstala ninguna aplicación y Bark conserva una copia local de las entradas de tu usuario.")
    return "\n".join(lineas)


def desactivar_elemento_inicio(nombre: str) -> str:
    """Desactiva una única entrada coincidente; se llama tras confirmación explícita."""
    consulta = nombre.strip().casefold()
    if not consulta:
        return "Indica el nombre exacto del elemento de inicio que quieres desactivar."
    coincidencias = [e for e in obtener_elementos_inicio() if consulta == e["nombre"].casefold() or consulta in e["nombre"].casefold()]
    if not coincidencias:
        return f"No encontré un elemento de inicio que coincida con '{nombre}'. Primero pide «mostrar programas de inicio»."
    if len(coincidencias) > 1:
        return "Encontré varias coincidencias; indica uno de estos nombres exactos:\n" + "\n".join(f"- {e['nombre']} ({e['tipo']})" for e in coincidencias)
    entrada = coincidencias[0]
    try:
        if entrada["tipo"] == "Registro":
            import winreg
            # Solo se permite modificar entradas del usuario; las globales pueden
            # requerir administrador y no conviene intentar sortear esa protección.
            if entrada["origen"] != "Usuario actual":
                return f"'{entrada['nombre']}' es global. Abre el Administrador de tareas > Aplicaciones de inicio para administrarla con permisos de Windows."
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entrada["ruta_registro"], 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as clave:
                valor, tipo = winreg.QueryValueEx(clave, entrada["nombre"])
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CLAVE_INICIO_DESACTIVADO) as respaldo:
                    winreg.SetValueEx(respaldo, entrada["nombre"], 0, tipo, valor)
                winreg.DeleteValue(clave, entrada["nombre"])
            return f"Desactivé '{entrada['nombre']}' del inicio y guardé una copia local para poder restaurarla."
        if entrada["tipo"] == "Carpeta Inicio":
            destino_base = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "Bark", "InicioDesactivado")
            os.makedirs(destino_base, exist_ok=True)
            destino = os.path.join(destino_base, os.path.basename(entrada["ruta"]))
            if os.path.exists(destino):
                return f"Ya existe una copia de '{entrada['nombre']}' en el respaldo de Bark; no hice cambios."
            shutil.move(entrada["ruta"], destino)
            return f"Desactivé '{entrada['nombre']}' del inicio y moví su acceso directo a un respaldo local."
        resultado = subprocess.run(["schtasks", "/Change", "/TN", entrada["tarea"], "/Disable"], capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace")
        if resultado.returncode == 0:
            return f"Desactivé la tarea de inicio '{entrada['nombre']}'. Puedes volver a habilitarla desde el Programador de tareas."
        return f"Windows no permitió desactivar '{entrada['nombre']}': {resultado.stderr.strip() or resultado.stdout.strip()}"
    except (OSError, subprocess.SubprocessError) as error:
        return f"No se pudo desactivar '{entrada['nombre']}': {error}"


def resumen_optimizacion() -> str:
    """Da recomendaciones observables y no realiza limpieza automática."""
    if platform.system() != "Windows":
        return "El análisis de optimización está disponible actualmente para Windows."
    diagnostico = diagnosticar_equipo()
    inicio = obtener_elementos_inicio()
    temp = Path(tempfile.gettempdir())
    try:
        tamano_temp = sum(archivo.stat().st_size for archivo in temp.rglob("*") if archivo.is_file()) / (1024 ** 2)
    except OSError:
        tamano_temp = 0
    sugerencias = [diagnostico, f"Inicio: {len(inicio)} elementos detectados.", f"Temporales del usuario: aproximadamente {tamano_temp:.0f} MB."]
    if len(inicio) >= 8:
        sugerencias.append("Recomendación: revisa los programas de inicio y desactiva solo los que reconozcas y no necesites al encender.")
    sugerencias.append("Herramientas disponibles: mostrar programas de inicio, desactivar al inicio NOMBRE, y limpiar temporales del usuario.")
    return "\n".join(sugerencias)


def limpiar_temporales_usuario() -> str:
    """Elimina solo archivos temporales del usuario; requiere confirmación."""
    raiz = Path(tempfile.gettempdir()).resolve()
    eliminados = 0
    liberados = 0
    try:
        for elemento in raiz.iterdir():
            try:
                if elemento.is_file() or elemento.is_symlink():
                    liberados += elemento.stat().st_size
                    elemento.unlink()
                    eliminados += 1
                elif elemento.is_dir() and elemento.name.lower().startswith(("bark_", "tmp")):
                    tamano = sum(a.stat().st_size for a in elemento.rglob("*") if a.is_file())
                    shutil.rmtree(elemento)
                    liberados += tamano
                    eliminados += 1
            except OSError:
                continue
        return f"Limpieza terminada: {eliminados} elemento(s) temporal(es) eliminados; espacio recuperado aproximado {liberados / (1024 ** 2):.1f} MB. Archivos en uso se conservaron."
    except OSError as error:
        return f"No se pudo completar la limpieza de temporales: {error}"


# ============================================================
# ASISTENTE DE DESARROLLO LOCAL
# ============================================================

def _ruta_proyecto_segura(ruta: str = "") -> Path:
    """Resuelve una ruta dentro del proyecto de Bark, sin escapar de él."""
    base = Path(__file__).resolve().parent.parent
    candidata = (base / ruta).resolve() if ruta else base
    if candidata != base and base not in candidata.parents:
        raise ValueError("La ruta debe estar dentro del proyecto local de Bark.")
    return candidata


def explorar_proyecto(ruta: str = "", profundidad: int = 2) -> str:
    """Lista archivos del proyecto sin inventar rutas ni leer su contenido."""
    try:
        base = _ruta_proyecto_segura(ruta)
        if not base.is_dir():
            return f"'{base}' no es una carpeta del proyecto."
        profundidad = max(0, min(int(profundidad), 4))
        ignorar = {"__pycache__", ".git", ".venv", "venv", "node_modules", "modelos", "bd_memoria"}
        elementos = []
        for actual, directorios, archivos in os.walk(base):
            rel = Path(actual).relative_to(base)
            nivel = len(rel.parts)
            directorios[:] = [d for d in directorios if d not in ignorar and nivel < profundidad]
            if nivel > profundidad:
                continue
            for archivo in sorted(archivos):
                if archivo.endswith((".py", ".txt", ".md", ".json", ".toml", ".yml", ".yaml", ".bat")):
                    elementos.append(str((rel / archivo).as_posix()))
        if not elementos:
            return "No encontré archivos de código o configuración en esa carpeta."
        limite = 120
        extra = f"\n… y {len(elementos) - limite} más." if len(elementos) > limite else ""
        prefijo = ruta or "."
        return f"Proyecto · {prefijo}:\n" + "\n".join(f"- {e}" for e in elementos[:limite]) + extra
    except (OSError, ValueError) as error:
        return f"No pude explorar el proyecto: {error}"


def leer_archivo_codigo(ruta: str, max_caracteres: int = 12000) -> str:
    """Lee fuente y configuración de Bark tras resolver una ruta conocida."""
    try:
        archivo = _ruta_proyecto_segura(ruta)
        if not archivo.is_file():
            return f"El archivo no existe en el proyecto: {ruta}"
        if archivo.suffix.lower() not in {".py", ".txt", ".md", ".json", ".toml", ".yml", ".yaml", ".bat"}:
            return f"No leo '{archivo.suffix}' como código o configuración."
        return archivo.read_text(encoding="utf-8", errors="replace")[:max(1, min(int(max_caracteres), 30000))]
    except (OSError, ValueError) as error:
        return f"No pude leer el archivo: {error}"


def reemplazar_texto_archivo(ruta: str, buscar: str, reemplazar: str) -> str:
    """Reemplazo exacto y atómico para ediciones de código controladas."""
    try:
        archivo = _ruta_proyecto_segura(ruta)
        if not archivo.is_file():
            return f"El archivo no existe en el proyecto: {ruta}"
        contenido = archivo.read_text(encoding="utf-8")
        apariciones = contenido.count(buscar)
        if apariciones != 1:
            return f"Edición cancelada: encontré {apariciones} coincidencias exactas; se requiere exactamente 1 para evitar modificar el bloque equivocado."
        nuevo = contenido.replace(buscar, reemplazar, 1)
        temporal = archivo.with_suffix(archivo.suffix + ".bark.tmp")
        temporal.write_text(nuevo, encoding="utf-8")
        os.replace(temporal, archivo)
        return f"Edición aplicada de forma atómica en: {archivo.relative_to(_ruta_proyecto_segura())}"
    except (OSError, ValueError) as error:
        return f"No pude aplicar la edición: {error}"


def verificar_archivo_python(ruta: str) -> str:
    """Compila un archivo Python del proyecto para detectar errores de sintaxis."""
    try:
        archivo = _ruta_proyecto_segura(ruta)
        if archivo.suffix.lower() != ".py" or not archivo.is_file():
            return "Indica un archivo .py existente dentro del proyecto."
        resultado = subprocess.run([sys.executable, "-m", "py_compile", str(archivo)], capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace")
        if resultado.returncode == 0:
            return f"Verificación correcta: {ruta} no tiene errores de sintaxis."
        return f"La verificación encontró un error:\n{resultado.stderr.strip() or resultado.stdout.strip()}"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return f"No pude verificar el archivo: {error}"


def crear_carpeta(ruta: str) -> str:
    try:
        os.makedirs(ruta, exist_ok=True)
        return f"Carpeta creada en: {ruta}"
    except Exception as e:
        return f"Error al crear carpeta: {e}"


def listar_archivos(ruta: str = ".") -> str:
    try:
        elementos = os.listdir(ruta)
        return "\n".join(elementos) if elementos else f"La carpeta '{ruta}' esta vacia."
    except FileNotFoundError:
        return f"La ruta '{ruta}' no existe."
    except Exception as e:
        return f"Error al listar archivos: {e}"


def eliminar_archivo(ruta_archivo: str) -> str:
    """Elimina un archivo. SIEMPRE debe pasar por pedir_permiso_usuario antes."""
    try:
        if os.path.isfile(ruta_archivo):
            os.remove(ruta_archivo)
            return f"Archivo '{ruta_archivo}' eliminado."
        return f"'{ruta_archivo}' no es un archivo valido o no existe."
    except Exception as e:
        return f"Error al eliminar archivo: {e}"


def escribir_archivo(ruta_archivo: str, contenido: str) -> str:
    """Escribe de forma atómica para no dejar archivos truncados tras un cierre."""
    try:
        ruta_archivo = os.path.abspath(os.path.expanduser(ruta_archivo.strip().strip('"')))
        carpeta = os.path.dirname(ruta_archivo) or "."
        os.makedirs(carpeta, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(prefix=".bark_", suffix=".tmp", dir=carpeta, text=True)
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            f.write(contenido)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ruta_archivo)
        return f"Archivo '{ruta_archivo}' escrito correctamente ({len(contenido)} caracteres)."
    except Exception as e:
        try:
            if 'temporal' in locals() and os.path.exists(temporal):
                os.remove(temporal)
        except OSError:
            pass
        return f"Error al escribir archivo: {e}"


# ============================================================
# BUSQUEDA WEB (local, sin API keys)
# ============================================================

def _curl_texto(url: str) -> str:
    """Obtiene una URL con el almacen TLS de Windows, sin desactivar TLS."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl no esta disponible")
    resultado = subprocess.run(
        [curl, "-L", "--silent", "--show-error", "--max-time", "15", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr[:250])
    return resultado.stdout


def _buscar_en_wikipedia(query: str, num_resultados: int) -> str:
    """Fuente de respaldo cuando un buscador exige CAPTCHA."""
    try:
        url = "https://es.wikipedia.org/w/api.php?" + urlencode({
            "action": "opensearch", "search": query, "limit": num_resultados,
            "namespace": 0, "format": "json",
        })
        _, titulos, _, enlaces = json.loads(_curl_texto(url))
        pares = [(titulo, enlace) for titulo, enlace in zip(titulos, enlaces) if enlace]
        if not pares:
            # Las frases largas no siempre son títulos enciclopédicos. Reintenta
            # con el término más informativo sin obligar al usuario a reformular.
            palabras = re.findall(r"[\wáéíóúñ]+", query, flags=re.IGNORECASE)
            if len(palabras) > 1:
                return _buscar_en_wikipedia(palabras[-1], num_resultados)
            return f"No se encontraron resultados para '{query}'."
        salida = [f"- {titulo}\n  {enlace}" for titulo, enlace in pares]
        return (
            f"Resultados para '{query}' (fuente alternativa: Wikipedia; "
            "el buscador bloqueo la consulta automatizada):\n" + "\n".join(salida)
        )
    except Exception as e:
        return f"No se pudo buscar informacion: {e}"

def buscar_informacion(query: str, num_resultados: int = 3) -> str:
    """
    Busca en DuckDuckGo (no requiere API key) y devuelve texto resumido
    de los primeros resultados. Usa el motor HTML simple de DDG.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
            timeout=10,
        )
        html = resp.text
    except requests.RequestException:
        # En Windows, curl usa el almacen de certificados del sistema. Es una
        # alternativa segura cuando Python no reconoce el certificado corporativo.
        try:
            html = _curl_texto(f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}")
        except Exception:
            return _buscar_en_wikipedia(query, num_resultados)

    try:
        soup = BeautifulSoup(html, "html.parser")
        resultados = soup.select("a.result__a, .result__title a")[:num_resultados]

        if not resultados:
            return _buscar_en_wikipedia(query, num_resultados)

        salida = []
        for r in resultados:
            titulo = r.get_text(strip=True)
            link = r.get("href")
            salida.append(f"- {titulo}\n  {link}")

        return f"Resultados para '{query}':\n" + "\n".join(salida)
    except Exception as e:
        return f"Error al buscar informacion: {e}"


def extraer_texto_pagina(url: str, max_caracteres: int = 3000) -> str:
    """Entra a una URL especifica y extrae el texto principal (sin scripts/estilos)."""
    try:
        from urllib.parse import urlparse
        import requests
        from bs4 import BeautifulSoup
        destino = urlparse(url.strip())
        if destino.scheme not in {"http", "https"} or not destino.netloc:
            return "La URL no es válida; debe ser una dirección http o https completa."
        # Evita que una URL inducida por contenido web lea servicios locales.
        if destino.hostname in {"localhost", "127.0.0.1", "::1"}:
            return "Por seguridad no puedo consultar direcciones locales."
        max_caracteres = max(1, min(int(max_caracteres), 20_000))
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException:
            html = _curl_texto(url)
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        texto = " ".join(soup.get_text(separator=" ").split())
        return texto[:max_caracteres]
    except Exception as e:
        return f"Error al extraer texto de la pagina: {e}"


def abrir_en_navegador(url: str) -> str:
    """Abre una URL en el navegador predeterminado del sistema."""
    try:
        # Los modelos pequeños a veces devuelven URLs pegadas a Markdown.
        coincidencia = re.search(r"https?://[^\s\]\)]+", url)
        url_limpia = coincidencia.group(0) if coincidencia else url.strip(" []()")
        if not url_limpia.startswith(("https://", "http://")):
            return "La URL no es valida; debe comenzar con http:// o https://."
        if not webbrowser.open(url_limpia):
            return "El sistema no confirmo que pudiera abrir el navegador."
        return f"Se solicito abrir el navegador en: {url_limpia}"
    except Exception as e:
        return f"Error al abrir navegador: {e}"


# ============================================================
# LECTURA DE ARCHIVOS LOCALES
# ============================================================

_ultimos_archivos_encontrados: list[str] = []
_ultimas_canciones_encontradas: list[dict] = []


def _carpetas_personales() -> list[str]:
    hogar = os.path.expanduser("~")
    candidatos = [os.path.join(hogar, nombre) for nombre in ("Desktop", "Documents", "Downloads")]
    candidatos.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resultados"))
    return [ruta for ruta in candidatos if os.path.isdir(ruta)]


def buscar_archivos_locales(consulta: str = "", fecha: str = "", limite: int = 8) -> str:
    """Encuentra documentos recientes por nombre/fecha y conserva opciones para elegir después."""
    global _ultimos_archivos_encontrados
    palabras = [palabra for palabra in re.findall(r"[\wáéíóúñ]+", consulta.lower()) if len(palabra) >= 2]
    objetivo = None
    hoy = date.today()
    fecha_normalizada = fecha.lower().strip()
    if fecha_normalizada in {"hoy", "hoy mismo"}:
        objetivo = hoy
    elif fecha_normalizada == "ayer":
        objetivo = hoy - timedelta(days=1)
    else:
        for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                objetivo = datetime.strptime(fecha_normalizada, formato).date()
                break
            except ValueError:
                continue

    encontrados = []
    extensiones = {".pdf", ".docx", ".txt", ".xlsx", ".pptx", ".jpg", ".jpeg", ".png", ".mp3", ".wav"}
    for raiz in _carpetas_personales():
        for directorio, subdirectorios, archivos in os.walk(raiz):
            # Evita revisar árboles enormes o técnicos dentro de las carpetas personales.
            subdirectorios[:] = [d for d in subdirectorios if not d.startswith(".") and d.lower() not in {"node_modules", "venv", "__pycache__"}]
            for archivo in archivos:
                ruta = os.path.join(directorio, archivo)
                if os.path.splitext(archivo)[1].lower() not in extensiones:
                    continue
                if palabras and not all(palabra in archivo.lower() for palabra in palabras):
                    continue
                try:
                    modificado = datetime.fromtimestamp(os.path.getmtime(ruta))
                except OSError:
                    continue
                if objetivo and modificado.date() != objetivo:
                    continue
                encontrados.append((modificado, ruta))
    encontrados.sort(reverse=True, key=lambda item: item[0])
    _ultimos_archivos_encontrados = [ruta for _, ruta in encontrados[:limite]]
    if not _ultimos_archivos_encontrados:
        detalle_fecha = f" del {fecha}" if fecha else ""
        return f"No encontré archivos{detalle_fecha} con ese criterio en Escritorio, Documentos, Descargas o resultados."
    opciones = "\n".join(
        f"{indice}. {os.path.basename(ruta)} — modificado {datetime.fromtimestamp(os.path.getmtime(ruta)).strftime('%d/%m/%Y %H:%M')}"
        for indice, ruta in enumerate(_ultimos_archivos_encontrados, 1)
    )
    return f"Encontré estas opciones. Di “abre la primera” o indica el nombre:\n{opciones}"


def abrir_archivo_encontrado(indice: int = 1) -> str:
    if not _ultimos_archivos_encontrados:
        return "No tengo una búsqueda de archivos reciente. Dime qué archivo quieres buscar primero."
    if not 1 <= indice <= len(_ultimos_archivos_encontrados):
        return f"Solo hay {len(_ultimos_archivos_encontrados)} opciones; indica un número válido."
    ruta = _ultimos_archivos_encontrados[indice - 1]
    try:
        os.startfile(ruta)
        return f"Abrí '{os.path.basename(ruta)}'."
    except OSError as error:
        return f"Encontré el archivo, pero no pude abrirlo: {error}"


def convertir_documento(ruta: str, formato_destino: str) -> str:
    """Convierte DOCX a PDF con Word o PDF a DOCX como texto editable."""
    ruta = ruta.strip().strip('"')
    if not os.path.isfile(ruta):
        return f"No existe el archivo '{ruta}'."
    origen = os.path.splitext(ruta)[1].lower()
    destino = formato_destino.lower().lstrip(".")
    ruta_destino = os.path.splitext(ruta)[0] + "." + destino
    if os.path.exists(ruta_destino):
        return f"Ya existe '{ruta_destino}'. Renómbralo o muévelo antes de convertir para no sobrescribirlo."
    try:
        if origen == ".docx" and destino == "pdf":
            import win32com.client
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            documento = None
            try:
                documento = word.Documents.Open(os.path.abspath(ruta), ReadOnly=True)
                documento.SaveAs2(os.path.abspath(ruta_destino), FileFormat=17)
            finally:
                if documento is not None:
                    documento.Close(False)
                word.Quit()
            return f"Convertí '{os.path.basename(ruta)}' a PDF: {ruta_destino}"
        if origen == ".pdf" and destino == "docx":
            import docx
            from PyPDF2 import PdfReader
            lector = PdfReader(ruta)
            documento = docx.Document()
            for pagina in lector.pages:
                documento.add_paragraph(pagina.extract_text() or "")
            documento.save(ruta_destino)
            return f"Convertí el texto de '{os.path.basename(ruta)}' a Word: {ruta_destino}. El diseño visual del PDF puede variar."
        return "Por ahora puedo convertir DOCX a PDF y PDF a DOCX."
    except Exception as error:
        return f"No pude convertir el archivo: {error}"


def buscar_canciones(consulta: str, limite: int = 5) -> str:
    """Busca metadatos de canciones y guarda opciones para reproducir en Spotify."""
    global _ultimas_canciones_encontradas
    _ultimas_canciones_encontradas = []
    try:
        import requests
        parametros = {"term": consulta, "entity": "song", "limit": limite, "country": "MX"}
        try:
            respuesta = requests.get("https://itunes.apple.com/search", params=parametros, timeout=8)
            respuesta.raise_for_status()
            datos = respuesta.json()
        except requests.RequestException:
            # En algunas instalaciones Windows curl tiene certificados/red que
            # Python no hereda; reutilizamos el respaldo ya usado por Bark.
            datos = json.loads(_curl_texto("https://itunes.apple.com/search?" + urlencode(parametros)))
        resultados = datos.get("results", [])
        _ultimas_canciones_encontradas = [
            {"titulo": item["trackName"], "artista": item["artistName"]}
            for item in resultados if item.get("trackName") and item.get("artistName")
        ]
    except Exception as error:
        return f"No pude buscar canciones en este momento: {error}"
    if not _ultimas_canciones_encontradas:
        return f"No encontré canciones para '{consulta}'."
    opciones = "\n".join(
        f"{numero}. {cancion['titulo']} — {cancion['artista']}"
        for numero, cancion in enumerate(_ultimas_canciones_encontradas, 1)
    )
    return f"Encontré estas canciones. Di “quiero la primera” o “la de [artista]”: \n{opciones}"


def reproducir_cancion_encontrada(indice: int = 0, artista: str = "") -> str:
    if not _ultimas_canciones_encontradas:
        return "No tengo una búsqueda de canciones reciente. Dime artista, canción o género primero."
    seleccionada = None
    if artista:
        artista_normalizado = artista.casefold()
        seleccionada = next((c for c in _ultimas_canciones_encontradas if artista_normalizado in c["artista"].casefold()), None)
        if seleccionada is None:
            return f"No encontré una opción de '{artista}' entre los últimos resultados."
    elif 1 <= indice <= len(_ultimas_canciones_encontradas):
        seleccionada = _ultimas_canciones_encontradas[indice - 1]
    else:
        return f"Elige una opción entre 1 y {len(_ultimas_canciones_encontradas)}."
    return buscar_y_reproducir_spotify(f"{seleccionada['titulo']} {seleccionada['artista']}")

def leer_documento(ruta: str, max_caracteres: int = 5000) -> str:
    """
    Lee el contenido de un archivo .txt, .pdf o .docx y devuelve el texto.
    Requiere: PyPDF2, python-docx (agregar a requirements.txt)
    """
    if not os.path.isfile(ruta):
        return f"El archivo '{ruta}' no existe."

    ext = os.path.splitext(ruta)[1].lower()

    try:
        if ext == ".txt":
            with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:max_caracteres]

        elif ext == ".pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(ruta)
            texto = ""
            for pagina in reader.pages:
                texto += pagina.extract_text() or ""
                if len(texto) >= max_caracteres:
                    break
            return texto[:max_caracteres]

        elif ext == ".docx":
            import docx
            doc = docx.Document(ruta)
            texto = "\n".join(p.text for p in doc.paragraphs)
            return texto[:max_caracteres]

        else:
            return f"Formato '{ext}' no soportado todavia."

    except Exception as e:
        return f"Error al leer documento: {e}"


def crear_documento_word(ruta_archivo: str, titulo: str, contenido: str,
                         referencias: str = "") -> str:
    """Crea un .docx con presentación académica APA 7 básica."""
    if not ruta_archivo.lower().endswith(".docx"):
        return "La ruta del documento Word debe terminar en .docx."
    if os.path.exists(ruta_archivo):
        return "Ya existe un archivo en esa ruta; elige otro nombre para no sobrescribirlo."
    try:
        import docx
        from docx.enum.section import WD_SECTION_START
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt

        os.makedirs(os.path.dirname(ruta_archivo) or ".", exist_ok=True)
        documento = docx.Document()
        seccion = documento.sections[0]
        seccion.page_width = Inches(8.5)
        seccion.page_height = Inches(11)
        seccion.top_margin = Inches(1)
        seccion.bottom_margin = Inches(1)
        seccion.left_margin = Inches(1)
        seccion.right_margin = Inches(1)
        estilo = documento.styles["Normal"]
        estilo.font.name = "Times New Roman"
        estilo.font.size = Pt(12)
        estilo.paragraph_format.line_spacing = 2
        estilo.paragraph_format.space_after = Pt(0)

        # Numeración de página en la esquina superior derecha.
        encabezado = seccion.header.paragraphs[0]
        encabezado.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        campo = OxmlElement("w:fldSimple")
        campo.set(qn("w:instr"), "PAGE")
        encabezado._p.append(campo)

        # Portada APA de estudiante; no se inventan autor, curso ni institución.
        portada = documento.add_paragraph()
        portada.alignment = WD_ALIGN_PARAGRAPH.CENTER
        portada.paragraph_format.space_before = Inches(2.2)
        portada.add_run(titulo).bold = True
        for linea in ("", "Autor: Usuario", "Investigación generada por Bark", date.today().isoformat()):
            p = documento.add_paragraph(linea)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        documento.add_section(WD_SECTION_START.NEW_PAGE)
        cuerpo_seccion = documento.sections[-1]
        cuerpo_seccion.top_margin = Inches(1)
        cuerpo_seccion.bottom_margin = Inches(1)
        cuerpo_seccion.left_margin = Inches(1)
        cuerpo_seccion.right_margin = Inches(1)
        encabezado_cuerpo = cuerpo_seccion.header.paragraphs[0]
        encabezado_cuerpo.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        campo_cuerpo = OxmlElement("w:fldSimple")
        campo_cuerpo.set(qn("w:instr"), "PAGE")
        encabezado_cuerpo._p.append(campo_cuerpo)

        encabezado = documento.add_paragraph()
        encabezado.alignment = WD_ALIGN_PARAGRAPH.CENTER
        encabezado.add_run(titulo).bold = True
        for parrafo in contenido.split("\n"):
            if parrafo.strip():
                if re.fullmatch(r"Fuente\s+\d+", parrafo.strip(), flags=re.I):
                    p = documento.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    p.add_run(parrafo.strip()).bold = True
                else:
                    p = documento.add_paragraph(parrafo.strip())
                    p.paragraph_format.first_line_indent = Inches(0.5)
        if referencias.strip():
            documento.add_page_break()
            documento.add_heading("Referencias", level=1)
            for referencia in referencias.split("\n"):
                if referencia.strip():
                    p = documento.add_paragraph(referencia.strip())
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.first_line_indent = Inches(-0.5)
        documento.save(ruta_archivo)
        return f"Documento Word creado correctamente en: {os.path.abspath(ruta_archivo)}"
    except Exception as e:
        return f"Error al crear documento Word: {e}"


def investigar_y_crear_word(tema: str, ruta_archivo: str = "") -> str:
    """Investiga fuentes web y guarda los extractos y referencias en un Word local."""
    resultados_texto = buscar_informacion(tema, num_resultados=3)
    if resultados_texto.startswith(("No se encontraron", "Error", "No se pudo")):
        return resultados_texto

    enlaces = re.findall(r"https?://\S+", resultados_texto)
    fuentes = []
    for enlace in enlaces[:3]:
        texto = extraer_texto_pagina(enlace, max_caracteres=1800)
        if texto and not texto.startswith("Error"):
            fuentes.append((enlace, texto))
    if not fuentes:
        return "Se encontraron enlaces, pero no se pudo extraer texto verificable de ellos."

    if not ruta_archivo:
        nombre = "_".join(re.findall(r"[A-Za-z0-9]+", tema.lower()))[:60] or "investigacion"
        ruta_archivo = os.path.join("resultados", f"investigacion_{nombre}.docx")
    cuerpo = [f"Investigación recopilada el {date.today().isoformat()} sobre: {tema}.", ""]
    referencias = []
    for numero, (enlace, texto) in enumerate(fuentes, 1):
        cuerpo.extend([f"Fuente {numero}", texto, ""])
        referencias.append(f"Fuente {numero}. (s. f.). Recuperado el {date.today().isoformat()}, de {enlace}")
    return crear_documento_word(ruta_archivo, f"Investigación: {tema}", "\n".join(cuerpo), "\n".join(referencias))


# ============================================================
# CAPTURA Y ANALISIS DE PANTALLA
# ============================================================

def tomar_captura_pantalla(ruta_guardado: str = "temp_captura.png") -> str:
    """
    Toma una captura de pantalla y la guarda localmente.
    Requiere: pyautogui, Pillow
    Nota: analizar el CONTENIDO de la imagen requeriria un modelo
    multimodal local (ej. LLaVA via Ollama) - eso es un paso aparte.
    """
    try:
        import pyautogui
        captura = pyautogui.screenshot()
        captura.save(ruta_guardado)
        return f"Captura guardada en: {ruta_guardado}"
    except Exception as e:
        return f"Error al tomar captura: {e}"


# ============================================================
# DESCARGA DE MEDIOS (YouTube, etc.)
# ============================================================

def descargar_video(url: str, solo_audio: bool = False, carpeta_destino: str = "descargas") -> str:
    """
    Descarga video o audio de YouTube usando yt-dlp.
    Requiere: yt-dlp instalado (pip install yt-dlp) y disponible en PATH,
    o el binario yt-dlp.exe empaquetado localmente.
    """
    try:
        os.makedirs(carpeta_destino, exist_ok=True)
        comando = ["yt-dlp", "-o", f"{carpeta_destino}/%(title)s.%(ext)s"]

        if solo_audio:
            comando += ["-x", "--audio-format", "mp3"]

        comando.append(url)

        resultado = subprocess.run(
            comando, capture_output=True, text=True, timeout=300
        )

        if resultado.returncode == 0:
            return f"Descarga completada en '{carpeta_destino}'."
        else:
            return f"Error al descargar: {resultado.stderr[:500]}"

    except FileNotFoundError:
        return "yt-dlp no esta instalado o no se encuentra en el PATH."
    except subprocess.TimeoutExpired:
        return "La descarga tardo demasiado y se cancelo (timeout)."
    except Exception as e:
        return f"Error al descargar video: {e}"


# ============================================================
# EJECUCION DE CODIGO (para que el agente pruebe lo que escribe)
# ============================================================

def ejecutar_script_python(ruta_script: str, carpeta_sandbox: str = "sandbox") -> str:
    """
    Ejecuta un script Python DENTRO de una carpeta sandbox aislada.
    Nunca ejecuta scripts fuera de esa carpeta, por seguridad.
    """
    ruta_absoluta = os.path.abspath(ruta_script)
    sandbox_absoluta = os.path.abspath(carpeta_sandbox)

    try:
        dentro_sandbox = os.path.commonpath((ruta_absoluta, sandbox_absoluta)) == sandbox_absoluta
    except ValueError:
        dentro_sandbox = False
    if not dentro_sandbox:
        return "Por seguridad, solo se pueden ejecutar scripts dentro de la carpeta sandbox."

    if not os.path.isfile(ruta_absoluta):
        return f"El script '{ruta_script}' no existe."

    try:
        resultado = subprocess.run(
            [sys.executable, ruta_absoluta],
            capture_output=True, text=True, timeout=30
        )
        salida = resultado.stdout
        errores = resultado.stderr
        return f"--- Salida ---\n{salida}\n--- Errores ---\n{errores}" if errores else f"--- Salida ---\n{salida}"
    except subprocess.TimeoutExpired:
        return "El script tardo demasiado y se detuvo (timeout de 30s)."
    except Exception as e:
        return f"Error al ejecutar script: {e}"


# ============================================================
# SEGURIDAD: PERMISOS DEL USUARIO
# ============================================================

def pedir_permiso_usuario(descripcion_accion: str) -> bool:
    """
    Pausa la ejecucion y pide confirmacion en consola.
    En la version final, esto debe abrir un dialogo real en ui.py
    (ej. con tkinter messagebox.askyesno) en lugar de usar input().
    """
    respuesta = input(f"\n[PERMISO REQUERIDO] Bark quiere: {descripcion_accion}\n¿Autorizas? (s/n): ")
    return respuesta.strip().lower() == "s"


# --- Zona de pruebas ---
if __name__ == "__main__":
    print(buscar_informacion("clima en Hidalgo Mexico hoy"))
    print()
    print(leer_documento("requirements.txt"))
# ============================================================
# CONTROL DE PESTAÑAS DEL NAVEGADOR
# ============================================================

def cerrar_pestana_navegador() -> str:
    """
    Cierra la pestaña activa del navegador enviando Ctrl+W.
    Requiere que el navegador tenga el foco (limitacion real: no
    podemos saber con certeza que ventana esta activa sin libreria
    adicional como pygetwindow).
    """
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "w")
        return "Se envio Ctrl+W para cerrar la pestaña activa."
    except Exception as e:
        return f"Error al cerrar pestaña: {e}"


# ============================================================
# CONTROL DE SPOTIFY
# ============================================================

def controlar_spotify(accion: str) -> str:
    """
    Controla la reproduccion de Spotify usando teclas multimedia del sistema.
    accion: "play_pausa", "siguiente", "anterior"
    Requiere que Spotify este abierto (no necesita estar en foco,
    las teclas multimedia son globales en Windows).
    """
    try:
        import pyautogui

        acciones_validas = {
            "play_pausa": "playpause",
            "siguiente": "nexttrack",
            "anterior": "prevtrack",
        }

        if accion not in acciones_validas:
            return f"Accion '{accion}' no reconocida. Usa: play_pausa, siguiente, anterior."

        pyautogui.press(acciones_validas[accion])
        return f"Comando '{accion}' enviado a Spotify."
    except Exception as e:
        return f"Error al controlar Spotify: {e}"


def buscar_y_reproducir_spotify(cancion: str) -> str:
    """
    Abre Spotify, usa su buscador (Ctrl+K) y escribe la busqueda.
    Limitacion real: no puede confirmar automaticamente cual resultado
    tocar sin vision o la API oficial de Spotify, asi que deja la
    busqueda lista y el usuario presiona Enter/selecciona.
    """
    try:
        import pyautogui
        import time

        abrir_aplicacion("spotify")
        time.sleep(2)  # esperar que la app tome foco

        pyautogui.hotkey("ctrl", "k")
        time.sleep(0.5)
        pyautogui.write(cancion, interval=0.02)
        time.sleep(1)
        pyautogui.press("down")
        pyautogui.press("enter")

        return (
            f"Se buscó '{cancion}' y se envió la orden de reproducir el primer resultado en Spotify. "
            "No puedo confirmar el estado final ni si está disponible sin conexión."
        )
    except Exception as e:
        return f"Error al buscar en Spotify: {e}"


# ============================================================
# CARPETA DE RESULTADOS EN ESCRITORIO
# ============================================================

def crear_carpeta_resultados(nombre_tarea: str) -> str:
    """
    Crea (o reutiliza) una carpeta 'Resultados_<nombre_tarea>' en el escritorio
    del usuario, y devuelve su ruta completa para que otras herramientas
    (escribir_archivo, generar PDF, etc.) guarden ahi sus salidas.
    """
    from config import config

    nombre_limpio = "".join(
        c for c in nombre_tarea if c.isalnum() or c in (" ", "_", "-")
    ).strip().replace(" ", "_")

    nombre_carpeta = f"{config.PREFIJO_CARPETA_RESULTADOS}{nombre_limpio}"
    ruta_completa = os.path.join(config.CARPETA_ESCRITORIO, nombre_carpeta)

    try:
        os.makedirs(ruta_completa, exist_ok=True)
        return f"Carpeta de resultados lista en: {ruta_completa}"
    except Exception as e:
        return f"Error al crear carpeta de resultados: {e}"
