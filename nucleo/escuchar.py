"""Voz de Bark: reconocimiento local y Piper como respaldo sin conexión."""

import os
import base64
import collections
import queue
import wave
import time
import re
import threading
import numpy as np
import requests
from difflib import get_close_matches

from config import config
from nucleo import audio_vivo
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
    """Elige el micrófono (None = automático) y lo recuerda para las próximas sesiones."""
    if indice is not None:
        dispositivos = {dispositivo["indice"] for dispositivo in dispositivos_entrada()}
        if indice not in dispositivos:
            raise ValueError("El micrófono seleccionado ya no está disponible.")
    global _microfono_confirmado
    config.DISPOSITIVO_ENTRADA_VOZ = indice
    if indice is not None:
        _recordar_microfono(indice)
    else:  # «Automático»: se vuelve a buscar cuál micrófono te oye
        _microfono_confirmado = None
        from nucleo import permisos
        permisos.guardar_preferencia("microfono_nombre", None)


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


_VIRTUALES = ("asignador de sonido", "controlador primario", "sound mapper", "primary sound capture",
              "mezcla estéreo", "mezcla estereo", "stereo mix", "what u hear", "altavoz", "speaker", "output")
_ORDEN_API = {"MME": 3, "Windows DirectSound": 2, "Windows WASAPI": 1}
# Índice que ya entregó voz en esta sesión (selección automática).
_microfono_confirmado = None
_escuchas_sin_voz = 0


def _clave_fisica(nombre: str) -> str:
    """MME recorta los nombres a 31 caracteres: así se agrupa el mismo micrófono en todas las API."""
    return " ".join(nombre.lower().split())[:31]


def candidatos_microfono() -> list[dict]:
    """Micrófonos físicos, uno por aparato, del más probable al menos probable.

    Orden: el que ya funcionó antes (guardado) › el predeterminado de Windows › otros
    micrófonos reales › entradas WDM-KS (suelen ser jacks de 3.5 mm vacíos que entregan
    un siseo y nunca voz). Se omiten mezcladores virtuales y perfiles Bluetooth
    «manos libres» de aparatos desconectados.
    """
    import sounddevice as sd
    from nucleo import permisos
    try:
        apis = [api["name"] for api in sd.query_hostapis()]
        dispositivos = list(sd.query_devices())
    except Exception:
        return []
    try:
        predeterminado = int(sd.default.device[0])
    except (TypeError, ValueError, IndexError):
        predeterminado = -1
    clave_predeterminada = _clave_fisica(dispositivos[predeterminado]["name"]) if 0 <= predeterminado < len(dispositivos) else ""
    guardado = _clave_fisica(permisos.preferencia("microfono_nombre") or "")
    mejores: dict[str, tuple] = {}
    for indice, d in enumerate(dispositivos):
        if d["max_input_channels"] < 1:
            continue
        nombre = d["name"]
        bajo = nombre.lower()
        if any(v in bajo for v in _VIRTUALES) or "hands-free" in bajo or "bthhfenum" in bajo or bajo.strip() in {"input ()", "input"}:
            continue
        api = apis[d["hostapi"]] if d["hostapi"] < len(apis) else ""
        clave = _clave_fisica(nombre)
        puntaje = (
            1 if guardado and clave == guardado else 0,
            1 if clave == clave_predeterminada else 0,
            0 if "WDM-KS" in api else 1,
            _ORDEN_API.get(api, 0),
            1 if ("micr" in bajo or "mic " in bajo or "array" in bajo) else 0,
        )
        info = {"indice": indice, "nombre": nombre, "api": api, "frecuencia": int(d["default_samplerate"]),
                "wdmks": "WDM-KS" in api, "puntaje": puntaje}
        if clave not in mejores or puntaje > mejores[clave]["puntaje"]:
            mejores[clave] = info
    return sorted(mejores.values(), key=lambda i: i["puntaje"], reverse=True)


