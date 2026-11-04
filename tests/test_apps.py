import unittest
from unittest.mock import patch

from acciones import apps
from nucleo.cerebro import _accion_directa, _resolver_herramienta, _ajustar_parametros, HERRAMIENTAS_DISPONIBLES

INDICE = [
    {"nombre": "Discord", "tipo": "aumid", "destino": "Discord.Inc.Discord"},
    {"nombre": "Spotify", "tipo": "aumid", "destino": "SpotifyAB.Spotify!Spotify"},
    {"nombre": "Roblox Player", "tipo": "archivo", "destino": r"C:\Start\Roblox Player.lnk"},
    {"nombre": "Roblox Studio", "tipo": "archivo", "destino": r"C:\Start\Roblox Studio.lnk"},
    {"nombre": "Visual Studio Code", "tipo": "archivo", "destino": r"C:\Start\Visual Studio Code.lnk"},
    {"nombre": "Grabadora de sonido", "tipo": "aumid", "destino": "Microsoft.WindowsSoundRecorder!App"},
    {"nombre": "Reproductor de Windows Media", "tipo": "aumid", "destino": "Microsoft.ZuneMusic!Music"},
    {"nombre": "Adobe Photoshop 2025", "tipo": "archivo", "destino": r"C:\Start\Photoshop.lnk"},
]


class TestResolucionDeApps(unittest.TestCase):
    def abrir(self, consulta):
        lanzados = []
        with patch.object(apps, "ES_WINDOWS", True), \
             patch.object(apps, "obtener_indice", return_value=INDICE), \
             patch.object(apps, "_lanzar", side_effect=lambda t, d: lanzados.append((t, d))):
            return apps.abrir_inteligente(consulta), lanzados

    def test_microsoft_store_con_todas_sus_formas_de_decirlo(self):
        for frase in ("microsoft store", "la store de microsoft", "tienda de microsoft", "store",
                      "tienda de windows", "Microsoft Store por favor", "la tienda"):
            resultado, lanzados = self.abrir(frase)
            self.assertTrue(resultado.abierta, frase)
            self.assertEqual(lanzados, [("protocolo", "ms-windows-store:")], frase)

    def test_apps_del_sistema_tienen_destino_sin_acceso_directo(self):
        casos = {"calculadora": ("exe", "calc.exe"), "bloc de notas": ("exe", "notepad.exe"),
                 "administrador de tareas": ("exe", "taskmgr.exe"), "fotos": ("protocolo", "ms-photos:"),
                 "calculadra": ("exe", "calc.exe")}
        for frase, esperado in casos.items():
            _, lanzados = self.abrir(frase)
            self.assertEqual(lanzados, [esperado], frase)

    def test_tolera_errores_de_dictado_y_acentos(self):
        for frase, esperado in {"discor": "Discord", "spotifi": "Spotify", "visual studio": "Visual Studio Code",
                                "grabadora": "Grabadora de sonido", "fotoshop": "Adobe Photoshop 2025"}.items():
            resultado, _ = self.abrir(frase)
            self.assertTrue(resultado.abierta, frase)
            self.assertEqual(resultado.nombre, esperado, frase)

    def test_nombre_ambiguo_pregunta_en_vez_de_adivinar(self):
        # «roblox» solo no está en el catálogo si se pide «roblox s»; con dos candidatos parecidos debe preguntar.
        with patch.object(apps, "ES_WINDOWS", True), \
             patch.object(apps, "obtener_indice", return_value=INDICE[2:4]), \
             patch.object(apps, "_lanzar") as lanzar:
            resultado = apps.abrir_inteligente("roblox s")
        self.assertFalse(lanzar.called and resultado.abierta and resultado.nombre == "")
        if not resultado.abierta:
            self.assertTrue(resultado.ambigua or resultado.sugerencias)

    def test_app_inexistente_no_abre_nada_y_no_inventa(self):
        resultado, lanzados = self.abrir("zzzxqwv")
        self.assertFalse(resultado.abierta)
        self.assertEqual(lanzados, [])

    def test_procesos_para_cerrar_apps_de_la_tienda(self):
        self.assertIn("WinStore.App.exe", apps.procesos_conocidos("la store de microsoft"))
        self.assertIn("CalculatorApp.exe", apps.procesos_conocidos("calculadora"))
        for entrada in apps.CATALOGO:  # explorer.exe jamás debe poder cerrarse
            self.assertNotIn("explorer.exe", [p.lower() for p in entrada["procesos"]])


