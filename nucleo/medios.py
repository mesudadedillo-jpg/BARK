"""Música y video que suenan en Windows: qué es, cuánto lleva y cómo controlarlo.

Lee los «controles multimedia del sistema» de Windows (los mismos que muestra el
panel de volumen): Spotify, YouTube en el navegador, Reproductor multimedia, etc.
Sin dependencias nuevas: un PowerShell oculto y persistente consulta la API de
WinRT y entrega una línea JSON por segundo. Los controles usan teclas multimedia.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$tipo) { $t = $asTask.MakeGenericMethod($tipo).Invoke($null, @($op)); $null = $t.Wait(4000); $t.Result }
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType=WindowsRuntime]
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
if (-not $mgr) { [Console]::Out.WriteLine('{"error":"sin_api"}'); exit }
$portada = '__PORTADA__'
$ultima = ''
while ($true) {
  $o = @{ titulo = $null }
  $s = $mgr.GetCurrentSession()
  if ($s) {
    $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $info = $s.GetPlaybackInfo(); $tl = $s.GetTimelineProperties()
    $clave = "$($p.Title)|$($p.Artist)"
    $o = @{ titulo = $p.Title; artista = $p.Artist; album = $p.AlbumTitle; app = $s.SourceAppUserModelId;
            estado = [string]$info.PlaybackStatus; posicion = $tl.Position.TotalSeconds; duracion = $tl.EndTime.TotalSeconds;
            actualizado = $tl.LastUpdatedTime.ToUnixTimeMilliseconds(); portada = $null }
    if ($clave -ne $ultima) {
      $ultima = $clave
      Remove-Item $portada -ErrorAction SilentlyContinue
      if ($p.Thumbnail) {
        $flujo = Await ($p.Thumbnail.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
        if ($flujo) {
          # PowerShell 5.1 no expone los métodos del flujo WinRT: se convierte por reflexión.
          $net = [System.IO.WindowsRuntimeStreamExtensions].GetMethod('AsStreamForRead', [Type[]]@([Windows.Storage.Streams.IInputStream])).Invoke($null, @($flujo))
          $memoria = New-Object IO.MemoryStream
          $net.CopyTo($memoria); $net.Dispose()
          if ($memoria.Length -gt 0) { [IO.File]::WriteAllBytes($portada, $memoria.ToArray()) }
        }
      }
    }
    if (Test-Path $portada) { $o.portada = $portada }
  } else { $ultima = '' }
  [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
  Start-Sleep -Milliseconds 1000
}
"""

_VK = {"reproducir": 0xB3, "siguiente": 0xB0, "anterior": 0xB1, "detener": 0xB2,
       "subir": 0xAF, "bajar": 0xAE, "silenciar": 0xAD}

_APPS = {
    "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "308046b0af4a39cb": "Firefox",
    "opera": "Opera", "brave": "Brave", "zune": "Reproductor", "mediaplayer": "Reproductor", "vlc": "VLC",
    "applemusic": "Apple Music", "itunes": "iTunes", "deezer": "Deezer", "tidal": "Tidal", "amazonmusic": "Amazon Music",
}


def nombre_app(app_id: str) -> str:
    """«Spotify.exe» / «Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic» → nombre legible."""
    bajo = (app_id or "").lower()
    for clave, nombre in _APPS.items():
        if clave in bajo:
            return nombre
    base = os.path.basename(app_id or "").split("!")[0].split("_")[0]
    return base.removesuffix(".exe").split(".")[-1].capitalize() or "Multimedia"


def interpretar(linea: str) -> dict | None:
    """Una línea JSON del monitor → estado normalizado, o None si no hay nada sonando."""
    try:
        datos = json.loads(linea)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(datos, dict) or not datos.get("titulo"):
        return None
    return {
        "titulo": str(datos.get("titulo") or ""), "artista": str(datos.get("artista") or ""),
        "album": str(datos.get("album") or ""), "app": nombre_app(str(datos.get("app") or "")),
        "app_id": str(datos.get("app") or ""), "reproduciendo": str(datos.get("estado")) == "Playing",
        "posicion": float(datos.get("posicion") or 0.0), "duracion": float(datos.get("duracion") or 0.0),
        "actualizado": float(datos.get("actualizado") or 0.0) / 1000.0, "portada": datos.get("portada"),
    }


def posicion_actual(estado: dict) -> float:
    """Windows solo actualiza la posición cada pocos segundos: se extrapola si está sonando."""
    posicion = estado.get("posicion", 0.0)
    if estado.get("reproduciendo") and estado.get("actualizado"):
        posicion += max(0.0, time.time() - estado["actualizado"])
    duracion = estado.get("duracion") or 0.0
    return min(posicion, duracion) if duracion else posicion


