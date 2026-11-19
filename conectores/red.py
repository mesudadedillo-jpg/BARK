"""Red e internet: estado de conexión, IP, ping, Wi-Fi y Bluetooth."""

from __future__ import annotations

import re
import socket

from conectores import similitud
from conectores._util import ES_WINDOWS, SOLO_WINDOWS, ejecutar, http_json, powershell_json, recortar
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("red", "Red e internet", "Conexión, IP local y pública, ping, redes Wi-Fi y dispositivos Bluetooth.")


def _netsh(*argumentos: str) -> str:
    # chcp 65001 hace que netsh escriba en UTF-8 y no se rompan acentos («Señal»).
    _codigo, salida = ejecutar(["cmd", "/c", "chcp 65001 >nul & netsh " + " ".join(argumentos)], timeout=20)
    return salida


def _ip_local() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # UDP no envía nada: solo elige la interfaz de salida
            return s.getsockname()[0]
        except OSError:
            return "sin red"


def hay_internet(timeout: float = 2.0) -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=timeout).close()
        return True
    except OSError:
        return False


def _ssid_actual() -> str:
    if not ES_WINDOWS:
        return ""
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", _netsh("wlan", "show", "interfaces"), flags=re.M)
    return m.group(1).strip() if m else ""


@R.herramienta("red", "Estado de la conexión: internet, Wi-Fi actual e IP local",
               alias=("estado_internet", "conexion"), palabras=("internet", "conexion", "conectado", "red", "wifi", "ip"))
def estado_red() -> str:
    partes = [f"Internet: {'conectado' if hay_internet() else 'SIN conexión'}", f"IP local: {_ip_local()}"]
    ssid = _ssid_actual()
    if ssid:
        partes.append(f"Wi-Fi: {ssid}")
    partes.append(f"Equipo: {socket.gethostname()}")
    return "  ·  ".join(partes)


@R.herramienta("red", "IP pública y ubicación aproximada de tu conexión", alias=("mi_ip", "ip"),
               palabras=("ip", "publica", "direccion"))
def ip_publica() -> str:
    try:
        datos = http_json("https://ipapi.co/json/")
        return f"IP pública: {datos.get('ip')} · {datos.get('city')}, {datos.get('region')} ({datos.get('org', '')})"
    except Exception:
        try:
            return f"IP pública: {http_json('https://api.ipify.org?format=json')['ip']}"
        except Exception as error:
            return f"No pude consultar la IP pública: {error}"


@R.herramienta("red", "Mide la latencia hacia un sitio o IP", alias=("ping",), palabras=("ping", "latencia", "lag", "responde"))
def hacer_ping(host: str = "google.com", veces: int = 4) -> str:
    host = re.sub(r"^https?://", "", host.strip()).split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        return "Ese host no es válido."
    veces = max(1, min(int(veces), 10))
    if ES_WINDOWS:
        _c, salida = ejecutar(["cmd", "/c", f"chcp 65001 >nul & ping -n {veces} {host}"], timeout=30)
    else:
        _c, salida = ejecutar(["ping", "-c", str(veces), host], timeout=30)
    lineas = [l for l in salida.splitlines() if l.strip()]
    return "\n".join(lineas[-3:]) or "No obtuve respuesta."


def _perfiles_wifi() -> list[str]:
    texto = _netsh("wlan", "show", "profiles")
    return [p.strip() for p in re.findall(
        r"(?:All User Profile|Perfil de todos los usuarios|Current User Profile|Perfil de usuario actual)\s*:\s*(.+)", texto)]


@R.herramienta("red", "Redes Wi-Fi disponibles con su señal", alias=("redes_wifi", "wifi_disponibles"),
               palabras=("wifi", "redes", "disponibles", "senal", "inalambrica"))
def redes_wifi() -> str:
    if not ES_WINDOWS:
        return SOLO_WINDOWS
    texto = _netsh("wlan", "show", "networks", "mode=bssid")
    redes: dict[str, str] = {}
    actual = None
    for linea in texto.splitlines():
        ssid = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", linea.strip())
        if ssid:
            actual = ssid.group(1).strip() or "(oculta)"
            redes.setdefault(actual, "")
            continue
        senal = re.search(r"(?:Signal|Señal|Se.al)\s*:\s*(\d+%)", linea)
        if senal and actual and not redes[actual]:
            redes[actual] = senal.group(1)
    if not redes:
        return "No veo redes Wi-Fi (¿está apagado el Wi-Fi?)."
    guardadas = set(_perfiles_wifi())
    orden = sorted(redes.items(), key=lambda par: -int(par[1].rstrip("%") or 0))
    return "\n".join(f"{n} {s}{' (guardada)' if n in guardadas else ''}" for n, s in orden)


@R.herramienta("red", "Se conecta a una red Wi-Fi guardada; acepta el nombre aproximado",
               alias=("conectar_red",), palabras=("conecta", "wifi", "red"))
def conectar_wifi(nombre: str) -> str:
    if not ES_WINDOWS:
        return SOLO_WINDOWS
    perfiles = _perfiles_wifi()
    elegido, sugerencias = similitud.elegir(nombre, perfiles)
    if not elegido:
        if not perfiles:
            return "No hay redes Wi-Fi guardadas; conéctate una vez desde Windows para que pueda usarla."
        return similitud.texto_sugerencias(nombre, sugerencias or perfiles[:4], "una red guardada")
    # Sin «cmd /c»: así subprocess entrecomilla bien los nombres con espacios.
    _codigo, salida = ejecutar(["netsh", "wlan", "connect", f"name={elegido}"], timeout=20)
    return f"Conectando a {elegido}… {recortar(salida, 200)}"


@R.herramienta("red", "Dispositivos Bluetooth emparejados", alias=("bluetooth",), palabras=("bluetooth", "audifonos", "emparejados"))
def dispositivos_bluetooth() -> str:
    if not ES_WINDOWS:
        return SOLO_WINDOWS
    filas = powershell_json("Get-PnpDevice -Class Bluetooth | Select-Object FriendlyName,Status")
    nombres = sorted({f"{d['FriendlyName']} ({'activo' if d.get('Status') == 'OK' else d.get('Status')})"
                      for d in filas if d.get("FriendlyName") and "Enumerator" not in d["FriendlyName"]})
    return "\n".join(nombres) or "No encontré dispositivos Bluetooth."
