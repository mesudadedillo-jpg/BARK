"""Piezas visuales del HUD de Bark: paneles con esquinas biseladas, barras segmentadas y el orbe vivo.

Todo es Tkinter puro (Canvas): sin dependencias nuevas.
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk

# Paleta HUD: azul noche con trazos cian de neón y un acento rosa, como un visor.
HUD = {
    "fondo": "#030a12", "rejilla": "#06141f", "panel": "#061723", "panel2": "#0a2231",
    "borde": "#0f4656", "linea": "#18c8dc", "linea_suave": "#0b5868", "texto": "#e4fbff",
    "suave": "#78a9b8", "tenue": "#3d6b78", "acento": "#2ff3ff", "acento2": "#8a7bff",
    "rosa": "#ff4f7b", "ambar": "#ffc857", "verde": "#45f0b0",
}

FUENTE_HUD = "Bahnschrift"     # incluida en Windows 10/11; aspecto técnico tipo DIN
FUENTE_TEXTO = "Segoe UI"

ESTADOS = {
    # estado: (color principal, etiqueta, pista)
    "en espera": (HUD["linea"], "EN ESPERA", "Toca el orbe para hablar"),
    "escuchando": (HUD["acento"], "ESCUCHANDO", "Habla con naturalidad"),
    "pensando": (HUD["acento2"], "PROCESANDO", "Razonando…"),
    "hablando": ("#9ffcff", "RESPONDIENDO", "Toca el orbe o pulsa Esc para interrumpir"),
    "suspendido": (HUD["ambar"], "SUSPENDIDO", "Di «Bark despierta» para reanudar"),
}


def mezclar(color_a: str, color_b: str, t: float) -> str:
    """Interpola dos colores #rrggbb (t=0 → a, t=1 → b)."""
    t = max(0.0, min(1.0, t))
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def texto_seguro(texto: str) -> str:
    """Tk en Windows no dibuja bien emojis fuera del plano básico: se quitan de títulos externos."""
    return "".join(c for c in str(texto or "") if ord(c) <= 0xFFFF).strip()


class PanelHUD(tk.Frame):
    """Marco con esquinas biseladas, título y botón opcional para agrandar/achicar."""

    def __init__(self, padre, titulo: str, al_expandir=None, relleno=(12, 30, 12, 12)):
        super().__init__(padre, bg=HUD["fondo"])
        self.titulo = titulo
        self.expandido = False
        self._al_expandir = al_expandir
        self.lienzo = tk.Canvas(self, bg=HUD["fondo"], highlightthickness=0, bd=0)
        self.lienzo.place(x=0, y=0, relwidth=1, relheight=1)
        izquierda, arriba, derecha, abajo = relleno
        self.cuerpo = tk.Frame(self, bg=HUD["panel"])
        self.cuerpo.pack(fill="both", expand=True, padx=(izquierda, derecha), pady=(arriba, abajo))
        self.lienzo.bind("<Configure>", lambda _e: self.dibujar())
        if al_expandir:
            self.lienzo.tag_bind("expandir", "<Button-1>", lambda _e: self.alternar())
            self.lienzo.tag_bind("expandir", "<Enter>", lambda _e: self.lienzo.itemconfig("expandir", fill=HUD["acento"]))
            self.lienzo.tag_bind("expandir", "<Leave>", lambda _e: self.lienzo.itemconfig("expandir", fill=HUD["suave"]))

    def alternar(self):
        self.expandido = not self.expandido
        self.dibujar()
        if self._al_expandir:
            self._al_expandir(self.expandido)

    def dibujar(self):
        c = self.lienzo
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            return
        b = 14  # bisel
        c.create_polygon(0, b, b, 0, w - 1, 0, w - 1, h - 1 - b, w - 1 - b, h - 1, 0, h - 1,
                         fill=HUD["panel"], outline=HUD["borde"], width=1)
        # Pestaña del título y línea de enlace, como en un visor.
        c.create_rectangle(16, 11, 20, 19, fill=HUD["linea"], outline="")
        texto = c.create_text(27, 15, text=self.titulo, anchor="w", fill=HUD["linea"], font=(FUENTE_HUD, 9, "bold"))
        x_fin = c.bbox(texto)[2] + 10
        c.create_line(x_fin, 15, w - 44, 15, fill=HUD["linea_suave"])
        for i in range(3):
            c.create_rectangle(w - 56 + i * 5 - 20, 13, w - 53 + i * 5 - 20, 17, fill=HUD["linea_suave"], outline="")
        # Escuadras brillantes en dos esquinas.
        c.create_line(w - 1 - 26, 1, w - 2, 1, w - 2, 20, fill=HUD["linea"], width=2)
        c.create_line(1, h - 22, 1, h - 2, 26, h - 2, fill=HUD["linea"], width=2)
        c.create_line(0, b, b, 0, fill=HUD["linea"], width=2)
        if self._al_expandir:
            c.create_text(w - 18, 15, text="▢" if not self.expandido else "▣", fill=HUD["suave"],
                          font=(FUENTE_TEXTO, 11), tags=("expandir",))