class MonitorMedios:
    """Mantiene ``estado`` al día con lo que suena en Windows (None = nada)."""

    def __init__(self):
        self.estado: dict | None = None
        self.disponible = os.name == "nt"
        self._proceso = None
        self._activo = False
        self._portada = os.path.join(tempfile.gettempdir(), f"bark_portada_{os.getpid()}.img")
        self._script = os.path.join(tempfile.gettempdir(), f"bark_medios_{os.getpid()}.ps1")

    def iniciar(self) -> None:
        if not self.disponible or self._activo:
            return
        self._activo = True
        threading.Thread(target=self._bucle, name="bark-medios", daemon=True).start()

    def _bucle(self) -> None:
        reintentos = 0
        while self._activo and reintentos < 5:
            try:
                # Windows PowerShell 5.1 (no pwsh): es el que trae la proyección de WinRT.
                with open(self._script, "w", encoding="utf-8-sig") as archivo:
                    archivo.write(_SCRIPT.replace("__PORTADA__", self._portada))
                self._proceso = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", self._script],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, encoding="utf-8", errors="replace",
                )
                for linea in self._proceso.stdout:
                    if not self._activo:
                        break
                    if '"error"' in linea:
                        self.disponible = False
                        self._activo = False
                        break
                    self.estado = interpretar(linea)
                    reintentos = 0
            except OSError:
                self.disponible = False
                break
            reintentos += 1
            time.sleep(2)

    def detener(self) -> None:
        self._activo = False
        if self._proceso and self._proceso.poll() is None:
            try:
                self._proceso.kill()
            except OSError:
                pass
        for ruta in (self._portada, self._script):
            try:
                os.remove(ruta)
            except OSError:
                pass


def tecla(accion: str) -> None:
    """reproducir | siguiente | anterior | detener | subir | bajar | silenciar (teclas multimedia)."""
    if os.name != "nt" or accion not in _VK:
        return
    usuario = ctypes.windll.user32
    usuario.keybd_event(_VK[accion], 0, 0, 0)
    usuario.keybd_event(_VK[accion], 0, 2, 0)


# ============================================================
# Agrandar o achicar la ventana del reproductor
# ============================================================

def _ventanas_visibles() -> list[tuple[int, str, str]]:
    """(hwnd, título, proceso) de las ventanas principales visibles."""
    usuario, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    resultado = []

    def proceso_de(hwnd) -> str:
        pid = wintypes.DWORD()
        usuario.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        manejador = kernel.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not manejador:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(520)
            tamano = wintypes.DWORD(520)
            if kernel.QueryFullProcessImageNameW(manejador, 0, buffer, ctypes.byref(tamano)):
                return os.path.basename(buffer.value).lower()
            return ""
        finally:
            kernel.CloseHandle(manejador)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visitar(hwnd, _):
        if usuario.IsWindowVisible(hwnd) and not usuario.GetWindow(hwnd, 4):  # GW_OWNER: solo ventanas principales
            largo = usuario.GetWindowTextLengthW(hwnd)
            if largo:
                titulo = ctypes.create_unicode_buffer(largo + 1)
                usuario.GetWindowTextW(hwnd, titulo, largo + 1)
                resultado.append((hwnd, titulo.value, proceso_de(hwnd)))
        return True

    usuario.EnumWindows(visitar, 0)
    return resultado


def ventana_reproductor(estado: dict | None) -> int | None:
    """Encuentra la ventana que reproduce: por título (pestañas de YouTube) o por programa."""
    if os.name != "nt" or not estado:
        return None
    titulo = (estado.get("titulo") or "").lower()
    app_id = (estado.get("app_id") or "").lower()
    ventanas = _ventanas_visibles()
    for hwnd, texto, _ in ventanas:
        if titulo and titulo[:40] in texto.lower():
            return hwnd
    for hwnd, _, proceso in ventanas:
        nombre = proceso.removesuffix(".exe")
        if nombre and (nombre in app_id or ("firefox" in nombre and "308046b0af4a39cb" in app_id)):
            return hwnd
    return None


def ajustar_ventana_reproductor(estado: dict | None, accion: str) -> str:
    """accion = agrandar | achicar | minimizar: actúa sobre la ventana del video o la canción."""
    hwnd = ventana_reproductor(estado)
    if not hwnd:
        return "No encontré la ventana del reproductor."
    usuario = ctypes.windll.user32
    comando = {"agrandar": 3, "achicar": 9, "minimizar": 6}.get(accion, 9)  # SW_MAXIMIZE / SW_RESTORE / SW_MINIMIZE
    usuario.ShowWindow(hwnd, comando)
    if accion != "minimizar":
        usuario.SetForegroundWindow(hwnd)
    return {"agrandar": "Reproductor maximizado.", "achicar": "Reproductor restaurado.", "minimizar": "Reproductor minimizado."}[accion]