def _abrir_flujo(candidato, callback):
    """Abre un micrófono a 16 kHz (lo que usa Whisper) o, si no puede, a su tasa nativa."""
    import sounddevice as sd
    dispositivo = sd.query_devices(candidato, kind="input")
    ultimo = None
    for tasa in dict.fromkeys((FRECUENCIA_WHISPER, int(dispositivo["default_samplerate"]))):
        try:
            return sd.RawInputStream(samplerate=tasa, blocksize=int(tasa * 0.064), dtype="int16",
                                     channels=1, device=candidato, callback=callback)
        except Exception as error:
            ultimo = error
    raise ultimo or RuntimeError("no se pudo abrir")


def _microfonos_a_escuchar() -> list[int]:
    """Uno solo si el usuario eligió o ya se confirmó uno; si no, hasta 3 a la vez."""
    if config.DISPOSITIVO_ENTRADA_VOZ is not None:
        return [config.DISPOSITIVO_ENTRADA_VOZ]
    candidatos = candidatos_microfono()
    indices = {c["indice"] for c in candidatos}
    if _microfono_confirmado in indices:
        return [_microfono_confirmado]
    from nucleo import permisos
    guardado = permisos.preferencia("microfono_nombre")
    if guardado and candidatos and _clave_fisica(candidatos[0]["nombre"]) == _clave_fisica(guardado):
        return [candidatos[0]["indice"]]
    # Las entradas WDM-KS pueden acaparar el hardware que usan las demás: solo si no hay otras.
    normales = [c["indice"] for c in candidatos if not c["wdmks"]]
    return (normales or [c["indice"] for c in candidatos])[:3]


def _abrir_flujo_entrada(callback):
    """Compatibilidad: abre el primer micrófono operativo."""
    errores = []
    for candidato in _microfonos_a_escuchar() + [None]:
        try:
            return _abrir_flujo(candidato, callback)
        except Exception as error:
            errores.append(str(error))
    raise VozNoDisponible("No se pudo abrir ningún micrófono: " + (errores[0] if errores else "sin entradas disponibles."))


def _nombre_dispositivo(indice) -> str:
    try:
        import sounddevice as sd
        return sd.query_devices(indice, kind="input")["name"]
    except Exception:
        return str(indice)


def _recordar_microfono(indice) -> None:
    """El micrófono que entregó voz real se usa directamente en la siguiente sesión."""
    global _microfono_confirmado
    _microfono_confirmado = indice
    try:
        from nucleo import permisos
        permisos.guardar_preferencia("microfono_nombre", _nombre_dispositivo(indice))
    except Exception:
        pass


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


