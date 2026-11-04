"""Interfaz translúcida y futurista de Bark, construida únicamente con Tkinter."""

import os
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, messagebox, scrolledtext

from acciones import comandos
from config import config
from nucleo.bd_manager import biblioteca_conversaciones, buscar_recuerdos, estadisticas_biblioteca
from nucleo.cerebro import configurar_solicitador_permiso, procesar_mensaje
from nucleo.escuchar import (
    VozNoDisponible, configurar_voz_gemini, configurar_voz_nube, dispositivos_entrada, ejecutar_modo_voz,
    hablar, modelos_whisper_disponibles, seleccionar_dispositivo_entrada,
    seleccionar_modelo_whisper, seleccionar_motor_voz, seleccionar_voz_local,
    solicitar_permiso_por_voz, voces_locales_disponibles,
)
from nucleo.proveedores import estado_proveedores
from nucleo.vision import VisionLocal, VisionNoDisponible


COLORES = {
    "fondo": "#050716", "panel": "#0a1024", "panel2": "#111a33",
    "panel_claro": "#162241", "borde": "#263b67", "texto": "#edf5ff",
    "suave": "#91a8ca", "acento": "#6cf7ed", "acento2": "#7c75ff",
    "usuario": "#152e57", "bark": "#10213b", "alerta": "#ffd56a",
    "boton": "#1b2d58", "boton_activo": "#564ff2", "boton_texto": "#ffffff",
    "titulo": "#080d20", "exito": "#62eab4",
}


