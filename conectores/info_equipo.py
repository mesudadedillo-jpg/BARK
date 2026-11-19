"""Información del equipo: hardware, uso de recursos, procesos y servicios/dispositivos de Windows."""

from __future__ import annotations

import os
import platform
from datetime import timedelta

from conectores import similitud
from conectores._util import ES_WINDOWS, SOLO_WINDOWS, comillas_ps, powershell, powershell_json
from conectores.registro import REGISTRO, solo_windows

R = REGISTRO
R.conector("info_equipo", "Información del equipo", "Procesador, RAM, gráfica, uso de CPU, procesos y tiempo encendido.")
R.conector("dispositivos", "Servicios y dispositivos", "Servicios de Windows, impresoras, audio y memorias USB.", requisito=solo_windows)


@R.herramienta("info_equipo", "Especificaciones: sistema, procesador, RAM, gráfica y tiempo encendido",
               alias=("especificaciones", "specs", "info_pc"),
               palabras=("especificaciones", "specs", "procesador", "cpu", "grafica", "gpu", "ram", "componentes", "windows", "version"))
def info_sistema() -> str:
    lineas = [f"Sistema: {platform.system()} {platform.release()} ({platform.version()})",
              f"Equipo: {platform.node()}  ·  Arquitectura: {platform.machine()}"]
    if ES_WINDOWS:
        cpu = powershell_json("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors")
        for c in cpu:
            lineas.append(f"Procesador: {c.get('Name', '').strip()} ({c.get('NumberOfCores')} núcleos / {c.get('NumberOfLogicalProcessors')} hilos)")
        ram = powershell_json("Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,Manufacturer,Model")
        for r in ram:
            lineas.append(f"RAM: {int(r.get('TotalPhysicalMemory') or 0) / 1024**3:.1f} GB  ·  Modelo: {r.get('Manufacturer', '')} {r.get('Model', '')}")
        for g in powershell_json("Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM"):
            vram = int(g.get("AdapterRAM") or 0) / 1024**3
            lineas.append(f"Gráfica: {g.get('Name')}" + (f" ({vram:.0f} GB)" if vram >= 1 else ""))
    else:
        lineas.append(f"Procesador: {platform.processor() or 'desconocido'} ({os.cpu_count()} hilos)")
    lineas.append(f"Encendido desde hace: {tiempo_encendido()}")
    return "\n".join(lineas)


@R.herramienta("info_equipo", "Cuánto tiempo lleva encendido el equipo", palabras=("encendido", "uptime", "prendida"))
def tiempo_encendido() -> str:
    if ES_WINDOWS:
        import ctypes
        segundos = ctypes.windll.kernel32.GetTickCount64() / 1000  # type: ignore[attr-defined]
    else:
        try:
            with open("/proc/uptime", encoding="utf-8") as f:
                segundos = float(f.read().split()[0])
        except OSError:
            return "desconocido"
    t = timedelta(seconds=int(segundos))
    horas, resto = divmod(t.seconds, 3600)
    return (f"{t.days} días, " if t.days else "") + f"{horas} h {resto // 60} min"


@R.herramienta("info_equipo", "Uso actual de CPU, RAM y disco del sistema", alias=("uso_cpu", "rendimiento_actual"),
               palabras=("uso", "cpu", "procesador", "ram", "porcentaje", "carga"))
def uso_recursos() -> str:
    if not ES_WINDOWS:
        carga = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
        return f"Carga promedio: {carga:.2f} sobre {os.cpu_count()} hilos."
    datos = powershell_json(
        "$c=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
        "$o=Get-CimInstance Win32_OperatingSystem; "
        "[pscustomobject]@{cpu=$c; total=$o.TotalVisibleMemorySize; libre=$o.FreePhysicalMemory}"
    )
    if not datos:
        return "No pude leer el uso de recursos."
    d = datos[0]
    total, libre = float(d.get("total") or 1), float(d.get("libre") or 0)
    return (f"CPU: {float(d.get('cpu') or 0):.0f}%  ·  RAM: {(total - libre) / total * 100:.0f}% usada "
            f"({(total - libre) / 1024**2:.1f} de {total / 1024**2:.1f} GB)")


@R.herramienta("info_equipo", "Procesos que más consumen; orden = memoria o cpu", alias=("procesos", "administrador_tareas"),
               palabras=("procesos", "consume", "abiertos", "tareas", "administrador"))
def listar_procesos(orden: str = "memoria", limite: int = 10) -> str:
    limite = max(1, min(int(limite), 30))
    if not ES_WINDOWS:
        return SOLO_WINDOWS
    propiedad = "CPU" if "cpu" in orden.lower() or "proces" in orden.lower() else "WorkingSet64"
    filas = powershell_json(
        f"Get-Process | Sort-Object {propiedad} -Descending | Select-Object -First {limite} Name,Id,"
        "@{n='MB';e={[math]::Round($_.WorkingSet64/1MB)}},@{n='CPU';e={[math]::Round($_.CPU,1)}}"
    )
    if not filas:
        return "No pude leer los procesos."
    return "\n".join(f"{p['Name']} (PID {p['Id']}): {p['MB']} MB, {p.get('CPU') or 0} s de CPU" for p in filas)


