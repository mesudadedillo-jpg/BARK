"""Lógica de la sesión Gemini Live sin red: setup, audio, transcripciones, interrupción y herramientas."""

import base64
import json
import threading
import time
import unittest

from nucleo import gemini_live


class _AltavozFalso:
    def __init__(self):
        self.recibido = bytearray()
        self.sonando = False

    def agregar(self, pcm):
        self.recibido.extend(pcm)
        self.sonando = True

    def vaciar(self):
        self.recibido.clear()
        self.sonando = False

    def abrir(self): pass
    def cerrar(self): pass


def _sesion(**kw):
    s = gemini_live.SesionGeminiLive(ejecutar_orden=kw.pop("ejecutar_orden", lambda o: "ok"), **kw)
    s._altavoz = _AltavozFalso()
    return s


def _audio(n=10):
    return {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
        "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(b"\x01\x00" * n).decode()}}]}}}


class TestSetup(unittest.TestCase):
    def test_mensaje_setup(self):
        setup = gemini_live.mensaje_setup("gemini-3.8-live", "Kore")["setup"]
        self.assertEqual(setup["model"], "models/gemini-3.8-live")
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertEqual(setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Kore")
        self.assertEqual(setup["tools"][0]["functionDeclarations"][0]["name"], "ejecutar_en_bark")
        self.assertIn("slidingWindow", setup["contextWindowCompression"])
        self.assertEqual(setup["sessionResumption"], {})
        json.dumps(setup)  # serializable tal cual

    def test_reanudar_con_handle(self):
        setup = gemini_live.mensaje_setup("models/x", "Puck", handle="abc")["setup"]
        self.assertEqual((setup["model"], setup["sessionResumption"]), ("models/x", {"handle": "abc"}))


class TestSesion(unittest.TestCase):
    def test_audio_y_transcripciones_forman_un_turno(self):
        turnos, subtitulos = [], []
        s = _sesion(al_turno=lambda u, b: turnos.append((u, b)), al_subtitulos=lambda u, b: subtitulos.append((u, b)))
        s.procesar_mensaje({"serverContent": {"inputTranscription": {"text": "¿Qué hora "}}})
        s.procesar_mensaje({"serverContent": {"inputTranscription": {"text": "es?"}}})
        self.assertEqual(s.estado(), "pensando")
        s.procesar_mensaje(_audio(5))
        s.procesar_mensaje({"serverContent": {"outputTranscription": {"text": "Son las diez."}}})
        self.assertEqual(len(s._altavoz.recibido), 10)
        self.assertEqual(s.estado(), "hablando")
        s.procesar_mensaje({"serverContent": {"turnComplete": True}})
        self.assertEqual(turnos, [("¿Qué hora es?", "Son las diez.")])
        self.assertEqual(subtitulos[-1], ("¿Qué hora es?", "Son las diez."))

    def test_interrupcion_del_servidor_vacia_el_altavoz(self):
        s = _sesion()
        s.procesar_mensaje(_audio())
        s.procesar_mensaje({"serverContent": {"interrupted": True}})
        self.assertEqual(len(s._altavoz.recibido), 0)

    def test_tocar_el_orbe_descarta_el_resto_de_la_respuesta(self):
        s = _sesion()
        s.procesar_mensaje(_audio())
        s.interrumpir()
        s.procesar_mensaje(_audio())  # Gemini sigue mandando la respuesta vieja
        self.assertEqual(len(s._altavoz.recibido), 0)
        s.procesar_mensaje({"serverContent": {"turnComplete": True}})
        s.procesar_mensaje(_audio())  # la siguiente respuesta sí suena
        self.assertGreater(len(s._altavoz.recibido), 0)

    def test_herramienta_pasa_la_orden_a_bark_y_responde(self):
        enviados, hecho = [], threading.Event()
        s = _sesion(ejecutar_orden=lambda orden: f"Hecho: {orden}")
        s._enviar = lambda m: (enviados.append(m), hecho.set())
        s.procesar_mensaje({"toolCall": {"functionCalls": [{"id": "1", "name": "ejecutar_en_bark", "args": {"orden": "abre Spotify"}}]}})
        self.assertTrue(hecho.wait(2))
        respuesta = enviados[0]["toolResponse"]["functionResponses"][0]
        self.assertEqual((respuesta["id"], respuesta["response"]["result"]), ("1", "Hecho: abre Spotify"))
        self.assertEqual(s._herramientas_activas, 0)

    def test_herramienta_cancelada_no_responde(self):
        enviados = []
        liberar = threading.Event()
        s = _sesion(ejecutar_orden=lambda orden: liberar.wait(2) and "tarde")
        s._enviar = enviados.append
        s.procesar_mensaje({"toolCall": {"functionCalls": [{"id": "7", "name": "ejecutar_en_bark", "args": {"orden": "x"}}]}})
        s.procesar_mensaje({"toolCallCancellation": {"ids": ["7"]}})
        liberar.set()
        time.sleep(0.2)
        self.assertEqual(enviados, [])

    def test_guarda_el_handle_para_reanudar(self):
        s = _sesion()
        s.procesar_mensaje({"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}})
        s.procesar_mensaje({"sessionResumptionUpdate": {"newHandle": "", "resumable": False}})
        self.assertEqual(s._handle, "h1")


if __name__ == "__main__":
    unittest.main()
