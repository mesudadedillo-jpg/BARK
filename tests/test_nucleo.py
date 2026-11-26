import json
import os
import uuid
import unittest
import requests
import numpy as np
from unittest.mock import patch

from nucleo.cerebro import _accion_directa, _contiene_dato_sensible, _detectar_recuerdo, _parsear_respuesta_json, _respuesta_directa
from nucleo.historial import Historial
from acciones.comandos import abrir_en_navegador
from acciones.comandos import _procesos_de_aplicacion, _terminos_busqueda_aplicacion
from nucleo.escuchar import ejecutar_modo_voz, solicitar_permiso_por_voz
from nucleo.escuchar import _debe_atender_sin_activacion, _es_transcripcion_fantasma, _extraer_orden_activacion
from nucleo.proveedores import _sesion_red, responder
from config import config
from nucleo import bd_manager


class TestNucleo(unittest.TestCase):
    def test_solo_acepta_json_puro(self):
        self.assertEqual(
            _parsear_respuesta_json('{"tipo":"respuesta_final","mensaje":"hola"}'),
            {"tipo": "respuesta_final", "mensaje": "hola"},
        )
        self.assertIsNone(_parsear_respuesta_json('Claro: {"tipo":"respuesta_final"}'))

    def test_historial_conserva_formato_json_del_asistente(self):
        historial = Historial()
        historial.agregar_turno("hola", "qué tal")
        mensajes = historial.obtener_mensajes_contexto()
        self.assertEqual(json.loads(mensajes[1]["content"])["tipo"], "respuesta_final")

    def test_url_invalida_no_se_abre(self):
        self.assertIn("no es valida", abrir_en_navegador("no-es-una-url"))

    def test_orden_spotify_no_pasa_por_busqueda_web(self):
        self.assertEqual(
            _accion_directa("reproduce N4DA de Mérida"),
            ("buscar_y_reproducir_spotify", {"cancion": "N4DA de Mérida"}),
        )

    def test_respuestas_rapidas_y_busqueda_web_no_requieren_llm(self):
        self.assertEqual(_respuesta_directa("calcula 12 x 3"), "Resultado: 36.")
        self.assertEqual(_respuesta_directa("gracias"), "De nada.")
        self.assertEqual(
            _accion_directa("busca Python en google"),
            ("abrir_en_navegador", {"url": "https://www.google.com/search?q=python"}),
        )
        self.assertEqual(
            _accion_directa("abre youtube"),
            ("abrir_en_navegador", {"url": "https://www.youtube.com"}),
        )
        self.assertEqual(
            _accion_directa("lanza Discord"),
            ("abrir_aplicacion", {"nombre_app": "discord"}),
        )
        self.assertEqual(
            _accion_directa("pon en marcha Discord"),
            ("abrir_aplicacion", {"nombre_app": "discord"}),
        )
        self.assertEqual(_accion_directa("muestra mi memoria"), ("consultar_memoria", {"consulta": ""}))

    def test_prompt_local_es_compacto_para_reducir_latencia(self):
        from nucleo.cerebro import _construir_system_prompt
        self.assertLess(len(_construir_system_prompt()), 2600)

    def test_detecta_recuerdo_explicito(self):
        self.assertEqual(_detectar_recuerdo("me llamo Mesud"), ("nombre", "Mesud"))
        self.assertEqual(
            _detectar_recuerdo("me gusta mucho la IA"),
            ("preferencia", "me gusta mucho la IA"),
        )
        self.assertEqual(
            _detectar_recuerdo("actualmente estudio la universidad"),
            ("perfil", "actualmente estudio la universidad"),
        )
        self.assertIsNone(_detectar_recuerdo("hoy hace calor"))

    def test_datos_sensibles_no_son_recuerdo_automatico(self):
        self.assertTrue(_contiene_dato_sensible("mi correo es persona@ejemplo.com"))
        self.assertEqual(_detectar_recuerdo("mi correo es persona@ejemplo.com"), ("dato privado", "persona@ejemplo.com"))
        self.assertIsNone(_detectar_recuerdo("mi password es secreto"))

    def test_ordenes_compuestas_de_apps(self):
        self.assertEqual(
            _accion_directa("abre roblox quiero jugar algo"),
            ("abrir_aplicacion", {"nombre_app": "roblox player"}),
        )
        self.assertEqual(
            _accion_directa("ábrelo y reproduce N4DA de Mérida"),
            ("buscar_y_reproducir_spotify", {"cancion": "N4DA de Mérida"}),
        )

    def test_detecta_diagnostico_local(self):
        self.assertEqual(_accion_directa("mi pc esta saturada"), ("diagnosticar_equipo", {}))

    def test_busqueda_local_de_juegos(self):
        self.assertEqual(_accion_directa("quiero jugar gta inicialo"), ("buscar_y_abrir_aplicacion", {"nombre": "gta"}))
        self.assertIn("rockstar", _terminos_busqueda_aplicacion("gta"))

    def test_entidades_locales_no_pasan_por_el_modelo(self):
        self.assertEqual(_accion_directa("¿qué juegos tengo?"), ("listar_juegos_instalados", {}))
        self.assertEqual(_accion_directa("en el escritorio"), ("listar_archivos", {"ruta": os.path.join(os.path.expanduser("~"), "Desktop")}))
        self.assertEqual(_accion_directa(r"C:\Program Files"), ("abrir_ubicacion", {"ruta": r"C:\Program Files"}))

    def test_abre_carpetas_personales_por_voz(self):
        self.assertEqual(
            _accion_directa("abre mis documentos"),
            ("abrir_ubicacion", {"ruta": os.path.join(os.path.expanduser("~"), "Documents")}),
        )

    def test_abre_steam_por_atajo_determinista(self):
        self.assertEqual(_accion_directa("abre Steam"), ("abrir_aplicacion", {"nombre_app": "steam"}))

    def test_abre_y_cierra_aplicaciones_fuera_de_la_lista_fija(self):
        self.assertEqual(_accion_directa("abre Discord"), ("abrir_aplicacion", {"nombre_app": "discord"}))
        self.assertEqual(_accion_directa("cierra Steam"), ("cerrar_aplicacion", {"nombre_app": "steam"}))
        self.assertEqual(_accion_directa("qué programas tengo"), ("listar_aplicaciones_instaladas", {}))

    def test_cierralo_usa_la_ultima_aplicacion_abierta(self):
        from acciones import comandos
        anterior = comandos.ultima_aplicacion_abierta()
        comandos._registrar_aplicacion_abierta("steam")
        try:
            self.assertEqual(_accion_directa("ciérralo"), ("cerrar_aplicacion", {"nombre_app": "steam"}))
        finally:
            comandos._registrar_aplicacion_abierta(anterior)

    def test_resuelve_proceso_activo_sin_tocar_procesos_del_sistema(self):
        with patch("acciones.comandos._procesos_activos", return_value=["Discord.exe", "System.exe", "DiscordCanary.exe"]):
            self.assertEqual(_procesos_de_aplicacion("discord"), ["Discord.exe"])

    def test_archivos_por_fecha_y_conversion_directos(self):
        self.assertEqual(
            _accion_directa("busca el archivo tarea de ayer"),
            ("buscar_archivos_locales", {"consulta": "tarea", "fecha": "ayer"}),
        )
        self.assertEqual(_accion_directa("abre la primera"), ("abrir_archivo_encontrado", {"indice": 1}))

    def test_musica_con_opciones(self):
        self.assertEqual(_accion_directa("busca canciones de Zoé"), ("buscar_canciones", {"consulta": "Zoé"}))
        self.assertEqual(_accion_directa("quiero la segunda"), ("reproducir_cancion_encontrada", {"indice": 2}))
        self.assertEqual(_accion_directa("quiero la de Zoé"), ("reproducir_cancion_encontrada", {"artista": "Zoé"}))

    def test_lenguaje_colloquial_musica_y_documentos(self):
        self.assertEqual(
            _accion_directa("ponme la rola Hasta la Raíz de Natalia Lafourcade"),
            ("buscar_y_reproducir_spotify", {"cancion": "Hasta la Raíz de Natalia Lafourcade"}),
        )
        self.assertEqual(_accion_directa("siguiente rola"), ("controlar_spotify", {"accion": "siguiente"}))
        self.assertEqual(
            _accion_directa("hazme un Word sobre la independencia de México"),
            ("investigar_y_crear_word", {"tema": "la independencia de México", "ruta_archivo": os.path.join("resultados", "la_independencia_de_mexico.docx")}),
        )

    def test_activacion_tolera_variantes_foneticas(self):
        self.assertEqual(_extraer_orden_activacion("Bark abre Spotify"), "abre spotify")
        self.assertEqual(_extraer_orden_activacion("bar pausa música"), "pausa música")
        self.assertEqual(_extraer_orden_activacion("adiós Bark"), "")

    def test_activacion_de_voz_requiere_chet_al_inicio(self):
        self.assertEqual(_extraer_orden_activacion("Bark abre Spotify", requiere_inicio=True), "abre spotify")
        self.assertIsNone(_extraer_orden_activacion("El asistente se llama Bark abre Spotify", requiere_inicio=True))
        self.assertTrue(_es_transcripcion_fantasma("El asistente se llama Bark. Las órdenes están en español."))

    def test_modo_conversacional_filtra_ambiente_y_acepta_intencion(self):
        self.assertTrue(_debe_atender_sin_activacion("abre Spotify"))
        self.assertTrue(_debe_atender_sin_activacion("¿qué tiempo hace?"))
        self.assertTrue(_debe_atender_sin_activacion("hazlo más corto", hay_contexto=True))
        self.assertFalse(_debe_atender_sin_activacion("sí"))
        self.assertFalse(_debe_atender_sin_activacion("estamos hablando de cualquier cosa"))

    def test_escucha_libre_entrega_la_frase_completa_a_bark(self):
        anterior = config.ESCUCHA_LIBRE
        config.ESCUCHA_LIBRE = True
        recibidas = []
        try:
            with patch("nucleo.escuchar.verificar_voz"), \
                 patch("nucleo.escuchar.hablar"), \
                 patch("nucleo.escuchar.escuchar_frase", side_effect=["abre Steam con mis parámetros", "salir"]):
                ejecutar_modo_voz(lambda frase: recibidas.append(frase) or "Entendido")
            self.assertEqual(recibidas, ["abre Steam con mis parámetros"])
        finally:
            config.ESCUCHA_LIBRE = anterior

    def test_sin_red_usa_directamente_el_modelo_local(self):
        with patch("nucleo.proveedores._hay_red", return_value=False):
            texto, origen = responder([{"role": "user", "content": "hola"}], lambda _: '{"tipo":"respuesta_final","mensaje":"local"}')
        self.assertEqual(origen, "local")
        self.assertIn("local", texto)

    def test_sesion_de_nube_ignora_proxy_ambiental_roto(self):
        self.assertFalse(_sesion_red().trust_env)

    def test_voz_gemini_falla_y_openai_es_el_respaldo(self):
        # La voz no debe terminar el hilo si Gemini se queda sin cuota.
        from nucleo import escuchar
        anterior_modo = config.MODO_VOZ
        anterior_gemini = config.GEMINI_API_KEY
        anterior_openai = config.OPENAI_API_KEY
        config.MODO_VOZ = "auto"
        config.GEMINI_API_KEY = "prueba"
        config.OPENAI_API_KEY = "prueba"
        reproducido = []

        class Salida:
            def escribir(self, audio, frecuencia, pausa): reproducido.append(frecuencia)
            def cerrar(self): pass

        escuchar._motores_nube_bloqueados_hasta.update(Gemini=0.0, OpenAI=0.0)
        try:
            with patch("nucleo.escuchar._sintetizar_gemini", side_effect=requests.HTTPError("429")) as gemini, \
                 patch("nucleo.escuchar._sintetizar_openai", return_value=(np.zeros(10, dtype=np.int16), 24000)) as openai, \
                 patch("nucleo.voz_local._Salida", Salida):
                escuchar.hablar("Hola")
            self.assertEqual(gemini.call_args[0][0], "Hola")
            self.assertEqual(openai.call_args[0][0], "Hola")
            self.assertEqual(reproducido, [24000])
        finally:
            escuchar._motores_nube_bloqueados_hasta.update(Gemini=0.0, OpenAI=0.0)
            config.MODO_VOZ = anterior_modo
            config.GEMINI_API_KEY = anterior_gemini
            config.OPENAI_API_KEY = anterior_openai

    def test_biblioteca_filtra_y_memoria_prioritaria_es_local(self):
        anterior = config.RUTA_MEMORIA
        respaldo_anterior = config.RUTA_RESPALDO_MEMORIA
        ruta_prueba = os.path.join(config.CARPETA_BD, "test_biblioteca.sqlite3")
        ruta_respaldo = ruta_prueba + ".respaldo"
        if os.path.exists(ruta_prueba):
            os.remove(ruta_prueba)
        config.RUTA_MEMORIA = ruta_prueba
        config.RUTA_RESPALDO_MEMORIA = ruta_respaldo
        try:
            bd_manager.guardar_turno("hablamos de música", "Claro")
            bd_manager.guardar_recuerdo("prioridad", "Mi objetivo es aprender Python", prioridad=3, sensible=True)
            self.assertEqual(len(bd_manager.biblioteca_conversaciones("hoy")), 1)
            self.assertEqual(len(bd_manager.buscar_recuerdos("Python", incluir_sensibles=False)), 0)
            self.assertEqual(bd_manager.buscar_recuerdos("Python", incluir_sensibles=True)[0]["prioridad"], 3)
        finally:
            config.RUTA_MEMORIA = anterior
            config.RUTA_RESPALDO_MEMORIA = respaldo_anterior
            if os.path.exists(ruta_prueba):
                os.remove(ruta_prueba)
            if os.path.exists(ruta_respaldo):
                os.remove(ruta_respaldo)

    def test_respaldo_de_memoria_es_una_base_consistente(self):
        memoria_anterior = config.RUTA_MEMORIA
        respaldo_anterior = config.RUTA_RESPALDO_MEMORIA
        sufijo = uuid.uuid4().hex
        config.RUTA_MEMORIA = os.path.join(config.CARPETA_BD, f"test_respaldo_{sufijo}.sqlite3")
        config.RUTA_RESPALDO_MEMORIA = config.RUTA_MEMORIA + ".respaldo"
        try:
            bd_manager.guardar_turno("mensaje", "respuesta")
            self.assertTrue(bd_manager.crear_respaldo_memoria())
            self.assertTrue(os.path.isfile(config.RUTA_RESPALDO_MEMORIA))
            self.assertEqual(bd_manager.verificar_memoria(), "Memoria local verificada correctamente.")
        finally:
            for ruta in (config.RUTA_MEMORIA, config.RUTA_RESPALDO_MEMORIA):
                for sufijo_archivo in ("", "-wal", "-shm"):
                    try:
                        os.remove(ruta + sufijo_archivo)
                    except FileNotFoundError:
                        pass
            config.RUTA_MEMORIA = memoria_anterior
            config.RUTA_RESPALDO_MEMORIA = respaldo_anterior


if __name__ == "__main__":
    unittest.main()
