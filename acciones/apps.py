"""
apps.py
Resolución inteligente de aplicaciones de Windows.

Problema que resuelve: «abre la store de microsoft» fallaba porque la Tienda,
la Calculadora, Fotos, etc. son apps de la Tienda (UWP) y no tienen acceso
directo .lnk. Aquí se combinan cuatro fuentes y una coincidencia difusa:

1. Catálogo de alias (sinónimos en español/inglés → protocolo, exe o proceso).
2. Get-StartApps de PowerShell: TODAS las apps del menú Inicio, también UWP.
3. Accesos directos de Inicio/Escritorio y App Paths del registro.
4. Coincidencia por tokens sin acentos, prefijos y similitud (difflib).

El índice se guarda en disco y se refresca en segundo plano, de modo que abrir
una app es instantáneo después del primer uso.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ES_WINDOWS = platform.system() == "Windows"
_BASE = Path(__file__).resolve().parent.parent
_RUTA_CACHE = _BASE / "bd_memoria" / "indice_apps.json"
_VIGENCIA_CACHE = 12 * 3600  # segundos antes de refrescar en segundo plano

# Umbrales de decisión. Por encima de _ABRIR se abre sin preguntar si el mejor
# candidato le saca ventaja al segundo; por debajo de _SUGERIR no se propone.
_ABRIR = 0.74
_VENTAJA = 0.07
_SUGERIR = 0.50

_RUIDO = {
    "la", "el", "los", "las", "de", "del", "mi", "mis", "una", "un", "app", "apps",
    "aplicacion", "aplicaciones", "programa", "programas", "por", "favor", "porfa",
    "ahora", "para", "en", "pc", "computadora", "compu", "y", "me", "lo", "al",
}


# ============================================================
# Texto
# ============================================================

def normalizar(texto: str) -> str:
    """Minúsculas, sin acentos ni signos; solo letras, números y espacios."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", str(texto).lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", sin_acentos).strip()


def tokens(texto: str) -> frozenset[str]:
    return frozenset(t for t in normalizar(texto).split() if t not in _RUIDO)


# ============================================================
# Catálogo de alias
# ============================================================
# destino: (tipo, valor). tipos: protocolo | exe. «procesos» sirve para cerrar.
# NUNCA incluir explorer.exe en «procesos»: cerrarlo mata el escritorio.

def _entrada(claves, destino, procesos=(), nombre=""):
    return {"claves": tuple(claves), "destino": destino, "procesos": tuple(procesos),
            "nombre": nombre or claves[0].title()}


