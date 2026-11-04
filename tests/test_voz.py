import unittest

import numpy as np

from nucleo.escuchar import DetectorVoz, es_alucinacion, remuestrear, _debe_atender_sin_activacion


def simular(detector, niveles, duracion=0.064):
    estado = "esperando"
    for i, nivel in enumerate(niveles):
        estado = detector.procesar(nivel, duracion)
        if estado in {"fin", "vacio"}:
            return estado, i
    return estado, len(niveles)


class TestVoz(unittest.TestCase):
    def test_cierra_la_frase_tras_el_silencio_aunque_el_flujo_nunca_se_detenga(self):
        # Antes el corte solo ocurría si la cola quedaba vacía: con el micrófono abierto jamás pasaba.
        detector = DetectorVoz(8, 0.75, 12)
        niveles = [4] * 10 + [80] * 25 + [4] * 40
        estado, indice = simular(detector, niveles)
        self.assertEqual(estado, "fin")
        self.assertLess(indice, 10 + 25 + 16)  # ~0.75 s de silencio ≈ 12 bloques

    def test_ignora_chasquidos_y_ruido_constante(self):
        detector = DetectorVoz(8, 0.75, 12)
        estado, _ = simular(detector, [5] * 20 + [90] + [5] * 20)
        self.assertNotEqual(estado, "fin")
        self.assertFalse(detector.hablando)
        ruidoso = DetectorVoz(8, 0.75, 12)
        estado, _ = simular(ruidoso, [30] * 100)  # ruido de fondo parejo no es voz
        self.assertFalse(ruidoso.hablando)

    def test_detecta_voz_sobre_ruido_de_fondo(self):
        detector = DetectorVoz(8, 0.75, 12)
        estado, _ = simular(detector, [20] * 20 + [200] * 20 + [20] * 30)
        self.assertEqual(estado, "fin")

    def test_limite_de_duracion_de_frase(self):
        detector = DetectorVoz(8, 0.75, 3)
        estado, indice = simular(detector, [5] * 5 + [100] * 200)
        self.assertEqual(estado, "fin")
        self.assertLess(indice, 70)

    def test_entrada_muerta_se_reporta_rapido(self):
        estado, indice = simular(DetectorVoz(8, 0.75, 12), [0] * 200)
        self.assertEqual(estado, "vacio")
        self.assertLess(indice, 80)

    def test_filtra_el_no_no_no_de_whisper(self):
        self.assertTrue(es_alucinacion("no no no no no no no no no no"))
        self.assertTrue(es_alucinacion("No, no, no."))
        self.assertTrue(es_alucinacion("Subtítulos realizados por la comunidad de Amara.org"))
        self.assertTrue(es_alucinacion("   "))
        for valida in ("abre discord", "no, mejor ábrelo en chrome", "sí", "no", "bark pon música de zoé"):
            self.assertFalse(es_alucinacion(valida), valida)
        self.assertFalse(_debe_atender_sin_activacion("no no no no no", hay_contexto=True))

    def test_remuestrea_a_16khz_conservando_la_duracion(self):
        for tasa in (48000, 44100, 32000, 16000):
            t = np.arange(tasa) / tasa  # 1 segundo
            audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)
            salida = remuestrear(audio, tasa)
            self.assertAlmostEqual(len(salida) / 16000, 1.0, delta=0.01, msg=str(tasa))
            self.assertGreater(float(np.max(np.abs(salida))), 0.8)
            # La frecuencia dominante sigue siendo 440 Hz (antes sonaba 3 veces más grave).
            pico = np.argmax(np.abs(np.fft.rfft(salida))) * 16000 / len(salida)
            self.assertAlmostEqual(pico, 440, delta=5, msg=str(tasa))


if __name__ == "__main__":
    unittest.main()