# ============================================================
# Servicios y dispositivos (Windows)
# ============================================================

def _servicios() -> list[dict]:
    return powershell_json("Get-Service | Select-Object Name,DisplayName,@{n='Estado';e={$_.Status.ToString()}}", timeout=40)


@R.herramienta("dispositivos", "Lista servicios de Windows; filtro por nombre opcional", alias=("servicios",),
               palabras=("servicios", "servicio"))
def listar_servicios(filtro: str = "", solo_activos: bool = False) -> str:
    servicios = _servicios()
    if solo_activos:
        servicios = [s for s in servicios if s.get("Estado") == "Running"]
    if filtro:
        servicios = [s for _, s in similitud.mejores(filtro, servicios, clave=lambda s: (s["Name"], s["DisplayName"] or ""), limite=15, umbral=0.5)]
    if not servicios:
        return "No encontré servicios con ese criterio."
    return "\n".join(f"{s['DisplayName']} ({s['Name']}): {s['Estado']}" for s in servicios[:40])


@R.herramienta("dispositivos", "Inicia, detiene o reinicia un servicio de Windows (puede requerir administrador)",
               palabras=("servicio", "reinicia", "detén", "inicia"), sensible=True)
def controlar_servicio(nombre: str, accion: str = "reiniciar") -> str:
    elegido, sugerencias = similitud.elegir(nombre, _servicios(), clave=lambda s: (s["Name"], s["DisplayName"] or ""))
    if not elegido:
        return similitud.texto_sugerencias(nombre, [s["DisplayName"] for s in sugerencias], "un servicio")
    comando = {"inicia": "Start-Service", "detie": "Stop-Service", "deten": "Stop-Service", "para": "Stop-Service"}
    cmdlet = next((v for k, v in comando.items() if accion.lower().startswith(k)), "Restart-Service")
    codigo, salida = powershell(f"{cmdlet} -Name {comillas_ps(elegido['Name'])} -ErrorAction Stop")
    if codigo != 0:
        return f"No pude con {elegido['DisplayName']}: {salida[:300]} (quizá necesite ejecutar Bark como administrador)."
    return f"Listo: {cmdlet} en {elegido['DisplayName']}."


@R.herramienta("dispositivos", "Impresoras instaladas y cuál es la predeterminada", alias=("impresoras",), palabras=("impresora", "imprimir"))
def listar_impresoras() -> str:
    filas = powershell_json("Get-CimInstance Win32_Printer | Select-Object Name,Default,WorkOffline")
    if not filas:
        return "No encontré impresoras."
    return "\n".join(f"{'★ ' if p.get('Default') else ''}{p['Name']}{' (sin conexión)' if p.get('WorkOffline') else ''}" for p in filas)


@R.herramienta("dispositivos", "Imprime un archivo en la impresora predeterminada", alias=("imprimir",),
               palabras=("imprime", "imprimir", "impresora"), sensible=True, rutas=("ruta",))
def imprimir_archivo(ruta: str) -> str:
    codigo, salida = powershell(f"Start-Process -FilePath {comillas_ps(ruta)} -Verb Print")
    return f"Mandé a imprimir {os.path.basename(ruta)}." if codigo == 0 else f"No pude imprimir: {salida[:300]}"


@R.herramienta("dispositivos", "Dispositivos de audio, cámaras y USB conectados", alias=("dispositivos_conectados", "usb"),
               palabras=("dispositivos", "usb", "audio", "bocinas", "audifonos", "microfono", "camara", "conectados"))
def listar_dispositivos(tipo: str = "") -> str:
    clases = {"audio": "AudioEndpoint", "camara": "Camera", "usb": "USB", "teclado": "Keyboard", "mouse": "Mouse"}
    clase = next((v for k, v in clases.items() if k in similitud.normalizar(tipo)), "")
    filtro = f"-Class {clase} " if clase else ""
    filas = powershell_json(
        f"Get-PnpDevice {filtro}-PresentOnly | Where-Object {{ $_.Class -in 'AudioEndpoint','Camera','USB','Keyboard','Mouse','Image','Bluetooth' }} "
        "| Select-Object FriendlyName,Class,Status", timeout=40,
    )
    if not filas:
        return "No encontré dispositivos de ese tipo."
    vistos = dict.fromkeys(f"[{d.get('Class')}] {d.get('FriendlyName')}" for d in filas if d.get("FriendlyName"))
    return "\n".join(list(vistos)[:40])