class VentanaBark:
    def __init__(self):
        self.raiz = tk.Tk()
        self.raiz.title("Bark — Asistente personal")
        self.raiz.geometry("1240x780")
        self.raiz.minsize(900, 600)
        self.raiz.configure(bg=COLORES["fondo"])
        # Una barra propia evita el borde claro del sistema y mantiene la
        # ventana totalmente visible. También permite arrastrarla libremente.
        self.raiz.overrideredirect(True)
        self.raiz.attributes("-alpha", 1.0)
        self._ventana_maximizada = False
        self._geometria_anterior = None
        self._botones = []
        self.estado = "en espera"
        self.fase = 0
        self.tiempo_inicio = 0
        self._placeholder_activo = True
        self.conteo_mensajes = 0
        self.hilo_voz = None
        self.detener_voz = threading.Event()
        self.vision = VisionLocal(self._gesto_recibido, self._estado_vision)
        self.voz_seleccionada = tk.StringVar(value=os.path.basename(config.RUTA_MODELO_TTS))
        self.motor_voz = tk.StringVar(value=config.MODO_VOZ if config.MODO_VOZ in {"auto", "gemini", "openai", "local"} else "auto")
        self.modelo_gemini = tk.StringVar(value=config.GEMINI_MODELO_TTS)
        self.voz_gemini = tk.StringVar(value=config.GEMINI_VOZ)
        self.modelo_tts = tk.StringVar(value=config.OPENAI_MODELO_TTS)
        self.voz_nube = tk.StringVar(value=config.OPENAI_VOZ)
        self.modelo_escucha = tk.StringVar(value=config.MODELO_WHISPER)
        self.leer_respuestas = tk.BooleanVar(value=True)
        self.lectura_activa = True
        self.escucha_libre = tk.BooleanVar(value=config.ESCUCHA_LIBRE)
        self.dispositivos_mic = {"Predeterminado de Windows": None}
        try:
            for dispositivo in dispositivos_entrada():
                self.dispositivos_mic[f"{dispositivo['indice']} · {dispositivo['nombre']}"] = dispositivo["indice"]
        except Exception:
            pass
        self.microfono = tk.StringVar(value=next((nombre for nombre, indice in self.dispositivos_mic.items() if indice == config.DISPOSITIVO_ENTRADA_VOZ), "Predeterminado de Windows"))
        configurar_solicitador_permiso(self._pedir_permiso)
        self._construir()
        self._animar()

    def _construir(self):
        self._construir_barra_ventana()
        barra = tk.Frame(self.raiz, bg=COLORES["fondo"], padx=30, pady=17)
        barra.pack(fill="x")
        marca = tk.Frame(barra, bg=COLORES["fondo"])
        marca.pack(side="left")
        tk.Label(marca, text="BARK", font=("Segoe UI", 25, "bold"), fg=COLORES["acento"], bg=COLORES["fondo"]).pack(anchor="w")
        tk.Label(marca, text="PERSONAL INTELLIGENCE SYSTEM  ·  LOCAL FIRST", font=("Segoe UI", 8, "bold"), fg=COLORES["suave"], bg=COLORES["fondo"]).pack(anchor="w")
        self.etiqueta_estado = tk.Label(barra, text="●  SISTEMA LISTO", font=("Segoe UI", 9, "bold"), fg=COLORES["exito"], bg=COLORES["panel2"], padx=13, pady=7)
        self.etiqueta_estado.pack(side="right")

        cuerpo = tk.Frame(self.raiz, bg=COLORES["fondo"], padx=24, pady=8)
        cuerpo.pack(fill="both", expand=True)
        # La configuración completa cabe también en portátiles: el lateral es
        # un panel de vidrio desplazable en lugar de ocultar controles abajo.
        lateral_contenedor = tk.Frame(cuerpo, bg=COLORES["panel"], highlightbackground=COLORES["borde"], highlightthickness=1, width=305)
        lateral_contenedor.pack(side="left", fill="y", padx=(0, 14))
        lateral_contenedor.pack_propagate(False)
        canvas_lateral = tk.Canvas(lateral_contenedor, bg=COLORES["panel"], highlightthickness=0, borderwidth=0)
        barra_lateral = tk.Scrollbar(lateral_contenedor, orient="vertical", command=canvas_lateral.yview)
        canvas_lateral.configure(yscrollcommand=barra_lateral.set)
        barra_lateral.pack(side="right", fill="y")
        canvas_lateral.pack(side="left", fill="both", expand=True)
        lateral = tk.Frame(canvas_lateral, bg=COLORES["panel"], padx=18, pady=18)
        ventana_lateral = canvas_lateral.create_window((0, 0), window=lateral, anchor="nw")
        lateral.bind("<Configure>", lambda _evento: canvas_lateral.configure(scrollregion=canvas_lateral.bbox("all")))
        canvas_lateral.bind("<Configure>", lambda evento: canvas_lateral.itemconfigure(ventana_lateral, width=evento.width))
        canvas_lateral.bind("<Enter>", lambda _evento: canvas_lateral.bind_all("<MouseWheel>", lambda evento: canvas_lateral.yview_scroll(-1 * (evento.delta // 120), "units")))
        canvas_lateral.bind("<Leave>", lambda _evento: canvas_lateral.unbind_all("<MouseWheel>"))

        self.orbita = tk.Canvas(lateral, width=250, height=196, bg=COLORES["panel"], highlightthickness=0)
        self.orbita.pack(pady=(2, 10))
        tk.Label(lateral, text="NÚCLEO BARK", font=("Segoe UI", 14, "bold"), fg=COLORES["texto"], bg=COLORES["panel"]).pack()
        self.descripcion_estado = tk.Label(lateral, text="En espera de “Bark”", wraplength=240, font=("Segoe UI", 10), fg=COLORES["suave"], bg=COLORES["panel"])
        self.descripcion_estado.pack(pady=(6, 14))
        self._etiqueta_pequena(lateral, "MICRÓFONO DE ENTRADA")
        self._selector(lateral, self.microfono, tuple(self.dispositivos_mic), self._cambiar_microfono).pack(fill="x", pady=(2, 8))
        self.boton_voz = self._boton(lateral, "Activar voz", self._alternar_voz)
        self.boton_voz.pack(fill="x", pady=4)
        self.boton_vision = self._boton(lateral, "Activar visión local", self._alternar_vision)
        self.boton_vision.pack(fill="x", pady=4)
        tk.Label(lateral, text="Cámara privada · palma = voz · puño = suspender", wraplength=240,
                 font=("Segoe UI", 8), fg=COLORES["suave"], bg=COLORES["panel"]).pack(anchor="w", pady=(0, 7))
        tk.Checkbutton(lateral, text="Leer respuestas del chat", variable=self.leer_respuestas,
                       command=self._cambiar_lectura, bg=COLORES["panel"], fg=COLORES["texto"],
                       selectcolor=COLORES["panel2"], activebackground=COLORES["panel"],
                       activeforeground=COLORES["acento"], font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 8))
        tk.Checkbutton(lateral, text="Modo conversacional · Bark opcional", variable=self.escucha_libre,
                       command=self._cambiar_escucha_libre, bg=COLORES["panel"], fg=COLORES["texto"],
                       selectcolor=COLORES["panel2"], activebackground=COLORES["panel"],
                       activeforeground=COLORES["acento"], font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))
        self._etiqueta_seccion(lateral, "CONFIGURACIÓN DE VOZ")
        self._selector(lateral, self.motor_voz, ("auto", "gemini", "openai", "local"), self._cambiar_motor_voz).pack(fill="x", pady=(2, 6))
        self._etiqueta_pequena(lateral, "PRIMARIA GEMINI · MODELO")
        self._selector(lateral, self.modelo_gemini, config.MODELOS_TTS_GEMINI, self._cambiar_voz_gemini).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "PRIMARIA GEMINI · VOZ")
        self._selector(lateral, self.voz_gemini, config.VOCES_GEMINI, self._cambiar_voz_gemini).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "RESPALDO OPENAI · MODELO")
        self._selector(lateral, self.modelo_tts, config.MODELOS_TTS_OPENAI, self._cambiar_voz_nube).pack(fill="x", pady=(2, 5))
        self._etiqueta_pequena(lateral, "RESPALDO OPENAI · VOZ")
        self._selector(lateral, self.voz_nube, config.VOCES_OPENAI, self._cambiar_voz_nube).pack(fill="x", pady=(2, 5))
        voces = voces_locales_disponibles() or [self.voz_seleccionada.get()]
        self._etiqueta_pequena(lateral, "RESPUESTA LOCAL · PIPER")
        self._selector(lateral, self.voz_seleccionada, voces, self._cambiar_voz).pack(fill="x", pady=(2, 8))
        self._etiqueta_seccion(lateral, "RECONOCIMIENTO")
        self._etiqueta_pequena(lateral, "MODELO WHISPER")
        self._selector(lateral, self.modelo_escucha, modelos_whisper_disponibles(), self._cambiar_modelo_escucha).pack(fill="x", pady=(2, 5))
        self._boton(lateral, "Ejecutar en segundo plano", self._segundo_plano).pack(fill="x", pady=4)
        self._boton(lateral, "Biblioteca", self._abrir_biblioteca).pack(fill="x", pady=4)
        self._boton(lateral, "Ver memoria", self._ver_memoria).pack(fill="x", pady=4)
        self._boton(lateral, "Diagnóstico del equipo", self._diagnostico).pack(fill="x", pady=4)
        self._etiqueta_seccion(lateral, "OPTIMIZACIÓN LOCAL")
        self._boton(lateral, "Centro de optimización", self._abrir_centro_optimizacion).pack(fill="x", pady=4)
        self._boton(lateral, "Analizar consumo de RAM", lambda: self._enviar_consulta("Diagnóstico de RAM")).pack(fill="x", pady=4)
        self._boton(lateral, "Liberar RAM avanzada", lambda: self._enviar_consulta("Liberar RAM avanzada")).pack(fill="x", pady=4)
        self._boton(lateral, "Ver programas de inicio", lambda: self._enviar_consulta("Mostrar programas de inicio")).pack(fill="x", pady=4)
        self._boton(lateral, "Limpiar temporales", lambda: self._enviar_consulta("Limpiar temporales")).pack(fill="x", pady=4)
        self._boton(lateral, "Probar micrófono", self._probar_microfono).pack(fill="x", pady=4)
        self._boton(lateral, "Personalizar colores", self._personalizar_colores).pack(fill="x", pady=(12, 4))
        self._boton(lateral, "Reiniciar Bark", self._reiniciar_bark).pack(fill="x", pady=(16, 4))
        self.etiqueta_proveedor = tk.Label(lateral, text=estado_proveedores(), justify="left", wraplength=190, font=("Segoe UI", 8), fg=COLORES["suave"], bg=COLORES["panel"])
        self.etiqueta_proveedor.pack(side="bottom", anchor="w")

        principal = tk.Frame(cuerpo, bg=COLORES["panel"], highlightbackground=COLORES["borde"], highlightthickness=1, padx=20, pady=18)
        principal.pack(side="left", fill="both", expand=True)
        cabecera_chat = tk.Frame(principal, bg=COLORES["panel"])
        cabecera_chat.pack(fill="x", pady=(0, 12))
        tk.Label(cabecera_chat, text="CONVERSACIÓN", font=("Segoe UI", 16, "bold"), fg=COLORES["texto"], bg=COLORES["panel"]).pack(side="left")
        tk.Label(cabecera_chat, text="LIVE SESSION", font=("Segoe UI", 8, "bold"), fg=COLORES["acento"], bg=COLORES["panel2"], padx=9, pady=5).pack(side="left", padx=10)
        self.metricas = tk.Label(cabecera_chat, text="LOCAL · PRIVADO · 0 MENSAJES", font=("Segoe UI", 8, "bold"), fg=COLORES["suave"], bg=COLORES["panel"])
        self.metricas.pack(side="right", pady=5)
        sugerencias = tk.Frame(principal, bg=COLORES["panel"])
        sugerencias.pack(fill="x", pady=(0, 12))
        tk.Label(sugerencias, text="ACCIONES RÁPIDAS", font=("Segoe UI", 7, "bold"), fg=COLORES["suave"], bg=COLORES["panel"]).pack(side="left", padx=(0, 8))
        for texto in ("¿Qué puedes hacer?", "Resume mi día", "Diagnostica el equipo"):
            self._chip(sugerencias, texto).pack(side="left", padx=(0, 6))
        self._chip(sugerencias, "Limpiar", accion=self._limpiar_chat, discreto=True).pack(side="right")
        self.chat = scrolledtext.ScrolledText(principal, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"], insertbackground=COLORES["acento"], relief="flat", font=("Segoe UI", 11), padx=16, pady=12)
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("usuario", foreground="#c8deff", background=COLORES["usuario"], lmargin1=85, lmargin2=85, rmargin=14, spacing1=12, spacing3=9)
        self.chat.tag_configure("bark", foreground=COLORES["texto"], background=COLORES["bark"], lmargin1=14, lmargin2=14, rmargin=85, spacing1=10, spacing3=9)
        self.chat.tag_configure("sistema", foreground=COLORES["suave"], justify="center", spacing1=8, spacing3=8)
        self._agregar("Bark", "Listo. Dime qué necesitas o activa el modo de voz. Puedo operar localmente incluso sin internet.", "bark")

        entrada = tk.Frame(principal, bg=COLORES["panel2"], highlightbackground=COLORES["borde"], highlightthickness=1, padx=12, pady=11)
        entrada.pack(fill="x", pady=(12, 0))
        tk.Label(entrada, text="›", bg=COLORES["panel2"], fg=COLORES["acento"], font=("Consolas", 18, "bold")).pack(side="left", padx=(1, 8))
        self.campo = tk.Entry(entrada, bg=COLORES["panel2"], fg=COLORES["suave"], insertbackground=COLORES["texto"], relief="flat", font=("Segoe UI", 11))
        self.campo.pack(side="left", fill="x", expand=True, padx=(4, 10))
        self.campo.bind("<Return>", lambda _: self._enviar())
        self.campo.bind("<FocusIn>", self._activar_campo)
        self.campo.bind("<FocusOut>", self._desactivar_campo)
        self.campo.insert(0, "Escribe un mensaje o usa una acción rápida…")
        self._boton(entrada, "ENVIAR  ↗", self._enviar).pack(side="right")
        self.raiz.bind("<Control-k>", lambda _evento: self._enfocar_campo())

        # Esquina de redimensionado para que la ventana sin marco siga siendo
        # cómoda de ajustar en monitores de cualquier tamaño.
        self.redimensionador = tk.Label(self.raiz, text="◢", bg=COLORES["fondo"], fg=COLORES["suave"], cursor="size_nw_se")
        self.redimensionador.place(relx=1, rely=1, anchor="se", x=-5, y=-3)
        self.redimensionador.bind("<Button-1>", self._iniciar_redimensionado)
        self.redimensionador.bind("<B1-Motion>", self._redimensionar)

    def _construir_barra_ventana(self):
        """Crea controles de ventana visibles para la interfaz sin marco."""
        barra = tk.Frame(self.raiz, bg=COLORES["titulo"], height=38, highlightthickness=1, highlightbackground=COLORES["borde"])
        barra.pack(fill="x")
        barra.pack_propagate(False)
        titulo = tk.Label(barra, text="  BARK  ·  ASISTENTE PERSONAL", bg=COLORES["titulo"], fg="#c9eeff", font=("Segoe UI", 9, "bold"), cursor="fleur")
        titulo.pack(side="left", fill="both", expand=True)
        for widget in (barra, titulo):
            widget.bind("<ButtonPress-1>", self._iniciar_arrastre)
            widget.bind("<B1-Motion>", self._arrastrar_ventana)
            widget.bind("<Double-Button-1>", lambda _evento: self._alternar_maximizar())

        self._control_ventana(barra, "—", self._minimizar).pack(side="right", fill="y")
        self._control_ventana(barra, "□", self._alternar_maximizar).pack(side="right", fill="y")
        self._control_ventana(barra, "×", self.raiz.destroy, cerrar=True).pack(side="right", fill="y")

    def _control_ventana(self, padre, texto, orden, cerrar=False):
        return tk.Button(padre, text=texto, command=orden, cursor="hand2", width=4, bg=COLORES["titulo"], fg="#ffffff", activeforeground="#ffffff", activebackground="#b3263c" if cerrar else COLORES["boton"], relief="flat", bd=0, font=("Segoe UI", 11, "bold"))

    def _iniciar_arrastre(self, evento):
        if self._ventana_maximizada:
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
        # Temporalmente se restaura el marco: Windows solo puede minimizar una
        # ventana decorada de forma fiable. Al volver, recupera el diseño limpio.
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
            ancho, alto = self.raiz.winfo_screenwidth(), self.raiz.winfo_screenheight()
            self.raiz.geometry(f"{ancho}x{alto}+0+0")
        self._ventana_maximizada = not self._ventana_maximizada

    def _iniciar_redimensionado(self, evento):
        self._tamano_inicial = (self.raiz.winfo_width(), self.raiz.winfo_height(), evento.x_root, evento.y_root)

    def _redimensionar(self, evento):
        ancho, alto, inicio_x, inicio_y = self._tamano_inicial
        nuevo_ancho = max(900, ancho + evento.x_root - inicio_x)
        nuevo_alto = max(600, alto + evento.y_root - inicio_y)
        self.raiz.geometry(f"{nuevo_ancho}x{nuevo_alto}")

    def _boton(self, padre, texto, orden):
        boton = tk.Button(padre, text=texto, command=orden, cursor="hand2", bg=COLORES["boton"], activebackground=COLORES["boton_activo"], fg=COLORES["boton_texto"], activeforeground=COLORES["titulo"], relief="flat", bd=0, highlightthickness=1, highlightbackground="#5bd7ff", font=("Segoe UI", 10, "bold"), padx=14, pady=9)
        boton.bind("<Enter>", lambda _evento, b=boton: b.config(bg=COLORES["boton_activo"]))
        boton.bind("<Leave>", lambda _evento, b=boton: b.config(bg=COLORES["boton"]))
        self._botones.append(boton)
        return boton

    def _chip(self, padre, texto, accion=None, discreto=False):
        """Crea una sugerencia compacta que mantiene el chat como foco central."""
        fondo = COLORES["panel"] if discreto else COLORES["panel_claro"]
        borde = COLORES["borde"] if discreto else COLORES["acento2"]
        boton = tk.Button(
            padre, text=texto, cursor="hand2", command=accion or (lambda t=texto: self._usar_sugerencia(t)),
            bg=fondo, fg=COLORES["suave"] if discreto else COLORES["texto"],
            activebackground=COLORES["boton_activo"], activeforeground="#ffffff",
            relief="flat", bd=0, highlightthickness=1, highlightbackground=borde,
            font=("Segoe UI", 8, "bold"), padx=9, pady=5,
        )
        boton.bind("<Enter>", lambda _evento, b=boton: b.config(bg=COLORES["boton_activo"], fg="#ffffff"))
        boton.bind("<Leave>", lambda _evento, b=boton, c=fondo: b.config(bg=c, fg=COLORES["suave"] if discreto else COLORES["texto"]))
        return boton

    def _activar_campo(self, _evento=None):
        if self._placeholder_activo:
            self.campo.delete(0, "end")
            self.campo.config(fg=COLORES["texto"])
            self._placeholder_activo = False

    def _desactivar_campo(self, _evento=None):
        if not self.campo.get().strip():
            self.campo.insert(0, "Escribe un mensaje o usa una acción rápida…")
            self.campo.config(fg=COLORES["suave"])
            self._placeholder_activo = True

    def _enfocar_campo(self):
        self.campo.focus_set()
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

    def _abrir_centro_optimizacion(self):
        ventana = tk.Toplevel(self.raiz)
        ventana.title("Centro de optimización · Bark")
        ventana.geometry("760x560")
        ventana.minsize(620, 440)
        ventana.configure(bg=COLORES["fondo"])
        cabecera = tk.Frame(ventana, bg=COLORES["panel"], padx=22, pady=18)
        cabecera.pack(fill="x")
        tk.Label(cabecera, text="CENTRO DE OPTIMIZACIÓN", bg=COLORES["panel"], fg=COLORES["acento"], font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(cabecera, text="Diagnóstico y mantenimiento local · Bark no elimina ni desactiva nada sin confirmación.", bg=COLORES["panel"], fg=COLORES["suave"], font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 0))
        acciones = tk.Frame(ventana, bg=COLORES["fondo"], padx=22, pady=14)
        acciones.pack(fill="x")
        visor = scrolledtext.ScrolledText(ventana, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"], relief="flat", font=("Segoe UI", 10), padx=15, pady=13)
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

    def _enviar_desde_centro(self, ventana, texto):
        if ventana.winfo_exists():
            ventana.destroy()
        self._enviar_consulta(texto)

    def _limpiar_chat(self):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.conteo_mensajes = 0
        self._agregar("Sistema", "Vista de conversación limpiada. Tu memoria guardada sigue intacta.", "sistema")

    def _personalizar_colores(self):
        ventana = tk.Toplevel(self.raiz)
        ventana.title("Colores de Bark")
        ventana.configure(bg=COLORES["panel"])
        ventana.resizable(False, False)
        tk.Label(ventana, text="PERSONALIZA LOS CONTROLES", bg=COLORES["panel"], fg=COLORES["acento"], font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(ventana, text="Elige colores para los botones. Se aplican de inmediato a esta sesión.", bg=COLORES["panel"], fg=COLORES["suave"], font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(0, 14))
        for clave, etiqueta in (("boton", "Color normal"), ("boton_activo", "Al pasar el cursor"), ("boton_texto", "Texto de botones"), ("acento", "Acento")):
            fila = tk.Frame(ventana, bg=COLORES["panel"])
            fila.pack(fill="x", padx=20, pady=4)
            tk.Label(fila, text=etiqueta, width=22, anchor="w", bg=COLORES["panel"], fg=COLORES["texto"], font=("Segoe UI", 9)).pack(side="left")
            self._boton(fila, "Elegir color", lambda c=clave: self._elegir_color(c)).pack(side="right")
        self._boton(ventana, "Cerrar", ventana.destroy).pack(anchor="e", padx=20, pady=(16, 18))

    def _elegir_color(self, clave):
        _nombre, color = colorchooser.askcolor(color=COLORES[clave], parent=self.raiz, title="Selecciona un color")
        if not color:
            return
        COLORES[clave] = color
        for boton in self._botones:
            if boton.winfo_exists():
                boton.config(bg=COLORES["boton"], activebackground=COLORES["boton_activo"], fg=COLORES["boton_texto"])
        self.etiqueta_estado.config(fg=COLORES["acento"])

    def _etiqueta_seccion(self, padre, texto):
        tk.Label(padre, text=f"── {texto}", font=("Segoe UI", 8, "bold"), fg=COLORES["acento"], bg=COLORES["panel"]).pack(anchor="w", pady=(10, 1))

    def _etiqueta_pequena(self, padre, texto):
        tk.Label(padre, text=texto, font=("Segoe UI", 7, "bold"), fg=COLORES["suave"], bg=COLORES["panel"]).pack(anchor="w")

    def _selector(self, padre, variable, opciones, al_cambiar):
        opciones = tuple(opciones) or (variable.get(),)
        selector = tk.OptionMenu(padre, variable, *opciones, command=al_cambiar)
        selector.config(bg=COLORES["panel2"], fg=COLORES["texto"], activebackground="#10445c", activeforeground=COLORES["acento"], relief="flat", bd=0, highlightthickness=1, highlightbackground=COLORES["borde"], font=("Segoe UI", 9))
        selector["menu"].config(bg=COLORES["panel2"], fg=COLORES["texto"], activebackground="#10445c", activeforeground=COLORES["acento"], relief="flat")
        return selector

    def _agregar(self, autor, texto, etiqueta):
        self.chat.configure(state="normal")
        self.chat.insert("end", f"{autor.upper()}\n", etiqueta)
        self.chat.insert("end", f"{texto}\n", etiqueta)
        self.chat.configure(state="disabled")
        self.chat.see("end")
        self.conteo_mensajes += 1
        if hasattr(self, "metricas"):
            self.metricas.config(text=f"LOCAL · PRIVADO · {self.conteo_mensajes} MENSAJES")

    def _enviar(self):
        mensaje = self.campo.get().strip()
        if not mensaje or self._placeholder_activo:
            return
        self.campo.delete(0, "end")
        self._placeholder_activo = False
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
        def terminar():
            self._agregar("Bark", respuesta, "bark")
            self._cambiar_estado("en espera")
            self.campo.configure(state="normal")
            self.campo.focus_set()
        self.raiz.after(0, terminar)
        if self.lectura_activa:
            threading.Thread(target=hablar, args=(respuesta,), daemon=True).start()

    def _pedir_permiso(self, descripcion):
        resultado = threading.Event()
        respuesta = {"valor": False}
        def dialogo():
            respuesta["valor"] = messagebox.askyesno("Permiso requerido", f"Bark quiere realizar:\n\n{descripcion}\n\n¿Autorizas?")
            resultado.set()
        self.raiz.after(0, dialogo)
        resultado.wait()
        return respuesta["valor"]

    def _alternar_voz(self):
        if self.hilo_voz and self.hilo_voz.is_alive():
            self.detener_voz.set()
            self.boton_voz.config(text="Deteniendo voz…", state="disabled")
            self._cambiar_estado("suspendido")
            return
        self.detener_voz.clear()
        self.boton_voz.config(text="Detener voz")
        self.hilo_voz = threading.Thread(target=self._voz_en_hilo, daemon=True)
        self.hilo_voz.start()

    def _alternar_vision(self):
        if self.vision.activa:
            self.vision.detener()
            self.boton_vision.config(text="Cerrando visión…", state="disabled")
            self.raiz.after(600, self._actualizar_boton_vision)
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
            if gesto == "activar_voz" and not (self.hilo_voz and self.hilo_voz.is_alive()):
                self._agregar("Visión", "Palma detectada: activando modo de voz.", "sistema")
                self._alternar_voz()
            elif gesto == "suspender_voz" and self.hilo_voz and self.hilo_voz.is_alive():
                self._agregar("Visión", "Puño detectado: suspendiendo voz.", "sistema")
                self.detener_voz.set()
        self.raiz.after(0, aplicar)

    def _cambiar_voz(self, nombre):
        try:
            seleccionar_voz_local(nombre)
            self._agregar("Sistema", f"Voz local seleccionada: {config.NOMBRE_VOZ_TTS}.", "sistema")
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_motor_voz(self, motor):
        try:
            seleccionar_motor_voz(motor)
            textos = {
                "auto": "Cadena automática: Gemini → OpenAI → Piper.",
                "gemini": "Gemini primero; después OpenAI y Piper si falla.",
                "openai": "OpenAI seleccionado; Piper será el respaldo si falla.",
                "local": "Voz local Piper seleccionada; no se usa la nube.",
            }
            self._agregar("Sistema", textos[motor], "sistema")
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_voz_gemini(self, _valor=None):
        try:
            configurar_voz_gemini(self.modelo_gemini.get(), self.voz_gemini.get())
            self._agregar("Sistema", f"Voz principal Gemini: {config.GEMINI_MODELO_TTS} · {config.GEMINI_VOZ}.", "sistema")
        except ValueError as error:
            self._agregar("Sistema", str(error), "sistema")

    def _cambiar_voz_nube(self, _valor=None):
        try:
            configurar_voz_nube(self.modelo_tts.get(), self.voz_nube.get())
            self._agregar("Sistema", f"Voz de nube: {config.OPENAI_MODELO_TTS} · {config.OPENAI_VOZ}.", "sistema")
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
            self.raiz.after(0, lambda: self._agregar("Sistema", "Modo voz iniciado. Habla: lo que se entienda aparecerá aquí.", "sistema"))
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
            self.raiz.after(0, lambda: (self.boton_voz.config(text="Activar voz", state="normal"), self._cambiar_estado("en espera")))

    def _estado_desde_voz(self, estado):
        self.raiz.after(0, lambda: self._cambiar_estado(estado))

    def _transcripcion_desde_voz(self, texto):
        """Refleja en el chat exactamente lo que Whisper oyó del micrófono."""
        self.raiz.after(0, lambda texto=texto: self._agregar("Tú · voz", texto, "usuario"))

    def _respuesta_desde_voz(self, texto):
        self.raiz.after(0, lambda texto=texto: self._agregar("Bark", texto, "bark"))

    def _evento_desde_voz(self, texto):
        self.raiz.after(0, lambda texto=texto: self._agregar("Voz", texto, "sistema"))

    def _cambiar_estado(self, estado):
        self.estado = estado
        etiquetas = {"en espera": ("● EN ESPERA", "En espera de “Bark”"), "escuchando": ("◉ ESCUCHANDO", "Micrófono local activo"), "pensando": ("◌ PENSANDO", "Razonando…"), "hablando": ("◉ HABLANDO", "Voz neuronal local"), "suspendido": ("◌ SUSPENDIDO", "Di “Bark despierta” para reanudar")}
        titulo, detalle = etiquetas.get(estado, ("● EN ESPERA", ""))
        if estado == "en espera":
            titulo = "●  SISTEMA LISTO"
        self.etiqueta_estado.config(text=titulo, fg=COLORES["exito"] if estado == "en espera" else (COLORES["acento"] if estado != "suspendido" else COLORES["alerta"]))
        self.descripcion_estado.config(text=detalle)

    def _animar(self):
        self.fase = (self.fase + 0.14) % 6.28
        import math
        base = {"en espera": 52, "escuchando": 64, "pensando": 57, "hablando": 68, "suspendido": 42}.get(self.estado, 52)
        amplitud = {"en espera": 2, "escuchando": 7, "pensando": 5, "hablando": 12, "suspendido": 1}.get(self.estado, 2)
        radio = base + math.sin(self.fase * (2 if self.estado == "hablando" else 1)) * amplitud
        self.orbita.delete("all")
        x, y = 125, 96
        # Retícula tenue y anillos orbitales: el movimiento representa que el
        # núcleo está vivo, no una pantalla de carga estática.
        for pos in range(10, 251, 20):
            self.orbita.create_line(pos, 0, pos, 196, fill="#0d1931")
        for pos in range(8, 197, 20):
            self.orbita.create_line(0, pos, 250, pos, fill="#0d1931")
        for factor, color in ((1.72, "#101f3a"), (1.43, "#17365a"), (1.17, "#245a72"), (1, COLORES["acento"])):
            r = radio * factor
            self.orbita.create_oval(x-r, y-r, x+r, y+r, fill=color, outline="")
        for distancia, velocidad, tamano in ((87, 1.2, 4), (70, -1.8, 3), (58, 2.2, 3)):
            px = x + math.cos(self.fase * velocidad) * distancia
            py = y + math.sin(self.fase * velocidad) * distancia * .46
            self.orbita.create_oval(px-tamano, py-tamano, px+tamano, py+tamano, fill=COLORES["acento2"], outline="")
        self.orbita.create_text(x, y-5, text="BARK", fill="#07101b", font=("Segoe UI", 14, "bold"))
        self.orbita.create_text(x, y+15, text=self.estado.upper(), fill="#07101b", font=("Segoe UI", 6, "bold"))
        self.raiz.after(40, self._animar)

    def _ver_memoria(self):
        recuerdos = buscar_recuerdos("", limite=100, incluir_sensibles=True)
        texto = "\n".join(f"• [{r['categoria']}] {r['contenido']}" for r in recuerdos) or "No hay recuerdos guardados."
        self._agregar("Memoria", texto, "sistema")

    def _abrir_biblioteca(self):
        ventana = tk.Toplevel(self.raiz)
        ventana.title("Biblioteca de Bark")
        ventana.geometry("820x620")
        ventana.configure(bg=COLORES["fondo"])
        periodo = tk.StringVar(value="hoy")
        consulta = tk.StringVar()
        barra = tk.Frame(ventana, bg=COLORES["fondo"], padx=16, pady=14)
        barra.pack(fill="x")
        stats = estadisticas_biblioteca()
        tk.Label(barra, text=f"BIBLIOTECA  ·  {stats['total']} turnos  ·  {stats['prioridad']} recuerdos prioritarios", bg=COLORES["fondo"], fg=COLORES["acento"], font=("Segoe UI", 11, "bold")).pack(anchor="w")
        controles = tk.Frame(barra, bg=COLORES["fondo"])
        controles.pack(fill="x", pady=(10, 0))
        visor = scrolledtext.ScrolledText(ventana, wrap="word", state="disabled", bg=COLORES["panel"], fg=COLORES["texto"], relief="flat", font=("Segoe UI", 10), padx=14, pady=12)
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
        self._agregar("Sistema", "Grabando 1.5 segundos para probar el micrófono; habla ahora.", "sistema")
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
        ruta_main = os.path.join(config.CARPETA_BASE, "main.py")
        os.execv(sys.executable, [sys.executable, ruta_main, "--ui"])

    def ejecutar(self):
        self.raiz.mainloop()


def iniciar_interfaz():
    VentanaBark().ejecutar()