CATALOGO = [
    _entrada(["microsoft store", "tienda de microsoft", "tienda microsoft", "windows store",
              "store", "tienda", "ms store", "tienda de windows", "tienda de aplicaciones"],
             ("protocolo", "ms-windows-store:"), ["WinStore.App.exe"], "Microsoft Store"),
    _entrada(["configuracion", "ajustes", "settings", "configuracion de windows"],
             ("protocolo", "ms-settings:"), ["SystemSettings.exe"], "Configuración"),
    _entrada(["calculadora", "calculator", "calc"],
             ("exe", "calc.exe"), ["CalculatorApp.exe", "calc.exe", "Calculator.exe"], "Calculadora"),
    _entrada(["bloc de notas", "notepad", "bloc notas", "notas de windows"],
             ("exe", "notepad.exe"), ["notepad.exe", "Notepad.exe"], "Bloc de notas"),
    _entrada(["paint", "mspaint", "pintura"],
             ("exe", "mspaint.exe"), ["mspaint.exe", "PaintStudio.View.exe"], "Paint"),
    _entrada(["explorador de archivos", "administrador de archivos", "este equipo", "mi pc",
              "file explorer", "explorador de windows"],
             ("exe", "explorer.exe"), [], "Explorador de archivos"),
    _entrada(["simbolo del sistema", "cmd", "consola", "command prompt"],
             ("exe", "cmd.exe"), ["cmd.exe"], "Símbolo del sistema"),
    _entrada(["terminal", "windows terminal", "terminal de windows"],
             ("exe", "wt.exe"), ["WindowsTerminal.exe"], "Terminal"),
    _entrada(["powershell", "power shell"],
             ("exe", "powershell.exe"), ["powershell.exe", "pwsh.exe"], "PowerShell"),
    _entrada(["administrador de tareas", "task manager", "monitor de tareas", "taskmgr"],
             ("exe", "taskmgr.exe"), ["Taskmgr.exe"], "Administrador de tareas"),
    _entrada(["panel de control", "control panel"],
             ("exe", "control.exe"), [], "Panel de control"),
    _entrada(["herramienta de recorte", "recortes", "recorte", "snipping tool", "recortar pantalla"],
             ("protocolo", "ms-screenclip:"), ["SnippingTool.exe", "ScreenClippingHost.exe"], "Recortes"),
    _entrada(["reloj", "alarmas", "alarma", "alarmas y reloj"],
             ("protocolo", "ms-clock:"), ["Time.exe"], "Alarmas y reloj"),
    _entrada(["fotos", "visor de fotos", "galeria"],
             ("protocolo", "ms-photos:"), ["Photos.exe", "Microsoft.Photos.exe"], "Fotos"),
    _entrada(["camara"],
             ("protocolo", "microsoft.windows.camera:"), ["WindowsCamera.exe"], "Cámara"),
    _entrada(["mapas", "maps"],
             ("protocolo", "bingmaps:"), ["Maps.exe"], "Mapas"),
    _entrada(["clima", "el clima", "tiempo", "weather"],
             ("protocolo", "msnweather:"), ["Microsoft.Msn.Weather.exe"], "Clima"),
    _entrada(["xbox", "xbox app"],
             ("protocolo", "xbox:"), ["XboxPcApp.exe", "XboxApp.exe"], "Xbox"),
    _entrada(["barra de juegos", "game bar", "xbox game bar"],
             ("protocolo", "ms-gamebar:"), ["GameBar.exe"], "Barra de juegos"),
    _entrada(["microsoft edge", "edge"],
             ("exe", "msedge.exe"), ["msedge.exe"], "Microsoft Edge"),
    _entrada(["google chrome", "chrome"],
             ("exe", "chrome.exe"), ["chrome.exe"], "Google Chrome"),
    _entrada(["firefox", "mozilla firefox"],
             ("exe", "firefox.exe"), ["firefox.exe"], "Firefox"),
    _entrada(["brave", "brave browser"],
             ("exe", "brave.exe"), ["brave.exe"], "Brave"),
    _entrada(["word", "microsoft word"],
             ("exe", "winword.exe"), ["WINWORD.EXE"], "Word"),
    _entrada(["excel", "microsoft excel"],
             ("exe", "excel.exe"), ["EXCEL.EXE"], "Excel"),
    _entrada(["powerpoint", "power point", "microsoft powerpoint"],
             ("exe", "powerpnt.exe"), ["POWERPNT.EXE"], "PowerPoint"),
    _entrada(["outlook", "microsoft outlook"],
             ("exe", "outlook.exe"), ["OUTLOOK.EXE", "olk.exe"], "Outlook"),
    _entrada(["visual studio code", "vscode", "vs code", "code", "visual estudio code"],
             ("exe", "code"), ["Code.exe"], "Visual Studio Code"),
    _entrada(["discord"], ("exe", "discord"), ["Discord.exe"], "Discord"),
    _entrada(["whatsapp", "wasap", "guasap"], ("exe", "whatsapp"),
             ["WhatsApp.exe", "WhatsApp.Root.exe"], "WhatsApp"),
    _entrada(["telegram"], ("exe", "telegram"), ["Telegram.exe"], "Telegram"),
    _entrada(["spotify"], ("protocolo", "spotify:"), ["Spotify.exe"], "Spotify"),
    _entrada(["steam"], ("protocolo", "steam://open/main"), ["steam.exe"], "Steam"),
    _entrada(["roblox", "roblox player"], ("protocolo", "roblox-player:"),
             ["RobloxPlayerBeta.exe"], "Roblox Player"),
]


