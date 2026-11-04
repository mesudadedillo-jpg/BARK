import unittest
from unittest.mock import patch

import numpy as np

from nucleo import voz_local
from nucleo.voz_local import dividir_frases, preparar_texto, _recortar_silencio, _pausa_despues


class TestVozLocal(unittest.TestCase):
    def test_no_lee_markdown_urls_rutas_ni_emojis(self):
        t = preparar_texto("**Listo** 🎉 Abrí https://google.com y guardé C:\\Users\\yo\\a.docx")
        self.assertNotIn("*", t); self.assertNotIn("http", t); self.assertNotIn("C:", t); self.assertNotIn("🎉", t)
        self.assertIn("el enlace", t)

    def test_normaliza_cifras_y_unidades(self):
        t = preparar_texto("RAM 85% y 3.5 GB libres a las 21:30")
        self.assertIn("por ciento", t); self.assertIn("3 punto 5", t); self.assertIn("gigabytes", t); self.assertIn("21 30", t)

    def test_listas_largas_no_se_leen_completas(self):
        lista = "Encontré 60 aplicaciones:\n" + "\n".join(f"- App {i}" for i in range(60))
        t = preparar_texto(lista)
        self.assertLess(len(t), 120)
        self.assertIn("pantalla", t)

    def test_respuesta_larga_se_corta_en_frase_completa(self):
        t = preparar_texto("Esta es una frase de prueba bastante normal. " * 40)
        self.assertLessEqual(len(t), 460)
        self.assertTrue(t.endswith("El resto está en pantalla."))

    def test_frases_sin_cortes_diminutos_ni_bloques_enormes(self):
        frases = dividir_frases("Listo. Abrí Discord y también dejé Spotify sonando, como pediste. ¿Algo más?")
        self.assertTrue(all(len(f) <= 200 for f in frases))
        self.assertFalse(any(len(f) < 8 for f in frases))
        largo = dividir_frases("palabra, " * 80)
        self.assertTrue(all(len(f) <= 200 for f in largo))
        self.assertGreater(len(largo), 2)

    def test_recorta_silencio_y_mantiene_el_habla(self):
        sr = 22050
        habla = (np.sin(np.linspace(0, 400, sr // 2)) * 8000).astype(np.int16)
        audio = np.concatenate([np.zeros(sr), habla, np.zeros(sr)])
        r = _recortar_silencio(audio, sr)
        self.assertLess(len(r), len(habla) + int(sr * 0.2))
        self.assertGreater(int(np.abs(r).max()), 7000)

    def test_pausas_segun_puntuacion(self):
        self.assertGreater(_pausa_despues("¿Algo más?"), _pausa_despues("Sí,"))

    def test_streaming_reproduce_en_orden_y_sintetiza_por_frases(self):
        reproducido, sintetizado = [], []

        def falso_sintetizar(frase):
            sintetizado.append(frase)
            return np.ones(100, dtype=np.int16), 22050

        class Salida:
            def escribir(self, audio, frecuencia, pausa): reproducido.append(frecuencia)
            def cerrar(self): pass

        with patch.object(voz_local, "sintetizar", side_effect=falso_sintetizar), patch.object(voz_local, "_Salida", Salida):
            voz_local.hablar_local("Primera frase de prueba larga. Segunda frase de prueba larga. Tercera frase de prueba.")
        self.assertEqual(len(sintetizado), 3)
        self.assertEqual(len(reproducido), 3)
        self.assertTrue(sintetizado[0].startswith("Primera"))

    def test_error_de_sintesis_no_deja_colgado_el_hilo(self):
        with patch.object(voz_local, "sintetizar", side_effect=RuntimeError("sin modelo")):
            with self.assertRaises(RuntimeError):
                voz_local.hablar_local("Hola, esto debe fallar limpiamente.")


if __name__ == "__main__":
    unittest.main()
