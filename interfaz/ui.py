"""Interfaz HUD futurista de Bark, construida únicamente con Tkinter.

Un visor sin barra de título del sistema: reloj, clima, telemetría, lo que suena
en Windows y un orbe central que late como una bocina cuando te escucha y cuando
habla. El modo Live deja solo el orbe y la voz, como el modo de voz de Gemini o ChatGPT.
"""

import ctypes
import math
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import colorchooser, messagebox, scrolledtext

from acciones import comandos
from config import config
from interfaz.hud import ESTADOS, FUENTE_HUD, FUENTE_TEXTO, HUD, BarraHUD, OrbeVivo, PanelHUD, mezclar, texto_seguro
from nucleo.bd_manager import biblioteca_conversaciones, buscar_recuerdos, estadisticas_biblioteca
from nucleo.cerebro import configurar_solicitador_permiso, procesar_mensaje
from nucleo.escuchar import (
    VozNoDisponible, candidatos_microfono, configurar_voz_gemini, configurar_voz_nube, dispositivos_entrada, ejecutar_modo_voz,
    hablar, modelos_whisper_disponibles, seleccionar_dispositivo_entrada,
    seleccionar_modelo_whisper, seleccionar_motor_voz, seleccionar_voz_local,
    solicitar_permiso_por_voz, voces_locales_disponibles,
)
from nucleo import audio_vivo, descargas, gemini_live, medios, permisos
from nucleo.bd_manager import guardar_turno
from nucleo.proveedores import estado_proveedores
from nucleo.telemetria import Telemetria
from nucleo.vision import VisionLocal, VisionNoDisponible

AUTOMATICO_MIC = "Automático (recomendado)"
_ES_WINDOWS = sys.platform == "win32"

# Las ventanas secundarias (biblioteca, conectores…) usan estas claves; siguen la
# paleta del HUD para que todo Bark se vea como una sola pieza.
COLORES = {
    "fondo": HUD["fondo"], "panel": HUD["panel"], "panel2": HUD["panel2"],
    "panel_claro": "#0d2c3d", "borde": HUD["borde"], "texto": HUD["texto"],
    "suave": HUD["suave"], "acento": HUD["acento"], "acento2": HUD["acento2"],
    "usuario": "#0a2838", "bark": "#071e2b", "alerta": HUD["ambar"],
    "boton": "#0a2b3b", "boton_activo": "#0f6f80", "boton_texto": "#dffcff",
    "titulo": HUD["fondo"], "exito": HUD["verde"],
}

_DIAS = ("LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO")
_MESES = ("ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC")
_MOTORES = (("auto", "AUTO"), ("gemini", "GEMINI"), ("openai", "OPENAI"), ("local", "LOCAL"))


def _colorref(color: str) -> int:
    """#rrggbb → COLORREF de Windows (0x00bbggrr)."""
    return int(color[5:7] + color[3:5] + color[1:3], 16)


def _hwnd(ventana: tk.Misc) -> int:
    ventana.update_idletasks()
    return ctypes.windll.user32.GetParent(ventana.winfo_id()) or ventana.winfo_id()


def mimetizar_barra(ventana: tk.Misc) -> None:
    """Pinta la barra de título de Windows con el fondo del HUD (Windows 11) o en oscuro (Windows 10)."""
    if not _ES_WINDOWS:
        return
    try:
        hwnd = _hwnd(ventana)
        for atributo, valor in ((20, 1), (35, _colorref(HUD["fondo"])), (34, _colorref(HUD["borde"])), (36, _colorref(HUD["linea"]))):
            dato = ctypes.c_int(valor)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, atributo, ctypes.byref(dato), ctypes.sizeof(dato))
    except Exception:
        pass


def _area_de_trabajo():
    """Rectángulo del escritorio sin la barra de tareas (para maximizar sin taparla)."""
    if not _ES_WINDOWS:
        return None
    from ctypes import wintypes
    rect = wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None


def _formato_tiempo(segundos: float) -> str:
    segundos = max(0, int(segundos))
    return f"{segundos // 3600}:{segundos % 3600 // 60:02d}:{segundos % 60:02d}" if segundos >= 3600 else f"{segundos // 60}:{segundos % 60:02d}"