def _afinidad_alias(consulta: str, clave: str) -> float:
    q, k = tokens(consulta), tokens(clave)
    if not q or not k:
        return 0.0
    if normalizar(consulta) == normalizar(clave):
        return 1.0
    if q == k:
        return 0.97
    # «abre la tienda de microsoft por favor» → las palabras de la clave están todas
    if k <= q and len(k) >= 2:
        return 0.90
    # Errores de dictado/escritura: «calculadra», «spotifi».
    if len(q) == 1 and len(k) == 1:
        a, b = next(iter(q)), next(iter(k))
        if len(a) >= 4 and len(b) >= 4:
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r >= 0.84:
                return 0.80 + (r - 0.84) * 0.5
    return 0.0


def buscar_en_catalogo(consulta: str) -> dict | None:
    """Devuelve la entrada de catálogo que mejor coincide, o None."""
    mejor, mejor_puntaje = None, 0.0
    for entrada in CATALOGO:
        puntaje = max(_afinidad_alias(consulta, clave) for clave in entrada["claves"])
        if puntaje > mejor_puntaje:
            mejor, mejor_puntaje = entrada, puntaje
    return mejor if mejor_puntaje >= 0.80 else None


def procesos_conocidos(consulta: str) -> list[str]:
    """Procesos asociados a una app del catálogo (para poder cerrarla)."""
    entrada = buscar_en_catalogo(consulta)
    return list(entrada["procesos"]) if entrada else []


# ============================================================
# Índice de apps instaladas
# ============================================================

def _powershell(script: str, timeout: int = 25) -> str:
    if not ES_WINDOWS:
        return ""
    preludio = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    try:
        resultado = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", preludio + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return resultado.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _apps_de_inicio() -> list[dict]:
    """Todas las apps que Windows muestra en Inicio, incluidas las de la Tienda."""
    salida = _powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
    if not salida.strip():
        return []
    try:
        datos = json.loads(salida)
    except json.JSONDecodeError:
        return []
    if isinstance(datos, dict):
        datos = [datos]
    return [
        {"nombre": str(d["Name"]).strip(), "tipo": "aumid", "destino": str(d["AppID"]).strip()}
        for d in datos if isinstance(d, dict) and d.get("Name") and d.get("AppID")
    ]


def _raices_accesos() -> list[str]:
    inicio = Path("Microsoft") / "Windows" / "Start Menu" / "Programs"
    return [
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.environ.get("APPDATA", ""), str(inicio)),
        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), str(inicio)),
    ]


def _accesos_directos() -> list[dict]:
    encontrados = []
    for raiz in _raices_accesos():
        if not raiz or not os.path.isdir(raiz):
            continue
        for directorio, _, archivos in os.walk(raiz):
            for archivo in archivos:
                if archivo.lower().endswith((".lnk", ".url")):
                    encontrados.append({
                        "nombre": os.path.splitext(archivo)[0],
                        "tipo": "archivo",
                        "destino": os.path.join(directorio, archivo),
                    })
    return encontrados


def _app_paths() -> list[dict]:
    if not ES_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    encontrados = []
    ruta = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(raiz, ruta) as clave:
                for i in range(winreg.QueryInfoKey(clave)[0]):
                    try:
                        exe = winreg.EnumKey(clave, i)
                    except OSError:
                        continue
                    if exe.lower().endswith(".exe"):
                        encontrados.append({"nombre": exe[:-4], "tipo": "exe", "destino": exe})
        except OSError:
            continue
    return encontrados


_RUIDO_NOMBRE = re.compile(r"uninstall|desinstal|readme|license|licencia|help|ayuda|website|sitio web|release notes", re.I)