class TestOrdenesDirectas(unittest.TestCase):
    def test_store_llega_a_abrir_aplicacion(self):
        self.assertEqual(_accion_directa("abre la store de microsoft"), ("abrir_aplicacion", {"nombre_app": "store de microsoft"}))
        self.assertEqual(_accion_directa("abre la calculadora"), ("abrir_aplicacion", {"nombre_app": "calculadora"}))

    def test_volumen_brillo_bateria(self):
        self.assertEqual(_accion_directa("sube el volumen"), ("controlar_volumen", {"accion": "subir", "pasos": 5}))
        self.assertEqual(_accion_directa("baja mucho el volumen"), ("controlar_volumen", {"accion": "bajar", "pasos": 10}))
        self.assertEqual(_accion_directa("silencia el pc"), ("controlar_volumen", {"accion": "silenciar"}))
        self.assertEqual(_accion_directa("pon el volumen al 40"), ("fijar_volumen", {"porcentaje": 40}))
        self.assertEqual(_accion_directa("brillo al 70%"), ("fijar_brillo", {"porcentaje": 70}))
        self.assertEqual(_accion_directa("cuánta batería tengo"), ("estado_bateria", {}))

    def test_temporizadores(self):
        self.assertEqual(_accion_directa("pon un temporizador de 10 minutos"), ("crear_temporizador", {"minutos": 10.0, "mensaje": ""}))
        self.assertEqual(_accion_directa("temporizador de una hora y media"), ("crear_temporizador", {"minutos": 90.0, "mensaje": ""}))
        self.assertEqual(_accion_directa("temporizador de 30 segundos"), ("crear_temporizador", {"minutos": 0.5, "mensaje": ""}))
        self.assertEqual(
            _accion_directa("recuérdame en 5 minutos que saque la pizza"),
            ("crear_temporizador", {"minutos": 5.0, "mensaje": "Recordatorio: saque la pizza"}),
        )

    def test_energia_ventanas_y_ajustes(self):
        self.assertEqual(_accion_directa("bloquea la pc"), ("bloquear_pc", {}))
        self.assertEqual(_accion_directa("apaga la computadora"), ("apagar_pc", {}))
        self.assertEqual(_accion_directa("cancela el apagado"), ("cancelar_apagado", {}))
        self.assertEqual(_accion_directa("minimiza todo"), ("minimizar_todo", {}))
        self.assertEqual(_accion_directa("cambia a chrome"), ("enfocar_ventana", {"nombre": "chrome"}))
        self.assertEqual(_accion_directa("abre la configuración de wifi"), ("abrir_configuracion", {"seccion": "wifi"}))
        self.assertEqual(_accion_directa("qué hay en el portapapeles"), ("leer_portapapeles", {}))
        self.assertEqual(_accion_directa("abre github.com"), ("abrir_en_navegador", {"url": "https://github.com"}))

    def test_no_confunde_musica_con_sistema(self):
        self.assertEqual(_accion_directa("pasa a la siguiente canción"), ("controlar_spotify", {"accion": "siguiente"}))
        self.assertEqual(_accion_directa("reproduce Zoé"), ("buscar_y_reproducir_spotify", {"cancion": "Zoé"}))
        self.assertEqual(_accion_directa("quiero escuchar Soda Stereo")[0], "buscar_y_reproducir_spotify")

    def test_acciones_de_energia_piden_permiso(self):
        from nucleo.cerebro import ACCIONES_SENSIBLES
        self.assertTrue({"apagar_pc", "reiniciar_pc", "suspender_pc"} <= ACCIONES_SENSIBLES)


class TestToleranciaDelModelo(unittest.TestCase):
    def test_corrige_nombres_de_herramienta_inventados(self):
        self.assertEqual(_resolver_herramienta("abrir_app"), "abrir_aplicacion")
        self.assertEqual(_resolver_herramienta("Abrir Aplicacion"), "abrir_aplicacion")
        self.assertEqual(_resolver_herramienta("listar_aplicacion_instaladas"), "listar_aplicaciones_instaladas")
        self.assertIsNone(_resolver_herramienta("hackear_nasa"))

    def test_reasigna_parametro_con_nombre_equivocado(self):
        funcion = HERRAMIENTAS_DISPONIBLES["abrir_aplicacion"]
        self.assertEqual(_ajustar_parametros(funcion, {"app": "calculadora"}), {"nombre_app": "calculadora"})
        self.assertEqual(_ajustar_parametros(funcion, {"nombre_app": "x"}), {"nombre_app": "x"})


if __name__ == "__main__":
    unittest.main()


class TestOptimizacion(unittest.TestCase):
    def test_ordenes_de_optimizacion(self):
        for frase in ("optimiza mi pc", "acelera la computadora", "mi pc va lenta", "limpia mi pc", "mejora el rendimiento de mi pc"):
            self.assertEqual(_accion_directa(frase), ("optimizar_pc", {}), frase)
        self.assertEqual(_accion_directa("optimiza la ram"), ("liberar_ram_avanzada", {}))
        self.assertEqual(_accion_directa("modo alto rendimiento"), ("plan_de_energia", {"modo": "alto rendimiento"}))
        self.assertEqual(_accion_directa("pon la pc en modo ahorro de energia"), ("plan_de_energia", {"modo": "ahorro de energia"}))

    def test_optimizar_exige_permiso_una_vez(self):
        from nucleo.cerebro import ACCIONES_SENSIBLES
        self.assertIn("optimizar_pc", ACCIONES_SENSIBLES)

    def test_eleccion_de_modelo_prefiere_qwen3_y_tiene_respaldo(self):
        import os, tempfile
        from unittest.mock import patch
        from config import config
        with tempfile.TemporaryDirectory() as carpeta:
            for nombre in ("qwen2.5-3b-instruct-q4_k_m.gguf", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "mmproj-F16.gguf"):
                open(os.path.join(carpeta, nombre), "w").close()
            with patch.object(config, "CARPETA_MODELOS", carpeta):
                elegido = config._elegir_modelo_llm()
                self.assertIn("Qwen3-4B", os.path.basename(elegido))
                self.assertTrue(all("mmproj" not in m for m in config._modelos_gguf()))
