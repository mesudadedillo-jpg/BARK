"""Pruebas de lo que alimenta al HUD: audio en vivo, voces de nube, interrupción y multimedia."""

import io
import json
import threading
import time
import unittest
import wave
from unittest.mock import patch

import numpy as np

from config import config
from interfaz.hud import mezclar, texto_seguro
from nucleo import audio_vivo, escuchar, medios, voz_local


def _wav(muestras: np.ndarray, frecuencia: int, canales: int = 1) -> bytes:
    memoria = io.BytesIO()
    with wave.open(memoria, "wb") as destino:
        destino.setnchannels(canales)
        destino.setsampwidth(2)
        destino.setframerate(frecuencia)
        destino.writeframes(muestras.astype("<i2").tobytes())
    return memoria.getvalue()


class TestAudioDeNube(unittest.TestCase):
    def test_wav_de_openai(self):
        audio, frecuencia = escuchar._audio_de_bytes(_wav(np.arange(100, dtype=np.int16), 24000))
        self.assertEqual(frecuencia, 24000)
        self.assertEqual(len(audio), 100)
        self.assertEqual(int(audio[5]), 5)

    def test_wav_en_streaming_con_tamano_desconocido(self):
        datos = bytearray(_wav(np.ones(50, dtype=np.int16), 22050))
        posicion = datos.index(b"data")
        datos[posicion + 4:posicion + 8] = b"\xff\xff\xff\xff"
        audio, frecuencia = escuchar._audio_de_bytes(bytes(datos))
        self.assertEqual((len(audio), frecuencia), (50, 22050))

    def test_wav_estereo_se_mezcla_a_mono(self):
        estereo = np.array([100, 300, 100, 300], dtype=np.int16)
        audio, _ = escuchar._audio_de_bytes(_wav(estereo, 16000, canales=2))
        self.assertEqual(audio.tolist(), [200, 200])

    def test_pcm_crudo_de_gemini_usa_la_frecuencia_del_mime(self):
        crudo = np.arange(10, dtype="<i2").tobytes() + b"\x01"  # byte suelto: se descarta
        audio, frecuencia = escuchar._audio_de_bytes(crudo, "audio/L16;codec=pcm;rate=24000")
        self.assertEqual((len(audio), frecuencia), (10, 24000))

    def test_primera_frase_sola_y_el_resto_en_un_bloque(self):
        self.assertEqual(escuchar.partes_para_nube(["Hola."]), ["Hola."])
        self.assertEqual(escuchar.partes_para_nube(["Uno.", "Dos.", "Tres."]), ["Uno.", "Dos. Tres."])


class TestAudioVivo(unittest.TestCase):
    def test_niveles_acotados(self):
        self.assertEqual(audio_vivo.nivel_desde_rms(0), 0.0)
        self.assertEqual(audio_vivo.nivel_desde_rms(30), 0.0)  # ruido de fondo
        self.assertGreater(audio_vivo.nivel_desde_rms(2000), audio_vivo.nivel_desde_rms(300))
        self.assertEqual(audio_vivo.nivel_desde_rms(32767), 1.0)

    def test_nivel_caduca(self):
        audio_vivo.reportar("entrada", 3000)
        self.assertGreater(audio_vivo.nivel("entrada"), 0.5)
        self.assertEqual(audio_vivo.nivel("entrada", vigencia=-1), 0.0)

    def test_interrumpir_marca_el_instante(self):
        inicio = time.monotonic()
        audio_vivo.interrumpir()
        self.assertTrue(audio_vivo.INTERRUPCION.is_set())
        self.assertTrue(audio_vivo.interrumpido_desde(inicio))
        audio_vivo.INTERRUPCION.clear()


class _FlujoFalso:
    """Sustituye al altavoz: «reproduce» en tiempo real sin hacer ruido."""

    def __init__(self, samplerate, **_):
        self.frecuencia = samplerate
        self.escrito = 0
        self.abortado = False

    def start(self): pass
    def stop(self): pass
    def close(self): pass
    def abort(self): self.abortado = True

    def write(self, datos):
        self.escrito += len(datos)
        time.sleep(len(datos) / self.frecuencia)