def construir_indice() -> list[dict]:
    """Construye el índice completo. Tarda 1-3 s en Windows; no se llama en cada orden."""
    vistos: dict[str, dict] = {}
    # Orden de prioridad: Get-StartApps (más fiable) > accesos directos > App Paths.
    for entrada in _apps_de_inicio() + _accesos_directos() + _app_paths():
        if _RUIDO_NOMBRE.search(entrada["nombre"]):
            continue
        clave = normalizar(entrada["nombre"])
        if clave and clave not in vistos:
            vistos[clave] = entrada
    return list(vistos.values())


_indice: list[dict] | None = None
_bloqueo = threading.Lock()
_refrescando = False


def _leer_cache() -> tuple[list[dict], float]:
    try:
        datos = json.loads(_RUTA_CACHE.read_text(encoding="utf-8"))
        return list(datos.get("apps", [])), float(datos.get("creado", 0))
    except (OSError, ValueError, TypeError):
        return [], 0.0


def _guardar_cache(apps: list[dict]) -> None:
    try:
        _RUTA_CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporal = _RUTA_CACHE.with_suffix(".tmp")
        temporal.write_text(json.dumps({"creado": time.time(), "apps": apps}, ensure_ascii=False), encoding="utf-8")
        temporal.replace(_RUTA_CACHE)
    except OSError:
        pass


def refrescar_indice() -> list[dict]:
    """Reconstruye el índice y lo guarda; seguro para llamarse desde un hilo."""
    global _indice, _refrescando
    try:
        apps = construir_indice()
        if apps:
            with _bloqueo:
                _indice = apps
            _guardar_cache(apps)
        return apps
    finally:
        _refrescando = False


def precalentar_indice() -> None:
    """Arranca la construcción del índice sin bloquear a quien llama."""
    global _refrescando
    if _refrescando or not ES_WINDOWS:
        return
    _refrescando = True
    threading.Thread(target=refrescar_indice, name="bark-indice-apps", daemon=True).start()


def obtener_indice() -> list[dict]:
    """Índice en memoria → caché en disco → construcción síncrona (solo la primera vez)."""
    global _indice
    if _indice is not None:
        return _indice
    apps, creado = _leer_cache()
    if apps:
        with _bloqueo:
            _indice = apps
        if time.time() - creado > _VIGENCIA_CACHE:
            precalentar_indice()  # sirve lo viejo ya y actualiza detrás
        return apps
    return refrescar_indice()


def nombres_indice() -> set[str]:
    return {e["nombre"] for e in obtener_indice()}


# ============================================================
# Coincidencia difusa
# ============================================================

def _afinidad_tokens(q: frozenset[str], e: frozenset[str]) -> float:
    if not q or not e:
        return 0.0
    total = 0.0
    for t in q:
        mejor = 0.0
        for u in e:
            if t == u:
                r = 1.0
            elif len(t) >= 3 and len(u) >= 3 and (u.startswith(t) or t.startswith(u)):
                r = 0.92
            else:
                r = difflib.SequenceMatcher(None, t, u).ratio()
            mejor = max(mejor, r)
        if mejor < 0.78:
            return 0.0
        total += mejor
    cobertura = total / len(q)
    # Años y versiones («2025», «64») no cuentan como palabras sobrantes del nombre.
    sobrantes = max(0, len([t for t in e if not t.isdigit()]) - len(q))
    return max(0.0, cobertura * 0.97 - 0.035 * sobrantes)


def puntuar(consulta: str, nombre: str) -> float:
    cn, nn = normalizar(consulta), normalizar(nombre)
    if not cn or not nn:
        return 0.0
    if cn == nn:
        return 1.0
    q, e = tokens(consulta), tokens(nombre)
    if q and q == e:
        return 0.98
    por_tokens = _afinidad_tokens(q, e)
    # Texto contiguo: «visual studio» dentro de «visual studio code».
    contiguo = 0.0
    if len(cn) >= 4 and (cn in nn or nn in cn):
        contiguo = 0.70 + 0.2 * min(len(cn), len(nn)) / max(len(cn), len(nn))
    similitud = difflib.SequenceMatcher(None, cn, nn).ratio() * 0.88
    return max(por_tokens, contiguo, similitud)