class VentanaBark:
    def __init__(self):
        self.raiz = tk.Tk()
        self.raiz.title("Bark — Asistente personal")
        self.raiz.geometry("1360x820")
        self.raiz.minsize(1080, 680)
        self.raiz.configure(bg=HUD["fondo"])
        # Sin barra de título del sistema: el encabezado del HUD hace de barra
        # (arrastrar, maximizar, minimizar y cerrar) y Bark sigue en la barra de tareas.
        self.raiz.overrideredirect(True)
        self._ventana_maximizada = False
        self._geometria_anterior = None
        self._botones = []
        self.estado = "en espera"
        self._placeholder_activo = True
        self.conteo_mensajes = 0
        self.hilo_voz = None
        self.detener_voz = threading.Event()
        self.modo_live = False
        self._max_entrada = 0.3
        self._clima = None
        self._medio_clave = None
        self._portada_img = None
        self.telemetria = Telemetria()
        self.medios = medios.MonitorMedios()
        self.vision = VisionLocal(self._gesto_recibido, self._estado_vision)
        self.voz_seleccionada = tk.StringVar(value=os.path.basename(config.RUTA_MODELO_TTS))
        # La voz elegida en el panel (Gemini, OpenAI o local) se recuerda entre sesiones.
        motor_guardado = permisos.preferencia("motor_voz")
        if motor_guardado in {"auto", "gemini", "openai", "local"}:
            config.MODO_VOZ = motor_guardado
        self.motor_voz = tk.StringVar(value=config.MODO_VOZ if config.MODO_VOZ in {"auto", "gemini", "openai", "local"} else "auto")
        self.modelo_gemini = tk.StringVar(value=config.GEMINI_MODELO_TTS)
        self.voz_gemini = tk.StringVar(value=config.GEMINI_VOZ)
        self.modelo_tts = tk.StringVar(value=config.OPENAI_MODELO_TTS)
        self.voz_nube = tk.StringVar(value=config.OPENAI_VOZ)
        self.modelo_escucha = tk.StringVar(value=config.MODELO_WHISPER)
        self.leer_respuestas = tk.BooleanVar(value=True)
        self.lectura_activa = True
        self.escucha_libre = tk.BooleanVar(value=config.ESCUCHA_LIBRE)
        self.interrumpir_voz = tk.BooleanVar(value=config.INTERRUMPIR_CON_VOZ)
        live_guardado = permisos.preferencia("gemini_live")
        usar_live = live_guardado if isinstance(live_guardado, bool) else (config.USAR_GEMINI_LIVE not in {"0", "no", "false", "off"})
        self.usar_gemini_live = tk.BooleanVar(value=usar_live and gemini_live.disponible())
        self.modelo_live = tk.StringVar(value=config.GEMINI_MODELO_LIVE)
        self.sesion_live = None
        self._live_parado_por_usuario = False
        # «Automático» escucha en varios micrófonos y se queda con el que te oye.
        # Primero los micrófonos físicos (ordenados por probabilidad) y luego el resto.
        self.dispositivos_mic = {AUTOMATICO_MIC: None}
        try:
            fisicos = candidatos_microfono()
            for dispositivo in fisicos:
                self.dispositivos_mic[f"{dispositivo['indice']} · {dispositivo['nombre']}"] = dispositivo["indice"]
            vistos = {d["indice"] for d in fisicos}
            for dispositivo in dispositivos_entrada():
                if dispositivo["indice"] not in vistos:
                    self.dispositivos_mic[f"{dispositivo['indice']} · {dispositivo['nombre']} (otro)"] = dispositivo["indice"]
        except Exception:
            pass
        self.microfono = tk.StringVar(value=next((nombre for nombre, indice in self.dispositivos_mic.items() if indice == config.DISPOSITIVO_ENTRADA_VOZ), AUTOMATICO_MIC))
        configurar_solicitador_permiso(self._pedir_permiso)
        self._construir()
        self.raiz.after(20, self._integrar_con_windows)
        self.raiz.protocol("WM_DELETE_WINDOW", self._cerrar)
        self.medios.iniciar()
        self._animar()
        self._tic_reloj()
        self._tic_sistema()
        self._tic_medios()
        self._actualizar_clima()

    # ============================================================
    # Ventana sin marco integrada con Windows
    # ============================================================

    def _integrar_con_windows(self):
        """Sin marco, pero con icono en la barra de tareas, Alt+Tab, esquinas redondeadas y borde del HUD."""
        if not _ES_WINDOWS:
            return
        try:
            usuario = ctypes.windll.user32
            self._hwnd_principal = hwnd = _hwnd(self.raiz)
            extendido = usuario.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            usuario.SetWindowLongW(hwnd, -20, (extendido & ~0x00000080) | 0x00040000)  # sin TOOLWINDOW, con APPWINDOW
            estilo = usuario.GetWindowLongW(hwnd, -16)  # GWL_STYLE
            usuario.SetWindowLongW(hwnd, -16, estilo | 0x00020000 | 0x00080000)  # MINIMIZEBOX | SYSMENU (minimizar desde la barra)
            for atributo, valor in ((33, 2), (34, _colorref(HUD["borde"]))):  # esquinas redondeadas, borde del HUD
                dato = ctypes.c_int(valor)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, atributo, ctypes.byref(dato), ctypes.sizeof(dato))
            # Windows solo vuelve a leer los estilos al ocultar y mostrar la ventana.
            self.raiz.withdraw()
            self.raiz.after(10, self.raiz.deiconify)
        except Exception:
            self._hwnd_principal = None

    def _ventana_secundaria(self) -> tk.Toplevel:
        ventana = tk.Toplevel(self.raiz)
        ventana.configure(bg=COLORES["fondo"])
        self.raiz.after(30, lambda: mimetizar_barra(ventana))
        return ventana

    def _iniciar_arrastre(self, evento):
        if self._ventana_maximizada:
            return
        if _ES_WINDOWS and getattr(self, "_hwnd_principal", None):
            # Arrastre nativo de Windows: fluido y con el mismo comportamiento que una barra de título.
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(self._hwnd_principal, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
            return
        self._arrastre_x, self._arrastre_y = evento.x_root, evento.y_root

    def _arrastrar_ventana(self, evento):
        if self._ventana_maximizada or not hasattr(self, "_arrastre_x"):
            return
        x = self.raiz.winfo_x() + evento.x_root - self._arrastre_x
        y = self.raiz.winfo_y() + evento.y_root - self._arrastre_y
        self.raiz.geometry(f"+{x}+{y}")
        self._arrastre_x, self._arrastre_y = evento.x_root, evento.y_root

    def _minimizar(self):
        if _ES_WINDOWS and getattr(self, "_hwnd_principal", None):
            ctypes.windll.user32.ShowWindow(self._hwnd_principal, 6)  # SW_MINIMIZE: vuelve desde la barra de tareas
            return
        # Respaldo: Tk solo puede minimizar una ventana decorada.
        self.raiz.overrideredirect(False)
        self.raiz.iconify()
        self.raiz.bind("<Map>", self._restaurar_sin_marco, add="+")

    def _restaurar_sin_marco(self, _evento=None):
        self.raiz.overrideredirect(True)
        self.raiz.unbind("<Map>")

    def _alternar_maximizar(self):
        if self._ventana_maximizada:
            self.raiz.geometry(self._geometria_anterior)
        else:
            self._geometria_anterior = self.raiz.geometry()
            area = _area_de_trabajo()
            if area:
                x, y, ancho, alto = area
                self.raiz.geometry(f"{ancho}x{alto}+{x}+{y}")
            else:
                self.raiz.geometry(f"{self.raiz.winfo_screenwidth()}x{self.raiz.winfo_screenheight()}+0+0")
        self._ventana_maximizada = not self._ventana_maximizada
        self.raiz.after(50, self._dibujar_encabezado)

    def _iniciar_redimensionado(self, evento):
        self._tamano_inicial = (self.raiz.winfo_width(), self.raiz.winfo_height(), evento.x_root, evento.y_root)

    def _redimensionar(self, evento):
        ancho, alto, inicio_x, inicio_y = self._tamano_inicial
        nuevo_ancho = max(1080, ancho + evento.x_root - inicio_x)
        nuevo_alto = max(680, alto + evento.y_root - inicio_y)
        self.raiz.geometry(f"{nuevo_ancho}x{nuevo_alto}")

    def _cerrar(self):
        self.detener_voz.set()
        if self.sesion_live:
            self._live_parado_por_usuario = True
            self.sesion_live.detener()
        audio_vivo.interrumpir()
        self.medios.detener()
        if self.vision.activa:
            self.vision.detener()
        self.raiz.destroy()

    # ============================================================
    # Construcción del HUD
    # ============================================================

    def _construir(self):
        self._construir_encabezado()
        self._construir_pie()

        self.cuerpo = tk.Frame(self.raiz, bg=HUD["fondo"])
        self.cuerpo.pack(fill="both", expand=True, padx=14, pady=(2, 4))
        self.cuerpo.grid_columnconfigure(0, minsize=290, weight=0)
        self.cuerpo.grid_columnconfigure(1, weight=1)
        self.cuerpo.grid_columnconfigure(2, minsize=300, weight=0)
        self.cuerpo.grid_rowconfigure(0, weight=1)

        self.col_izq = tk.Frame(self.cuerpo, bg=HUD["fondo"], width=290)
        self.col_izq.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.centro = tk.Frame(self.cuerpo, bg=HUD["fondo"])
        self.centro.grid(row=0, column=1, sticky="nsew")
        self.col_der = tk.Frame(self.cuerpo, bg=HUD["fondo"], width=300)
        self.col_der.grid(row=0, column=2, sticky="nsew", padx=(10, 0))

        self._construir_reloj(self.col_izq)
        self._construir_clima(self.col_izq)
        self._construir_sistema(self.col_izq)
        self._construir_nucleo(self.col_izq)
        self._construir_medios(self.col_der)
        self._construir_panel_voz(self.col_der)
        self._construir_acciones(self.col_der)
        self._construir_centro(self.centro)
        self._construir_ajustes()

        self.raiz.bind("<Control-k>", lambda _evento: self._enfocar_campo())
        self.raiz.bind("<Escape>", self._tecla_escape)
        self.raiz.bind("<F11>", lambda _e: self._alternar_live())

    # ---------------------------------------------------------- encabezado
    def _construir_encabezado(self):
        """Hace de barra de título: se funde con el HUD y se arrastra desde cualquier punto libre."""
        self.encabezado = tk.Canvas(self.raiz, height=62, bg=HUD["fondo"], highlightthickness=0, bd=0)
        self.encabezado.pack(fill="x")
        self.encabezado.bind("<Configure>", lambda _e: self._dibujar_encabezado())
        self.encabezado.bind("<ButtonPress-1>", self._presionar_encabezado)
        self.encabezado.bind("<B1-Motion>", self._arrastrar_ventana)
        self.encabezado.bind("<Double-Button-1>", self._doble_clic_encabezado)
        acciones = {
            "b_live": self._alternar_live, "b_ajustes": self._alternar_ajustes,
            "b_min": self._minimizar, "b_max": self._alternar_maximizar, "b_cerrar": self._cerrar,
        }
        for etiqueta, orden in acciones.items():
            self.encabezado.tag_bind(etiqueta, "<ButtonRelease-1>", lambda _e, o=orden: o())
            self.encabezado.tag_bind(etiqueta, "<Enter>", lambda _e, t=etiqueta: self._resaltar_boton(t, True))
            self.encabezado.tag_bind(etiqueta, "<Leave>", lambda _e, t=etiqueta: self._resaltar_boton(t, False))

    def _es_boton_encabezado(self) -> bool:
        return any(t.startswith("b_") for t in self.encabezado.gettags("current"))

    def _presionar_encabezado(self, evento):
        if not self._es_boton_encabezado():
            self._iniciar_arrastre(evento)

    def _doble_clic_encabezado(self, _evento):
        if not self._es_boton_encabezado():
            self._alternar_maximizar()

    def _resaltar_boton(self, etiqueta, activo):
        color = (HUD["rosa"] if etiqueta == "b_cerrar" else HUD["acento"]) if activo else HUD["suave"]
        for item in self.encabezado.find_withtag(etiqueta):
            if self.encabezado.type(item) == "text":
                self.encabezado.itemconfig(item, fill=color)
            elif self.encabezado.type(item) == "polygon":
                self.encabezado.itemconfig(item, outline=color if activo else HUD["borde"])

    def _dibujar_encabezado(self):
        c = self.encabezado
        c.delete("all")
        w = c.winfo_width()
        if w < 300:
            return
        color_estado, texto_estado = self._estado_visual()
        # Marca: hexágono y nombre.
        cx, cy = 34, 31
        puntos = []
        for i in range(6):
            ang = math.radians(60 * i + 30)
            puntos += [cx + math.cos(ang) * 15, cy + math.sin(ang) * 15]
        c.create_polygon(puntos, outline=HUD["acento"], fill=HUD["panel"], width=2)
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=color_estado, outline="")
        c.create_text(60, 24, text="BARK", anchor="w", fill=HUD["acento"], font=(FUENTE_HUD, 20, "bold"))
        c.create_text(61, 45, text="SISTEMA DE INTELIGENCIA PERSONAL  ·  LOCAL FIRST", anchor="w",
                      fill=HUD["tenue"], font=(FUENTE_HUD, 7))
        # Líneas de visor que cruzan el encabezado.
        x0, x1 = 330, w - 470
        if x1 > x0 + 40:
            c.create_line(x0, 31, x0 + 30, 16, x1 - 30, 16, x1, 31, fill=HUD["linea_suave"])
            c.create_line(x0 + 40, 22, x0 + 140, 22, fill=HUD["linea"], width=2)
            for i in range(0, int(x1 - x0 - 80), 14):
                c.create_line(x0 + 40 + i, 46, x0 + 40 + i, 50 if i % 70 else 54, fill=HUD["borde"])
        # Píldora de estado en el centro.
        mx = w / 2
        c.create_polygon(mx - 92, 31, mx - 80, 19, mx + 80, 19, mx + 92, 31, mx + 80, 43, mx - 80, 43,
                         fill=HUD["panel"], outline=mezclar(color_estado, HUD["fondo"], 0.3))
        c.create_text(mx, 31, text=texto_estado, fill=color_estado, font=(FUENTE_HUD, 9, "bold"))
        # Botones: LIVE, AJUSTES y controles de ventana (sin cromo del sistema).
        x = w - 16
        for etiqueta, texto, ancho, fuente in (
            ("b_cerrar", "✕", 34, (FUENTE_TEXTO, 12)), ("b_max", "▢", 34, (FUENTE_TEXTO, 12)),
            ("b_min", "—", 34, (FUENTE_TEXTO, 11)),
        ):
            c.create_rectangle(x - ancho, 12, x, 50, outline="", fill=HUD["fondo"], tags=(etiqueta,))
            c.create_text(x - ancho / 2, 31, text=texto, fill=HUD["suave"], font=fuente, tags=(etiqueta,))
            x -= ancho + 2
        x -= 14
        for etiqueta, texto, ancho in (("b_ajustes", "⚙  AJUSTES", 110), ("b_live", "◉  MODO LIVE" if not self.modo_live else "◉  SALIR DE LIVE", 150)):
            c.create_polygon(x - ancho, 31, x - ancho + 10, 19, x, 19, x, 43, x - ancho + 10, 43,
                             fill=HUD["panel"], outline=HUD["borde"], tags=(etiqueta,))
            c.create_text(x - ancho / 2 + 4, 31, text=texto, fill=HUD["acento"] if etiqueta == "b_live" and self.modo_live else HUD["suave"],
                          font=(FUENTE_HUD, 9, "bold"), tags=(etiqueta,))
            x -= ancho + 10
        c.create_line(0, 61, w, 61, fill=HUD["borde"])
        c.create_line(0, 61, 220, 61, fill=HUD["linea"], width=2)
        c.create_line(w - 220, 61, w, 61, fill=HUD["linea"], width=2)

    def _estado_visual(self):
        color, etiqueta, _ = ESTADOS.get(self.estado, ESTADOS["en espera"])
        if self.estado == "en espera":
            return HUD["verde"], "●  SISTEMA LISTO"
        return color, f"◉  {etiqueta}"

    # ---------------------------------------------------------- pie
    def _construir_pie(self):
        self.pie = tk.Canvas(self.raiz, height=24, bg=HUD["fondo"], highlightthickness=0, bd=0)
        self.pie.pack(side="bottom", fill="x")
        self.pie.bind("<Configure>", lambda _e: self._dibujar_pie())
        # Esquina para redimensionar la ventana sin marco.
        self.redimensionador = tk.Label(self.raiz, text="◢", bg=HUD["fondo"], fg=HUD["linea"], cursor="size_nw_se")
        self.redimensionador.place(relx=1, rely=1, anchor="se", x=-4, y=-2)
        self.redimensionador.bind("<Button-1>", self._iniciar_redimensionado)
        self.redimensionador.bind("<B1-Motion>", self._redimensionar)

    def _dibujar_pie(self):
        c = self.pie
        c.delete("all")
        w = c.winfo_width()
        proveedor = estado_proveedores().splitlines()[0] if estado_proveedores() else ""
        c.create_line(0, 3, w, 3, fill=HUD["borde"])
        c.create_text(16, 14, anchor="w", text=texto_seguro(proveedor).upper()[:90], fill=HUD["tenue"], font=(FUENTE_HUD, 7))
        self._texto_pie = c.create_text(w - 30, 14, anchor="e", fill=HUD["tenue"], font=(FUENTE_HUD, 7),
                                        text=f"LOCAL · PRIVADO · {self.conteo_mensajes} MENSAJES  ·  ESC INTERRUMPE  ·  F11 LIVE")
        for i in range(12):
            c.create_rectangle(w / 2 - 60 + i * 10, 11, w / 2 - 54 + i * 10, 16, outline="",
                               fill=HUD["linea"] if i < 4 else HUD["borde"])

    # ---------------------------------------------------------- columna izquierda
    def _construir_reloj(self, padre):
        panel = PanelHUD(padre, "CRONO · LOCAL")
        panel.pack(fill="x", pady=(0, 10))
        c = self.lienzo_reloj = tk.Canvas(panel.cuerpo, height=104, bg=HUD["panel"], highlightthickness=0, bd=0)
        c.pack(fill="x", padx=6, pady=(0, 2))
        self.item_hora = c.create_text(4, 38, anchor="w", text="--:--", fill=HUD["texto"], font=(FUENTE_HUD, 40, "bold"))
        self.item_segundos = c.create_text(150, 48, anchor="w", text="00", fill=HUD["acento"], font=(FUENTE_HUD, 16, "bold"))
        self.item_fecha = c.create_text(6, 82, anchor="w", text="", fill=HUD["suave"], font=(FUENTE_HUD, 10, "bold"))
        self.barra_segundos = BarraHUD(panel.cuerpo, segmentos=30, alto=6, color=HUD["acento"])
        self.barra_segundos.pack(fill="x", padx=8, pady=(0, 8))

    def _construir_clima(self, padre):
        self.panel_clima = panel = PanelHUD(padre, "CLIMA · ATMÓSFERA", al_expandir=self._expandir_clima)
        panel.pack(fill="x", pady=(0, 10))
        fila = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        fila.pack(fill="x", padx=8, pady=(2, 0))
        self.clima_icono = tk.Label(fila, text="◌", bg=HUD["panel"], fg=HUD["acento"], font=(FUENTE_TEXTO, 30))
        self.clima_icono.pack(side="left")
        self.clima_temp = tk.Label(fila, text="--°", bg=HUD["panel"], fg=HUD["texto"], font=(FUENTE_HUD, 30, "bold"))
        self.clima_temp.pack(side="left", padx=(6, 8))
        columna = tk.Frame(fila, bg=HUD["panel"])
        columna.pack(side="left", fill="x", expand=True)
        self.clima_desc = tk.Label(columna, text="Consultando…", bg=HUD["panel"], fg=HUD["texto"], anchor="w",
                                   font=(FUENTE_TEXTO, 10, "bold"))
        self.clima_desc.pack(fill="x")
        self.clima_ciudad = tk.Label(columna, text="", bg=HUD["panel"], fg=HUD["suave"], anchor="w", font=(FUENTE_TEXTO, 8))
        self.clima_ciudad.pack(fill="x")
        self.clima_detalle = tk.Label(panel.cuerpo, text="", bg=HUD["panel"], fg=HUD["suave"], anchor="w",
                                      font=(FUENTE_HUD, 8), justify="left")
        self.clima_detalle.pack(fill="x", padx=10, pady=(2, 8))
        self.clima_pronostico = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        self.clima_detalle.bind("<Button-1>", lambda _e: self._actualizar_clima(programar=False))

    def _construir_sistema(self, padre):
        self.panel_sistema = panel = PanelHUD(padre, "TELEMETRÍA · EQUIPO", al_expandir=self._expandir_sistema)
        panel.pack(fill="x", pady=(0, 10))
        self.barras_sistema = {}
        for clave, nombre in (("cpu", "CPU"), ("ram", "RAM"), ("bateria", "ENERGÍA")):
            fila = tk.Frame(panel.cuerpo, bg=HUD["panel"])
            fila.pack(fill="x", padx=8, pady=(3, 3))
            cabecera = tk.Frame(fila, bg=HUD["panel"])
            cabecera.pack(fill="x")
            tk.Label(cabecera, text=nombre, bg=HUD["panel"], fg=HUD["suave"], font=(FUENTE_HUD, 8, "bold")).pack(side="left")
            valor = tk.Label(cabecera, text="--", bg=HUD["panel"], fg=HUD["texto"], font=(FUENTE_HUD, 8, "bold"))
            valor.pack(side="right")
            barra = BarraHUD(fila, segmentos=24, alto=8)
            barra.pack(fill="x", pady=(2, 0))
            self.barras_sistema[clave] = (barra, valor)
        self.grafica_cpu = tk.Canvas(panel.cuerpo, height=44, bg=HUD["panel"], highlightthickness=0, bd=0)
        self.grafica_cpu.pack(fill="x", padx=8, pady=(6, 2))
        self._historial_cpu = []
        self.sistema_detalle = tk.Label(panel.cuerpo, text=self._texto_equipo(), bg=HUD["panel"], fg=HUD["suave"],
                                        justify="left", anchor="w", wraplength=250, font=(FUENTE_TEXTO, 8))
        tk.Frame(panel.cuerpo, bg=HUD["panel"], height=6).pack()

    def _construir_nucleo(self, padre):
        panel = PanelHUD(padre, "NÚCLEO IA · ENLACES")
        panel.pack(fill="both", expand=True)
        self.filas_nucleo = {}
        for clave, nombre in (("cerebro", "CEREBRO"), ("voz", "VOZ"), ("oido", "OÍDO"), ("vision", "VISIÓN")):
            fila = tk.Frame(panel.cuerpo, bg=HUD["panel"])
            fila.pack(fill="x", padx=8, pady=3)
            punto = tk.Label(fila, text="●", bg=HUD["panel"], fg=HUD["verde"], font=(FUENTE_TEXTO, 8))
            punto.pack(side="left")
            tk.Label(fila, text=nombre, width=8, anchor="w", bg=HUD["panel"], fg=HUD["suave"], font=(FUENTE_HUD, 8, "bold")).pack(side="left", padx=(4, 0))
            valor = tk.Label(fila, text="", anchor="w", justify="left", wraplength=180, bg=HUD["panel"], fg=HUD["texto"], font=(FUENTE_TEXTO, 8))
            valor.pack(side="left", fill="x", expand=True)
            self.filas_nucleo[clave] = (punto, valor)
        self._refrescar_nucleo()

    def _refrescar_nucleo(self):
        if not hasattr(self, "filas_nucleo"):
            return
        nube = [n for n, clave in (("Gemini", config.GEMINI_API_KEY), ("ChatGPT", config.OPENAI_API_KEY)) if clave]
        local = os.path.splitext(config.NOMBRE_MODELO)[0][:22]
        cerebro = f"{local} (sin red)" if config.PROVEEDOR_PREFERIDO == "local" or not nube else " → ".join(nube + [local])
        motor = self.motor_voz.get()
        voz = f"Gemini Live · {config.GEMINI_VOZ}" if self.usar_gemini_live.get() else {"auto": "Gemini → OpenAI → Piper", "gemini": f"Gemini · {config.GEMINI_VOZ}",
               "openai": f"OpenAI · {config.OPENAI_VOZ}", "local": "Piper local"}.get(motor, motor)
        microfono = self.microfono.get().split("·", 1)[-1].strip()[:26]
        vozactiva = self._voz_activa()
        filas = {
            "cerebro": (cerebro, HUD["verde"]),
            "voz": (voz, HUD["verde"] if motor == "local" or nube else HUD["ambar"]),
            "oido": (f"Whisper {config.MODELO_WHISPER} · {microfono}", HUD["acento"] if vozactiva else HUD["tenue"]),
            "vision": ("Cámara activa · gestos" if self.vision.activa else "Apagada", HUD["acento"] if self.vision.activa else HUD["tenue"]),
        }
        for clave, (texto, color) in filas.items():
            punto, valor = self.filas_nucleo[clave]
            punto.config(fg=color)
            valor.config(text=texto)

    def _dibujar_grafica_cpu(self):
        c = self.grafica_cpu
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        if w < 40:
            return
        for y in (h * 0.25, h * 0.5, h * 0.75):
            c.create_line(0, y, w, y, fill=HUD["rejilla"])
        datos = self._historial_cpu[-60:]
        if len(datos) < 2:
            return
        paso = w / 59
        puntos = []
        for i, valor in enumerate(datos):
            puntos += [w - (len(datos) - 1 - i) * paso, h - 2 - (h - 4) * valor / 100]
        c.create_polygon(puntos[0], h, *puntos, puntos[-2], h, fill=mezclar(HUD["linea"], HUD["panel"], 0.82), outline="")
        c.create_line(*puntos, fill=HUD["linea"], width=2)
        c.create_text(4, 2, text="CPU · 2 MIN", anchor="nw", fill=HUD["tenue"], font=(FUENTE_HUD, 6))

    # ---------------------------------------------------------- columna derecha
    def _construir_medios(self, padre):
        self.panel_medios = panel = PanelHUD(padre, "MULTIMEDIA · EN REPRODUCCIÓN", al_expandir=self._expandir_medios)
        panel.pack(fill="x", pady=(0, 10))
        self.medios_portada_grande = tk.Label(panel.cuerpo, bg=HUD["panel"], bd=0)
        fila = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        fila.pack(fill="x", padx=8, pady=(2, 4))
        self.medios_portada = tk.Label(fila, bg=HUD["panel2"], fg=HUD["acento"], text="♪", width=4, height=2,
                                       font=(FUENTE_TEXTO, 18), bd=0)
        self.medios_portada.pack(side="left")
        self._medios_info = info = tk.Frame(fila, bg=HUD["panel"])
        info.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.medios_titulo = tk.Label(info, text="Nada sonando", bg=HUD["panel"], fg=HUD["texto"], anchor="w",
                                      justify="left", wraplength=180, font=(FUENTE_TEXTO, 10, "bold"))
        self.medios_titulo.pack(fill="x")
        self.medios_artista = tk.Label(info, text="Reproduce música o un video", bg=HUD["panel"], fg=HUD["suave"],
                                       anchor="w", justify="left", wraplength=180, font=(FUENTE_TEXTO, 8))
        self.medios_artista.pack(fill="x")
        self.medios_app = tk.Label(info, text="", bg=HUD["panel"], fg=HUD["acento"], anchor="w", font=(FUENTE_HUD, 7, "bold"))
        self.medios_app.pack(fill="x", pady=(2, 0))
        self.barra_medios = BarraHUD(panel.cuerpo, segmentos=36, alto=5, color=HUD["acento"])
        self.barra_medios.pack(fill="x", padx=8, pady=(2, 0))
        self.medios_tiempo = tk.Label(panel.cuerpo, text="", bg=HUD["panel"], fg=HUD["tenue"], anchor="e", font=(FUENTE_HUD, 7))
        self.medios_tiempo.pack(fill="x", padx=8)
        controles = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        controles.pack(fill="x", padx=6, pady=(2, 8))
        for texto, orden, ayuda in (
            ("⏮", lambda: medios.tecla("anterior"), "Anterior"),
            ("⏯", lambda: medios.tecla("reproducir"), "Reproducir / pausa"),
            ("⏭", lambda: medios.tecla("siguiente"), "Siguiente"),
            ("♪−", lambda: medios.tecla("bajar"), "Bajar volumen"),
            ("♪+", lambda: medios.tecla("subir"), "Subir volumen"),
            ("⤢", lambda: self._ventana_reproductor("agrandar"), "Agrandar el reproductor"),
            ("⤡", lambda: self._ventana_reproductor("achicar"), "Achicar el reproductor"),
        ):
            self._boton_icono(controles, texto, orden, ayuda).pack(side="left", padx=2, expand=True, fill="x")

    def _construir_panel_voz(self, padre):
        panel = PanelHUD(padre, "ENLACE DE VOZ")
        panel.pack(fill="x", pady=(0, 10))
        motores = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        motores.pack(fill="x", padx=8, pady=(2, 4))
        self.botones_motor = {}
        for clave, texto in _MOTORES:
            boton = tk.Button(motores, text=texto, cursor="hand2", relief="flat", bd=0, font=(FUENTE_HUD, 8, "bold"),
                              highlightthickness=1, pady=4, command=lambda c=clave: self._elegir_motor(c))
            boton.pack(side="left", expand=True, fill="x", padx=1)
            self.botones_motor[clave] = boton
        self.texto_voz_actual = tk.Label(panel.cuerpo, text="", bg=HUD["panel"], fg=HUD["suave"], anchor="w",
                                         font=(FUENTE_TEXTO, 8))
        self.texto_voz_actual.pack(fill="x", padx=10)
        self.medidor = tk.Canvas(panel.cuerpo, height=26, bg=HUD["panel"], highlightthickness=0, bd=0)
        self.medidor.pack(fill="x", padx=8, pady=(4, 4))
        self._refrescar_motores()
        botones = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        botones.pack(fill="x", padx=8, pady=(0, 4))
        self.boton_voz = self._boton(botones, "●  ACTIVAR VOZ", self._alternar_voz)
        self.boton_voz.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._boton(botones, "◉  LIVE", self._alternar_live).pack(side="left", expand=True, fill="x")
        for texto, variable, orden in (
            ("Gemini Live · voz a voz en tiempo real", self.usar_gemini_live, self._cambiar_gemini_live),
            ("Interrumpir hablando (mejor con audífonos)", self.interrumpir_voz, self._cambiar_interrupcion),
            ("Leer en voz alta las respuestas del chat", self.leer_respuestas, self._cambiar_lectura),
        ):
            tk.Checkbutton(panel.cuerpo, text=texto, variable=variable, command=orden, bg=HUD["panel"], fg=HUD["suave"],
                           selectcolor=HUD["panel2"], activebackground=HUD["panel"], activeforeground=HUD["acento"],
                           font=(FUENTE_TEXTO, 8), anchor="w").pack(fill="x", padx=6)
        tk.Frame(panel.cuerpo, bg=HUD["panel"], height=4).pack()

    def _construir_acciones(self, padre):
        panel = PanelHUD(padre, "ACCIONES RÁPIDAS")
        panel.pack(fill="both", expand=True)
        rejilla = tk.Frame(panel.cuerpo, bg=HUD["panel"])
        rejilla.pack(fill="both", expand=True, padx=6, pady=(2, 8))
        acciones = (
            ("¿Qué puedes hacer?", None), ("Resume mi día", None), ("Diagnostica el equipo", None),
            ("¿Qué clima hará mañana?", None), ("Conectores", self._abrir_conectores),
            ("Optimización", self._abrir_centro_optimizacion), ("Biblioteca", self._abrir_biblioteca),
            ("Memoria", self._ver_memoria),
        )
        for indice, (texto, accion) in enumerate(acciones):
            self._chip(rejilla, texto, accion=accion).grid(row=indice // 2, column=indice % 2, sticky="ew", padx=2, pady=2)
        rejilla.grid_columnconfigure(0, weight=1)
        rejilla.grid_columnconfigure(1, weight=1)

    # ---------------------------------------------------------- centro
    def _construir_centro(self, padre):
        padre.grid_columnconfigure(0, weight=1)
        padre.grid_rowconfigure(0, weight=3)
        padre.grid_rowconfigure(1, weight=2)
        self.orbe = OrbeVivo(padre, al_tocar=self._tocar_orbe)
        self.orbe.grid(row=0, column=0, sticky="nsew")
        self.orbe.bind("<Double-Button-1>", lambda _e: self._alternar_live())

        self.panel_chat = panel = PanelHUD(padre, "REGISTRO DE CONVERSACIÓN", al_expandir=self._expandir_chat)
        panel.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.chat = tk.Text(panel.cuerpo, wrap="word", state="disabled", bg=HUD["panel"], fg=HUD["texto"], height=6,
                            insertbackground=HUD["acento"], relief="flat", bd=0, font=(FUENTE_TEXTO, 10), padx=10, pady=6)
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("usuario", foreground="#bfe9ff", background=COLORES["usuario"], lmargin1=90, lmargin2=90, rmargin=10, spacing1=8, spacing3=6)
        self.chat.tag_configure("bark", foreground=HUD["texto"], background=COLORES["bark"], lmargin1=10, lmargin2=10, rmargin=90, spacing1=8, spacing3=6)
        self.chat.tag_configure("sistema", foreground=HUD["tenue"], justify="center", spacing1=4, spacing3=4, font=(FUENTE_TEXTO, 8))
        self.chat.tag_configure("autor_usuario", foreground=HUD["acento"], background=COLORES["usuario"], lmargin1=90, rmargin=10, font=(FUENTE_HUD, 7, "bold"), spacing1=8)
        self.chat.tag_configure("autor_bark", foreground=HUD["linea"], background=COLORES["bark"], lmargin1=10, rmargin=90, font=(FUENTE_HUD, 7, "bold"), spacing1=8)
        self._agregar("Bark", "Listo. Escribe, toca el orbe para hablar o entra al modo Live. Puedo operar localmente incluso sin internet.", "bark")

        entrada = tk.Frame(panel.cuerpo, bg=HUD["panel2"], highlightbackground=HUD["borde"], highlightthickness=1, padx=10, pady=6)
        entrada.pack(fill="x", pady=(6, 0))
        tk.Label(entrada, text="›", bg=HUD["panel2"], fg=HUD["acento"], font=("Consolas", 16, "bold")).pack(side="left", padx=(0, 6))
        self.campo = tk.Entry(entrada, bg=HUD["panel2"], fg=HUD["suave"], insertbackground=HUD["texto"], relief="flat", font=(FUENTE_TEXTO, 11))
        self.campo.pack(side="left", fill="x", expand=True, padx=(2, 8))
        self.campo.bind("<Return>", lambda _: self._enviar())
        self.campo.bind("<FocusIn>", self._activar_campo)
        self.campo.bind("<FocusOut>", self._desactivar_campo)
        self.campo.insert(0, "Escribe un mensaje…  (Ctrl+K)")
        self._boton_icono(entrada, "◉", self._alternar_voz, "Activar o detener la voz").pack(side="right", padx=(4, 0))
        self._boton(entrada, "ENVIAR  ↗", self._enviar).pack(side="right")

        # Controles del modo Live (aparecen sobre el orbe).
        self.barra_live = tk.Frame(padre, bg=HUD["fondo"])
        for texto, orden in (("❚❚  PAUSAR VOZ", self._alternar_voz), ("✋  INTERRUMPIR", self._interrumpir),
                             ("✕  SALIR DE LIVE", self._alternar_live)):
            self._boton(self.barra_live, texto, orden).pack(side="left", padx=6)

    # ---------------------------------------------------------- cajón de ajustes
    def _construir_ajustes(self):
        """Toda la configuración avanzada en un cajón lateral, para que el HUD quede limpio."""
        self.cajon = tk.Frame(self.raiz, bg=HUD["panel"], highlightbackground=HUD["linea"], highlightthickness=1)
        cabecera = tk.Frame(self.cajon, bg=HUD["panel"], padx=16, pady=12)
        cabecera.pack(fill="x")
        tk.Label(cabecera, text="AJUSTES DEL SISTEMA", bg=HUD["panel"], fg=HUD["acento"], font=(FUENTE_HUD, 12, "bold")).pack(side="left")
        cerrar = tk.Label(cabecera, text="✕", bg=HUD["panel"], fg=HUD["suave"], cursor="hand2", font=(FUENTE_TEXTO, 12))
        cerrar.pack(side="right")
        cerrar.bind("<Button-1>", lambda _e: self._alternar_ajustes())
        contenedor = tk.Frame(self.cajon, bg=HUD["panel"])
        contenedor.pack(fill="both", expand=True)
        canvas_lateral = tk.Canvas(contenedor, bg=HUD["panel"], highlightthickness=0, borderwidth=0)
        canvas_lateral.pack(side="left", fill="both", expand=True)
        lateral = tk.Frame(canvas_lateral, bg=HUD["panel"], padx=18, pady=6)
        ventana_lateral = canvas_lateral.create_window((0, 0), window=lateral, anchor="nw")
        lateral.bind("<Configure>", lambda _evento: canvas_lateral.configure(scrollregion=canvas_lateral.bbox("all")))
        canvas_lateral.bind("<Configure>", lambda evento: canvas_lateral.itemconfigure(ventana_lateral, width=evento.width))
        desplazar = lambda evento: canvas_lateral.yview_scroll(-1 * (evento.delta // 120), "units")
        self.cajon.bind("<Enter>", lambda _evento: self.raiz.bind_all("<MouseWheel>", desplazar))
        self.cajon.bind("<Leave>", lambda _evento: self.raiz.unbind_all("<MouseWheel>"))

        self.descripcion_estado = tk.Label(lateral, text="", wraplength=280, font=(FUENTE_TEXTO, 9), fg=HUD["suave"], bg=HUD["panel"], justify="left")
        self._etiqueta_seccion(lateral, "MICRÓFONO Y ESCUCHA")
        self._etiqueta_pequena(lateral, "MICRÓFONO DE ENTRADA")
        self._selector(lateral, self.microfono, tuple(self.dispositivos_mic), self._cambiar_microfono).pack(fill="x", pady=(2, 8))
        tk.Checkbutton(lateral, text="Modo conversacional · Bark opcional", variable=self.escucha_libre,
                       command=self._cambiar_escucha_libre, bg=HUD["panel"], fg=HUD["texto"],
                       selectcolor=HUD["panel2"], activebackground=HUD["panel"],
                       activeforeground=HUD["acento"], font=(FUENTE_TEXTO, 9)).pack(anchor="w", pady=(0, 4))
        self._etiqueta_pequena(lateral, "MODELO WHISPER")
        self._selector(lateral, self.modelo_escucha, modelos_whisper_disponibles(), self._cambiar_modelo_escucha).pack(fill="x", pady=(2, 5))
        self._boton(lateral, "Probar micrófono", self._probar_microfono).pack(fill="x", pady=4)
        self._etiqueta_seccion(lateral, "VOCES EXTERNAS")
        self._etiqueta_pequena(lateral, "ORDEN DE VOCES")
        self._selector(lateral, self.motor_voz, ("auto", "gemini", "openai", "local"), self._cambiar_motor_voz).pack(fill="x", pady=(2, 6))
        self._etiqueta_pequena(lateral, "GEMINI · MODELO")
        self._selector(lateral, self.modelo_gemini, config.MODELOS_TTS_GEMINI, self._cambiar_voz_gemini).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "GEMINI · VOZ")
        self._selector(lateral, self.voz_gemini, config.VOCES_GEMINI, self._cambiar_voz_gemini).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "GEMINI LIVE · MODELO (VOZ A VOZ)")
        self._selector(lateral, self.modelo_live, config.MODELOS_LIVE_GEMINI, self._cambiar_modelo_live).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "OPENAI (CHATGPT) · MODELO")
        self._selector(lateral, self.modelo_tts, config.MODELOS_TTS_OPENAI, self._cambiar_voz_nube).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "OPENAI (CHATGPT) · VOZ")
        self._selector(lateral, self.voz_nube, config.VOCES_OPENAI, self._cambiar_voz_nube).pack(fill="x", pady=(2, 5))
        voces = voces_locales_disponibles() or [self.voz_seleccionada.get()]
        self._etiqueta_pequena(lateral, "LOCAL SIN INTERNET · PIPER")
        self.selector_voz_local = self._selector(lateral, self.voz_seleccionada, voces, self._cambiar_voz)
        self.selector_voz_local.pack(fill="x", pady=(2, 5))
        self._boton(lateral, "Descargar voces optimizadas", self._descargar_voces).pack(fill="x", pady=(0, 8))
        tk.Label(lateral, text="Las claves GEMINI_API_KEY y OPENAI_API_KEY van en el archivo .env.", wraplength=280,
                 justify="left", font=(FUENTE_TEXTO, 8), fg=HUD["tenue"], bg=HUD["panel"]).pack(anchor="w")
        self._etiqueta_seccion(lateral, "VISIÓN")
        self.boton_vision = self._boton(lateral, "Activar visión local", self._alternar_vision)
        self.boton_vision.pack(fill="x", pady=4)
        tk.Label(lateral, text="Cámara privada · palma = voz · puño = suspender", wraplength=280,
                 font=(FUENTE_TEXTO, 8), fg=HUD["suave"], bg=HUD["panel"]).pack(anchor="w", pady=(0, 7))
        self._etiqueta_seccion(lateral, "EQUIPO Y PERMISOS")
        tk.Label(lateral, text=self._texto_equipo(), justify="left", wraplength=280, font=(FUENTE_TEXTO, 8),
                 fg=HUD["suave"], bg=HUD["panel"]).pack(anchor="w", pady=(0, 5))
        if config.FALTA_MODELO_RECOMENDADO:
            self.boton_modelo = self._boton(lateral, f"Descargar {config.MODELO_RECOMENDADO.nombre} ({config.MODELO_RECOMENDADO.tamano_gb:.1f} GB)", self._descargar_modelo)
            self.boton_modelo.pack(fill="x", pady=4)
        self._boton(lateral, "Permisos de micrófono y cámara", self._ver_permisos).pack(fill="x", pady=4)
        self._boton(lateral, "Ejecutar en segundo plano", self._segundo_plano).pack(fill="x", pady=4)
        self._boton(lateral, "Diagnóstico del equipo", self._diagnostico).pack(fill="x", pady=4)
        self._etiqueta_seccion(lateral, "OPTIMIZACIÓN LOCAL")
        self._boton(lateral, "Centro de optimización", self._abrir_centro_optimizacion).pack(fill="x", pady=4)
        self._boton(lateral, "Analizar consumo de RAM", lambda: self._enviar_consulta("Diagnóstico de RAM")).pack(fill="x", pady=4)
        self._boton(lateral, "Liberar RAM avanzada", lambda: self._enviar_consulta("Liberar RAM avanzada")).pack(fill="x", pady=4)
        self._boton(lateral, "Ver programas de inicio", lambda: self._enviar_consulta("Mostrar programas de inicio")).pack(fill="x", pady=4)
        self._boton(lateral, "Limpiar temporales", lambda: self._enviar_consulta("Limpiar temporales")).pack(fill="x", pady=4)
        self._etiqueta_seccion(lateral, "APARIENCIA Y SISTEMA")
        self._boton(lateral, "Personalizar colores", self._personalizar_colores).pack(fill="x", pady=4)
        self._boton(lateral, "Limpiar conversación", self._limpiar_chat).pack(fill="x", pady=4)
        self._boton(lateral, "Reiniciar Bark", self._reiniciar_bark).pack(fill="x", pady=(12, 4))
        tk.Label(lateral, text=estado_proveedores(), justify="left", wraplength=280, font=(FUENTE_TEXTO, 8),
                 fg=HUD["suave"], bg=HUD["panel"]).pack(anchor="w", pady=(10, 14))
        self._ajustes_abiertos = False

    def _alternar_ajustes(self):
        self._ajustes_abiertos = not self._ajustes_abiertos
        if self._ajustes_abiertos:
            self.cajon.place(relx=1, x=-14, y=70, anchor="ne", width=350, relheight=1, height=-104)
            self.cajon.lift()
        else:
            self.cajon.place_forget()
            self.raiz.unbind_all("<MouseWheel>")

    # ============================================================
    # Modo Live, orbe e interrupción
    # ============================================================

    def _alternar_live(self):
        """Modo Live: el orbe ocupa todo, la conversación sigue por voz y los subtítulos en pantalla."""
        self.modo_live = not self.modo_live
        if self.modo_live:
            self.col_izq.grid_remove()
            self.col_der.grid_remove()
            self.panel_chat.grid_remove()
            # Las columnas laterales reservan ancho mínimo y el registro su fila: se liberan para el orbe.
            self.cuerpo.grid_columnconfigure(0, minsize=0)
            self.cuerpo.grid_columnconfigure(2, minsize=0)
            self.centro.grid_rowconfigure(1, weight=0)
            self.barra_live.place(in_=self.orbe, relx=0.5, rely=1.0, y=-14, anchor="s")
            self.barra_live.lift()
            if not self._voz_activa():
                self._alternar_voz()
        else:
            self.barra_live.place_forget()
            self.cuerpo.grid_columnconfigure(0, minsize=290)
            self.cuerpo.grid_columnconfigure(2, minsize=300)
            self._expandir_chat(self.panel_chat.expandido)
            self.col_izq.grid()
            self.col_der.grid()
            self.panel_chat.grid()
        self.orbe.modo_live = self.modo_live
        self.raiz.after(30, self.orbe._construir)
        self._dibujar_encabezado()

    def _tocar_orbe(self):
        """Como en Gemini Live: tocar mientras habla lo interrumpe; si la voz está apagada, la enciende."""
        if self.estado == "hablando":
            self._interrumpir()
        elif not self._voz_activa():
            self._alternar_voz()

    def _interrumpir(self):
        audio_vivo.interrumpir()
        if self.sesion_live and self.sesion_live.activa:
            self.sesion_live.interrumpir()
        self.orbe.fijar_subtitulos(bark="")

    def _tecla_escape(self, _evento=None):
        if self.estado == "hablando":
            self._interrumpir()
        elif self._ajustes_abiertos:
            self._alternar_ajustes()
        elif self.modo_live:
            self._alternar_live()

    def _cambiar_interrupcion(self):
        config.INTERRUMPIR_CON_VOZ = self.interrumpir_voz.get()
        estado = ("activado: si hablas fuerte mientras Bark responde, se calla y te escucha (usa audífonos para evitar que se oiga a sí mismo)"
                  if config.INTERRUMPIR_CON_VOZ else "desactivado: toca el orbe o pulsa Esc para interrumpir")
        self._agregar("Sistema", f"Interrumpir con la voz {estado}.", "sistema")

    def _elegir_motor(self, motor):
        self.motor_voz.set(motor)
        self._cambiar_motor_voz(motor)

    def _refrescar_motores(self):
        actual = self.motor_voz.get()
        for clave, boton in self.botones_motor.items():
            activo = clave == actual
            boton.config(bg=HUD["linea"] if activo else HUD["panel2"], fg=HUD["fondo"] if activo else HUD["suave"],
                         activebackground=HUD["acento"], activeforeground=HUD["fondo"],
                         highlightbackground=HUD["linea"] if activo else HUD["borde"])
        detalle = {
            "auto": f"Gemini ({config.GEMINI_VOZ}) → OpenAI ({config.OPENAI_VOZ}) → Piper",
            "gemini": f"Gemini · {config.GEMINI_VOZ} · respaldo OpenAI y Piper",
            "openai": f"OpenAI · {config.OPENAI_VOZ} · respaldo Piper",
            "local": f"Piper local · {config.NOMBRE_VOZ_TTS} · sin internet",
        }[actual if actual in {"auto", "gemini", "openai", "local"} else "auto"]
        sin_clave = (actual in {"gemini"} and not config.GEMINI_API_KEY) or (actual == "openai" and not config.OPENAI_API_KEY)
        if self.usar_gemini_live.get():
            detalle = f"Gemini Live · {config.GEMINI_VOZ} en tiempo real · respaldo: {detalle}"
        self.texto_voz_actual.config(text=("⚠ Falta la clave en .env · " if sin_clave else "") + detalle, wraplength=270, justify="left")

    def _nivel_objetivo(self) -> float:
        """Qué tan grande debe verse el orbe ahora: tu voz al escuchar, la de Bark al hablar."""
        t = time.monotonic()
        if self.estado == "escuchando":
            nivel = audio_vivo.nivel("entrada")
            # Ganancia automática: micrófonos bajos también mueven el orbe.
            self._max_entrada = max(0.25, nivel, self._max_entrada * 0.996)
            return min(1.0, nivel / self._max_entrada) * 0.95
        if self.estado == "hablando":
            if audio_vivo.activo("salida"):
                return min(1.0, audio_vivo.nivel("salida") * 1.15)
            return 0.35 + 0.25 * math.sin(t * 9) * math.sin(t * 2.3)  # salida sin medición: pulso sintético
        if self.estado == "pensando":
            return 0.14 + 0.08 * math.sin(t * 4)
        if self.estado == "suspendido":
            return 0.02
        return 0.05 + 0.035 * math.sin(t * 1.6)

    def _animar(self):
        if self.sesion_live and self.sesion_live.activa:
            nuevo = self.sesion_live.estado()
            if nuevo != self.estado:
                self._cambiar_estado(nuevo)
        try:
            self.orbe.actualizar(self.estado, self._nivel_objetivo())
            self._dibujar_medidor(self.orbe.nivel)
        except tk.TclError:
            return
        self.raiz.after(33, self._animar)

    def _dibujar_medidor(self, nivel):
        c = self.medidor
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40:
            return
        c.delete("all")
        barras = 32
        paso = w / barras
        t = time.monotonic()
        color = ESTADOS.get(self.estado, ESTADOS["en espera"])[0]
        for i in range(barras):
            forma = 0.45 + 0.55 * abs(math.sin(i * 0.7 + t * 6.5))
            alto = max(2, (h - 4) * nivel * forma)
            x = i * paso
            c.create_rectangle(x + 1, (h - alto) / 2, x + paso - 2, (h + alto) / 2, outline="",
                               fill=mezclar(color, HUD["panel"], 0.15 if alto > 3 else 0.7))

    # ============================================================
    # Datos en vivo: reloj, clima, sistema y multimedia
    # ============================================================

    def _tic_reloj(self):
        ahora = datetime.now()
        c = self.lienzo_reloj
        c.itemconfig(self.item_hora, text=ahora.strftime("%H:%M"))
        caja = c.bbox(self.item_hora)
        if caja:
            c.coords(self.item_segundos, caja[2] + 6, 48)
        c.itemconfig(self.item_segundos, text=ahora.strftime("%S"))
        c.itemconfig(self.item_fecha, text=f"{_DIAS[ahora.weekday()]}  ·  {ahora.day:02d} {_MESES[ahora.month - 1]} {ahora.year}")
        self.barra_segundos.fijar(ahora.second / 59 * 100)
        self.raiz.after(1000 - ahora.microsecond // 1000, self._tic_reloj)

    def _actualizar_clima(self, programar=True):
        if not self._clima:
            self.clima_desc.config(text="Consultando clima…")

        def trabajo():
            try:
                from conectores.informacion import clima_datos
                datos = clima_datos(os.getenv("BARK_CIUDAD", ""))
            except Exception:
                datos = None
            try:
                self.raiz.after(0, lambda: self._mostrar_clima(datos))
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=trabajo, name="bark-clima", daemon=True).start()
        if programar:
            self.raiz.after(15 * 60 * 1000, self._actualizar_clima)

    def _mostrar_clima(self, datos):
        if not datos:
            if not self._clima:
                self.clima_desc.config(text="Sin datos del clima")
                self.clima_ciudad.config(text="Revisa la conexión o fija BARK_CIUDAD en .env")
            return
        self._clima = datos
        self.clima_icono.config(text=datos["icono"])
        self.clima_temp.config(text=f"{datos['temperatura']:.0f}°")
        self.clima_desc.config(text=datos["descripcion"].capitalize())
        self.clima_ciudad.config(text=f"{datos['ciudad']}, {datos['region']}"[:40])
        self.clima_detalle.config(text=f"SENSACIÓN {datos['sensacion']:.0f}°   ·   HUMEDAD {datos['humedad']}%   ·   VIENTO {datos['viento']:.0f} KM/H")
        for hijo in self.clima_pronostico.winfo_children():
            hijo.destroy()
        for dia in datos["dias"]:
            fila = tk.Frame(self.clima_pronostico, bg=HUD["panel"])
            fila.pack(fill="x", pady=1)
            tk.Label(fila, text=dia["nombre"].upper(), width=8, anchor="w", bg=HUD["panel"], fg=HUD["suave"], font=(FUENTE_HUD, 8, "bold")).pack(side="left")
            tk.Label(fila, text=dia["icono"], bg=HUD["panel"], fg=HUD["acento"], font=(FUENTE_TEXTO, 11)).pack(side="left", padx=4)
            tk.Label(fila, text=f"{dia['maxima']:.0f}° / {dia['minima']:.0f}°", bg=HUD["panel"], fg=HUD["texto"], font=(FUENTE_HUD, 9)).pack(side="left", padx=6)
            tk.Label(fila, text=f"☂ {dia['lluvia']}%", bg=HUD["panel"], fg=HUD["suave"], font=(FUENTE_HUD, 8)).pack(side="right")

    def _expandir_clima(self, expandido):
        if expandido:
            self.clima_pronostico.pack(fill="x", padx=10, pady=(0, 8))
        else:
            self.clima_pronostico.pack_forget()

    def _tic_sistema(self):
        try:
            datos = self.telemetria.leer()
        except Exception:
            datos = None
        if datos:
            barra, etiqueta = self.barras_sistema["cpu"]
            barra.fijar(datos["cpu"])
            self._historial_cpu = (self._historial_cpu + [datos["cpu"]])[-60:]
            self._dibujar_grafica_cpu()
            etiqueta.config(text=f"{datos['cpu']:.0f}%")
            barra, etiqueta = self.barras_sistema["ram"]
            barra.fijar(datos["ram"])
            etiqueta.config(text=f"{datos['ram']:.0f}%  ·  {datos['ram_usada']:.1f}/{datos['ram_total']:.0f} GB")
            barra, etiqueta = self.barras_sistema["bateria"]
            if datos["bateria"] is None:
                barra.color = HUD["verde"]
                barra.fijar(100)
                etiqueta.config(text="CORRIENTE ⚡")
            else:
                barra.color = HUD["rosa"] if datos["bateria"] <= 15 and not datos["corriente"] else HUD["verde"]
                barra.fijar(datos["bateria"])
                etiqueta.config(text=f"{datos['bateria']}%{'  ⚡' if datos['corriente'] else ''}")
        self._refrescar_nucleo()
        self.raiz.after(2000, self._tic_sistema)

    def _expandir_sistema(self, expandido):
        if expandido:
            self.sistema_detalle.config(text=self._texto_equipo() + "\n" + estado_proveedores())
            self.sistema_detalle.pack(fill="x", padx=10, pady=(0, 8))
        else:
            self.sistema_detalle.pack_forget()

    def _tic_medios(self):
        estado = self.medios.estado
        if estado:
            clave = (estado["titulo"], estado["artista"], estado.get("portada"))
            self.medios_titulo.config(text=texto_seguro(estado["titulo"])[:90] or "Sin título")
            self.medios_artista.config(text=texto_seguro(estado["artista"] or estado["album"])[:70])
            self.medios_app.config(text=("▶  " if estado["reproduciendo"] else "❚❚  ") + estado["app"].upper())
            if estado["duracion"] > 0:
                posicion = medios.posicion_actual(estado)
                self.barra_medios.fijar(posicion / estado["duracion"] * 100)
                self.medios_tiempo.config(text=f"{_formato_tiempo(posicion)} / {_formato_tiempo(estado['duracion'])}")
            else:
                self.barra_medios.fijar(0)
                self.medios_tiempo.config(text="EN VIVO" if estado["reproduciendo"] else "")
            if clave != self._medio_clave:
                self._medio_clave = clave
                self._cargar_portada(estado.get("portada"))
        elif self._medio_clave is not None or not self.medios.disponible:
            self._medio_clave = None
            self.medios_titulo.config(text="Nada sonando")
            self.medios_artista.config(text="Reproduce música o un video" if self.medios.disponible else "Controles multimedia no disponibles")
            self.medios_app.config(text="")
            self.medios_tiempo.config(text="")
            self.barra_medios.fijar(0)
            self._cargar_portada(None)
        self.raiz.after(500, self._tic_medios)

    def _cargar_portada(self, ruta):
        grande = self.panel_medios.expandido
        self._portada_img = self._portada_grande = None
        if ruta and os.path.isfile(ruta):
            try:
                from PIL import Image, ImageTk
                with Image.open(ruta) as imagen:
                    imagen = imagen.convert("RGB")
                    pequena = imagen.copy()
                    pequena.thumbnail((64, 64))
                    self._portada_img = ImageTk.PhotoImage(pequena)
                    if grande:
                        grande_img = imagen.copy()
                        grande_img.thumbnail((250, 170))
                        self._portada_grande = ImageTk.PhotoImage(grande_img)
            except Exception:
                self._portada_img = None
        if self._portada_img:
            self.medios_portada.config(image=self._portada_img, text="", width=64, height=64)
        else:
            self.medios_portada.config(image="", text="♪", width=4, height=2)
        if grande and self._portada_grande:
            # Panel agrandado: portada grande arriba y sin la miniatura repetida.
            self.medios_portada_grande.config(image=self._portada_grande)
            self.medios_portada_grande.pack(before=self.medios_portada.master, pady=(4, 4))
            self.medios_portada.pack_forget()
        else:
            self.medios_portada_grande.pack_forget()
            if not self.medios_portada.winfo_ismapped():
                self.medios_portada.pack(side="left", before=self._medios_info)

    def _expandir_medios(self, _expandido):
        """Agranda o achica el panel: portada grande y más espacio para el título."""
        self.medios_titulo.config(wraplength=250 if _expandido else 180)
        self._cargar_portada((self.medios.estado or {}).get("portada"))

    def _ventana_reproductor(self, accion):
        estado = self.medios.estado
        if not estado:
            self._agregar("Sistema", "No hay música ni video sonando ahora.", "sistema")
            return

        def trabajo():
            try:
                resultado = medios.ajustar_ventana_reproductor(estado, accion)
            except Exception as error:
                resultado = f"No pude ajustar el reproductor: {error}"
            if resultado.startswith("No "):
                self.raiz.after(0, lambda: self._agregar("Sistema", resultado, "sistema"))

        threading.Thread(target=trabajo, daemon=True).start()

    def _expandir_chat(self, expandido):
        """Agranda el registro de conversación (el orbe se achica) o lo devuelve a su tamaño."""
        self.centro.grid_rowconfigure(0, weight=1 if expandido else 3)
        self.centro.grid_rowconfigure(1, weight=4 if expandido else 2)

    # ============================================================
    # Controles reutilizables
    # ============================================================

    def _boton(self, padre, texto, orden):
        boton = tk.Button(padre, text=texto, command=orden, cursor="hand2", bg=COLORES["boton"], activebackground=COLORES["boton_activo"],
                          fg=COLORES["boton_texto"], activeforeground="#ffffff", relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=HUD["linea_suave"], font=(FUENTE_HUD, 9, "bold"), padx=12, pady=7)
        boton.bind("<Enter>", lambda _evento, b=boton: b.config(bg=COLORES["boton_activo"]))
        boton.bind("<Leave>", lambda _evento, b=boton: b.config(bg=COLORES["boton"]))
        self._botones.append(boton)
        return boton

    def _boton_icono(self, padre, texto, orden, ayuda=""):
        boton = tk.Button(padre, text=texto, command=orden, cursor="hand2", bg=HUD["panel2"], fg=HUD["acento"],
                          activebackground=COLORES["boton_activo"], activeforeground="#ffffff", relief="flat", bd=0,
                          highlightthickness=1, highlightbackground=HUD["borde"], font=("Segoe UI Symbol", 11), padx=6, pady=2)
        boton.bind("<Enter>", lambda _e, b=boton: (b.config(bg=COLORES["boton_activo"]), self._pista(ayuda)))
        boton.bind("<Leave>", lambda _e, b=boton: (b.config(bg=HUD["panel2"]), self._pista("")))
        return boton

    def _pista(self, texto):
        if hasattr(self, "_texto_pie") and texto is not None:
            self.pie.itemconfig(self._texto_pie, text=texto.upper() if texto else
                                f"LOCAL · PRIVADO · {self.conteo_mensajes} MENSAJES  ·  ESC INTERRUMPE  ·  F11 LIVE")

    def _chip(self, padre, texto, accion=None, discreto=False):
        """Acceso compacto; sin acción propia, envía el texto como consulta."""
        boton = tk.Button(
            padre, text=texto, cursor="hand2", command=accion or (lambda t=texto: self._usar_sugerencia(t)),
            bg=HUD["panel2"], fg=HUD["suave"] if discreto else HUD["texto"],
            activebackground=COLORES["boton_activo"], activeforeground="#ffffff",
            relief="flat", bd=0, highlightthickness=1, highlightbackground=HUD["borde"],
            font=(FUENTE_TEXTO, 8, "bold"), padx=8, pady=6,
        )
        boton.bind("<Enter>", lambda _evento, b=boton: b.config(bg=COLORES["boton_activo"], fg="#ffffff", highlightbackground=HUD["linea"]))
        boton.bind("<Leave>", lambda _evento, b=boton: b.config(bg=HUD["panel2"], fg=HUD["suave"] if discreto else HUD["texto"], highlightbackground=HUD["borde"]))
        return boton

    def _activar_campo(self, _evento=None):
        if self._placeholder_activo:
            self.campo.delete(0, "end")
            self.campo.config(fg=HUD["texto"])
            self._placeholder_activo = False

    def _desactivar_campo(self, _evento=None):
        if not self.campo.get().strip():
            self.campo.insert(0, "Escribe un mensaje…  (Ctrl+K)")
            self.campo.config(fg=HUD["suave"])
            self._placeholder_activo = True

    def _enfocar_campo(self):
        self.campo.focus_force()
        self._activar_campo()
        return "break"

    def _usar_sugerencia(self, texto):
        self._enfocar_campo()
        self.campo.delete(0, "end")
        self.campo.insert(0, texto)
        self._enviar()

    def _enviar_consulta(self, texto):
        """Lleva los accesos visuales al mismo flujo seguro que el chat."""
        if str(self.campo.cget("state")) == "disabled":
            return
        self._enfocar_campo()
        self.campo.delete(0, "end")
        self.campo.insert(0, texto)
        self._enviar()

    def _etiqueta_seccion(self, padre, texto):
        tk.Label(padre, text=f"▸ {texto}", font=(FUENTE_HUD, 8, "bold"), fg=HUD["acento"], bg=HUD["panel"]).pack(anchor="w", pady=(12, 2))

    def _etiqueta_pequena(self, padre, texto):
        tk.Label(padre, text=texto, font=(FUENTE_HUD, 7, "bold"), fg=HUD["suave"], bg=HUD["panel"]).pack(anchor="w")

    def _selector(self, padre, variable, opciones, al_cambiar):
        opciones = tuple(opciones) or (variable.get(),)
        selector = tk.OptionMenu(padre, variable, *opciones, command=al_cambiar)
        selector.config(bg=HUD["panel2"], fg=HUD["texto"], activebackground="#0f4a5a", activeforeground=HUD["acento"], relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=HUD["borde"], font=(FUENTE_TEXTO, 9))
        selector["menu"].config(bg=HUD["panel2"], fg=HUD["texto"], activebackground="#0f4a5a", activeforeground=HUD["acento"], relief="flat")
        return selector

    # ============================================================
    # Ventanas secundarias (misma lógica de siempre, con estilo HUD)
    # ============================================================

    def _abrir_centro_optimizacion(self):
        ventana = self._ventana_secundaria()
        ventana.title("Centro de optimización · Bark")
        ventana.geometry("760x560")
        ventana.minsize(620, 440)
        cabecera = tk.Frame(ventana, bg=COLORES["panel"], padx=22, pady=18)
        cabecera.pack(fill="x")
        tk.Label(cabecera, text="CENTRO DE OPTIMIZACIÓN", bg=COLORES["panel"], fg=COLORES["acento"], font=(FUENTE_HUD, 15, "bold")).pack(anchor="w")
        tk.Label(cabecera, text="Diagnóstico y mantenimiento local · Bark no elimina ni desactiva nada sin confirmación.", bg=COLORES["panel"], fg=COLORES["suave"], font=(FUENTE_TEXTO, 9)).pack(anchor="w", pady=(5, 0))
        acciones = tk.Frame(ventana, bg=COLORES["fondo"], padx=22, pady=14)
        acciones.pack(fill="x")
        visor = scrolledtext.ScrolledText(ventana, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"], relief="flat", font=(FUENTE_TEXTO, 10), padx=15, pady=13)
        visor.pack(fill="both", expand=True, padx=22, pady=(0, 20))

        def mostrar(texto):
            if not visor.winfo_exists():
                return
            visor.configure(state="normal")
            visor.delete("1.0", "end")
            visor.insert("end", texto)
            visor.configure(state="disabled")

        def analizar():
            mostrar("Analizando el equipo local…")
            def trabajo():
                resultado = comandos.resumen_optimizacion()
                ventana.after(0, lambda: mostrar(resultado))
            threading.Thread(target=trabajo, daemon=True).start()

        self._boton(acciones, "Analizar equipo", analizar).pack(side="left", padx=(0, 8))
        self._boton(acciones, "Analizar RAM", lambda: self._enviar_desde_centro(ventana, "Diagnóstico de RAM")).pack(side="left", padx=(0, 8))
        self._boton(acciones, "Liberar RAM", lambda: self._enviar_desde_centro(ventana, "Liberar RAM avanzada")).pack(side="left", padx=(0, 8))
        self._boton(acciones, "Programas de inicio", lambda: self._enviar_desde_centro(ventana, "Mostrar programas de inicio")).pack(side="left", padx=(0, 8))
        self._boton(acciones, "Limpiar temporales", lambda: self._enviar_desde_centro(ventana, "Limpiar temporales")).pack(side="left")
        analizar()

    def _abrir_conectores(self):
        """Panel tipo «conectores» de Claude: qué puede tocar Bark y con qué herramientas."""
        from conectores import REGISTRO, mcp

        ventana = self._ventana_secundaria()
        ventana.title("Conectores · Bark")
        ventana.geometry("820x600")
        ventana.minsize(640, 460)
        cabecera = tk.Frame(ventana, bg=COLORES["panel"], padx=22, pady=18)
        cabecera.pack(fill="x")
        tk.Label(cabecera, text="CONECTORES", bg=COLORES["panel"], fg=COLORES["acento"], font=(FUENTE_HUD, 15, "bold")).pack(anchor="w")
        resumen = tk.Label(cabecera, bg=COLORES["panel"], fg=COLORES["suave"], font=(FUENTE_TEXTO, 9))
        resumen.pack(anchor="w", pady=(5, 0))

        barra = tk.Frame(ventana, bg=COLORES["fondo"], padx=22, pady=12)
        barra.pack(fill="x")
        busqueda = tk.Entry(barra, bg=COLORES["panel2"], fg=COLORES["texto"], insertbackground=COLORES["acento"],
                            relief="flat", font=(FUENTE_TEXTO, 10))
        busqueda.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        visor = scrolledtext.ScrolledText(ventana, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"],
                                          relief="flat", font=(FUENTE_TEXTO, 10), padx=15, pady=13)
        visor.pack(fill="both", expand=True, padx=22, pady=(0, 20))
        visor.tag_configure("titulo", foreground=COLORES["acento"], font=(FUENTE_TEXTO, 11, "bold"))
        visor.tag_configure("apagado", foreground=COLORES["suave"], font=(FUENTE_TEXTO, 11, "bold"))
        visor.tag_configure("suave", foreground=COLORES["suave"])
        visor.tag_configure("herramienta", font=("Consolas", 9))

        def escribir(bloques):
            if not visor.winfo_exists():
                return
            visor.configure(state="normal")
            visor.delete("1.0", "end")
            for texto, etiqueta in bloques:
                visor.insert("end", texto, *((etiqueta,) if etiqueta else ()))
            visor.configure(state="disabled")

        def mostrar(_evento=None):
            consulta = busqueda.get().strip()
            if consulta:
                encontradas = REGISTRO.buscar(consulta, limite=15, solo_disponibles=False)
                bloques = [(f"Herramientas para «{consulta}»\n\n", "titulo")]
                bloques += [(f"{h.firma()}\n", "herramienta") for h in encontradas] or [("Ninguna coincide.\n", "suave")]
                escribir(bloques)
                return
            estado = REGISTRO.estado()
            listos = sum(1 for c in estado if c["disponible"])
            resumen.config(text=f"{listos} de {len(estado)} conectores listos · {len(REGISTRO.herramientas)} herramientas · "
                                "edita config/conectores_mcp.json para sumar servidores MCP")
            bloques = []
            for c in sorted(estado, key=lambda c: (not c["disponible"], c["origen"] != "integrado", c["nombre"])):
                marca = "●" if c["disponible"] else "○"
                bloques.append((f"{marca} {c['nombre']}\n", "titulo" if c["disponible"] else "apagado"))
                detalle = c["descripcion"] if c["disponible"] else f"No disponible: {c['motivo']}"
                bloques.append((f"{detalle}\n", "suave"))
                if c["herramientas"]:
                    bloques.append(("  " + "  ·  ".join(c["herramientas"]) + "\n", "herramienta"))
                bloques.append(("\n", None))
            for modulo, error in REGISTRO.errores_carga.items():
                bloques.append((f"○ {modulo}: {error}\n", "apagado"))
            escribir(bloques)

        def recargar():
            resumen.config(text="Reconectando servidores MCP…")
            def trabajo():
                mcp.conectar_todo()
                ventana.after(0, mostrar)
            threading.Thread(target=trabajo, daemon=True).start()

        def abrir_configuracion():
            import shutil
            if not os.path.exists(mcp.RUTA_CONFIG):
                ejemplo = os.path.join(os.path.dirname(mcp.RUTA_CONFIG), "conectores_mcp.example.json")
                shutil.copyfile(ejemplo, mcp.RUTA_CONFIG)
            os.startfile(mcp.RUTA_CONFIG)

        busqueda.bind("<KeyRelease>", mostrar)
        self._boton(barra, "Recargar MCP", recargar).pack(side="left", padx=(0, 8))
        self._boton(barra, "Configurar MCP", abrir_configuracion).pack(side="left")
        mostrar()

    def _enviar_desde_centro(self, ventana, texto):
        if ventana.winfo_exists():
            ventana.destroy()
        self._enviar_consulta(texto)

    def _limpiar_chat(self):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.conteo_mensajes = 0
        self.orbe.fijar_subtitulos("", "")
        self._agregar("Sistema", "Vista de conversación limpiada. Tu memoria guardada sigue intacta.", "sistema")

    def _personalizar_colores(self):
        ventana = self._ventana_secundaria()
        ventana.title("Colores de Bark")
        ventana.configure(bg=COLORES["panel"])
        ventana.resizable(False, False)
        tk.Label(ventana, text="PERSONALIZA LOS CONTROLES", bg=COLORES["panel"], fg=COLORES["acento"], font=(FUENTE_HUD, 11, "bold")).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(ventana, text="Elige colores para los botones. Se aplican de inmediato a esta sesión.", bg=COLORES["panel"], fg=COLORES["suave"], font=(FUENTE_TEXTO, 9)).pack(anchor="w", padx=20, pady=(0, 14))
        for clave, etiqueta in (("boton", "Color normal"), ("boton_activo", "Al pasar el cursor"), ("boton_texto", "Texto de botones"), ("acento", "Acento")):
            fila = tk.Frame(ventana, bg=COLORES["panel"])
            fila.pack(fill="x", padx=20, pady=4)
            tk.Label(fila, text=etiqueta, width=22, anchor="w", bg=COLORES["panel"], fg=COLORES["texto"], font=(FUENTE_TEXTO, 9)).pack(side="left")
            self._boton(fila, "Elegir color", lambda c=clave: self._elegir_color(c)).pack(side="right")
        self._boton(ventana, "Cerrar", ventana.destroy).pack(anchor="e", padx=20, pady=(16, 18))

    def _elegir_color(self, clave):
        _nombre, color = colorchooser.askcolor(color=COLORES[clave], parent=self.raiz, title="Selecciona un color")
        if not color:
            return
        COLORES[clave] = color
        if clave == "acento":
            HUD["acento"] = color
        for boton in self._botones:
            if boton.winfo_exists():
                boton.config(bg=COLORES["boton"], activebackground=COLORES["boton_activo"], fg=COLORES["boton_texto"])
        self._dibujar_encabezado()

    def _agregar(self, autor, texto, etiqueta):
        self.chat.configure(state="normal")
        if etiqueta == "sistema":
            self.chat.insert("end", f"— {texto} —\n", etiqueta)
        else:
            self.chat.insert("end", f"{autor.upper()}\n", f"autor_{etiqueta}")
            self.chat.insert("end", f"{texto}\n", etiqueta)
        self.chat.configure(state="disabled")
        self.chat.see("end")
        self.conteo_mensajes += 1
        if hasattr(self, "_texto_pie"):
            self._pista("")
        if hasattr(self, "orbe"):
            if etiqueta == "usuario":
                self.orbe.fijar_subtitulos(texto, "")
            elif etiqueta == "bark":
                self.orbe.fijar_subtitulos(bark=texto)

    def _enviar(self):
        mensaje = self.campo.get().strip()
        if not mensaje or self._placeholder_activo:
            return
        self.campo.delete(0, "end")
        self._placeholder_activo = False
        if self.sesion_live and self.sesion_live.activa:
            # Con Gemini Live activo, lo escrito va a la misma conversación y responde con voz.
            self._agregar("Tú", mensaje, "usuario")
            try:
                self.sesion_live.enviar_texto(mensaje)
            except Exception as error:
                self._agregar("Sistema", f"No pude enviarlo a Gemini Live: {error}", "sistema")
            return
        # Una conversación tiene orden. Bloquear el envío evita respuestas
        # cruzadas si el usuario pulsa Enter varias veces mientras se infiere.
        self.campo.configure(state="disabled")
        self._agregar("Tú", mensaje, "usuario")
        self._cambiar_estado("pensando")
        threading.Thread(target=self._procesar_en_hilo, args=(mensaje,), daemon=True).start()

    def _procesar_en_hilo(self, mensaje):
        try:
            respuesta = procesar_mensaje(mensaje)
        except Exception as error:
            respuesta = f"Ocurrió un error al procesar el mensaje: {error}"
        voz_en_curso = self.hilo_voz and self.hilo_voz.is_alive()
        def terminar():
            self._agregar("Bark", respuesta, "bark")
            self._cambiar_estado("hablando" if self.lectura_activa else ("escuchando" if voz_en_curso else "en espera"))
            self.campo.configure(state="normal")
            self.campo.focus_set()
        self.raiz.after(0, terminar)
        if self.lectura_activa:
            def leer():
                hablar(respuesta)
                self.raiz.after(0, lambda: self._cambiar_estado("escuchando" if self.hilo_voz and self.hilo_voz.is_alive() else "en espera")
                                if self.estado == "hablando" else None)
            threading.Thread(target=leer, daemon=True).start()

    def _pedir_permiso(self, descripcion):
        resultado = threading.Event()
        respuesta = {"valor": False}
        def dialogo():
            respuesta["valor"] = messagebox.askyesno("Permiso requerido", f"Bark quiere realizar:\n\n{descripcion}\n\n¿Autorizas?")
            resultado.set()
        self.raiz.after(0, dialogo)
        resultado.wait()
        return respuesta["valor"]

    # ============================================================
    # Permisos (Windows + consentimiento del usuario)
    # ============================================================

    def _permiso_dispositivo(self, clave: str, capacidad: str, explicacion: str) -> bool:
        """Pide el permiso la primera vez y comprueba que Windows no bloquee el dispositivo."""
        if permisos.consentimiento(clave) is not True:
            if not messagebox.askyesno("Permiso de Bark", explicacion + "\n\n¿Permites que Bark lo use?"):
                permisos.guardar_consentimiento(clave, False)
                self._agregar("Sistema", "Permiso no concedido. Puedes cambiarlo en «Permisos de micrófono y cámara».", "sistema")
                return False
            permisos.guardar_consentimiento(clave, True)
        if permisos.estado_windows(capacidad) == "denegado":
            if messagebox.askyesno("Windows bloquea el acceso", permisos.explicar_bloqueo(capacidad) + "\n\n¿Abro esa configuración ahora?"):
                permisos.abrir_ajustes_privacidad(capacidad)
            self._agregar("Sistema", permisos.explicar_bloqueo(capacidad), "sistema")
            return False
        return True

    def _ver_permisos(self):
        ventana = self._ventana_secundaria()
        ventana.title("Permisos de Bark")
        ventana.geometry("520x420")
        marco = tk.Frame(ventana, bg=COLORES["fondo"], padx=18, pady=16)
        marco.pack(fill="both", expand=True)
        tk.Label(marco, text="PERMISOS", bg=COLORES["fondo"], fg=COLORES["acento"], font=(FUENTE_HUD, 12, "bold")).pack(anchor="w")
        estado = tk.Label(marco, justify="left", wraplength=470, bg=COLORES["fondo"], fg=COLORES["texto"], font=(FUENTE_TEXTO, 10))
        estado.pack(anchor="w", pady=(8, 12))

        def refrescar():
            estado.config(text=permisos.resumen() + "\n\nTodo el audio y la cámara se procesan en este equipo.")

        def conceder(clave, valor):
            permisos.guardar_consentimiento(clave, valor)
            refrescar()

        def automatico():
            seleccionar_dispositivo_entrada(None)
            self.microfono.set(AUTOMATICO_MIC)
            refrescar()

        for texto, accion in (
            ("Permitir micrófono", lambda: conceder("microfono", True)),
            ("Quitar permiso de micrófono", lambda: conceder("microfono", False)),
            ("Permitir cámara", lambda: conceder("camara", True)),
            ("Quitar permiso de cámara", lambda: conceder("camara", False)),
            ("Volver a detectar el micrófono", automatico),
            ("Abrir privacidad del micrófono en Windows", lambda: permisos.abrir_ajustes_privacidad("microphone")),
            ("Abrir privacidad de la cámara en Windows", lambda: permisos.abrir_ajustes_privacidad("webcam")),
            ("Abrir Sonido › Grabación (silencio y niveles)", permisos.abrir_ajustes_grabacion),
        ):
            self._boton(marco, texto, accion).pack(fill="x", pady=2)
        refrescar()

    def _texto_equipo(self) -> str:
        equipo = config.EQUIPO
        texto = (f"{equipo.ram_total_gb:.0f} GB RAM · {equipo.nucleos_fisicos} núcleos"
                 + (f" · {equipo.gpu} {equipo.vram_gb:.0f} GB" if equipo.gpu else "")
                 + f"\nModelo local: {config.NOMBRE_MODELO}")
        if config.FALTA_MODELO_RECOMENDADO:
            texto += f"\nRecomendado para tu equipo: {config.MODELO_RECOMENDADO.nombre}"
        return texto

    def _descargar_en_hilo(self, trabajo, al_terminar=None):
        def ejecutar():
            avisar = lambda texto: self.raiz.after(0, lambda texto=texto: self._agregar("Descarga", texto, "sistema"))
            try:
                trabajo(avisar)
            except Exception as error:
                avisar(f"No se pudo descargar: {error}")
            finally:
                if al_terminar:
                    self.raiz.after(0, al_terminar)
        threading.Thread(target=ejecutar, name="bark-descarga", daemon=True).start()

    def _descargar_modelo(self):
        modelo = config.MODELO_RECOMENDADO
        if not messagebox.askyesno(
            "Descargar modelo local",
            f"Tu equipo tiene {config.EQUIPO.ram_total_gb:.0f} GB de RAM. El modelo adecuado es:\n\n"
            f"{modelo.nombre} · {modelo.tamano_gb:.1f} GB\n{modelo.descripcion}\n\n"
            f"Se descargará de huggingface.co/{modelo.repo} a la carpeta modelos/. ¿Continuar?",
        ):
            return
        self.boton_modelo.config(text="Descargando modelo…", state="disabled")
        self._descargar_en_hilo(lambda avisar: descargas.descargar_modelo(modelo, avisar),
                                lambda: self.boton_modelo.config(text="Modelo descargado · reinicia Bark"))

    def _descargar_voces(self):
        pendientes = descargas.voces_por_descargar()
        if not pendientes:
            self._agregar("Sistema", "Ya tienes las voces recomendadas para tu equipo. Elige una en «Local sin internet · Piper».", "sistema")
            return
        lista = "\n".join(f"• {v.clave} (~{v.tamano_mb} MB): {v.descripcion}" for v in pendientes)
        if not messagebox.askyesno("Descargar voces", f"Voces recomendadas para tu equipo:\n\n{lista}\n\n"
                                   "Se descargarán de huggingface.co/rhasspy/piper-voices. ¿Continuar?"):
            return

        def trabajo(avisar):
            for voz in pendientes:
                descargas.descargar_voz(voz, avisar)

        self._descargar_en_hilo(trabajo, self._refrescar_voces)

    def _refrescar_voces(self):
        menu = self.selector_voz_local["menu"]
        menu.delete(0, "end")
        for nombre in voces_locales_disponibles():
            menu.add_command(label=nombre, command=lambda n=nombre: (self.voz_seleccionada.set(n), self._cambiar_voz(n)))
        self._agregar("Sistema", "Voces actualizadas: elige la nueva en «Local sin internet · Piper».", "sistema")

    # ============================================================
    # Voz y visión
    # ============================================================

    def _voz_activa(self) -> bool:
        return bool((self.hilo_voz and self.hilo_voz.is_alive()) or (self.sesion_live and self.sesion_live.activa))

    def _alternar_voz(self):
        if self.sesion_live and self.sesion_live.activa:
            self._live_parado_por_usuario = True
            self.sesion_live.detener()
            self.boton_voz.config(text="Deteniendo voz…", state="disabled")
            self._cambiar_estado("suspendido")
            return
        if self.hilo_voz and self.hilo_voz.is_alive():
            self.detener_voz.set()
            audio_vivo.interrumpir()
            self.boton_voz.config(text="Deteniendo voz…", state="disabled")
            self._cambiar_estado("suspendido")
            return
        if not self._permiso_dispositivo(
            "microfono", "microphone",
            "Para el modo voz, Bark necesita escuchar tu micrófono. El audio se transcribe en este equipo "
            "con Whisper y no se guarda ni se envía a internet.",
        ):
            return
        if self.usar_gemini_live.get() and gemini_live.disponible():
            self._iniciar_gemini_live()
            return
        self._iniciar_voz_clasica()

    def _iniciar_voz_clasica(self):
        self.detener_voz.clear()
        self.boton_voz.config(text="■  DETENER VOZ")
        self._cambiar_estado("escuchando")
        self.hilo_voz = threading.Thread(target=self._voz_en_hilo, daemon=True)
        self.hilo_voz.start()

    def _iniciar_gemini_live(self):
        """Conversación voz a voz con Gemini Live; las acciones del equipo pasan por el agente de Bark."""
        despachar = lambda funcion: self.raiz.after(0, funcion)

        def al_turno(usuario, bark):
            if usuario or bark:
                try:
                    guardar_turno(usuario or "(texto)", bark)  # la Biblioteca también guarda lo hablado en Live
                except Exception:
                    pass
            despachar(lambda: (usuario and self._agregar("Tú · voz", usuario, "usuario"),
                               bark and self._agregar("Bark · Live", bark, "bark")))

        def al_terminar(error):
            def actualizar():
                self.boton_voz.config(text="●  ACTIVAR VOZ", state="normal")
                self._cambiar_estado("en espera")
                if error and not self._live_parado_por_usuario:
                    self._agregar("Sistema", f"{error} Sigo con el modo de voz clásico (Whisper + voz de Bark).", "sistema")
                    self._iniciar_voz_clasica()
            despachar(actualizar)

        self._live_parado_por_usuario = False
        self.sesion_live = gemini_live.SesionGeminiLive(
            ejecutar_orden=procesar_mensaje, al_turno=al_turno,
            al_subtitulos=lambda u, b: despachar(lambda: self.orbe.fijar_subtitulos(u, b)),
            al_evento=lambda texto: despachar(lambda: self._agregar("Live", texto, "sistema")),
            al_terminar=al_terminar, modelo=self.modelo_live.get(),
        )
        try:
            self.sesion_live.iniciar()
        except gemini_live.LiveNoDisponible as error:
            self._agregar("Sistema", f"{error} Uso el modo de voz clásico.", "sistema")
            self._iniciar_voz_clasica()
            return
        self.boton_voz.config(text="■  DETENER VOZ")
        self._cambiar_estado("escuchando")
        self._agregar("Sistema", "Conectando con Gemini Live…", "sistema")

    def _cambiar_gemini_live(self):
        if self.usar_gemini_live.get() and not gemini_live.disponible():
            self.usar_gemini_live.set(False)
            self._agregar("Sistema", "Gemini Live necesita GEMINI_API_KEY en .env y la librería websockets.", "sistema")
            return
        permisos.guardar_preferencia("gemini_live", self.usar_gemini_live.get())
        estado = ("activado: hablas y Gemini te responde con su voz en tiempo real; Bark ejecuta las acciones del equipo"
                  if self.usar_gemini_live.get() else "desactivado: se usa Whisper + la voz elegida")
        aviso = " Se aplica la próxima vez que actives la voz." if self._voz_activa() else ""
        self._agregar("Sistema", f"Gemini Live {estado}.{aviso}", "sistema")
        self._refrescar_motores()

    def _cambiar_modelo_live(self, modelo):
        config.GEMINI_MODELO_LIVE = modelo
        self._agregar("Sistema", f"Modelo de Gemini Live: {modelo} (se aplica al activar la voz).", "sistema")

    def _alternar_vision(self):
        if self.vision.activa:
            self.vision.detener()
            self.boton_vision.config(text="Cerrando visión…", state="disabled")
            self.raiz.after(600, self._actualizar_boton_vision)
            return
        if not self._permiso_dispositivo(
            "camara", "webcam",
            "La visión local usa tu cámara para reconocer gestos (palma = voz, puño = suspender). "
            "Los fotogramas se procesan en este equipo y nunca se guardan ni se envían.",
        ):
            return
        try:
            self.vision.iniciar()
            self.boton_vision.config(text="Detener visión local")
            self._agregar("Sistema", "Visión local activada. La cámara se procesa solo en este equipo; pulsa Esc en su ventana para cerrarla.", "sistema")
        except VisionNoDisponible as error:
            self._agregar("Sistema", str(error), "sistema")

    def _actualizar_boton_vision(self):
        if self.vision.activa:
            self.raiz.after(300, self._actualizar_boton_vision)
            return
        self.boton_vision.config(text="Activar visión local", state="normal")

    def _estado_vision(self, texto):
        self.raiz.after(0, lambda: self._agregar("Visión", texto, "sistema"))
        if "detenida" in texto.lower():
            self.raiz.after(0, self._actualizar_boton_vision)

    def _gesto_recibido(self, gesto):
        def aplicar():
            if gesto == "activar_voz" and not self._voz_activa():
                self._agregar("Visión", "Palma detectada: activando modo de voz.", "sistema")
                self._alternar_voz()
            elif gesto == "suspender_voz" and self._voz_activa():
                self._agregar("Visión", "Puño detectado: suspendiendo voz.", "sistema")
                self._alternar_voz()
        self.raiz.after(0, aplicar)

    def _cambiar_voz(self, nombre):
        try:
            seleccionar_voz_local(nombre)
            self._agregar("Sistema", f"Voz local seleccionada: {config.NOMBRE_VOZ_TTS}.", "sistema")
            self._refrescar_motores()
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_motor_voz(self, motor):
        try:
            seleccionar_motor_voz(motor)
            permisos.guardar_preferencia("motor_voz", motor)
            textos = {
                "auto": "Cadena automática: Gemini → OpenAI → Piper.",
                "gemini": "Gemini primero; después OpenAI y Piper si falla.",
                "openai": "OpenAI (voz de ChatGPT) seleccionado; Piper será el respaldo si falla.",
                "local": "Voz local Piper seleccionada; no se usa la nube.",
            }
            self._agregar("Sistema", textos[motor], "sistema")
            self._refrescar_motores()
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_voz_gemini(self, _valor=None):
        try:
            configurar_voz_gemini(self.modelo_gemini.get(), self.voz_gemini.get())
            self._agregar("Sistema", f"Voz principal Gemini: {config.GEMINI_MODELO_TTS} · {config.GEMINI_VOZ}.", "sistema")
            self._refrescar_motores()
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_voz_nube(self, _valor=None):
        try:
            configurar_voz_nube(self.modelo_tts.get(), self.voz_nube.get())
            self._agregar("Sistema", f"Voz de nube: {config.OPENAI_MODELO_TTS} · {config.OPENAI_VOZ}.", "sistema")
            self._refrescar_motores()
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_modelo_escucha(self, nombre):
        try:
            seleccionar_modelo_whisper(nombre)
            self._agregar("Sistema", f"Escucha configurada con Whisper {nombre}.", "sistema")
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_microfono(self, nombre):
        try:
            seleccionar_dispositivo_entrada(self.dispositivos_mic[nombre])
            self._agregar("Sistema", f"Micrófono: {nombre}.", "sistema")
        except (KeyError, ValueError) as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_lectura(self):
        self.lectura_activa = self.leer_respuestas.get()
        estado = "activada" if self.lectura_activa else "desactivada"
        self._agregar("Sistema", f"Lectura de respuestas del chat {estado}.", "sistema")

    def _cambiar_escucha_libre(self):
        config.ESCUCHA_LIBRE = self.escucha_libre.get()
        estado = "activado: Bark responde a órdenes, preguntas y seguimientos; ignora charla ambiental" if config.ESCUCHA_LIBRE else "desactivado: vuelve a requerir “Bark”"
        self._agregar("Sistema", f"Escucha libre {estado}.", "sistema")

    def _voz_en_hilo(self):
        try:
            configurar_solicitador_permiso(solicitar_permiso_por_voz)
            self.raiz.after(0, lambda: self._agregar("Sistema", "Modo voz iniciado. Habla con naturalidad; toca el orbe o pulsa Esc para interrumpir.", "sistema"))
            ejecutar_modo_voz(
                procesar_mensaje,
                self._estado_desde_voz,
                self.detener_voz,
                al_transcripcion=self._transcripcion_desde_voz,
                al_respuesta=self._respuesta_desde_voz,
                al_evento=self._evento_desde_voz,
                procesar_toda_transcripcion=True,
            )
        except VozNoDisponible as error:
            # Las variables de ``except`` se eliminan al terminar el bloque en
            # Python. El callback de Tk corre después, así que conservamos el
            # texto antes de programarlo para que el error de voz sea visible.
            mensaje_error = str(error)
            self.raiz.after(0, lambda mensaje=mensaje_error: self._agregar("Sistema", mensaje, "sistema"))
        finally:
            configurar_solicitador_permiso(self._pedir_permiso)
            try:
                self.raiz.after(0, lambda: (self.boton_voz.config(text="●  ACTIVAR VOZ", state="normal"), self._cambiar_estado("en espera")))
            except (tk.TclError, RuntimeError):
                pass

    def _estado_desde_voz(self, estado):
        self.raiz.after(0, lambda: self._cambiar_estado(estado))

    def _transcripcion_desde_voz(self, texto):
        """Refleja en el chat (y en los subtítulos del orbe) exactamente lo que Whisper oyó."""
        self.raiz.after(0, lambda texto=texto: self._agregar("Tú · voz", texto, "usuario"))

    def _respuesta_desde_voz(self, texto):
        self.raiz.after(0, lambda texto=texto: self._agregar("Bark", texto, "bark"))

    def _evento_desde_voz(self, texto):
        self.raiz.after(0, lambda texto=texto: self._agregar("Voz", texto, "sistema"))

    def _cambiar_estado(self, estado):
        self.estado = estado
        self.descripcion_estado.config(text=ESTADOS.get(estado, ESTADOS["en espera"])[2])
        self._dibujar_encabezado()

    def _ver_memoria(self):
        recuerdos = buscar_recuerdos("", limite=100, incluir_sensibles=True)
        texto = "\n".join(f"• [{r['categoria']}] {r['contenido']}" for r in recuerdos) or "No hay recuerdos guardados."
        self._agregar("Memoria", texto, "sistema")

    def _abrir_biblioteca(self):
        ventana = self._ventana_secundaria()
        ventana.title("Biblioteca de Bark")
        ventana.geometry("820x620")
        periodo = tk.StringVar(value="hoy")
        consulta = tk.StringVar()
        barra = tk.Frame(ventana, bg=COLORES["fondo"], padx=16, pady=14)
        barra.pack(fill="x")
        stats = estadisticas_biblioteca()
        tk.Label(barra, text=f"BIBLIOTECA  ·  {stats['total']} turnos  ·  {stats['prioridad']} recuerdos prioritarios", bg=COLORES["fondo"], fg=COLORES["acento"], font=(FUENTE_HUD, 11, "bold")).pack(anchor="w")
        controles = tk.Frame(barra, bg=COLORES["fondo"])
        controles.pack(fill="x", pady=(10, 0))
        visor = scrolledtext.ScrolledText(ventana, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"], relief="flat", font=(FUENTE_TEXTO, 10), padx=14, pady=12)
        visor.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        def actualizar(*_):
            turnos = biblioteca_conversaciones(periodo.get(), consulta.get(), limite=1000)
            contenido = "\n\n".join(f"[{t['creado_en']}]\nTú: {t['usuario']}\nBark: {t['chet']}" for t in turnos) or "No hay conversaciones para este filtro."
            visor.configure(state="normal")
            visor.delete("1.0", "end")
            visor.insert("end", contenido)
            visor.configure(state="disabled")

        for valor, etiqueta in (("hoy", "Hoy"), ("semana", "Semana"), ("todo", "Todo")):
            tk.Radiobutton(controles, text=etiqueta, variable=periodo, value=valor, command=actualizar, bg=COLORES["fondo"], fg=COLORES["texto"], selectcolor=COLORES["panel2"], activebackground=COLORES["fondo"]).pack(side="left", padx=(0, 8))
        entrada = tk.Entry(controles, textvariable=consulta, bg=COLORES["panel2"], fg=COLORES["texto"], insertbackground=COLORES["texto"], relief="flat")
        entrada.pack(side="left", fill="x", expand=True, padx=(12, 8))
        entrada.bind("<Return>", actualizar)
        self._boton(controles, "Buscar", actualizar).pack(side="right")
        actualizar()

    def _segundo_plano(self):
        from nucleo.servicio import iniciar_en_segundo_plano
        self._agregar("Sistema", iniciar_en_segundo_plano(), "sistema")

    def _diagnostico(self):
        self._agregar("Bark", comandos.diagnosticar_equipo(), "bark")

    def _probar_microfono(self):
        from nucleo.escuchar import diagnosticar_microfono
        if not self._permiso_dispositivo("microfono", "microphone",
                                         "Para probar el micrófono, Bark grabará 3 segundos. El audio no se guarda."):
            return
        self._agregar("Sistema", "Probando todos los micrófonos a la vez durante 3 segundos: habla ahora («hola Bark, ¿me escuchas?»).", "sistema")
        def probar():
            resultado = diagnosticar_microfono()
            self.raiz.after(0, lambda: self._agregar("Sistema", resultado, "sistema"))
        threading.Thread(target=probar, daemon=True).start()

    def _reiniciar_bark(self):
        if not messagebox.askyesno(
            "Reiniciar Bark",
            "Se detendrá el modo voz y se reiniciará Bark. El historial guardado no se borra. ¿Continuar?",
        ):
            return
        self.detener_voz.set()
        self.medios.detener()
        ruta_main = os.path.join(config.CARPETA_BASE, "main.py")
        os.execv(sys.executable, [sys.executable, ruta_main, "--ui"])

    def ejecutar(self):
        self.raiz.mainloop()


def iniciar_interfaz():
    VentanaBark().ejecutar()
