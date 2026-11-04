"""Voz de Bark: reconocimiento local y Piper como respaldo sin conexión."""

import os
import base64
import collections
import queue
import tempfile
import wave
import winsound
import time
import re
import threading
import numpy as np
import requests
from difflib import get_close_matches

from config import config
from nucleo.proveedores import _sesion_red


class VozNoDisponible(RuntimeError):
    pass


FRECUENCIA_WHISPER = 16000
_FRASES_ALUCINADAS = (
    "amara org", "subtitulos realizados", "subtitulado por", "gracias por ver", "suscribete",
    "no olvides suscribirte", "hasta la proxima", "gracias por vernos", "musica de fondo",
)


def _sin_acentos(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def es_alucinacion(texto: str) -> bool:
    """Detecta el texto que Whisper inventa con ruido o silencio («no no no no…»)."""
    palabras = re.findall(r"[a-z0-9ñ]+", _sin_acentos(texto))
    if not palabras:
        return True
    plano = " ".join(palabras)
    if any(frase in plano for frase in _FRASES_ALUCINADAS):
        return True
    racha = maxima = 1
    for anterior, actual in zip(palabras, palabras[1:]):
        racha = racha + 1 if actual == anterior else 1
        maxima = max(maxima, racha)
    if maxima >= 3:  # «no no no»
        return True
    return len(palabras) >= 6 and len(set(palabras)) / len(palabras) < 0.35


def remuestrear(audio: np.ndarray, origen: int, destino: int = FRECUENCIA_WHISPER) -> np.ndarray:
    """Convierte audio float32 mono a 16 kHz (lo que Whisper espera) sin depender de scipy."""
    if origen == destino or len(audio) == 0:
        return audio.astype(np.float32, copy=False)
    if origen % destino == 0:
        factor = origen // destino
        recorte = len(audio) - len(audio) % factor
        return audio[:recorte].reshape(-1, factor).mean(axis=1).astype(np.float32)
    # Filtro de media móvil (antialias) y interpolación lineal.
    ancho = max(1, int(round(origen / destino)))
    if ancho > 1:
        audio = np.convolve(audio, np.ones(ancho, dtype=np.float32) / ancho, mode="same")
    posiciones = np.linspace(0, len(audio) - 1, int(len(audio) * destino / origen))
    return np.interp(posiciones, np.arange(len(audio)), audio).astype(np.float32)


class DetectorVoz:
    """Decide cuándo empieza y termina una frase a partir del nivel RMS de cada bloque.

    El umbral se adapta al ruido ambiental, ignora chasquidos breves y cierra la frase
    tras ``silencio_final`` segundos sin voz. ``estado`` es esperando|hablando|fin|vacio.
    """

    def __init__(self, umbral_minimo: float, silencio_final: float, max_frase: float,
                 espera_max: float = 60.0, min_voz: float = 0.30):
        self.umbral_minimo, self.silencio_final = umbral_minimo, silencio_final
        self.max_frase, self.espera_max, self.min_voz = max_frase, espera_max, min_voz
        self.ruido = None
        self.hablando = False
        self.pico = 0.0
        self._t = self._ultimo_voz = self._inicio = self._voz_total = 0.0
        self._seguidos = 0

    @property
    def umbral(self) -> float:
        return max(self.umbral_minimo, (self.ruido or 0.0) * 2.5 + 3.0)

    def procesar(self, rms: float, duracion: float) -> str:
        self._t += duracion
        self.pico = max(self.pico, rms)
        if self.ruido is None:
            self.ruido = rms
        voz = rms >= self.umbral
        if not self.hablando:
            if voz:
                self._seguidos += 1
                if self._seguidos >= 2:  # dos bloques seguidos: no es un chasquido
                    self.hablando = True
                    self._inicio = self._t - duracion * self._seguidos
                    self._ultimo_voz = self._t
                    self._voz_total = duracion * self._seguidos
                return "hablando" if self.hablando else "esperando"
            self._seguidos = 0
            self.ruido = 0.9 * self.ruido + 0.1 * rms
            if self._t >= 4.0 and self.pico < 1.5:
                return "vacio"  # entrada muerta (micrófono virtual o desconectado)
            return "vacio" if self._t >= self.espera_max else "esperando"
        if voz:
            self._ultimo_voz = self._t
            self._voz_total += duracion
        if self._t - self._ultimo_voz >= self.silencio_final:
            if self._voz_total >= self.min_voz:
                return "fin"
            self.hablando, self._seguidos, self._voz_total = False, 0, 0.0  # ruido breve
            return "esperando"
        return "fin" if self._t - self._inicio >= self.max_frase else "hablando"


_modelo_whisper = None
_voz_neural = None
_motores_nube_bloqueados_hasta = {"Gemini": 0.0, "OpenAI": 0.0}
# Solo se usa mientras Windows no tenga un micrófono elegido explícitamente.
# Si una entrada abre pero no entrega voz, la siguiente escucha prueba la otra.
_ultimo_microfono_automatico = None


def dispositivos_entrada() -> list[dict]:
    """Devuelve micrófonos reales disponibles para diagnóstico y selección."""
    import sounddevice as sd
    return [
        {"indice": indice, "nombre": dispositivo["name"], "frecuencia": int(dispositivo["default_samplerate"])}
        for indice, dispositivo in enumerate(sd.query_devices()) if dispositivo["max_input_channels"] > 0
    ]


def seleccionar_dispositivo_entrada(indice: int | None) -> None:
    """Elige el micrófono que se utilizará durante esta ejecución."""
    global _modelo_whisper
    if indice is not None:
        dispositivos = {dispositivo["indice"] for dispositivo in dispositivos_entrada()}
        if indice not in dispositivos:
            raise ValueError("El micrófono seleccionado ya no está disponible.")
    config.DISPOSITIVO_ENTRADA_VOZ = indice


def modelos_whisper_disponibles() -> list[str]:
    """Lista los modelos de Whisper descargados y los tamaños disponibles."""
    modelos = {"tiny", "base", "small", "medium", "large-v3"}
    if os.path.isdir(config.RUTA_MODELOS_WHISPER):
        for nombre in os.listdir(config.RUTA_MODELOS_WHISPER):
            if os.path.isfile(os.path.join(config.RUTA_MODELOS_WHISPER, nombre, "model.bin")):
                modelos.add(nombre)
    return sorted(modelos, key=lambda nombre: (nombre not in {"tiny", "base", "small", "medium", "large-v3"}, nombre))


def seleccionar_modelo_whisper(nombre: str) -> None:
    """Selecciona el modelo de reconocimiento para la siguiente escucha."""
    global _modelo_whisper
    nombre = os.path.basename(nombre.strip())
    if not nombre or nombre in {".", ".."}:
        raise ValueError("El modelo de escucha no es válido.")
    config.MODELO_WHISPER = nombre
    config.RUTA_MODELO_WHISPER = os.path.join(config.RUTA_MODELOS_WHISPER, nombre)
    _modelo_whisper = None


def configurar_voz_nube(modelo: str, voz: str) -> None:
    """Configura la voz y el modelo OpenAI elegidos en la sesión actual."""
    if modelo not in config.MODELOS_TTS_OPENAI:
        raise ValueError("El modelo de voz de nube no es válido.")
    if voz not in config.VOCES_OPENAI:
        raise ValueError("La voz de nube no es válida.")
    config.OPENAI_MODELO_TTS = modelo
    config.OPENAI_VOZ = voz


def configurar_voz_gemini(modelo: str, voz: str) -> None:
    """Configura la voz Gemini preferida durante la sesión actual."""
    if modelo not in config.MODELOS_TTS_GEMINI:
        raise ValueError("El modelo de voz Gemini no es válido.")
    if voz not in config.VOCES_GEMINI:
        raise ValueError("La voz Gemini no es válida.")
    config.GEMINI_MODELO_TTS = modelo
    config.GEMINI_VOZ = voz


def seleccionar_motor_voz(motor: str) -> None:
    """Cambia entre síntesis local, nube o preferencia automática."""
    if motor not in {"local", "gemini", "openai", "auto"}:
        raise ValueError("El motor de voz no es válido.")
    config.MODO_VOZ = motor


def _abrir_flujo_entrada(callback):
    """Abre el primer micrófono operativo y lo conserva para la sesión actual."""
    global _ultimo_microfono_automatico
    import sounddevice as sd
    seleccion_automatica = config.DISPOSITIVO_ENTRADA_VOZ is None
    if not seleccion_automatica:
        candidatos = [config.DISPOSITIVO_ENTRADA_VOZ]
    else:
        # ``None`` suele resolver al "Asignador de sonido Microsoft", un
        # intermediario virtual que puede abrirse sin recibir voz. Priorizamos
        # entradas físicas reales, dejando los mapeadores del sistema al final.
        entradas = dispositivos_entrada()

        def prioridad(dispositivo):
            nombre = dispositivo["nombre"].lower()
            virtual = any(texto in nombre for texto in (
                "asignador de sonido", "controlador primario", "sound mapper",
                "primary sound capture", "mezcla estéreo", "stereo mix",
            ))
            microfono = "micrófono" in nombre or "microphone" in nombre
            # En este equipo Realtek/AMD son las entradas integradas. Los
            # perfiles Bluetooth "Hands-Free" aparecen aun desconectados y
            # suelen abrirse sin señal, por eso van después de ellas.
            integrado = 2 if "realtek" in nombre else 1 if ("amd" in nombre or "array" in nombre) else 0
            bluetooth = "hands-free" in nombre or "bthhfenum" in nombre
            return (0 if virtual else 1, integrado, 0 if bluetooth else 1, 1 if microfono else 0)

        candidatos = [d["indice"] for d in sorted(entradas, key=prioridad, reverse=True)]
        # Tras un periodo sin señal se rota la prioridad. Esto evita que un
        # micrófono virtual o desconectado capture siempre el modo de voz.
        if _ultimo_microfono_automatico in candidatos:
            posicion = candidatos.index(_ultimo_microfono_automatico)
            candidatos = candidatos[posicion + 1:] + candidatos[:posicion + 1]
    errores = []
    for candidato in dict.fromkeys(candidatos):
        try:
            dispositivo = sd.query_devices(candidato, kind="input")
        except Exception as error:
            errores.append(str(error))
            continue
        # Se pide 16 kHz directamente (lo que Whisper necesita); si el dispositivo
        # no lo admite se abre a su tasa nativa y luego se remuestrea.
        for tasa in dict.fromkeys((FRECUENCIA_WHISPER, int(dispositivo["default_samplerate"]))):
            try:
                flujo = sd.RawInputStream(
                    samplerate=tasa, blocksize=int(tasa * 0.064),
                    dtype="int16", channels=1, device=candidato, callback=callback,
                )
            except Exception as error:
                errores.append(str(error))
                continue
            if candidato is not None and not seleccion_automatica:
                config.DISPOSITIVO_ENTRADA_VOZ = candidato
            if seleccion_automatica:
                _ultimo_microfono_automatico = candidato
            return flujo
    raise VozNoDisponible("No se pudo abrir ningún micrófono: " + (errores[0] if errores else "sin entradas disponibles."))


def voces_locales_disponibles() -> list[str]:
    """Lista modelos Piper instalados; añadir un .onnx habilita una nueva voz."""
    carpeta = os.path.join(config.CARPETA_MODELOS, "piper")
    if not os.path.isdir(carpeta):
        return []
    return sorted(nombre for nombre in os.listdir(carpeta) if nombre.lower().endswith(".onnx"))


def seleccionar_voz_local(nombre_archivo: str) -> None:
    """Cambia de voz Piper sin tocar archivos ni requerir conexión."""
    global _voz_neural
    ruta = os.path.join(config.CARPETA_MODELOS, "piper", os.path.basename(nombre_archivo))
    if not os.path.isfile(ruta) or not ruta.lower().endswith(".onnx"):
        raise ValueError("La voz seleccionada no existe en modelos/piper.")
    config.RUTA_MODELO_TTS = ruta
    config.NOMBRE_VOZ_TTS = os.path.splitext(os.path.basename(ruta))[0]
    _voz_neural = None
    from nucleo import voz_local
    voz_local.reiniciar()


def verificar_voz() -> None:
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError as error:
        raise VozNoDisponible("Falta faster-whisper. Instálalo y reinicia Bark.") from error


def diagnosticar_microfono(duracion: float = 1.5) -> str:
    """Comprueba que el micrófono abre y entrega señal de audio real."""
    try:
        import sounddevice as sd
        import numpy as np

        candidatos = [config.DISPOSITIVO_ENTRADA_VOZ] if config.DISPOSITIVO_ENTRADA_VOZ is not None else [None] + [d["indice"] for d in dispositivos_entrada()]
        errores = []
        for candidato in dict.fromkeys(candidatos):
            try:
                dispositivo = sd.query_devices(candidato, kind="input")
                frecuencia = int(dispositivo["default_samplerate"])
                audio = sd.rec(int(frecuencia * duracion), samplerate=frecuencia, channels=1, dtype="int16", device=candidato)
                sd.wait()
                nivel_rms = int(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                if candidato is not None and config.DISPOSITIVO_ENTRADA_VOZ is None:
                    config.DISPOSITIVO_ENTRADA_VOZ = candidato
                estado = "Se detectó sonido." if nivel_rms >= config.VOZ_UMBRAL_RMS else "El dispositivo funciona, pero no detectó voz suficiente."
                return f"Micrófono abierto: {dispositivo['name']} (índice {dispositivo['index']}, {frecuencia} Hz). Nivel: {nivel_rms}; umbral: {config.VOZ_UMBRAL_RMS}. {estado}"
            except Exception as error:
                errores.append(str(error))
        return "No pude abrir ningún micrófono. " + (errores[0] if errores else "Windows no entregó dispositivos de entrada.")
    except Exception as error:
        return f"No pude grabar desde el micrófono predeterminado: {error}"


_bloqueo_whisper = threading.Lock()


def _cargar_modelo_whisper():
    with _bloqueo_whisper:
        return _cargar_modelo_whisper_sin_bloqueo()


def _cargar_modelo_whisper_sin_bloqueo():
    global _modelo_whisper
    if _modelo_whisper is None:
        try:
            from faster_whisper import WhisperModel
            os.makedirs(config.RUTA_MODELOS_WHISPER, exist_ok=True)
            print(f"Cargando Whisper {config.MODELO_WHISPER}; la primera vez se descargará el modelo local...")
            ruta_modelo = config.RUTA_MODELO_WHISPER if os.path.isfile(os.path.join(config.RUTA_MODELO_WHISPER, "model.bin")) else config.MODELO_WHISPER
            _modelo_whisper = WhisperModel(
                ruta_modelo,
                device="cpu",
                compute_type="int8",
                cpu_threads=max(2, min(4, config.HILOS_CPU)),
                download_root=config.RUTA_MODELOS_WHISPER,
            )
            print("Whisper listo.")
        except Exception as error:
            raise VozNoDisponible(f"No pude preparar Whisper: {error}") from error
    return _modelo_whisper


def _precargar_whisper() -> None:
    try:
        _cargar_modelo_whisper()
    except Exception:
        pass  # el error real se informa al primer intento de transcribir


def _transcribir(audio: np.ndarray) -> str:
    """Transcribe audio float32 a 16 kHz descartando alucinaciones de Whisper."""
    pico = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if pico < 1e-4:
        return ""
    if pico < 0.3:  # micrófonos bajos: normaliza para que Whisper reciba una señal útil
        audio = audio * min(0.7 / pico, 20.0)
    modelo = _cargar_modelo_whisper()
    opciones = dict(
        language="es", task="transcribe", beam_size=1, best_of=1, temperature=0.0,
        condition_on_previous_text=False, without_timestamps=True,
        no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.2,
    )
    try:
        segmentos, _ = modelo.transcribe(
            audio, vad_filter=True,
            vad_parameters=dict(threshold=0.45, min_silence_duration_ms=500, speech_pad_ms=200),
            **opciones,
        )
        segmentos = list(segmentos)
    except Exception:
        segmentos = list(modelo.transcribe(audio, vad_filter=False, **opciones)[0])
    partes = [
        segmento.text.strip() for segmento in segmentos
        if not (getattr(segmento, "no_speech_prob", 0.0) > 0.6 and getattr(segmento, "avg_logprob", 0.0) < -0.8)
    ]
    texto = " ".join(parte for parte in partes if parte).strip()
    return "" if es_alucinacion(texto) else texto


def _escuchar_frase_whisper(detener_evento=None, al_parcial=None, al_evento=None) -> str:
    """Captura una frase con detección adaptativa de voz y la transcribe localmente."""
    import sounddevice as sd
    global _ultimo_microfono_automatico

    cola = queue.Queue()
    avisos = []

    def callback(indata, frames, time_info, status):
        if status:
            avisos.append(str(status))
        cola.put(bytes(indata))

    flujo = _abrir_flujo_entrada(callback)
    frecuencia = int(flujo.samplerate)
    if al_evento:
        try:
            mensaje = f"Micrófono activo: {sd.query_devices(flujo.device, kind='input')['name']} · habla ahora."
        except Exception:
            mensaje = "Micrófono activo · habla ahora."
        print(f"[MICRÓFONO] {mensaje}")
        al_evento(mensaje)

    detector = DetectorVoz(
        umbral_minimo=max(6.0, config.VOZ_UMBRAL_RMS * 0.4),
        silencio_final=config.VOZ_SILENCIO_FINAL_SEGUNDOS,
        max_frase=config.VOZ_TIEMPO_MAX_FRASE_SEGUNDOS,
    )
    bloques, prebuffer, estado, hablaba = [], collections.deque(maxlen=10), "esperando", False
    with flujo:
        while not (detener_evento and detener_evento.is_set()):
            try:
                bloque = cola.get(timeout=0.2)
            except queue.Empty:
                continue
            muestras = np.frombuffer(bloque, dtype=np.int16)
            if not len(muestras):
                continue
            rms = float(np.sqrt(np.mean(muestras.astype(np.float32) ** 2)))
            estado = detector.procesar(rms, len(muestras) / frecuencia)
            if detector.hablando:
                if not hablaba:
                    bloques = list(prebuffer)  # conserva la primera sílaba de «Bark»
                    if al_evento:
                        al_evento("Te escucho…")
                bloques.append(bloque)
            else:
                if hablaba:
                    bloques = []  # ruido breve descartado
                prebuffer.append(bloque)
            hablaba = detector.hablando
            if avisos:
                print(f"[MICRÓFONO] {avisos.pop(0)}")
            if estado in {"fin", "vacio"}:
                break

    if detener_evento and detener_evento.is_set():
        return ""  # al pulsar «detener» no se transcribe nada a medias
    if not bloques:
        if detector.pico < 1.5:
            # Entrada sin señal: la siguiente escucha prueba otro micrófono.
            if config.DISPOSITIVO_ENTRADA_VOZ is None:
                _ultimo_microfono_automatico = flujo.device
            if al_evento:
                al_evento("El micrófono no entrega señal. Probaré con otra entrada o revisa el dispositivo seleccionado.")
        elif config.DISPOSITIVO_ENTRADA_VOZ is None:
            config.DISPOSITIVO_ENTRADA_VOZ = flujo.device  # tiene señal real: se queda fijo
        return ""
    if config.DISPOSITIVO_ENTRADA_VOZ is None:
        config.DISPOSITIVO_ENTRADA_VOZ = flujo.device
    audio = np.frombuffer(b"".join(bloques), dtype=np.int16).astype(np.float32) / 32768.0
    audio = remuestrear(audio, frecuencia)
    if os.getenv("BARK_VOZ_DEBUG") == "1":  # guarda lo que realmente oyó Whisper
        with wave.open(os.path.join(config.CARPETA_BASE, "resultados", "ultima_voz.wav"), "wb") as archivo:
            archivo.setnchannels(1)
            archivo.setsampwidth(2)
            archivo.setframerate(FRECUENCIA_WHISPER)
            archivo.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    if al_evento:
        al_evento(f"Audio recibido ({len(audio) / FRECUENCIA_WHISPER:.1f} s). Transcribiendo…")
    try:
        texto = _transcribir(audio)
    except Exception as error:
        raise VozNoDisponible(f"Whisper no pudo transcribir el audio: {error}") from error
    if texto and al_parcial:
        al_parcial(texto)
    return texto


def _reproducir_wav(datos: bytes, prefijo: str) -> None:
    """Reproduce bytes WAV y elimina el archivo temporal aun si falla Windows."""
    descriptor, ruta_wav = tempfile.mkstemp(prefix=prefijo, suffix=".wav")
    os.close(descriptor)
    try:
        with open(ruta_wav, "wb") as archivo_wav:
            archivo_wav.write(datos)
        winsound.PlaySound(ruta_wav, winsound.SND_FILENAME)
    finally:
        if os.path.exists(ruta_wav):
            os.remove(ruta_wav)


def _hablar_con_gemini(texto: str) -> None:
    """Sintetiza texto con Gemini TTS y reproduce el WAV devuelto."""
    if not config.GEMINI_API_KEY:
        raise requests.RequestException("Gemini no está configurado.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODELO_TTS}:generateContent"
    respuesta = _sesion_red().post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}, json={
        "contents": [{"role": "user", "parts": [{
            "text": texto[:1500],
            "speech_metadata": {"style": "Voz cálida, serena y natural en español de México."},
        }]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": config.GEMINI_VOZ}},
        },
    }, timeout=config.TIEMPO_ESPERA_VOZ_NUBE)
    respuesta.raise_for_status()
    try:
        datos = base64.b64decode(respuesta.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise requests.RequestException("Gemini no devolvió audio válido.") from error
    _reproducir_wav(datos, "bark_gemini_")


def _hablar_con_openai(texto: str) -> None:
    """Sintetiza texto con OpenAI y reproduce el WAV devuelto."""
    respuesta = _sesion_red().post("https://api.openai.com/v1/audio/speech", headers={
        "Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json",
    }, json={
        "model": config.OPENAI_MODELO_TTS, "voice": config.OPENAI_VOZ,
        "input": texto[:1500], "response_format": "wav",
        "instructions": "Habla en español de México con una voz femenina, serena, cálida y natural.",
    }, timeout=config.TIEMPO_ESPERA_VOZ_NUBE)
    respuesta.raise_for_status()
    _reproducir_wav(respuesta.content, "bark_openai_")


def hablar(texto: str) -> None:
    """Gemini -> OpenAI -> Piper; un fallo de nube nunca detiene Bark."""
    # Al elegir Gemini/auto se prueba Gemini primero. OpenAI es la segunda
    # opción en auto y también el respaldo de Gemini cuando no haya cuota.
    motores = []
    if config.MODO_VOZ in {"auto", "gemini"}:
        motores.append(("Gemini", _hablar_con_gemini, bool(config.GEMINI_API_KEY)))
    if config.MODO_VOZ in {"auto", "gemini", "openai"}:
        motores.append(("OpenAI", _hablar_con_openai, bool(config.OPENAI_API_KEY)))
    for nombre, sintetizar, configurado in motores:
        if not configurado:
            continue
        if time.monotonic() < _motores_nube_bloqueados_hasta[nombre]:
            print(f"[VOZ] {nombre} está en pausa tras un fallo; usando el siguiente respaldo.")
            continue
        try:
            sintetizar(texto)
            return
        except (requests.RequestException, OSError, RuntimeError) as error:
            respuesta_error = getattr(error, "response", None)
            estado = getattr(respuesta_error, "status_code", None)
            # Una respuesta sin cuota/autorización o un timeout no se reintenta
            # en cada frase. La próxima síntesis se desvía a los respaldos.
            _motores_nube_bloqueados_hasta[nombre] = time.monotonic() + (120 if estado in {401, 403, 429} else 60)
            if estado == 429:
                print(f"[VOZ] {nombre} limitó las solicitudes; queda en pausa temporal.")
            print(f"[VOZ] {nombre} no disponible; probando el siguiente respaldo: {error}")
    if config.MODO_VOZ != "local":
        print("[VOZ] Las voces de nube no están disponibles; usando Piper local.")
    try:
        from nucleo import voz_local
        voz_local.hablar_local(texto)
    except Exception as error:
        print(f"[VOZ] No se pudo sintetizar la respuesta: {error}")


def escuchar_frase(detener_evento=None, al_parcial=None, al_evento=None) -> str:
    """Escucha una orden con Whisper local y detector de silencio."""
    verificar_voz()
    return _escuchar_frase_whisper(detener_evento, al_parcial, al_evento)


def solicitar_permiso_por_voz(descripcion: str) -> bool:
    """Solicita una autorización inequívoca antes de una acción sensible."""
    print(f"[PERMISO POR VOZ] Bark quiere: {descripcion}")
    hablar("Esta acción requiere autorización. Responde sí para autorizar o no para cancelar.")
    respuesta = escuchar_frase().lower().strip()
    autorizado = respuesta in {"si", "sí", "autorizo", "confirmo", "adelante"}
    hablar("Autorizado." if autorizado else "Acción cancelada.")
    return autorizado


def _extraer_orden_activacion(frase: str, requiere_inicio: bool = False) -> str | None:
    """Extrae la orden que sigue a Bark.

    En escucha continua se exige que Bark vaya al inicio para que una
    transcripción accidental que lo mencione en medio no dispare acciones.
    """
    palabras = frase.lower().replace(",", " ").replace(".", " ").split()
    for indice, palabra in enumerate(palabras):
        if requiere_inicio and indice > 1:
            return None
        if palabra in config.PALABRAS_ACTIVACION or get_close_matches(palabra, config.PALABRAS_ACTIVACION, n=1, cutoff=0.72):
            return " ".join(palabras[indice + 1:]).strip(" ,.")
    return None


def _es_transcripcion_fantasma(frase: str) -> bool:
    texto = frase.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return "asistente se llama bark" in texto or "ordenes estan en espanol" in texto


def _debe_atender_sin_activacion(frase: str, hay_contexto: bool = False) -> bool:
    """Decide localmente si una frase parece dirigida a Bark.

    Atiende intención clara y seguimientos recientes; descarta charla casual,
    ruido y transcripciones demasiado cortas antes de gastar CPU en el modelo.
    """
    texto = " ".join(frase.lower().split())
    palabras = re.findall(r"[a-záéíóúüñ0-9]+", texto)
    if es_alucinacion(texto):
        return False
    if texto in {"salir", "terminar", "detente", "apagate", "apágate", "duerme"}:
        return True
    if len(palabras) < 2 or _es_transcripcion_fantasma(texto):
        return False
    if "?" in frase or texto.startswith(("que ", "qué ", "como ", "cómo ", "cuando ", "cuándo ", "donde ", "dónde ", "quien ", "quién ", "puedes ", "me puedes")):
        return True
    inicios_orden = (
        "abre", "cierra", "busca", "encuentra", "reproduce", "pon ", "pausa", "reanuda",
        "crea", "escribe", "convierte", "lee", "investiga", "dime", "explica", "ayudame",
        "ayúdame", "recuerda", "guarda", "diagnostica", "muestrame", "muéstrame", "salir",
        "terminar", "apagate", "apágate", "detente", "suspendete", "suspéndete", "duerme",
    )
    if texto.startswith(inicios_orden):
        return True
    seguimientos = ("si", "sí", "no", "tambien", "también", "ahora", "eso", "ese", "esa", "hazlo", "continua", "continúa", "mejor", "otra", "otro")
    return hay_contexto and (texto.startswith(seguimientos) or len(palabras) >= 4)


def ejecutar_modo_voz(procesar_mensaje, al_estado=None, detener_evento=None, al_transcripcion=None, al_respuesta=None, al_evento=None, procesar_toda_transcripcion=False) -> None:
    """Escucha por intención: Bark es opcional en el modo conversacional."""
    verificar_voz()
    # Whisper tarda unos segundos en cargar: se hace ya, mientras se habla la bienvenida.
    threading.Thread(target=_precargar_whisper, daemon=True).start()
    instruccion = "Modo conversacional activo. Habla naturalmente; responderé cuando detecte que te diriges a mí." if config.ESCUCHA_LIBRE else "Modo voz activo. Di Bark seguido de tu orden; di Bark salir para terminar."
    print(instruccion)
    # La síntesis local (Piper) puede tardar al cargar el modelo. Nunca debe
    # impedir que el micrófono abra, sobre todo en el primer uso de Bark.
    if al_evento:
        al_evento(instruccion)
    else:
        threading.Thread(target=hablar, args=(instruccion,), daemon=True).start()
    suspendido = False
    ultimo_turno = 0.0
    while not (detener_evento and detener_evento.is_set()):
        if al_estado:
            al_estado("escuchando")
        try:
            frase = escuchar_frase(detener_evento, al_evento=al_evento)
        except VozNoDisponible as error:
            print(f"[VOZ] {error}")
            hablar("No pude abrir el micrófono. Revisa el dispositivo de entrada.")
            return
        if not frase:
            # Un periodo sin voz no debe apagar el asistente.
            continue
        print(f"[OÍDO] {frase}")
        if al_transcripcion:
            al_transcripcion(frase)
        if _es_transcripcion_fantasma(frase):
            print("[VOZ] Transcripción descartada por posible eco o ruido.")
            continue
        frase_normalizada = frase.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
        if any(frase_normalizada.strip() == cierre for cierre in ("adios bark", "adios bar", "bark adios", "bar adios")):
            hablar("Hasta luego.")
            return
        orden = _extraer_orden_activacion(frase, requiere_inicio=True)
        if orden is None:
            if procesar_toda_transcripcion:
                orden = frase.strip()
            else:
                hay_contexto = time.monotonic() - ultimo_turno <= config.VENTANA_CONTEXTO_VOZ_SEGUNDOS
                if not config.ESCUCHA_LIBRE or not _debe_atender_sin_activacion(frase, hay_contexto):
                    print("[VOZ] Frase ambiental ignorada.")
                    continue
                orden = frase.strip()
        orden_normalizada = orden.lower().strip()
        if suspendido:
            if orden_normalizada not in {"despierta", "reanuda", "activar", "continua", "continuar", ""}:
                continue
            suspendido = False
            hablar("Te escucho.")
            orden = escuchar_frase(detener_evento, al_evento=al_evento)
            if not orden:
                return
        if orden_normalizada in {"suspendete", "suspéndete", "duerme", "espera"}:
            suspendido = True
            if al_estado:
                al_estado("suspendido")
            hablar("Quedo en espera. Di Bark despierta cuando me necesites.")
            continue
        if orden_normalizada in {"salir", "terminar", "apagate", "apágate", "detente", "cierra", "cierrate", "ciérrate", "adios", "adiós"}:
            hablar("Modo voz terminado.")
            return
        if not orden:
            hablar("Te escucho.")
            orden = escuchar_frase(detener_evento, al_evento=al_evento)
            if not orden:
                return
        if al_estado:
            al_estado("pensando")
        respuesta = procesar_mensaje(orden)
        ultimo_turno = time.monotonic()
        print(f"Bark: {respuesta}")
        if al_respuesta:
            al_respuesta(respuesta)
        if al_estado:
            al_estado("hablando")
        hablar(respuesta)
