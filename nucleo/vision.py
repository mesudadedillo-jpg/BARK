"""Visión local y opt-in para Bark.

La cámara nunca se abre al iniciar Bark y ningún fotograma sale del equipo.
MediaPipe analiza manos y rasgos faciales localmente; las etiquetas son señales
visuales, no diagnósticos de emociones ni identificación de personas.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from config import config


RUTA_MODELOS_VISION = Path(config.CARPETA_MODELOS) / "vision"
RUTA_GESTOS = RUTA_MODELOS_VISION / "gesture_recognizer.task"
RUTA_ROSTRO = RUTA_MODELOS_VISION / "face_landmarker.task"


class VisionNoDisponible(RuntimeError):
    pass


class VisionLocal:
    """Vista previa local: palma abierta alterna escucha; puño la suspende."""

    def __init__(self, al_gesto=None, al_estado=None):
        self._detener = threading.Event()
        self._hilo: threading.Thread | None = None
        self._al_gesto = al_gesto
        self._al_estado = al_estado
        self._ultimo_gesto = ""
        self._ultimo_gesto_en = 0.0

    @property
    def activa(self) -> bool:
        return bool(self._hilo and self._hilo.is_alive())

    def iniciar(self) -> None:
        if self.activa:
            return
        self._verificar_dependencias()
        self._detener.clear()
        self._hilo = threading.Thread(target=self._bucle, name="BarkVision", daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        self._detener.set()

    @staticmethod
    def _verificar_dependencias() -> None:
        faltantes = [str(ruta.name) for ruta in (RUTA_GESTOS, RUTA_ROSTRO) if not ruta.is_file()]
        if faltantes:
            raise VisionNoDisponible("Faltan modelos locales de visión: " + ", ".join(faltantes))
        # Evita que Matplotlib cree cachés fuera del proyecto al importar MediaPipe.
        os.environ.setdefault("MPLCONFIGDIR", str(Path(config.CARPETA_BD) / "matplotlib"))
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
        except ImportError as error:
            raise VisionNoDisponible("Faltan OpenCV o MediaPipe. Instala requirements.txt.") from error

    def _avisar(self, texto: str) -> None:
        if self._al_estado:
            self._al_estado(texto)

    def _emitir_gesto(self, gesto: str) -> None:
        ahora = time.monotonic()
        if gesto == self._ultimo_gesto and ahora - self._ultimo_gesto_en < 1.5:
            return
        self._ultimo_gesto, self._ultimo_gesto_en = gesto, ahora
        if self._al_gesto:
            self._al_gesto(gesto)

    def _bucle(self) -> None:
        import cv2
        import mediapipe as mp
        import numpy as np

        base = mp.tasks.BaseOptions
        vision = mp.tasks.vision
        opciones_gestos = vision.GestureRecognizerOptions(base_options=base(model_asset_path=str(RUTA_GESTOS)), num_hands=1)
        opciones_rostro = vision.FaceLandmarkerOptions(
            base_options=base(model_asset_path=str(RUTA_ROSTRO)), num_faces=1, output_face_blendshapes=True
        )
        camara = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not camara.isOpened():
            self._avisar("No pude abrir la cámara. Revisa permisos o cierra otra aplicación que la esté usando.")
            return
        self._avisar("Cámara local activa. Palma: activar voz · puño: suspender · Esc: cerrar.")
        try:
            with vision.GestureRecognizer.create_from_options(opciones_gestos) as gestos, vision.FaceLandmarker.create_from_options(opciones_rostro) as rostro:
                while not self._detener.is_set():
                    ok, cuadro = camara.read()
                    if not ok:
                        self._avisar("La cámara dejó de entregar video.")
                        return
                    cuadro = cv2.flip(cuadro, 1)
                    rgb = cv2.cvtColor(cuadro, cv2.COLOR_BGR2RGB)
                    imagen = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
                    resultado_gestos = gestos.recognize(imagen)
                    resultado_rostro = rostro.detect(imagen)
                    etiqueta = "Sin mano"
                    if resultado_gestos.gestures and resultado_gestos.gestures[0]:
                        etiqueta = resultado_gestos.gestures[0][0].category_name
                        if etiqueta == "Open_Palm":
                            self._emitir_gesto("activar_voz")
                        elif etiqueta == "Closed_Fist":
                            self._emitir_gesto("suspender_voz")
                    rostro_texto = "Rostro no detectado"
                    if resultado_rostro.face_blendshapes and resultado_rostro.face_blendshapes[0]:
                        rasgos = {r.category_name: r.score for r in resultado_rostro.face_blendshapes[0]}
                        if max(rasgos.get("mouthSmileLeft", 0), rasgos.get("mouthSmileRight", 0)) > 0.55:
                            rostro_texto = "Sonrisa visual estimada"
                        else:
                            rostro_texto = "Rostro detectado"
                    cv2.rectangle(cuadro, (10, 10), (500, 75), (6, 20, 38), -1)
                    cv2.putText(cuadro, f"Gesto: {etiqueta}", (22, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (32, 245, 234), 2)
                    cv2.putText(cuadro, rostro_texto, (22, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (220, 250, 255), 1)
                    cv2.imshow("Bark Vision — local", cuadro)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
        except Exception as error:
            self._avisar(f"Visión local detenida: {error}")
        finally:
            camara.release()
            cv2.destroyAllWindows()
            self._avisar("Cámara local detenida.")

