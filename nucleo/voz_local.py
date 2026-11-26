"""
voz_local.py
Voz local de Bark (Piper) pensada para sonar fluida y empezar a hablar enseguida.

Qué hace distinto a la síntesis de un solo bloque:
- Divide la respuesta en frases y sintetiza la siguiente MIENTRAS suena la actual,
  así que el primer sonido sale tras ~1 frase y no tras toda la respuesta.
- Limpia lo que no se debe leer (markdown, emojis, URLs, rutas, listas largas).
- Normaliza símbolos y cifras para que se pronuncien como en español.
- Recorta el silencio que Piper deja en los bordes y pone pausas naturales entre frases.
- Más variación de entonación/ritmo (noise_scale / noise_w_scale) para evitar el tono plano.
- Reproduce desde memoria con sounddevice: sin archivos temporales ni cortes entre frases.
"""

from __future__ import annotations

import io
import os
import queue
import re
import threading
import wave

import numpy as np

from config import config
from nucleo import audio_vivo

_voz = None
_ruta_cargada = None
_cfg_sintesis = None
_bloqueo_carga = threading.Lock()
# Reentrante: la cadena de voces de nube puede caer a Piper sin soltar el turno.
_bloqueo_reproduccion = threading.RLock()  # dos respuestas nunca se solapan
# Compartido con la interfaz y las voces de nube: interrumpir corta cualquier voz.
_interrumpir = audio_vivo.INTERRUPCION


# ============================================================
# Carga del modelo
# ============================================================

def cargar_voz():
    """Carga (una sola vez) la voz Piper elegida; recarga si el usuario cambia de voz."""
    global _voz, _ruta_cargada
    with _bloqueo_carga:
        ruta = config.RUTA_MODELO_TTS
        if _voz is None or _ruta_cargada != ruta:
            if not os.path.isfile(ruta):
                raise FileNotFoundError(f"Falta el modelo neuronal de voz: {ruta}")
            from piper import PiperVoice
            _voz = PiperVoice.load(ruta)
            _ruta_cargada = ruta
        return _voz


def reiniciar() -> None:
    """Fuerza recargar la voz en el siguiente uso (al cambiar de voz en la interfaz)."""
    global _voz
    _voz = None


def precargar() -> None:
    """Carga y calienta la voz en segundo plano para que la primera respuesta no espere."""
    try:
        sintetizar("Listo.")
    except Exception:
        pass


def detener_voz() -> None:
    """Corta la locución en curso (la interfaz puede llamarlo al pulsar «detener»)."""
    audio_vivo.interrumpir()


def _configuracion():
    """SynthesisConfig con prosodia más expresiva; degrada si la versión de Piper es antigua."""
    global _cfg_sintesis
    if _cfg_sintesis is None:
        try:
            from piper import SynthesisConfig
        except ImportError:
            _cfg_sintesis = False
            return None
        for parametros in (
            dict(length_scale=config.VOZ_RITMO_PIPER, noise_scale=config.VOZ_VARIACION_PIPER,
                 noise_w_scale=config.VOZ_RITMO_VARIABLE_PIPER),
            dict(length_scale=config.VOZ_RITMO_PIPER),
        ):
            try:
                _cfg_sintesis = SynthesisConfig(**parametros)
                break
            except TypeError:
                continue
        else:
            _cfg_sintesis = False
    return _cfg_sintesis or None


# ============================================================
# Preparación del texto
# ============================================================

_EMOJIS = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
_UNIDADES = (
    (r"\bGB\b", " gigabytes"), (r"\bMB\b", " megabytes"), (r"\bKB\b", " kilobytes"),
    (r"°C", " grados"), (r"%", " por ciento"), (r"&", " y "), (r"→|->|=>", " a "),
    (r"\bPID\b", " identificador"), (r"\bvs\.?\b", " contra "),
)