def buscar(consulta: str, limite: int = 5, indice: list[dict] | None = None) -> list[tuple[float, dict]]:
    """Mejores candidatos ordenados por puntaje (sin duplicados de destino)."""
    candidatos = []
    destinos = set()
    for entrada in (indice if indice is not None else obtener_indice()):
        p = puntuar(consulta, entrada["nombre"])
        if p >= _SUGERIR:
            candidatos.append((p, entrada))
    candidatos.sort(key=lambda par: (-par[0], len(par[1]["nombre"])))
    resultado = []
    for p, e in candidatos:
        if e["destino"].lower() in destinos:
            continue
        destinos.add(e["destino"].lower())
        resultado.append((p, e))
        if len(resultado) >= limite:
            break
    return resultado


# ============================================================
# Lanzamiento
# ============================================================

@dataclass
class Resultado:
    abierta: bool
    mensaje: str
    nombre: str = ""
    ambigua: bool = False
    sugerencias: list[str] = field(default_factory=list)


def _lanzar(tipo: str, destino: str) -> None:
    if tipo == "aumid":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{destino}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif tipo == "exe":
        ruta = shutil.which(destino) or shutil.which(destino + ".exe") or destino
        try:
            os.startfile(ruta)  # type: ignore[attr-defined]  # ShellExecute resuelve App Paths
        except OSError:
            subprocess.Popen([ruta])
    else:  # protocolo | archivo
        os.startfile(destino)  # type: ignore[attr-defined]


def abrir_inteligente(consulta: str) -> Resultado:
    """Resuelve «consulta» (nombre hablado) y abre la mejor app. No lanza excepciones."""
    consulta = consulta.strip().strip('"')
    if not consulta:
        return Resultado(False, "No me dijiste qué aplicación abrir.")
    if not ES_WINDOWS:
        return Resultado(False, "La apertura inteligente de apps solo está disponible en Windows.")

    # 1) Catálogo: sinónimos y apps de la Tienda con destino conocido.
    entrada = buscar_en_catalogo(consulta)
    if entrada:
        tipo, destino = entrada["destino"]
        try:
            _lanzar(tipo, destino)
            return Resultado(True, f"Abrí {entrada['nombre']}.", entrada["nombre"])
        except OSError:
            pass  # p. ej. Discord sin exe en PATH: el índice puede tener su acceso real.

    # 2) Índice local con coincidencia difusa.
    candidatos = buscar(consulta)
    if candidatos:
        mejor_p, mejor = candidatos[0]
        segundo_p = candidatos[1][0] if len(candidatos) > 1 else 0.0
        if mejor_p >= _ABRIR and (mejor_p - segundo_p >= _VENTAJA or mejor_p >= 0.97):
            try:
                _lanzar(mejor["tipo"], mejor["destino"])
                return Resultado(True, f"Abrí {mejor['nombre']}.", mejor["nombre"])
            except OSError as error:
                return Resultado(False, f"Encontré '{mejor['nombre']}' pero no pude abrirla: {error}", mejor["nombre"])
        if mejor_p >= _ABRIR:
            nombres = [e["nombre"] for p, e in candidatos[:4] if mejor_p - p <= 0.15]
            lista = "\n".join(f"- {n}" for n in nombres)
            return Resultado(False, f"Hay varias coincidencias para '{consulta}'. ¿Cuál quieres?\n{lista}",
                             ambigua=True, sugerencias=nombres)
        return Resultado(False, "", sugerencias=[e["nombre"] for _, e in candidatos[:3]])
    return Resultado(False, "")


def sugerir(consulta: str, limite: int = 3) -> list[str]:
    return [e["nombre"] for _, e in buscar(consulta, limite)]
