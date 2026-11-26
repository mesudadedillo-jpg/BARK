"""Gemini Live: conversación de voz a voz en tiempo real, como el modo Live de la app de Gemini.

El micrófono se envía en streaming por WebSocket a la Live API de Google; Gemini
detecta cuándo terminas de hablar, responde con su propia voz (audio de 24 kHz) y
se puede interrumpir. Cuando hay que *hacer* algo en el equipo (abrir apps,
volumen, archivos, clima, notas…), Gemini llama a la herramienta
``ejecutar_en_bark``, que pasa la orden al agente de Bark con sus 123 herramientas
y sus permisos de siempre.

Sin dependencias nuevas salvo ``websockets`` (cliente síncrono, con hilos).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
from datetime import datetime

import numpy as np

from config import config
from nucleo import audio_vivo

URL_LIVE = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
FRECUENCIA_ENTRADA = 16000
FRECUENCIA_SALIDA = 24000

HERRAMIENTA = {
    "name": "ejecutar_en_bark",
    "description": (
        "Ejecuta una acción real en la PC del usuario o consulta datos locales o de internet mediante el agente Bark: "
        "abrir o cerrar programas, volumen y brillo, música y videos, buscar y mover archivos, clima y pronóstico, "
        "notas, pendientes, agenda y recordatorios, WhatsApp y correo, diagnóstico y optimización del equipo, red y Wi-Fi, "
        "instalar programas, tipo de cambio, noticias, Wikipedia y lo que recuerdes del usuario. "
        "Úsala siempre que el usuario pida hacer o consultar algo de su equipo; nunca finjas haberlo hecho."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"orden": {"type": "STRING", "description": "La petición completa del usuario, en español natural."}},
        "required": ["orden"],
    },
}


class LiveNoDisponible(RuntimeError):
    pass


def instrucciones_sistema() -> str:
    """Personalidad de Bark + reglas para conversar por voz en tiempo real."""
    ruta = os.path.join(config.CARPETA_BASE, "config", "personalidad.txt")
    try:
        with open(ruta, encoding="utf-8") as archivo:
            personalidad = archivo.read()[:2500]
    except OSError:
        personalidad = "Eres Bark, un asistente de IA que vive en la PC del usuario."
    ahora = datetime.now()
    ciudad = os.getenv("BARK_CIUDAD", "").strip()
    return (
        f"{personalidad}\n\n"
        "MODO DE VOZ EN TIEMPO REAL:\n"
        "- Estás conversando por voz. Responde en español de México con frases cortas y naturales, como en una llamada.\n"
        "- No leas listas largas, rutas, enlaces ni código: resume y di que el detalle queda en pantalla.\n"
        "- Para cualquier acción o dato del equipo del usuario llama a ejecutar_en_bark con la orden completa y luego "
        "cuenta el resultado en una o dos frases. Mientras esperas puedes decir algo breve como «dame un segundo».\n"
        "- Si te interrumpen, detente y atiende lo nuevo.\n"
        f"- Fecha y hora actuales: {ahora:%A %d/%m/%Y %H:%M}." + (f" Ciudad del usuario: {ciudad}." if ciudad else "")
    )


def mensaje_setup(modelo: str, voz: str, handle: str | None = None) -> dict:
    """Primer mensaje de la sesión (formato verificado contra la Live API)."""
    setup = {
        "model": modelo if modelo.startswith("models/") else f"models/{modelo}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voz}}},
        },
        "systemInstruction": {"parts": [{"text": instrucciones_sistema()}]},
        "tools": [{"functionDeclarations": [HERRAMIENTA]}],
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        # Sin compresión, las sesiones de audio se cortan a los 15 minutos.
        "contextWindowCompression": {"slidingWindow": {}},
        "sessionResumption": {"handle": handle} if handle else {},
    }
    return {"setup": setup}


class _Altavoz:
    """Reproduce el audio de Gemini sin cortes (callback de sounddevice) y publica su nivel para el orbe."""

    def __init__(self):
        self._buffer = bytearray()
        self._bloqueo = threading.Lock()
        self._flujo = None
        self.ultimo_sonido = 0.0

    def abrir(self):
        import sounddevice as sd
        self._flujo = sd.RawOutputStream(samplerate=FRECUENCIA_SALIDA, channels=1, dtype="int16",
                                         blocksize=int(FRECUENCIA_SALIDA * 0.04), callback=self._callback)
        self._flujo.start()

    def _callback(self, salida, frames, _tiempo, _estado):
        necesarios = frames * 2
        with self._bloqueo:
            trozo = bytes(self._buffer[:necesarios])
            del self._buffer[:necesarios]
        if trozo:
            muestras = np.frombuffer(trozo[:len(trozo) // 2 * 2], dtype=np.int16)
            volumen = float(np.clip(config.VOZ_VOLUMEN, 0.0, 1.0))
            if volumen < 1.0:
                trozo = (muestras.astype(np.float32) * volumen).astype(np.int16).tobytes()
            audio_vivo.reportar_bloque("salida", muestras)
            self.ultimo_sonido = time.monotonic()
        salida[:] = trozo + b"\x00" * (necesarios - len(trozo))

    def agregar(self, pcm: bytes):
        with self._bloqueo:
            self._buffer.extend(pcm)

    def vaciar(self):
        with self._bloqueo:
            self._buffer.clear()
        audio_vivo.reportar("salida", 0.0)

    @property
    def sonando(self) -> bool:
        with self._bloqueo:
            pendiente = len(self._buffer)
        return pendiente > 0 or time.monotonic() - self.ultimo_sonido < 0.35

    def cerrar(self):
        if self._flujo is not None:
            try:
                self._flujo.abort()
                self._flujo.close()
            except Exception:
                pass
            self._flujo = None


class SesionGeminiLive:
    """Una conversación Live. ``iniciar()`` arranca hilos; ``detener()`` la termina.

    Callbacks (se llaman desde hilos; la interfaz debe pasarlos a Tk con ``after``):
    - al_turno(usuario, bark): turno completo, para el registro de conversación.
    - al_subtitulos(usuario, bark): texto parcial mientras se habla.
    - al_evento(texto): avisos (conexión, errores, herramientas).
    - al_terminar(error | None): la sesión terminó.
    - ejecutar_orden(orden) -> str: puente hacia el agente de Bark.
    """

    def __init__(self, ejecutar_orden, al_turno=None, al_subtitulos=None, al_evento=None, al_terminar=None,
                 modelo: str | None = None, voz: str | None = None):
        self.ejecutar_orden = ejecutar_orden
        self.al_turno = al_turno or (lambda *_: None)
        self.al_subtitulos = al_subtitulos or (lambda *_: None)
        self.al_evento = al_evento or (lambda *_: None)
        self.al_terminar = al_terminar or (lambda *_: None)
        self.modelo = modelo or config.GEMINI_MODELO_LIVE
        self.voz = voz or config.GEMINI_VOZ
        self._detener = threading.Event()
        self._ws = None
        self._bloqueo_envio = threading.Lock()
        self._altavoz = _Altavoz()
        self._handle = None
        self._descartar_turno = False
        self._herramientas_activas = 0
        self._cancelados: set = set()
        self._esperando_respuesta = False
        self._texto_usuario = ""
        self._texto_bark = ""
        self._hilo = None

    # ------------------------------------------------------------------ ciclo de vida
    @property
    def activa(self) -> bool:
        return bool(self._hilo and self._hilo.is_alive())

    def iniciar(self):
        if not config.GEMINI_API_KEY:
            raise LiveNoDisponible("Gemini Live necesita GEMINI_API_KEY en el archivo .env.")
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError as error:
            raise LiveNoDisponible("Falta la librería websockets: instala con «pip install websockets».") from error
        self._detener.clear()
        self._hilo = threading.Thread(target=self._ejecutar, name="bark-gemini-live", daemon=True)
        self._hilo.start()

    def detener(self):
        self._detener.set()
        self._altavoz.vaciar()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def enviar_texto(self, texto: str) -> None:
        """Lo que escribes en el chat también va a la conversación Live (y te responde con voz)."""
        self._esperando_respuesta = True
        self._enviar({"clientContent": {"turns": [{"role": "user", "parts": [{"text": texto}]}], "turnComplete": True}})

    def interrumpir(self):
        """Toque en el orbe o Esc: se calla ya y descarta el resto de esta respuesta."""
        if self._altavoz.sonando:
            self._descartar_turno = True
        self._altavoz.vaciar()

    def estado(self) -> str:
        """Estado para el orbe: hablando, pensando o escuchando."""
        if self._altavoz.sonando:
            return "hablando"
        if self._herramientas_activas or self._esperando_respuesta:
            return "pensando"
        return "escuchando"

    # ------------------------------------------------------------------ conexión
    def _enviar(self, mensaje: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._bloqueo_envio:
            ws.send(json.dumps(mensaje))

    def _conectar(self):
        from websockets.sync.client import connect
        ws = connect(f"{URL_LIVE}?key={config.GEMINI_API_KEY}", max_size=None, open_timeout=15, close_timeout=2)
        ws.send(json.dumps(mensaje_setup(self.modelo, self.voz, self._handle)))
        respuesta = json.loads(ws.recv(timeout=15))
        if "setupComplete" not in respuesta:
            ws.close()
            raise LiveNoDisponible(f"Gemini Live rechazó la sesión: {str(respuesta)[:200]}")
        return ws

    def _ejecutar(self):
        error_final = None
        microfono = None
        try:
            self._altavoz.abrir()
            reconexiones = 0
            while not self._detener.is_set():
                try:
                    self._ws = self._conectar()
                except LiveNoDisponible:
                    raise
                except Exception as error:
                    raise LiveNoDisponible(self._explicar_error(error)) from error
                if reconexiones == 0:
                    self.al_evento(f"Gemini Live conectado ({self.modelo} · voz {self.voz}). Habla cuando quieras.")
                if microfono is None:
                    microfono = threading.Thread(target=self._bucle_microfono, name="bark-live-mic", daemon=True)
                    microfono.start()
                motivo = self._bucle_recepcion()
                self._ws = None
                if self._detener.is_set():
                    break
                if motivo == "goaway" or (motivo == "cerrado" and self._handle):
                    reconexiones += 1  # la conexión dura ~10 min: se reanuda la misma conversación
                    continue
                raise LiveNoDisponible(motivo if motivo not in {"cerrado", "goaway"} else "Se cerró la conexión con Gemini Live.")
        except LiveNoDisponible as error:
            error_final = str(error)
        except Exception as error:  # noqa: BLE001  (la voz nunca debe tumbar la interfaz)
            error_final = f"Gemini Live se detuvo: {error}"
        finally:
            self._detener.set()
            self._altavoz.cerrar()
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            self.al_terminar(error_final)

    @staticmethod
    def _explicar_error(error: Exception) -> str:
        texto = str(error)
        codigo = getattr(getattr(error, "response", None), "status_code", None)
        if codigo in {401, 403} or "403" in texto or "401" in texto:
            return "Gemini Live rechazó la clave (revisa GEMINI_API_KEY)."
        if codigo == 429 or "429" in texto or "RESOURCE_EXHAUSTED" in texto:
            return "Gemini Live no tiene cuota disponible ahora mismo (límite de uso)."
        return f"No pude conectar con Gemini Live: {texto[:160]}"

    # ------------------------------------------------------------------ micrófono → Gemini
    def _bucle_microfono(self):
        from nucleo import escuchar
        cola: queue.Queue = queue.Queue()
        try:
            indices = escuchar._microfonos_a_escuchar()
            flujo = escuchar._abrir_flujo(indices[0] if indices else None, lambda indata, *_: cola.put(bytes(indata)))
        except Exception as error:
            self.al_evento(f"No pude abrir el micrófono: {error}")
            self.detener()
            return
        frecuencia = int(flujo.samplerate)
        with flujo:
            while not self._detener.is_set():
                try:
                    bloque = cola.get(timeout=0.2)
                except queue.Empty:
                    continue
                muestras = np.frombuffer(bloque, dtype=np.int16)
                audio_vivo.reportar_bloque("entrada", muestras)
                if frecuencia != FRECUENCIA_ENTRADA:
                    flotante = escuchar.remuestrear(muestras.astype(np.float32) / 32768.0, frecuencia, FRECUENCIA_ENTRADA)
                    muestras = (np.clip(flotante, -1, 1) * 32767).astype(np.int16)
                # Sin cancelación de eco, Gemini se oiría a sí mismo por los altavoces y se
                # interrumpiría solo: mientras habla se manda silencio, salvo que el usuario
                # active «interrumpir hablando» (pensado para audífonos).
                if self._altavoz.sonando and not config.INTERRUMPIR_CON_VOZ:
                    muestras = np.zeros_like(muestras)
                try:
                    self._enviar({"realtimeInput": {"audio": {
                        "data": base64.b64encode(muestras.astype("<i2").tobytes()).decode("ascii"),
                        "mimeType": f"audio/pcm;rate={FRECUENCIA_ENTRADA}",
                    }}})
                except Exception:
                    time.sleep(0.1)  # reconectando: se descarta este bloque

    # ------------------------------------------------------------------ Gemini → altavoz
    def _bucle_recepcion(self) -> str:
        ws = self._ws
        while not self._detener.is_set():
            try:
                crudo = ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            except Exception as error:
                motivo = str(error)
                if "1011" in motivo or "1007" in motivo or "1008" in motivo:
                    return f"Gemini Live cerró la sesión: {motivo[:160]}"
                return "cerrado"
            try:
                mensaje = json.loads(crudo)
            except (TypeError, ValueError):
                continue
            if "goAway" in mensaje:
                return "goaway"
            self.procesar_mensaje(mensaje)
        return "detenido"

    def procesar_mensaje(self, mensaje: dict) -> None:
        """Atiende un mensaje del servidor (separado del socket para poder probarlo)."""
        actualizacion = mensaje.get("sessionResumptionUpdate")
        if actualizacion:
            self._handle = actualizacion.get("newHandle") if actualizacion.get("resumable") else self._handle
        if "toolCall" in mensaje:
            for llamada in mensaje["toolCall"].get("functionCalls", []):
                self._herramientas_activas += 1
                threading.Thread(target=self._atender_herramienta, args=(llamada,), daemon=True).start()
        if "toolCallCancellation" in mensaje:
            self._cancelados.update(mensaje["toolCallCancellation"].get("ids", []))
        contenido = mensaje.get("serverContent")
        if not contenido:
            return
        if contenido.get("interrupted"):
            self._altavoz.vaciar()  # el usuario habló encima: Gemini ya dejó de generar
        entrada = contenido.get("inputTranscription", {}).get("text")
        if entrada:
            if self._texto_bark and not self._texto_usuario:
                self._cerrar_turno()  # empezó una pregunta nueva tras una respuesta sin cierre
            self._texto_usuario += entrada
            self._esperando_respuesta = True
            self.al_subtitulos(self._texto_usuario.strip(), "")
        for parte in contenido.get("modelTurn", {}).get("parts", []):
            datos = parte.get("inlineData")
            if datos and datos.get("mimeType", "").startswith("audio/pcm"):
                self._esperando_respuesta = False
                if not self._descartar_turno:
                    self._altavoz.agregar(base64.b64decode(datos["data"]))
        salida = contenido.get("outputTranscription", {}).get("text")
        if salida and not self._descartar_turno:
            self._texto_bark += salida
            self.al_subtitulos(self._texto_usuario.strip(), self._texto_bark.strip())
        if contenido.get("turnComplete") or contenido.get("interrupted"):
            self._descartar_turno = False
            self._esperando_respuesta = False
            if contenido.get("turnComplete"):
                self._cerrar_turno()

    def _cerrar_turno(self):
        usuario, bark = self._texto_usuario.strip(), self._texto_bark.strip()
        self._texto_usuario = self._texto_bark = ""
        if usuario or bark:
            self.al_turno(usuario, bark)

    def _atender_herramienta(self, llamada: dict):
        identificador, nombre = llamada.get("id"), llamada.get("name", "")
        orden = str((llamada.get("args") or {}).get("orden", "")).strip()
        try:
            if nombre != HERRAMIENTA["name"] or not orden:
                resultado = "Herramienta desconocida o sin orden."
            else:
                self.al_evento(f"Bark ejecuta: {orden}")
                resultado = self.ejecutar_orden(orden)
        except Exception as error:  # noqa: BLE001
            resultado = f"Error al ejecutar la orden: {error}"
        finally:
            self._herramientas_activas = max(0, self._herramientas_activas - 1)
        if identificador in self._cancelados or self._detener.is_set():
            return
        try:
            self._enviar({"toolResponse": {"functionResponses": [
                {"id": identificador, "name": nombre, "response": {"result": str(resultado)[:4000]}}
            ]}})
        except Exception:
            pass


def disponible() -> bool:
    """Hay clave de Gemini y librería websockets: se puede usar el modo Live real."""
    if not config.GEMINI_API_KEY:
        return False
    try:
        import websockets.sync.client  # noqa: F401
        return True
    except ImportError:
        return False