def preparar_texto(texto: str, max_caracteres: int = 420) -> str:
    """Convierte la respuesta del chat en texto cómodo de escuchar."""
    t = str(texto or "")
    t = re.sub(r"```.*?```", " Te dejé el código en pantalla. ", t, flags=re.S)
    lineas = [l for l in t.splitlines() if l.strip()]
    viñetas = sum(1 for l in lineas if re.match(r"\s*(?:[-*•]|\d+[.)])\s+", l))
    if len(lineas) >= 6 or viñetas >= 4:
        # Listas y resultados largos: se lee el encabezado y el detalle queda en pantalla.
        t = lineas[0] + (". Te dejé el detalle en pantalla." if len(lineas) > 1 else "")
    t = re.sub(r"https?://\S+|www\.\S+", " el enlace ", t)
    t = re.sub(r"[A-Za-z]:\\[^\s]*", " la ruta indicada ", t)
    t = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", ". ", t)
    t = re.sub(r"[*_`#>|~]+", " ", t)
    t = _EMOJIS.sub("", t)
    for patron, reemplazo in _UNIDADES:
        t = re.sub(patron, reemplazo, t)
    t = re.sub(r"(\d)\.(\d)", r"\1 punto \2", t)
    t = re.sub(r"(\d{1,2}):(\d{2})\b", r"\1 \2", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"([.!?…])\s*\.\s*", r"\1 ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > max_caracteres:
        corte = max(t.rfind(". ", 0, max_caracteres), t.rfind("? ", 0, max_caracteres), t.rfind("! ", 0, max_caracteres))
        t = (t[:corte + 1] if corte > 60 else t[:max_caracteres].rsplit(" ", 1)[0]) + " El resto está en pantalla."
    return t


def dividir_frases(texto: str, maximo: int = 200) -> list[str]:
    """Frases de tamaño cómodo: ni cortes sueltos de 3 palabras ni bloques enormes."""
    crudas = [f.strip() for f in re.split(r"(?<=[.!?…;:])\s+", texto) if f.strip()]
    frases: list[str] = []
    for frase in crudas:
        while len(frase) > maximo:  # frase muy larga: se corta en la coma más cercana
            corte = frase.rfind(", ", 0, maximo)
            if corte < 40:
                corte = frase.rfind(" ", 0, maximo)
            if corte <= 0:
                break
            frases.append(frase[:corte + 1].strip())
            frase = frase[corte + 1:].strip()
        if frase:
            frases.append(frase)
    unidas: list[str] = []
    for frase in frases:  # una frase diminuta («Listo.») se pega a la siguiente
        if unidas and len(unidas[-1]) < 18 and len(unidas[-1]) + len(frase) < maximo:
            unidas[-1] = f"{unidas[-1]} {frase}"
        else:
            unidas.append(frase)
    return unidas


def _pausa_despues(frase: str) -> float:
    """Segundos de silencio tras la frase, según su puntuación final."""
    final = frase.rstrip()[-1:]
    return {"?": 0.28, "!": 0.26, ".": 0.22, "…": 0.30, ":": 0.16, ";": 0.16, ",": 0.12}.get(final, 0.18)


# ============================================================
# Síntesis y audio
# ============================================================

def _recortar_silencio(audio: np.ndarray, frecuencia: int) -> np.ndarray:
    """Quita el silencio de los bordes (deja 40 ms) y suaviza para evitar clics."""
    if len(audio) == 0:
        return audio
    magnitud = np.abs(audio.astype(np.int32))
    activos = np.nonzero(magnitud > max(60, int(magnitud.max() * 0.02)))[0]
    if len(activos) == 0:
        return audio
    margen = int(frecuencia * 0.04)
    recorte = audio[max(0, activos[0] - margen):activos[-1] + margen].astype(np.float32)
    fundido = min(len(recorte) // 2, int(frecuencia * 0.008))
    if fundido > 0:
        rampa = np.linspace(0.0, 1.0, fundido, dtype=np.float32)
        recorte[:fundido] *= rampa
        recorte[-fundido:] *= rampa[::-1]
    return recorte.astype(np.int16)


def sintetizar(frase: str) -> tuple[np.ndarray, int]:
    """Frase → (audio int16 mono, frecuencia), usando el mismo método de Piper que ya funcionaba."""
    voz = cargar_voz()
    memoria = io.BytesIO()
    with wave.open(memoria, "wb") as destino:
        configuracion = _configuracion()
        try:
            if configuracion is not None:
                voz.synthesize_wav(frase, destino, syn_config=configuracion)
            else:
                voz.synthesize_wav(frase, destino)
        except TypeError:  # Piper antiguo sin syn_config
            voz.synthesize_wav(frase, destino)
    memoria.seek(0)
    with wave.open(memoria, "rb") as origen:
        frecuencia = origen.getframerate()
        audio = np.frombuffer(origen.readframes(origen.getnframes()), dtype=np.int16)
    return _recortar_silencio(audio, frecuencia), frecuencia


class _Salida:
    """Altavoz en streaming con sounddevice; si falla, acumula y reproduce con winsound."""

    def __init__(self):
        self._flujo = None
        self._frecuencia = None
        self._acumulado: list[np.ndarray] = []
        self._respaldo = False

    def _abrir(self, frecuencia: int) -> None:
        if self._flujo is not None or self._respaldo:
            return
        try:
            import sounddevice as sd
            self._flujo = sd.OutputStream(samplerate=frecuencia, channels=1, dtype="int16", latency="low")
            self._flujo.start()
            self._frecuencia = frecuencia
        except Exception:
            self._flujo = None
            self._respaldo = True
            self._frecuencia = frecuencia

    def _cambiar_frecuencia(self, frecuencia: int) -> None:
        """Gemini (24 kHz) y Piper (22 kHz) pueden turnarse en una misma respuesta."""
        if self._frecuencia in (None, frecuencia):
            return
        if self._flujo is not None:
            try:
                self._flujo.stop()
            finally:
                self._flujo.close()
            self._flujo = None
        elif self._respaldo:
            self._reproducir_respaldo()

    def escribir(self, audio: np.ndarray, frecuencia: int, pausa: float) -> None:
        self._cambiar_frecuencia(frecuencia)
        self._abrir(frecuencia)
        volumen = float(np.clip(config.VOZ_VOLUMEN, 0.0, 1.0))
        datos = np.concatenate([(audio.astype(np.float32) * volumen).astype(np.int16),
                                np.zeros(int(frecuencia * pausa), dtype=np.int16)])
        if self._respaldo:
            self._acumulado.append(datos)
            return
        # Trozos de 40 ms: la interrupción es inmediata y el orbe sigue la voz con fluidez.
        paso = max(1, frecuencia // 25)
        for inicio in range(0, len(datos), paso):
            if _interrumpir.is_set():
                return
            trozo = datos[inicio:inicio + paso]
            audio_vivo.reportar_bloque("salida", trozo)
            self._flujo.write(trozo.reshape(-1, 1))

    def _reproducir_respaldo(self) -> None:
        if self._acumulado and not _interrumpir.is_set():
            import winsound
            memoria = io.BytesIO()
            with wave.open(memoria, "wb") as destino:
                destino.setnchannels(1)
                destino.setsampwidth(2)
                destino.setframerate(self._frecuencia)
                destino.writeframes(np.concatenate(self._acumulado).tobytes())
            winsound.PlaySound(memoria.getvalue(), winsound.SND_MEMORY)
        self._acumulado = []

    def cerrar(self) -> None:
        try:
            if self._flujo is not None:
                try:
                    if _interrumpir.is_set():
                        self._flujo.abort()  # interrumpido: se calla ya, sin terminar el búfer
                    else:
                        self._flujo.stop()  # espera a que termine de sonar lo ya enviado
                finally:
                    self._flujo.close()
            elif self._respaldo:
                self._reproducir_respaldo()
        finally:
            audio_vivo.reportar("salida", 0.0)


def hablar_local(texto: str) -> None:
    """Habla el texto con Piper en streaming por frases."""
    frases = dividir_frases(preparar_texto(texto))
    if not frases:
        return
    with _bloqueo_reproduccion:
        _interrumpir.clear()
        cola: queue.Queue = queue.Queue(maxsize=3)

        def productor() -> None:
            try:
                for frase in frases:
                    if _interrumpir.is_set():
                        break
                    audio, frecuencia = sintetizar(frase)
                    cola.put((audio, frecuencia, _pausa_despues(frase)))
            except Exception as error:
                cola.put(error)
            finally:
                cola.put(None)

        threading.Thread(target=productor, name="bark-tts", daemon=True).start()
        salida = _Salida()
        try:
            while True:
                elemento = cola.get()
                if elemento is None:
                    break
                if isinstance(elemento, Exception):
                    raise elemento
                if not _interrumpir.is_set():
                    salida.escribir(*elemento)
        finally:
            try:
                salida.cerrar()
            finally:
                _interrumpir.set()  # el productor deja de sintetizar
                while not cola.empty():  # y no queda bloqueado esperando espacio
                    try:
                        cola.get_nowait()
                    except queue.Empty:
                        break