class BarraHUD(tk.Canvas):
    """Barra segmentada en paralelogramos (CPU, RAM, progreso…)."""

    def __init__(self, padre, segmentos: int = 22, alto: int = 9, color=None, **kw):
        super().__init__(padre, height=alto, bg=kw.pop("bg", HUD["panel"]), highlightthickness=0, bd=0, **kw)
        self.segmentos, self.alto = segmentos, alto
        self.valor = 0.0
        self.color = color
        self.bind("<Configure>", lambda _e: self.fijar(self.valor))

    def fijar(self, valor: float):
        self.valor = max(0.0, min(100.0, float(valor or 0)))
        self.delete("all")
        w = self.winfo_width()
        if w < 20:
            return
        paso = w / self.segmentos
        llenos = round(self.valor / 100 * self.segmentos)
        color = self.color or (HUD["rosa"] if self.valor >= 90 else HUD["ambar"] if self.valor >= 75 else HUD["linea"])
        for i in range(self.segmentos):
            x = i * paso
            relleno = color if i < llenos else HUD["panel2"]
            self.create_polygon(x + 3, 0, x + paso - 1, 0, x + paso - 4, self.alto, x, self.alto, fill=relleno, outline="")


class OrbeVivo(tk.Canvas):
    """El núcleo de Bark: un orbe que late como una bocina con tu voz y con la suya.

    ``actualizar(estado, nivel)`` se llama ~30 veces por segundo; ``nivel`` va de 0 a 1.
    """

    BARRAS = 72

    def __init__(self, padre, al_tocar=None, **kw):
        super().__init__(padre, bg=HUD["fondo"], highlightthickness=0, bd=0, **kw)
        self.al_tocar = al_tocar
        self.nivel = 0.0
        self.estado = "en espera"
        self.fase = 0.0
        self._t0 = time.monotonic()
        self._ondas: list[list] = []  # [item, edad]
        self._ultima_onda = 0.0
        self._ruido = [random.uniform(0, math.tau) for _ in range(self.BARRAS)]
        self._items = {}
        self.subtitulo_usuario = ""
        self.subtitulo_bark = ""
        self.modo_live = False
        self.bind("<Configure>", lambda _e: self._construir())
        self.tag_bind("nucleo", "<Button-1>", self._tocar)
        self.tag_bind("nucleo", "<Enter>", lambda _e: self.config(cursor="hand2"))
        self.tag_bind("nucleo", "<Leave>", lambda _e: self.config(cursor=""))

    def _tocar(self, _evento=None):
        if self.al_tocar:
            self.al_tocar()

    # -------------------------------------------------- geometría
    def _construir(self):
        self.delete("all")
        self._ondas.clear()
        self._items = {}
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 80:
            return
        # Cabe todo: anillos (1.74 R arriba), etiqueta de estado y, en Live, subtítulos y controles.
        extra = 54 + (170 if self.modo_live else 0)
        self.R = R = max(36.0, min(w * 0.27, (h - extra) / 3.44))
        holgura = max(0.0, h - extra - 3.44 * R)
        cx, cy = w / 2, 6 + 1.74 * R + holgura * 0.45
        self.cx, self.cy = cx, cy
        # Rejilla de fondo y escalas laterales.
        for x in range(0, w, 32):
            self.create_line(x, 0, x, h, fill=HUD["rejilla"])
        for y in range(0, h, 32):
            self.create_line(0, y, w, y, fill=HUD["rejilla"])
        for lado in (-1, 1):
            x = cx + lado * min(w * 0.46, R * 2.15)
            for i in range(-8, 9):
                largo = 14 if i % 4 == 0 else 7
                y = cy + i * R * 0.11
                self.create_line(x, y, x - lado * largo, y, fill=HUD["linea_suave"])
            self.create_line(x, cy - R * 0.95, x, cy + R * 0.95, fill=HUD["borde"])
            self.create_text(x - lado * 22, cy - R * 0.99, text="100" if lado < 0 else "SYS", fill=HUD["tenue"],
                             font=(FUENTE_HUD, 7), anchor="e" if lado < 0 else "w")
        # Anillos fijos: marcas cada 5°, anillo exterior fino y arcos punteados.
        for i in range(72):
            ang = math.radians(i * 5)
            r1, r2 = R * 1.50, R * (1.56 if i % 6 == 0 else 1.53)
            self.create_line(cx + math.cos(ang) * r1, cy + math.sin(ang) * r1, cx + math.cos(ang) * r2,
                             cy + math.sin(ang) * r2, fill=HUD["linea_suave"])
        self.create_oval(cx - R * 1.62, cy - R * 1.62, cx + R * 1.62, cy + R * 1.62, outline=HUD["borde"])
        self.create_arc(cx - R * 1.72, cy - R * 1.72, cx + R * 1.72, cy + R * 1.72, start=200, extent=140,
                        style="arc", outline=HUD["linea_suave"], width=1, dash=(2, 4))
        it = self._items
        it["halos"] = [self.create_oval(0, 0, 0, 0, outline="", width=w_) for w_ in (16, 10, 6, 3)]
        it["barras"] = [self.create_line(0, 0, 0, 0, width=3, capstyle="round") for _ in range(self.BARRAS)]
        it["arcos"] = [self.create_arc(0, 0, 0, 0, style="arc", width=a) for a in (3, 2, 2)]
        it["rosa"] = self.create_arc(0, 0, 0, 0, style="arc", width=3, outline=HUD["rosa"])
        it["nucleo"] = self.create_oval(0, 0, 0, 0, fill="#04131d", outline="", tags=("nucleo",))
        it["anillo_int"] = self.create_oval(0, 0, 0, 0, outline="", width=1, tags=("nucleo",))
        it["radios"] = [self.create_line(0, 0, 0, 0, width=2, tags=("nucleo",)) for _ in range(3)]
        it["centro"] = self.create_oval(0, 0, 0, 0, outline="", width=2, tags=("nucleo",))
        it["etiqueta"] = self.create_text(cx, cy + R * 1.70 + 12, text="", fill=HUD["linea"], font=(FUENTE_HUD, 11, "bold"))
        it["pista"] = self.create_text(cx, cy + R * 1.70 + 32, text="", fill=HUD["tenue"], font=(FUENTE_TEXTO, 8))
        ancho_texto = min(w - 60, 760)
        base = cy + R * 1.70 + 58
        it["sub_usuario"] = self.create_text(cx, base, text="", fill=HUD["suave"], width=ancho_texto,
                                             font=(FUENTE_TEXTO, 10, "italic"), justify="center", anchor="n")
        it["sub_bark"] = self.create_text(cx, base + 24, text="", fill=HUD["texto"], width=ancho_texto,
                                          font=(FUENTE_TEXTO, 12 if self.modo_live else 11), justify="center", anchor="n")
        self.fijar_subtitulos(self.subtitulo_usuario, self.subtitulo_bark)

    def fijar_subtitulos(self, usuario: str | None = None, bark: str | None = None):
        if usuario is not None:
            self.subtitulo_usuario = usuario
        if bark is not None:
            self.subtitulo_bark = bark
        if "sub_usuario" not in self._items:
            return
        if not self.modo_live:
            self.itemconfig(self._items["sub_usuario"], text="")
            self.itemconfig(self._items["sub_bark"], text="")
            return
        recorte = 220
        texto_b = self.subtitulo_bark if len(self.subtitulo_bark) <= recorte else self.subtitulo_bark[:recorte].rsplit(" ", 1)[0] + "…"
        self.itemconfig(self._items["sub_usuario"], text=f"«{self.subtitulo_usuario}»" if self.subtitulo_usuario else "")
        self.itemconfig(self._items["sub_bark"], text=texto_b)
        y_usuario = self.bbox(self._items["sub_usuario"])
        if y_usuario:
            self.coords(self._items["sub_bark"], self.cx, y_usuario[3] + 8)

    # -------------------------------------------------- animación
    def actualizar(self, estado: str, nivel_objetivo: float):
        if "nucleo" not in self._items:
            return
        ahora = time.monotonic() - self._t0
        self.estado = estado
        sube = nivel_objetivo > self.nivel
        self.nivel += (nivel_objetivo - self.nivel) * (0.55 if sube else 0.16)
        n = self.nivel
        color, etiqueta, pista = ESTADOS.get(estado, ESTADOS["en espera"])
        velocidad = {"pensando": 3.2, "hablando": 1.6, "escuchando": 1.2}.get(estado, 0.5)
        self.fase += 0.033 * velocidad
        cx, cy, R = self.cx, self.cy, self.R
        it = self._items

        # Núcleo: se expande como el cono de una bocina.
        r = R * (0.60 + 0.30 * n)
        self.coords(it["nucleo"], cx - r, cy - r, cx + r, cy + r)
        for indice, halo in enumerate(it["halos"]):
            radio = r + 2 + indice * 1.5
            self.coords(halo, cx - radio, cy - radio, cx + radio, cy + radio)
            self.itemconfig(halo, outline=mezclar(color, HUD["fondo"], (0.86, 0.68, 0.42, 0.0)[indice]))
        ri = r * 0.78
        self.coords(it["anillo_int"], cx - ri, cy - ri, cx + ri, cy + ri)
        self.itemconfig(it["anillo_int"], outline=mezclar(color, HUD["fondo"], 0.55))
        rc = r * (0.16 + 0.10 * n)
        self.coords(it["centro"], cx - rc, cy - rc, cx + rc, cy + rc)
        self.itemconfig(it["centro"], outline=color, fill=mezclar(color, "#04131d", 0.7 - 0.5 * n))
        for k, radio in enumerate(it["radios"]):
            ang = self.fase * 0.8 + k * math.tau / 3
            self.coords(radio, cx + math.cos(ang) * rc, cy + math.sin(ang) * rc,
                        cx + math.cos(ang) * ri, cy + math.sin(ang) * ri)
            self.itemconfig(radio, fill=mezclar(color, HUD["fondo"], 0.35))

        # Barras radiales: el «espectro» que vibra con el audio.
        base = R * 1.10 + 0.3 * r - 0.3 * R * 0.60
        for i, barra in enumerate(it["barras"]):
            ang = i * math.tau / self.BARRAS - math.pi / 2
            onda = 0.5 + 0.5 * math.sin(i * 0.9 + ahora * 7.5 + self._ruido[i]) * math.sin(i * 0.37 - ahora * 3.1)
            largo = 3 + n * R * 0.30 * (0.35 + 0.65 * abs(onda))
            x1, y1 = cx + math.cos(ang) * base, cy + math.sin(ang) * base
            x2, y2 = cx + math.cos(ang) * (base + largo), cy + math.sin(ang) * (base + largo)
            self.coords(barra, x1, y1, x2, y2)
            self.itemconfig(barra, fill=mezclar(color, HUD["fondo"], 0.75 - 0.65 * min(1.0, n * 1.4 * abs(onda) + 0.1)))

        # Arcos que giran (más rápido al pensar) y el acento rosa.
        for k, (arco, radio_f, extension, sentido) in enumerate(zip(it["arcos"], (1.32, 1.40, 0.92), (70, 40, 120), (1, -1.6, 2.2))):
            radio = R * radio_f
            self.coords(arco, cx - radio, cy - radio, cx + radio, cy + radio)
            self.itemconfig(arco, start=(self.fase * 60 * sentido + k * 120) % 360, extent=extension,
                            outline=mezclar(color, HUD["fondo"], 0.15 + 0.2 * k))
        radio = R * 1.40
        self.coords(it["rosa"], cx - radio, cy - radio, cx + radio, cy + radio)
        self.itemconfig(it["rosa"], start=(-self.fase * 96 + 200) % 360, extent=14 + 26 * n)

        # Ondas expansivas cuando el volumen pega fuerte.
        if n > 0.42 and ahora - self._ultima_onda > 0.32 and len(self._ondas) < 5:
            self._ultima_onda = ahora
            self._ondas.append([self.create_oval(0, 0, 0, 0, outline=color, width=2), 0.0, r])
            self.tag_lower(self._ondas[-1][0], it["halos"][0])
        for onda in list(self._ondas):
            onda[1] += 0.045
            radio = onda[2] + onda[1] * R * 1.1
            self.coords(onda[0], cx - radio, cy - radio, cx + radio, cy + radio)
            self.itemconfig(onda[0], outline=mezclar(color, HUD["fondo"], onda[1]))
            if onda[1] >= 1.0:
                self.delete(onda[0])
                self._ondas.remove(onda)

        self.itemconfig(it["etiqueta"], text=etiqueta, fill=color)
        self.itemconfig(it["pista"], text=pista)
