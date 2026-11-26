"""Conectores creados por Bark, habilidades (prompts guardados) y el razonamiento breve del cerebro."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config import config
from conectores import creador, habilidades, mcp
from conectores.registro import Resultado
from nucleo import cerebro


class TestCatalogo(unittest.TestCase):
    def test_encuentra_recetas_por_nombre_aproximado(self):
        self.assertEqual(creador._buscar_receta("ElevenLabs"), "elevenlabs")
        self.assertEqual(creador._buscar_receta("eleven labs"), "elevenlabs")
        self.assertEqual(creador._buscar_receta("github"), "github")
        self.assertIsNone(creador._buscar_receta("Totalplay"))

    def test_recetas_no_llevan_claves_escritas(self):
        for receta in creador.CATALOGO.values():
            texto = json.dumps(receta)
            for variable in receta.get("claves", {}):
                self.assertIn("${%s}" % variable, texto)

    def test_instalar_sin_clave_guarda_y_pide_ajustes(self):
        guardados = {}
        with patch.object(mcp, "guardar_servidor", side_effect=lambda n, a: guardados.update({n: a})), \
             patch.object(creador, "_faltan_programas", return_value=None), \
             patch.object(mcp, "registrar_servidor") as registrar, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            texto = creador.instalar_conector("eleven labs")
        self.assertIn("elevenlabs", guardados)
        self.assertEqual(guardados["elevenlabs"]["entorno"]["ELEVENLABS_API_KEY"], "${ELEVENLABS_API_KEY}")
        self.assertIn("Ajustes", texto)
        self.assertIn("archivado", texto)
        registrar.assert_not_called()  # sin clave no se intenta conectar

    def test_instalar_con_clave_conecta(self):
        with patch.object(mcp, "guardar_servidor"), patch.object(creador, "_faltan_programas", return_value=None), \
             patch.object(mcp, "registrar_servidor", return_value="hora: 2 herramientas") as registrar:
            texto = creador.instalar_conector("hora")
        registrar.assert_called_once()
        self.assertIn("listo", texto)


class TestConectorPropio(unittest.TestCase):
    def test_no_acepta_una_clave_como_nombre_de_variable(self):
        texto = creador.agregar_conector_mcp("notion", comando="npx -y @notionhq/notion-mcp-server", variable_clave="ntn_abc123secreto")
        self.assertIn("NOMBRE de la variable", texto)

    def test_solo_https_para_remotos(self):
        self.assertIn("https", creador.agregar_conector_mcp("raro", url="http://ejemplo.com/mcp"))

    def test_comando_local_con_variable(self):
        guardados = {}
        with patch.object(mcp, "guardar_servidor", side_effect=lambda n, a: guardados.update({n: a})), \
             patch.object(creador, "_faltan_programas", return_value=None), \
             patch.dict(os.environ, {"NOTION_TOKEN": ""}):
            creador.agregar_conector_mcp("Mi Notion", comando="npx -y @notionhq/notion-mcp-server", variable_clave="NOTION_TOKEN")
        ajustes = guardados["mi_notion"]
        self.assertEqual((ajustes["comando"], ajustes["argumentos"]), ("npx", ["-y", "@notionhq/notion-mcp-server"]))
        self.assertEqual(ajustes["entorno"], {"NOTION_TOKEN": "${NOTION_TOKEN}"})


class TestConfigMcp(unittest.TestCase):
    def test_guardar_quitar_y_variables(self):
        ruta = os.path.join(tempfile.mkdtemp(), "conectores_mcp.json")
        mcp.guardar_servidor("eleven", {"comando": "uvx", "entorno": {"K": "${ELEVEN_KEY}"}, "activo": True}, ruta)
        mcp.guardar_servidor("apagado", {"url": "https://x", "encabezados": {"A": "${OTRA}"}, "activo": False}, ruta)
        self.assertEqual(mcp.variables_requeridas(ruta), ["ELEVEN_KEY"])  # los inactivos no piden clave
        self.assertTrue(mcp.quitar_servidor("eleven", ruta))
        self.assertEqual(list(mcp.leer_configuracion(ruta)), ["apagado"])
        self.assertFalse(mcp.quitar_servidor("no_existe", ruta))


class TestHabilidades(unittest.TestCase):
    def setUp(self):
        self.ruta = os.path.join(tempfile.mkdtemp(), "habilidades.json")
        self.parche = patch.object(habilidades, "RUTA", self.ruta)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()

    def test_crear_activar_y_borrar(self):
        texto = habilidades.crear_habilidad("Resumen de correo", "Resume en 3 viñetas y sugiere una acción.",
                                            "resumen de correo, email largo")
        self.assertIn("resumen_de_correo", texto)
        activas = habilidades.habilidades_para("hazme un resumen de este correo por favor")
        self.assertEqual([n for n, _ in activas], ["resumen_de_correo"])
        self.assertEqual(habilidades.habilidades_para("pon música de Daft Punk"), [])
        self.assertIn("3 viñetas", habilidades.contexto_para("resumen del correo de Juan"))
        self.assertIn("Borré", habilidades.borrar_habilidad("resumen correo"))
        self.assertEqual(habilidades.cargar(), {})

    def test_rechaza_habilidad_vacia(self):
        self.assertIn("Necesito", habilidades.crear_habilidad("x", "corta"))


class TestRazonamiento(unittest.TestCase):
    def test_activo_salvo_modelo_local_que_ya_piensa(self):
        anterior = (config.RAZONAMIENTO, config.PROVEEDOR_PREFERIDO, config.NOMBRE_MODELO)
        try:
            config.RAZONAMIENTO, config.PROVEEDOR_PREFERIDO = "auto", "local"
            config.NOMBRE_MODELO = "qwen2.5-3b-instruct-q4_k_m.gguf"
            self.assertTrue(cerebro.razonamiento_activo())
            config.NOMBRE_MODELO = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
            self.assertFalse(cerebro.razonamiento_activo())
            config.RAZONAMIENTO = "1"
            self.assertTrue(cerebro.razonamiento_activo())
            self.assertIn('"plan"', cerebro._construir_system_prompt())
        finally:
            config.RAZONAMIENTO, config.PROVEEDOR_PREFERIDO, config.NOMBRE_MODELO = anterior

    def test_varios_pasos_con_continuar(self):
        respuestas = iter([
            json.dumps({"plan": "Abro Spotify y luego busco Daft Punk.", "tipo": "accion", "herramienta": "abrir_aplicacion",
                        "parametros": {"nombre_app": "spotify"}, "continuar": True}),
            json.dumps({"tipo": "accion", "herramienta": "buscar_y_reproducir_spotify", "parametros": {"consulta": "daft punk"}}),
        ])
        ejecutadas = []

        def ejecutar(nombre, parametros, _permiso):
            ejecutadas.append(nombre)
            return Resultado(f"hecho {nombre}", nombre, ok=True)

        with patch.object(cerebro, "responder", side_effect=lambda m, _l: (next(respuestas), "prueba")), \
             patch.object(cerebro.REGISTRO, "ejecutar", side_effect=ejecutar), \
             patch.object(cerebro, "_accion_directa", return_value=None), \
             patch.object(cerebro, "_respuesta_directa", return_value=None), \
             patch.object(cerebro, "guardar_turno"), patch.object(cerebro, "_guardar_recuerdo_si_corresponde", return_value=None), \
             patch.object(cerebro.historial_sesion, "agregar_turno"):
            texto = cerebro.procesar_mensaje("abre spotify y pon algo de daft punk")
        self.assertEqual(ejecutadas, ["abrir_aplicacion", "buscar_y_reproducir_spotify"])
        self.assertEqual(texto, "hecho buscar_y_reproducir_spotify")


class TestJson(unittest.TestCase):
    def test_rescata_bloque_json_pero_no_prosa(self):
        valla = chr(96) * 3
        salto = chr(10)
        bloque = valla + "json" + salto + '{"tipo":"respuesta_final","mensaje":"ok"}' + salto + valla
        self.assertEqual(cerebro._parsear_respuesta_json(bloque), {"tipo": "respuesta_final", "mensaje": "ok"})
        self.assertIsNone(cerebro._parsear_respuesta_json('Por ejemplo: {"tipo":"accion","herramienta":"apagar_pc"}'))


class TestRutasDirectas(unittest.TestCase):
    def test_conecta_conector_vs_wifi(self):
        self.assertEqual(cerebro._accion_directa("conecta ElevenLabs")[0], "instalar_conector")
        self.assertNotEqual((cerebro._accion_directa("conéctate a Totalplay") or ("",))[0], "instalar_conector")
        self.assertEqual(cerebro._accion_directa("qué conectores puedes instalar")[0], "catalogo_conectores")


if __name__ == "__main__":
    unittest.main()