class TestSalidaInterrumpible(unittest.TestCase):
    def setUp(self):
        audio_vivo.INTERRUPCION.clear()

    def test_publica_nivel_y_se_corta_al_interrumpir(self):
        import sounddevice
        flujos = []

        def crear(**kw):
            flujos.append(_FlujoFalso(**kw))
            return flujos[-1]

        voz = (np.sin(np.linspace(0, 2000, 22050 * 2)) * 8000).astype(np.int16)  # 2 s de «voz»
        niveles = []
        with patch.object(sounddevice, "OutputStream", side_effect=crear):
            salida = voz_local._Salida()
            hilo = threading.Thread(target=salida.escribir, args=(voz, 22050, 0.0))
            inicio = time.monotonic()
            hilo.start()
            time.sleep(0.25)
            niveles.append(audio_vivo.nivel("salida"))
            audio_vivo.interrumpir()
            hilo.join(timeout=2)
            salida.cerrar()
        self.assertGreater(niveles[0], 0.5, "el orbe debe recibir el nivel de la voz")
        self.assertLess(time.monotonic() - inicio, 0.8, "interrumpir debe cortar en ~40 ms, no al final")
        self.assertLess(flujos[0].escrito, len(voz))
        self.assertTrue(flujos[0].abortado)
        audio_vivo.INTERRUPCION.clear()

    def test_cambio_de_frecuencia_reabre_el_altavoz(self):
        import sounddevice
        flujos = []
        with patch.object(sounddevice, "OutputStream", side_effect=lambda **kw: flujos.append(_FlujoFalso(**kw)) or flujos[-1]):
            salida = voz_local._Salida()
            salida.escribir(np.zeros(240, dtype=np.int16), 24000, 0.0)
            salida.escribir(np.zeros(220, dtype=np.int16), 22050, 0.0)
            salida.cerrar()
        self.assertEqual([f.frecuencia for f in flujos], [24000, 22050])

    def test_hablar_con_nube_se_detiene_al_interrumpir(self):
        reproducido = []

        class Salida:
            def escribir(self, audio, frecuencia, pausa):
                reproducido.append(frecuencia)
                audio_vivo.interrumpir()  # el usuario toca el orbe durante la primera frase

            def cerrar(self): pass

        anterior = (config.MODO_VOZ, config.GEMINI_API_KEY)
        config.MODO_VOZ, config.GEMINI_API_KEY = "gemini", "prueba"
        escuchar._motores_nube_bloqueados_hasta.update(Gemini=0.0, OpenAI=0.0)
        try:
            with patch.object(escuchar, "_sintetizar_gemini", return_value=(np.zeros(10, dtype=np.int16), 24000)) as gemini, \
                 patch.object(voz_local, "_Salida", Salida):
                inicio = time.monotonic()
                escuchar.hablar("Primera frase bastante larga. Segunda frase bastante larga. Tercera frase.")
            self.assertEqual(reproducido, [24000])
            self.assertLess(time.monotonic() - inicio, 1.0)
            self.assertLessEqual(gemini.call_count, 2)
        finally:
            config.MODO_VOZ, config.GEMINI_API_KEY = anterior
            audio_vivo.INTERRUPCION.clear()


class TestMedios(unittest.TestCase):
    def test_interpreta_lo_que_suena(self):
        linea = json.dumps({"titulo": "Get Lucky", "artista": "Daft Punk", "app": "Spotify.exe", "estado": "Playing",
                            "posicion": 30.5, "duracion": 248.0, "actualizado": 1_700_000_000_000})
        estado = medios.interpretar(linea)
        self.assertEqual((estado["titulo"], estado["app"], estado["reproduciendo"]), ("Get Lucky", "Spotify", True))
        self.assertEqual(estado["duracion"], 248.0)

    def test_sin_sesion_o_linea_rota_es_none(self):
        self.assertIsNone(medios.interpretar('{"titulo": null}'))
        self.assertIsNone(medios.interpretar("no es json"))

    def test_nombres_de_apps(self):
        self.assertEqual(medios.nombre_app("308046B0AF4A39CB"), "Firefox")
        self.assertEqual(medios.nombre_app("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic"), "Reproductor")
        self.assertEqual(medios.nombre_app("Brave"), "Brave")

    def test_posicion_se_extrapola_solo_si_suena(self):
        hace_10_s = time.time() - 10
        sonando = {"posicion": 5.0, "duracion": 100.0, "reproduciendo": True, "actualizado": hace_10_s}
        self.assertAlmostEqual(medios.posicion_actual(sonando), 15.0, delta=0.5)
        self.assertEqual(medios.posicion_actual({**sonando, "reproduciendo": False}), 5.0)
        self.assertEqual(medios.posicion_actual({**sonando, "duracion": 8.0}), 8.0)


class TestHud(unittest.TestCase):
    def test_mezclar_colores(self):
        self.assertEqual(mezclar("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(mezclar("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(mezclar("#000000", "#ffffff", 2.0), "#ffffff")

    def test_texto_seguro_quita_emojis_fuera_del_plano_basico(self):
        self.assertEqual(texto_seguro("¿Los conocías? 🦋"), "¿Los conocías?")


if __name__ == "__main__":
    unittest.main()