def probar_microfonos(duracion: float = 3.0) -> list[dict]:
    """Graba a la vez de todos los micrófonos candidatos y mide voz, ruido y pico de cada uno."""
    import sounddevice as sd  # noqa: F401  (falla aquí si no está instalado)
    niveles: dict = {}
    picos: dict = {}
    flujos = []
    for c in candidatos_microfono()[:6]:
        indice = c["indice"]
        niveles[indice], picos[indice] = [], [0.0]

        def callback(indata, frames, time_info, status, indice=indice):
            muestras = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float32)
            if len(muestras):
                niveles[indice].append(float(np.sqrt(np.mean(muestras ** 2))))
                picos[indice].append(float(np.abs(muestras).max()))

        try:
            flujo = _abrir_flujo(indice, callback)
            flujo.start()
            flujos.append((c, flujo))
        except Exception as error:
            c["error"] = str(error)
            flujos.append((c, None))
    time.sleep(duracion)
    resultados = []
    for c, flujo in flujos:
        if flujo is not None:
            try:
                flujo.stop()
                flujo.close()
            except Exception:
                pass
        lista = sorted(niveles.get(c["indice"], []))
        ruido = lista[len(lista) // 5] if lista else 0.0     # percentil 20: el fondo
        voz = lista[int(len(lista) * 0.9)] if lista else 0.0  # percentil 90: lo más fuerte
        resultados.append({**c, "ruido": ruido, "voz": voz, "pico": max(picos.get(c["indice"], [0.0])),
                           "relacion": voz / max(ruido, 1.0)})
    return resultados


def diagnosticar_microfono(duracion: float = 3.0) -> str:
    """Prueba todos los micrófonos a la vez y elige el que mejor te oye. Habla durante la prueba."""
    from nucleo import permisos
    if permisos.estado_windows("microphone") == "denegado":
        return permisos.explicar_bloqueo("microphone")
    try:
        resultados = probar_microfonos(duracion)
    except Exception as error:
        return f"No pude grabar desde los micrófonos: {error}"
    if not resultados:
        return ("Windows no entregó ningún micrófono. Conecta uno o revisa que no esté desactivado "
                "en Configuración › Sistema › Sonido.")
    lineas = []
    for r in resultados:
        if r.get("error"):
            lineas.append(f"✗ {r['nombre']}: no abre ({r['error'][:60]})")
        else:
            lineas.append(f"• {r['nombre']} [{r['api']}]: voz {r['voz']:.0f} · fondo {r['ruido']:.0f} · pico {r['pico']:.0f}")
    abiertos = [r for r in resultados if not r.get("error")]
    con_voz = [r for r in abiertos if r["voz"] >= config.VOZ_UMBRAL_RMS * 0.4 and r["relacion"] >= 3]
    if con_voz:
        mejor = max(con_voz, key=lambda r: (r["relacion"], r["voz"]))
        if config.DISPOSITIVO_ENTRADA_VOZ is None:
            _recordar_microfono(mejor["indice"])
        conclusion = f"✓ Te oigo mejor por «{mejor['nombre']}». Lo usaré en el modo voz."
    elif abiertos and all(r["pico"] == 0 for r in abiertos):
        conclusion = ("Todos los micrófonos entregan silencio absoluto: suele ser el micrófono silenciado, una tecla "
                      "de silencio del teclado o el permiso de privacidad de Windows. Revisa Configuración › Privacidad "
                      "› Micrófono y el panel Sonido › Grabación.")
    else:
        conclusion = ("Ningún micrófono captó voz clara. Habla durante los 3 segundos de la prueba; si sigue igual, "
                      "sube el volumen de entrada en Sonido › Grabación › Propiedades › Niveles.")
    return f"Prueba de micrófonos ({duracion:.0f} s):\n" + "\n".join(lineas) + "\n" + conclusion


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


class _Entrada:
    """Estado de escucha de un micrófono: su detector y lo que lleva grabado."""

    def __init__(self, indice, frecuencia: int):
        self.indice, self.frecuencia = indice, frecuencia
        self.detector = DetectorVoz(
            umbral_minimo=max(6.0, config.VOZ_UMBRAL_RMS * 0.4),
            silencio_final=config.VOZ_SILENCIO_FINAL_SEGUNDOS,
            max_frase=config.VOZ_TIEMPO_MAX_FRASE_SEGUNDOS,
        )
        self.bloques: list = []
        self.prebuffer = collections.deque(maxlen=10)
        self.hablaba = False
        self.terminado = False
        self.pico_absoluto = 0.0

    def procesar(self, bloque: bytes) -> str:
        muestras = np.frombuffer(bloque, dtype=np.int16)
        if not len(muestras):
            return "esperando"
        self.pico_absoluto = max(self.pico_absoluto, float(np.abs(muestras.astype(np.int32)).max()))
        rms = float(np.sqrt(np.mean(muestras.astype(np.float32) ** 2)))
        audio_vivo.reportar("entrada", rms)  # el orbe de la interfaz late con tu voz
        estado = self.detector.procesar(rms, len(muestras) / self.frecuencia)
        if self.detector.hablando:
            if not self.hablaba:
                self.bloques = list(self.prebuffer)  # conserva la primera sílaba de «Bark»
            self.bloques.append(bloque)
        else:
            if self.hablaba:
                self.bloques = []  # ruido breve descartado
            self.prebuffer.append(bloque)
        self.hablaba = self.detector.hablando
        return estado


def _escuchar_frase_whisper(detener_evento=None, al_parcial=None, al_evento=None) -> str:
    """Captura una frase con detección adaptativa de voz y la transcribe localmente.

    Mientras no se sepa qué micrófono te oye, escucha en varios a la vez y usa el
    primero que capte una frase completa; ese queda recordado.
    """
    import contextlib
    from nucleo import permisos
    global _microfono_confirmado, _escuchas_sin_voz

    if permisos.estado_windows("microphone") == "denegado":
        raise VozNoDisponible(permisos.explicar_bloqueo("microphone"))

    cola = queue.Queue()
    avisos = []
    entradas: dict = {}
    flujos = []
    errores = []
    for candidato in _microfonos_a_escuchar():
        def callback(indata, frames, time_info, status, candidato=candidato):
            if status:
                avisos.append(str(status))
            cola.put((candidato, bytes(indata)))
        try:
            flujo = _abrir_flujo(candidato, callback)
        except Exception as error:
            errores.append(str(error))
            continue
        entradas[candidato] = _Entrada(candidato, int(flujo.samplerate))
        flujos.append(flujo)
    if not flujos:
        if config.DISPOSITIVO_ENTRADA_VOZ is not None:
            raise VozNoDisponible("No pude abrir el micrófono elegido: " + (errores[0] if errores else "no disponible")
                                  + ". Elige «Automático» o prueba otro.")
        try:
            flujo = _abrir_flujo(None, lambda indata, *_: cola.put((None, bytes(indata))))
        except Exception as error:
            raise VozNoDisponible("No se pudo abrir ningún micrófono: " + (errores[0] if errores else str(error))) from error
        entradas[None] = _Entrada(None, int(flujo.samplerate))
        flujos.append(flujo)

    if al_evento:
        nombres = [_nombre_dispositivo(i) for i in entradas]
        if len(nombres) > 1:
            mensaje = f"Escuchando en {len(nombres)} micrófonos a la vez ({', '.join(nombres)}): usaré el que te oiga. Habla ahora."
        else:
            mensaje = f"Micrófono activo: {nombres[0]} · habla ahora."
        print(f"[MICRÓFONO] {mensaje}")
        al_evento(mensaje)

    elegido = None
    aviso_escucha = False
    with contextlib.ExitStack() as pila:
        for flujo in flujos:
            pila.enter_context(flujo)
        while not (detener_evento and detener_evento.is_set()):
            try:
                candidato, bloque = cola.get(timeout=0.2)
            except queue.Empty:
                continue
            entrada = entradas.get(candidato)
            if entrada is None or entrada.terminado:
                continue
            estado = entrada.procesar(bloque)
            if entrada.detector.hablando and not aviso_escucha and al_evento:
                aviso_escucha = True
                al_evento("Te escucho…" if len(entradas) == 1 else f"Te escucho por «{_nombre_dispositivo(candidato)}»…")
            if avisos:
                print(f"[MICRÓFONO] {avisos.pop(0)}")
            if estado == "fin":
                elegido = entrada
                break
            if estado == "vacio":
                entrada.terminado = True
                if all(e.terminado for e in entradas.values()):
                    break

    if detener_evento and detener_evento.is_set():
        return ""  # al pulsar «detener» no se transcribe nada a medias
    if elegido is None or not elegido.bloques:
        _escuchas_sin_voz += 1
        picos = [e.pico_absoluto for e in entradas.values()]
        if al_evento and picos and max(picos) == 0:
            al_evento("Windows entrega silencio absoluto: el micrófono está silenciado, desactivado o bloqueado. "
                      "Revisa la tecla de silencio, Configuración › Privacidad › Micrófono y Sonido › Grabación.")
        elif al_evento and entradas and max(e.detector.pico for e in entradas.values()) < 1.5:
            al_evento("El micrófono no entrega señal. Prueba otro en el selector o pulsa «Probar micrófono».")
        # Un micrófono «confirmado» que deja de dar señal (p. ej. audífonos apagados) se vuelve a buscar.
        if _microfono_confirmado is not None and _escuchas_sin_voz >= 2 and max(picos or [0]) < 3:
            _microfono_confirmado = None
        return ""
    _escuchas_sin_voz = 0
    if config.DISPOSITIVO_ENTRADA_VOZ is None and elegido.indice is not None and _microfono_confirmado != elegido.indice:
        _recordar_microfono(elegido.indice)  # solo tras captar voz real, nunca por silencio
        if al_evento and len(entradas) > 1:
            al_evento(f"Micrófono elegido: {_nombre_dispositivo(elegido.indice)}. Lo usaré de aquí en adelante.")
    audio = np.frombuffer(b"".join(elegido.bloques), dtype=np.int16).astype(np.float32) / 32768.0
    audio = remuestrear(audio, elegido.frecuencia)
    if os.getenv("BARK_VOZ_DEBUG") == "1":  # guarda lo que realmente oyó Whisper
        with wave.open(os.path.join(config.CARPETA_BD, "ultima_voz.wav"), "wb") as archivo:
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


def _audio_de_bytes(datos: bytes, tipo_mime: str = "") -> tuple[np.ndarray, int]:
    """WAV (OpenAI) o PCM crudo L16 (Gemini) → (audio int16 mono, frecuencia)."""
    canales = 1
    if datos[:4] == b"RIFF" and datos[8:12] == b"WAVE":
        posicion, frecuencia, bits, inicio = 12, 24000, 16, None
        while posicion + 8 <= len(datos):
            ident = datos[posicion:posicion + 4]
            tamano = int.from_bytes(datos[posicion + 4:posicion + 8], "little")
            if ident == b"fmt ":
                canales = int.from_bytes(datos[posicion + 10:posicion + 12], "little") or 1
                frecuencia = int.from_bytes(datos[posicion + 12:posicion + 16], "little")
                bits = int.from_bytes(datos[posicion + 22:posicion + 24], "little")
            elif ident == b"data":
                inicio = posicion + 8  # en streaming el tamaño puede venir como 0xFFFFFFFF
                break
            posicion += 8 + tamano + (tamano & 1)
        if inicio is None or bits != 16:
            raise ValueError("El audio WAV recibido no es PCM de 16 bits.")
        crudo = datos[inicio:]
    else:
        coincidencia = re.search(r"rate=(\d+)", tipo_mime or "")
        frecuencia = int(coincidencia.group(1)) if coincidencia else 24000
        crudo = datos
    ancho = 2 * canales
    audio = np.frombuffer(crudo[:len(crudo) // ancho * ancho], dtype="<i2")
    if canales > 1:
        audio = audio.reshape(-1, canales).mean(axis=1)
    return audio.astype(np.int16), frecuencia


def _sintetizar_gemini(texto: str, espera: float | None = None) -> tuple[np.ndarray, int]:
    """Sintetiza texto con Gemini TTS y devuelve el audio listo para reproducir."""
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
    }, timeout=espera or config.TIEMPO_ESPERA_VOZ_NUBE)
    respuesta.raise_for_status()
    try:
        parte = respuesta.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        return _audio_de_bytes(base64.b64decode(parte["data"]), parte.get("mimeType", ""))
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise requests.RequestException("Gemini no devolvió audio válido.") from error


def _sintetizar_openai(texto: str, espera: float | None = None) -> tuple[np.ndarray, int]:
    """Sintetiza texto con OpenAI TTS y devuelve el audio listo para reproducir."""
    respuesta = _sesion_red().post("https://api.openai.com/v1/audio/speech", headers={
        "Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json",
    }, json={
        "model": config.OPENAI_MODELO_TTS, "voice": config.OPENAI_VOZ,
        "input": texto[:1500], "response_format": "wav",
        "instructions": "Habla en español de México con una voz femenina, serena, cálida y natural.",
    }, timeout=espera or config.TIEMPO_ESPERA_VOZ_NUBE)
    respuesta.raise_for_status()
    try:
        return _audio_de_bytes(respuesta.content)
    except ValueError as error:
        raise requests.RequestException("OpenAI no devolvió audio válido.") from error


def _motores_nube() -> list[tuple[str, object]]:
    """Voces de nube configuradas y no pausadas, en el orden de preferencia elegido."""
    motores = []
    if config.MODO_VOZ in {"auto", "gemini"} and config.GEMINI_API_KEY:
        motores.append(("Gemini", _sintetizar_gemini))
    if config.MODO_VOZ in {"auto", "gemini", "openai"} and config.OPENAI_API_KEY:
        motores.append(("OpenAI", _sintetizar_openai))
    ahora = time.monotonic()
    for nombre, _ in motores:
        if ahora < _motores_nube_bloqueados_hasta[nombre]:
            print(f"[VOZ] {nombre} está en pausa tras un fallo; usando el siguiente respaldo.")
    return [(nombre, funcion) for nombre, funcion in motores if ahora >= _motores_nube_bloqueados_hasta[nombre]]


def _pausar_motor(nombre: str, error: Exception) -> None:
    # Una respuesta sin cuota/autorización o un timeout no se reintenta
    # en cada frase. La próxima síntesis se desvía a los respaldos.
    estado = getattr(getattr(error, "response", None), "status_code", None)
    _motores_nube_bloqueados_hasta[nombre] = time.monotonic() + (120 if estado in {401, 403, 429} else 60)
    if estado == 429:
        print(f"[VOZ] {nombre} limitó las solicitudes; queda en pausa temporal.")
    print(f"[VOZ] {nombre} no disponible; probando el siguiente respaldo: {error}")


def partes_para_nube(frases: list[str]) -> list[str]:
    """Primera frase sola (empieza a sonar enseguida) y el resto en un bloque.

    Así la voz de nube arranca rápido como en los modos Live, sin multiplicar
    las solicitudes (ni chocar con los límites por minuto de las APIs).
    """
    if len(frases) <= 1:
        return list(frases)
    return [frases[0], " ".join(frases[1:])]


def hablar(texto: str) -> None:
    """Gemini → OpenAI → Piper en streaming; un fallo de nube nunca detiene Bark.

    Mientras suena la primera frase se sintetiza el resto, el nivel de la voz se
    publica para el orbe y ``audio_vivo.interrumpir()`` la corta al instante.
    """
    from nucleo import voz_local
    motores = _motores_nube()
    if not motores:
        if config.MODO_VOZ != "local":
            print("[VOZ] Las voces de nube no están disponibles; usando Piper local.")
        try:
            voz_local.hablar_local(texto)
        except Exception as error:
            print(f"[VOZ] No se pudo sintetizar la respuesta: {error}")
        return
    frases = voz_local.dividir_frases(voz_local.preparar_texto(texto))
    if not frases:
        return
    with voz_local._bloqueo_reproduccion:
        audio_vivo.INTERRUPCION.clear()
        fin = threading.Event()
        cola: queue.Queue = queue.Queue(maxsize=4)

        def entregar(elemento) -> bool:
            while not (fin.is_set() or audio_vivo.INTERRUPCION.is_set()):
                try:
                    cola.put(elemento, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def productor() -> None:
            try:
                for numero, parte in enumerate(partes_para_nube(frases)):
                    if fin.is_set() or audio_vivo.INTERRUPCION.is_set():
                        break
                    audio = None
                    while motores and audio is None:
                        nombre, sintetizar = motores[0]
                        try:
                            # El resto se sintetiza mientras suena la primera frase: puede tardar más.
                            audio = sintetizar(parte, config.TIEMPO_ESPERA_VOZ_NUBE * (1 if numero == 0 else 2.5))
                        except (requests.RequestException, OSError, RuntimeError, ValueError) as error:
                            _pausar_motor(nombre, error)
                            motores.pop(0)
                    if audio is not None:
                        if not entregar((audio[0], audio[1], 0.22)):
                            break
                        continue
                    for frase in voz_local.dividir_frases(parte):  # ninguna nube respondió: Piper
                        if fin.is_set() or audio_vivo.INTERRUPCION.is_set():
                            break
                        audio_local, frecuencia = voz_local.sintetizar(frase)
                        if not entregar((audio_local, frecuencia, voz_local._pausa_despues(frase))):
                            break
            except Exception as error:
                entregar(error)
            finally:
                entregar(None)

        threading.Thread(target=productor, name="bark-tts-nube", daemon=True).start()
        salida = voz_local._Salida()
        try:
            while not audio_vivo.INTERRUPCION.is_set():
                try:
                    elemento = cola.get(timeout=0.1)
                except queue.Empty:
                    continue
                if elemento is None:
                    break
                if isinstance(elemento, Exception):
                    print(f"[VOZ] No se pudo sintetizar la respuesta: {elemento}")
                    break
                salida.escribir(*elemento)
        finally:
            fin.set()
            salida.cerrar()


def _vigilar_interrupcion(detener: threading.Event) -> None:
    """Mientras Bark habla, si tú hablas fuerte por encima de su voz, lo interrumpe.

    Sin cancelación de eco el micrófono también oye a Bark por los altavoces; por
    eso el umbral es alto y la opción es voluntaria: funciona mejor con audífonos.
    """
    try:
        indices = _microfonos_a_escuchar()
        cola: queue.Queue = queue.Queue()
        flujo = _abrir_flujo(indices[0] if indices else None, lambda indata, *_: cola.put(bytes(indata)))
    except Exception:
        return
    seguidos = 0
    with flujo:
        while not detener.is_set():
            try:
                bloque = cola.get(timeout=0.1)
            except queue.Empty:
                continue
            muestras = np.frombuffer(bloque, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(muestras ** 2))) if len(muestras) else 0.0
            seguidos = seguidos + 1 if rms >= config.VOZ_UMBRAL_INTERRUPCION else 0
            if seguidos >= 5:  # ~0.3 s de voz fuerte y continua, no un golpe
                print("[VOZ] Te oí hablar: interrumpo la respuesta.")
                audio_vivo.interrumpir()
                return


def hablar_interrumpible(texto: str) -> bool:
    """Habla y, si está activado, escucha a la vez para dejarte interrumpir. True si lo interrumpiste."""
    inicio = time.monotonic()
    vigia_detener = threading.Event()
    vigia = None
    if config.INTERRUMPIR_CON_VOZ:
        vigia = threading.Thread(target=_vigilar_interrupcion, args=(vigia_detener,), name="bark-vigia", daemon=True)
        vigia.start()
    try:
        hablar(texto)
    finally:
        vigia_detener.set()
        if vigia is not None:
            vigia.join(timeout=1.0)  # libera el micrófono antes de volver a escuchar
    return audio_vivo.interrumpido_desde(inicio)


def escuchar_frase(detener_evento=None, al_parcial=None, al_evento=None) -> str:
    """Escucha una orden con Whisper local y detector de silencio."""
    verificar_voz()
    return _escuchar_frase_whisper(detener_evento, al_parcial, al_evento)


_AFIRMACIONES = {"si", "autorizo", "confirmo", "adelante", "claro", "dale", "hazlo", "ok", "okay", "va",
                 "sale", "correcto", "afirmativo", "procede", "acepto", "permitido", "permito"}
_NEGACIONES = {"no", "cancela", "cancelar", "nunca", "negativo", "detente", "alto"}


def es_afirmacion(texto: str):
    """True = sí, False = no, None = no se entendió. Tolera «Sí.», «sí, adelante», «claro que sí»."""
    palabras = re.findall(r"[a-zñ]+", _sin_acentos(texto or "").lower())
    if not palabras:
        return None
    if any(p in _NEGACIONES for p in palabras[:4]):
        return False
    if any(p in _AFIRMACIONES for p in palabras[:4]):
        return True
    return None


def solicitar_permiso_por_voz(descripcion: str) -> bool:
    """Solicita una autorización inequívoca antes de una acción sensible (dos intentos)."""
    print(f"[PERMISO POR VOZ] Bark quiere: {descripcion}")
    hablar(f"Necesito tu permiso para {descripcion}. ¿Lo autorizas? Responde sí o no.")
    for intento in range(2):
        decision = es_afirmacion(escuchar_frase())
        if decision is not None:
            hablar("Autorizado." if decision else "Acción cancelada.")
            return decision
        if intento == 0:
            hablar("No te entendí. Di sí para autorizar o no para cancelar.")
    hablar("No escuché una respuesta clara; la acción se cancela.")
    return False


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
            if al_evento:
                al_evento(str(error))
            hablar("No pude usar el micrófono. Te dejé el motivo en pantalla.")
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
        if hablar_interrumpible(respuesta) and al_evento:
            al_evento("Interrumpido. Te escucho.")
