"""Pruebas: elección de modelo según el equipo, orden de micrófonos, permisos y respuestas sí/no."""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import hardware  # noqa: E402
from nucleo import escuchar, permisos  # noqa: E402


def _equipo(ram, vram=0.0, nucleos=8):
    return hardware.Equipo(ram, ram / 2, nucleos * 2, nucleos, "GPU" if vram else "", vram)


class TestModeloSegunEquipo(unittest.TestCase):
    def test_cada_nivel_de_ram_tiene_su_modelo(self):
        casos = {4: "qwen3-1.7b", 8: "qwen3-1.7b", 7.8: "qwen3-1.7b", 12: "qwen3-4b", 15.3: "qwen3-4b",
                 16: "qwen3-4b", 24: "qwen3-8b", 32: "qwen3-30b-a3b", 31.6: "qwen3-30b-a3b",
                 48: "qwen3-30b-a3b", 63.7: "deepseek-r1-32b", 128: "deepseek-r1-32b"}
        for ram, clave in casos.items():
            with self.subTest(ram=ram):
                self.assertEqual(hardware.modelo_recomendado(_equipo(ram)).clave, clave)

    def test_el_viejo_qwen25_nunca_se_recomienda(self):
        for ram in (4, 8, 12, 16, 32, 64):
            self.assertNotEqual(hardware.modelo_recomendado(_equipo(ram)).clave, "qwen2.5-3b")

    def setUp(self):
        # Tamaños simulados: en NTFS truncate() escribe ceros reales, así que no se crean archivos grandes.
        self._tamanos = {}
        real = os.path.getsize
        self._parche = patch("os.path.getsize", lambda ruta: self._tamanos.get(ruta) or real(ruta))
        self._parche.start()

    def tearDown(self):
        self._parche.stop()

    def _archivos(self, carpeta, nombres_y_gb):
        rutas = []
        for nombre, gb in nombres_y_gb:
            ruta = os.path.join(carpeta, nombre)
            open(ruta, "wb").close()
            self._tamanos[ruta] = int(gb * 1024 ** 3)
            rutas.append(ruta)
        return rutas

    def test_usa_el_recomendado_si_esta_descargado(self):
        with tempfile.TemporaryDirectory() as carpeta:
            rutas = self._archivos(carpeta, [("qwen2.5-3b-instruct-q4_k_m.gguf", 2.0), ("Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2.5)])
            self.assertEqual(os.path.basename(hardware.elegir_modelo(rutas, _equipo(16))), "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")

    def test_sin_el_recomendado_elige_el_mas_capaz_que_cabe(self):
        with tempfile.TemporaryDirectory() as carpeta:
            rutas = self._archivos(carpeta, [("Qwen3-1.7B-Q4_K_M.gguf", 1.1), ("Qwen3-8B-Q4_K_M.gguf", 5.0),
                                             ("DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf", 19.9)])
            # 16 GB: DeepSeek no cabe; el 8B sí aunque no sea el recomendado (4B).
            self.assertEqual(os.path.basename(hardware.elegir_modelo(rutas, _equipo(16))), "Qwen3-8B-Q4_K_M.gguf")
            # 8 GB: el 8B (5 GB + 4 de margen) no cabe: el ligero.
            self.assertEqual(os.path.basename(hardware.elegir_modelo(rutas, _equipo(8))), "Qwen3-1.7B-Q4_K_M.gguf")
            # 64 GB: DeepSeek.
            self.assertIn("DeepSeek", hardware.elegir_modelo(rutas, _equipo(64)))

    def test_capas_de_gpu_segun_vram(self):
        with tempfile.TemporaryDirectory() as carpeta:
            (ruta,) = self._archivos(carpeta, [("Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2.5)])
            self.assertEqual(hardware.capas_gpu_automaticas(ruta, _equipo(16, vram=6)), -1)
            self.assertEqual(hardware.capas_gpu_automaticas(ruta, _equipo(16, vram=0)), 0)
            (grande,) = self._archivos(carpeta, [("Qwen3-8B-Q4_K_M.gguf", 5.0)])
            parcial = hardware.capas_gpu_automaticas(grande, _equipo(16, vram=4))
            self.assertTrue(0 < parcial < 36)

    def test_voces_ligeras_en_equipos_modestos(self):
        self.assertEqual(hardware.voces_recomendadas(_equipo(8, nucleos=4))[0].clave, "es_MX-ald-x_low")
        self.assertEqual(hardware.voces_recomendadas(_equipo(32))[0].clave, "es_MX-claude-high")

    def test_url_de_voz_piper(self):
        voz = next(v for v in hardware.VOCES_PIPER if v.clave == "es_MX-ald-x_low")
        self.assertEqual(voz.url, "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/ald/x_low/es_MX-ald-x_low.onnx")

    def test_deteccion_real_no_falla(self):
        equipo = hardware.detectar_equipo()
        self.assertGreater(equipo.ram_total_gb, 0)
        self.assertGreaterEqual(equipo.nucleos_logicos, equipo.nucleos_fisicos)


class _SoundDeviceFalso(types.ModuleType):
    """Réplica de los dispositivos de la laptop que tenía el problema."""

    def __init__(self, predeterminado=1):
        super().__init__("sounddevice")
        self.default = types.SimpleNamespace(device=[predeterminado, 4])
        self._apis = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
        entrada = lambda nombre, api, tasa=44100: {"name": nombre, "hostapi": api, "max_input_channels": 2, "default_samplerate": tasa}
        self._dispositivos = [
            entrada("Asignador de sonido Microsoft - Input", 0),
            entrada("Microphone Array (AMD Audio Dev", 0),
            entrada("Auriculares con micrófono (E13L", 0),
            entrada("Microphone Array (AMD Audio Device)", 1),
            entrada("Microphone Array (AMD Audio Device)", 2, 48000),
            entrada("Micrófono (Realtek HD Audio Mic input)", 3),
            entrada("Mezcla estéreo (Realtek HD Audio Stereo input)", 3, 48000),
            entrada("Auriculares con micrófono (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0\r\n;(HAVIT))", 3, 8000),
            entrada("Altavoz de PC (Realtek HD Audio output with HAP)", 3),
        ]

    def query_hostapis(self):
        return self._apis

    def query_devices(self, *args, **kwargs):
        return self._dispositivos


class TestMicrofonos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ruta = patch.object(permisos, "RUTA_PERMISOS", os.path.join(self._tmp.name, "permisos.json"))
        self._ruta.start()
        self._confirmado = escuchar._microfono_confirmado
        escuchar._microfono_confirmado = None

    def tearDown(self):
        self._ruta.stop()
        self._tmp.cleanup()
        escuchar._microfono_confirmado = self._confirmado

    def _candidatos(self, sd):
        with patch.dict(sys.modules, {"sounddevice": sd}):
            return escuchar.candidatos_microfono()

    def test_el_predeterminado_va_primero_y_el_jack_realtek_al_final(self):
        nombres = [c["nombre"] for c in self._candidatos(_SoundDeviceFalso())]
        self.assertEqual(nombres[0], "Microphone Array (AMD Audio Dev")
        self.assertEqual(nombres[-1], "Micrófono (Realtek HD Audio Mic input)")

    def test_omite_virtuales_bluetooth_desconectado_y_salidas(self):
        nombres = " ".join(c["nombre"] for c in self._candidatos(_SoundDeviceFalso())).lower()
        for excluido in ("asignador", "mezcla", "hands-free", "altavoz"):
            self.assertNotIn(excluido, nombres)

    def test_un_aparato_aparece_una_sola_vez(self):
        amd = [c for c in self._candidatos(_SoundDeviceFalso()) if "amd" in c["nombre"].lower()]
        self.assertEqual(len(amd), 1)
        self.assertEqual(amd[0]["api"], "MME")

    def test_el_microfono_que_funciono_antes_tiene_prioridad(self):
        permisos.guardar_preferencia("microfono_nombre", "Auriculares con micrófono (E13L)")
        self.assertEqual(self._candidatos(_SoundDeviceFalso())[0]["nombre"], "Auriculares con micrófono (E13L")

    def test_sin_confirmar_escucha_varios_pero_nunca_wdmks(self):
        with patch.dict(sys.modules, {"sounddevice": _SoundDeviceFalso()}), \
             patch.object(escuchar.config, "DISPOSITIVO_ENTRADA_VOZ", None):
            indices = escuchar._microfonos_a_escuchar()
        self.assertEqual(indices, [1, 2])  # AMD y audífonos; el jack Realtek (5) queda fuera

    def test_el_silencio_no_fija_un_microfono(self):
        """Antes, 60 s de siseo del jack vacío lo dejaban fijo para siempre."""
        entrada = escuchar._Entrada(5, 16000)
        siseo = (escuchar.np.random.default_rng(0).normal(0, 3, 1024)).astype(escuchar.np.int16).tobytes()
        estado = "esperando"
        for _ in range(int(61 / 0.064)):
            estado = entrada.procesar(siseo)
            if estado != "esperando":
                break
        self.assertEqual(estado, "vacio")
        self.assertEqual(entrada.bloques, [])


class TestPermisos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ruta = patch.object(permisos, "RUTA_PERMISOS", os.path.join(self._tmp.name, "permisos.json"))
        self._ruta.start()

    def tearDown(self):
        self._ruta.stop()
        self._tmp.cleanup()

    def test_recuerda_las_decisiones(self):
        self.assertIsNone(permisos.consentimiento("microfono"))
        permisos.guardar_consentimiento("microfono", True)
        permisos.guardar_consentimiento("camara", False)
        self.assertTrue(permisos.consentimiento("microfono"))
        self.assertFalse(permisos.consentimiento("camara"))
        permisos.olvidar_decisiones()
        self.assertIsNone(permisos.consentimiento("microfono"))

    def test_estado_de_windows_es_valido(self):
        self.assertIn(permisos.estado_windows("microphone"), {"permitido", "denegado", "desconocido"})

    def test_respuestas_por_voz(self):
        for texto in ("Sí.", "sí, adelante", "Claro que sí", "Si", "ok", "Autorizo."):
            self.assertTrue(escuchar.es_afirmacion(texto), texto)
        for texto in ("No.", "no, cancela", "Cancela eso"):
            self.assertFalse(escuchar.es_afirmacion(texto), texto)
        for texto in ("", "mmm", "¿qué?"):
            self.assertIsNone(escuchar.es_afirmacion(texto), texto)

    def test_permiso_por_voz_reintenta_si_no_entiende(self):
        with patch.object(escuchar, "hablar"), \
             patch.object(escuchar, "escuchar_frase", side_effect=["eh", "Sí."]):
            self.assertTrue(escuchar.solicitar_permiso_por_voz("borrar el archivo"))
        with patch.object(escuchar, "hablar"), \
             patch.object(escuchar, "escuchar_frase", side_effect=["", ""]):
            self.assertFalse(escuchar.solicitar_permiso_por_voz("borrar el archivo"))


class TestGuardarEnEnv(unittest.TestCase):
    def test_reemplaza_agrega_y_conserva_comentarios(self):
        from pathlib import Path
        from config import config
        salto = chr(10)
        carpeta = tempfile.mkdtemp()
        ruta = Path(carpeta) / ".env"
        ruta.write_text(salto.join(["# Mis claves", "GEMINI_API_KEY=vieja", "BARK_VOZ=local", ""]), encoding="utf-8")
        anterior = (config.GEMINI_API_KEY, os.environ.get("GEMINI_API_KEY"), os.environ.get("BARK_PRUEBA_ENV"))
        try:
            config.guardar_en_env({"GEMINI_API_KEY": "nueva", "BARK_PRUEBA_ENV": "Tulantepec"}, ruta)
            esperado = salto.join(["# Mis claves", "GEMINI_API_KEY=nueva", "BARK_VOZ=local", "BARK_PRUEBA_ENV=Tulantepec", ""])
            self.assertEqual(ruta.read_text(encoding="utf-8"), esperado)
            self.assertEqual(config.GEMINI_API_KEY, "nueva")  # activa sin reiniciar
            self.assertEqual(os.environ["BARK_PRUEBA_ENV"], "Tulantepec")
            self.assertFalse((Path(carpeta) / ".env.tmp").exists())
        finally:
            config.GEMINI_API_KEY = anterior[0]
            for clave, valor in (("GEMINI_API_KEY", anterior[1]), ("BARK_PRUEBA_ENV", anterior[2])):
                if valor is None:
                    os.environ.pop(clave, None)
                else:
                    os.environ[clave] = valor


if __name__ == "__main__":
    unittest.main()
