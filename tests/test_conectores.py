import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time
from unittest.mock import patch

from conectores import REGISTRO, mcp, similitud
from conectores.registro import Registro
from conectores.productividad import interpretar_fecha, interpretar_hora
from conectores.software import _tabla_winget
from config import config
from nucleo.cerebro import _accion_directa, _construir_system_prompt, _herramientas_para


class TestRegistro(unittest.TestCase):
    def test_todos_los_conectores_cargan_sin_errores(self):
        self.assertEqual(REGISTRO.errores_carga, {})
        self.assertGreaterEqual(len(REGISTRO.herramientas), 110)
        for h in REGISTRO.herramientas.values():
            self.assertTrue(h.descripcion, h.nombre)
            self.assertIn(h.conector, REGISTRO.conectores)

    def test_resuelve_nombres_y_alias_inventados(self):
        self.assertEqual(REGISTRO.resolver("mandar_whatsapp")[0], "enviar_whatsapp")
        self.assertEqual(REGISTRO.resolver("Clima")[0], "clima")
        self.assertEqual(REGISTRO.resolver("instalar_progama")[0], "instalar_programa")

    def test_herramienta_inexistente_da_sugerencias_y_vuelve_al_modelo(self):
        resultado = REGISTRO.ejecutar("mandar_mensaje_por_whatsapp", {"contacto": "x"})
        self.assertFalse(resultado.ok)
        self.assertTrue(resultado.desconocida and resultado.observar)
        self.assertIn("enviar_whatsapp", resultado.texto)

    def test_buscador_de_herramientas_por_intencion(self):
        casos = {
            "qué clima hace en Pachuca": "clima",
            "instala discord": "instalar_programa",
            "mándale whatsapp a mamá": "enviar_whatsapp",
            "precio del dólar": "tipo_de_cambio",
            "quiero ver mis pendientes": "listar_pendientes",
            "convierte millas a kilómetros": "convertir_unidades",
        }
        for consulta, esperada in casos.items():
            nombres = [h.nombre for h in REGISTRO.buscar(consulta, limite=3, solo_disponibles=False)]
            self.assertIn(esperada, nombres, consulta)

    def test_ajusta_parametros_por_parecido_y_convierte_tipos(self):
        registro = Registro()
        registro.conector("x", "X", "")

        def fijar(porcentaje: int, nombre_dispositivo: str = "") -> str:
            return f"{porcentaje}:{nombre_dispositivo}"

        h = registro.registrar_funcion(fijar, "x", "Fija algo")
        self.assertEqual(registro.ajustar_parametros(h, {"porcentaje": "40%", "dispositivo": "bocinas"}),
                         {"porcentaje": 40, "nombre_dispositivo": "bocinas"})
        self.assertEqual(registro.ejecutar("fijar", {"valor": "70"}).texto, "70:")

    def test_faltan_parametros_requeridos(self):
        resultado = REGISTRO.ejecutar("clima_x_no", {})
        self.assertFalse(resultado.ok)
        resultado = REGISTRO.ejecutar("convertir_unidades", {"cantidad": 3})
        self.assertIn("me falta", resultado.texto)
        self.assertTrue(resultado.observar)

    def test_sensible_sin_permiso_no_se_ejecuta(self):
        llamadas = []
        registro = Registro()
        registro.conector("x", "X", "")
        registro.registrar_funcion(lambda: llamadas.append(1) or "hecho", "x", "Peligroso", nombre="peligro", sensible=True)
        self.assertIn("NO autorizó", registro.ejecutar("peligro", {}, permiso=lambda _d: False).texto)
        self.assertEqual(llamadas, [])
        self.assertEqual(registro.ejecutar("peligro", {}, permiso=lambda _d: True).texto, "hecho")

    def test_conector_no_disponible_explica_el_motivo(self):
        registro = Registro()
        registro.conector("x", "Cosa rara", "", requisito=lambda: "falta instalar algo")
        registro.registrar_funcion(lambda: "ok", "x", "Hace algo", nombre="hacer")
        self.assertIn("falta instalar algo", registro.ejecutar("hacer").texto)
        self.assertEqual(registro.buscar("hace algo"), [])

    def test_prompt_fijo_y_herramientas_dinamicas(self):
        self.assertLess(len(_construir_system_prompt()), 2600)
        nombres = [h.nombre for h in _herramientas_para("qué clima hace mañana")]
        self.assertIn("clima", nombres)
        self.assertIn("buscar_herramientas", nombres)
        self.assertLessEqual(len(nombres), 16)


class TestSimilitud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        os.makedirs(os.path.join(base, "bark_AI", "config"))
        os.makedirs(os.path.join(base, "Tareas Ética"))
        os.makedirs(os.path.join(base, "Tareas Cálculo"))
        open(os.path.join(base, "bark_AI", "Reporte Final.docx"), "w").close()
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrige_rutas_que_no_existen(self):
        ruta, _ = similitud.resolver_ruta(os.path.join(self.base, "bark ai", "Config"))
        self.assertEqual(ruta, os.path.join(self.base, "bark_AI", "config"))
        ruta, _ = similitud.resolver_ruta(os.path.join(self.base, "bark_ai", "reporte final"))
        self.assertEqual(ruta, os.path.join(self.base, "bark_AI", "Reporte Final.docx"))

    def test_nombre_suelto_se_busca_en_carpetas_personales(self):
        with patch.object(similitud, "carpetas_personales", return_value=[self.base]):
            ruta, _ = similitud.resolver_ruta("reporte final")
            nada, opciones = similitud.resolver_ruta("tareas")
        self.assertTrue(ruta.endswith("Reporte Final.docx"), ruta)
        self.assertIsNone(nada)
        self.assertEqual(len(opciones), 2)

    def test_ruta_ambigua_devuelve_opciones(self):
        ruta, opciones = similitud.resolver_ruta(os.path.join(self.base, "tareas"))
        self.assertIsNone(ruta)
        self.assertEqual(len(opciones), 2)

    def test_herramienta_con_ruta_corrige_y_avisa(self):
        resultado = REGISTRO.ejecutar("info_archivo", {"ruta": os.path.join(self.base, "bark ai", "reporte final")})
        self.assertTrue(resultado.ok, resultado.texto)
        self.assertIn("ruta más parecida", resultado.texto)

    def test_busqueda_por_nombre_en_carpetas(self):
        ranking = similitud.buscar_por_nombre("tarea etica", raices=[self.base], solo_carpetas=True)
        self.assertTrue(ranking[0][1].endswith("Tareas Ética"))

    def test_elegir_pregunta_cuando_hay_empate(self):
        elegido, opciones = similitud.elegir("mama", ["Mamá", "Mamá Lupe", "Papá"])
        self.assertEqual(elegido, "Mamá")
        elegido, opciones = similitud.elegir("tareas", ["Tareas Ética", "Tareas Cálculo"])
        self.assertIsNone(elegido)
        self.assertEqual(len(opciones), 2)


class TestProductividad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parche = patch.object(config, "CARPETA_BD", self.tmp.name)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        self.tmp.cleanup()

    def test_notas_y_pendientes_con_nombres_aproximados(self):
        REGISTRO.ejecutar("crear_nota", {"texto": "comprar tortillas y café"})
        self.assertIn("tortillas", REGISTRO.ejecutar("listar_notas", {"consulta": "tortilla"}).texto)
        REGISTRO.ejecutar("agregar_pendiente", {"tarea": "Entregar la tarea de ética"})
        self.assertIn("taché", REGISTRO.ejecutar("completar_pendiente", {"tarea": "tarea de etica"}).texto)
        self.assertIn("No tienes pendientes", REGISTRO.ejecutar("listar_pendientes").texto)

    def test_contactos_y_whatsapp(self):
        REGISTRO.ejecutar("guardar_contacto", {"nombre": "Mamá", "telefono": "771 123 4567"})
        with patch("webbrowser.open") as abrir:
            texto = REGISTRO.ejecutar("enviar_whatsapp", {"contacto": "mama", "mensaje": "ya voy"}).texto
        abrir.assert_called_once_with("https://wa.me/527711234567?text=ya%20voy")
        self.assertIn("Enviar", texto)
        self.assertIn("No encontré", REGISTRO.ejecutar("enviar_whatsapp", {"contacto": "zzzz"}).texto)

    def test_agenda(self):
        REGISTRO.ejecutar("agregar_evento", {"titulo": "Dentista", "fecha": "mañana", "hora": "5"})
        texto = REGISTRO.ejecutar("eventos_de", {"fecha": "mañana"}).texto
        self.assertIn("Dentista", texto)
        self.assertIn("17:00", texto)


class TestFechasYHoras(unittest.TestCase):
    def test_fechas(self):
        hoy = date(2026, 11, 18)  # miércoles
        self.assertEqual(interpretar_fecha("mañana", hoy), date(2026, 11, 19))
        self.assertEqual(interpretar_fecha("pasado mañana", hoy), date(2026, 11, 20))
        self.assertEqual(interpretar_fecha("el viernes", hoy), date(2026, 11, 20))
        self.assertEqual(interpretar_fecha("25/12", hoy), date(2026, 12, 25))
        self.assertEqual(interpretar_fecha("3 de enero", hoy), date(2027, 1, 3))

    def test_horas(self):
        tarde = datetime(2026, 11, 18, 15, 0)
        self.assertEqual(interpretar_hora("17:30", tarde), time(17, 30))
        self.assertEqual(interpretar_hora("5", tarde), time(17, 0))
        self.assertEqual(interpretar_hora("5 de la mañana", tarde), time(5, 0))
        self.assertEqual(interpretar_hora("9", tarde), time(21, 0))
        self.assertEqual(interpretar_hora("7 y media de la noche", tarde), time(19, 30))
        self.assertEqual(interpretar_hora("12 pm", tarde), time(12, 0))


class TestInformacion(unittest.TestCase):
    def test_clima_elige_la_ciudad_correcta_y_formatea(self):
        def falso(url, parametros=None, timeout=10):
            if "geocoding" in url:
                return {"results": [
                    {"name": "Pachuca de Soto", "latitude": 20.1, "longitude": -98.7, "admin1": "Hidalgo", "population": 300000},
                    {"name": "Pachuca", "latitude": 1, "longitude": 1, "admin1": "Otro", "population": 50},
                    {"name": "Pachuca", "latitude": 20.1, "longitude": -98.7, "admin1": "Hidalgo", "population": 277000},
                ]}
            return {"current": {"temperature_2m": 17.6, "apparent_temperature": 16.2, "relative_humidity_2m": 60,
                                "weather_code": 2, "wind_speed_10m": 11.0},
                    "daily": {"temperature_2m_max": [22, 23, 21], "temperature_2m_min": [8, 9, 7],
                              "precipitation_probability_max": [10, 70, 0], "weather_code": [2, 61, 0]}}
        with patch("conectores.informacion.http_json", falso):
            hoy = REGISTRO.ejecutar("clima", {"ciudad": "Pachuca"}).texto
            manana = REGISTRO.ejecutar("clima", {"ciudad": "Pachuca", "dia": "mañana"}).texto
        self.assertTrue(hoy.startswith("Pachuca, Hidalgo: 18°C"), hoy)
        self.assertIn("parcialmente nublado", hoy)
        self.assertIn("Mañana: lluvia ligera", manana)
        self.assertIn("70%", manana)

    def test_tipo_de_cambio_con_nombres_hablados(self):
        with patch("conectores.informacion.http_json", return_value={"rates": {"MXN": 18.5}, "time_last_update_utc": "Wed, 18 Nov 2026"}):
            texto = REGISTRO.ejecutar("tipo_de_cambio", {"de": "dolares", "a": "pesos", "cantidad": 100}).texto
        self.assertTrue(texto.startswith("100.00 USD = 1,850.00 MXN"), texto)

    def test_sin_internet_da_mensaje_claro(self):
        import requests
        with patch("conectores.informacion.http_json", side_effect=requests.ConnectionError("proxy")):
            texto = REGISTRO.ejecutar("tipo_de_cambio", {}).texto
        self.assertIn("No pude conectarme a internet", texto)

    def test_unidades(self):
        self.assertEqual(REGISTRO.ejecutar("convertir_unidades", {"cantidad": 5, "de": "millas", "a": "kilometros"}).texto, "5 mi = 8.047 km")
        self.assertEqual(REGISTRO.ejecutar("convertir_unidades", {"cantidad": "100", "de": "fahrenheit", "a": "celsius"}).texto, "100 F = 37.78 C")
        self.assertIn("No se puede", REGISTRO.ejecutar("convertir_unidades", {"cantidad": 1, "de": "kilo", "a": "metro"}).texto)


class TestWinget(unittest.TestCase):
    def test_tabla_con_progreso_y_encabezado_en_espanol(self):
        salida = (
            "   - \\ | /\n"
            "Nombre             Id.                   Versión    Origen\n"
            "-----------------------------------------------------------\n"
            "Discord            Discord.Discord       1.0.9180   winget\n"
            "Discord PTB        Discord.Discord.PTB   1.0.1110   winget\n"
        )
        filas = _tabla_winget(salida)
        self.assertEqual(filas[0]["id"], "Discord.Discord")
        self.assertEqual(filas[1]["nombre"], "Discord PTB")


class TestRutasDirectas(unittest.TestCase):
    def test_nuevas_ordenes(self):
        casos = {
            "¿Qué clima hace en Pachuca?": ("clima", {"ciudad": "Pachuca", "dia": "hoy"}),
            "convierte 100 dólares a pesos": ("tipo_de_cambio", {"de": "dolares", "a": "pesos", "cantidad": 100.0}),
            "instala Discord": ("instalar_programa", {"nombre": "Discord"}),
            "anota comprar tortillas y café.": ("crear_nota", {"texto": "comprar tortillas y café"}),
            "agrega a mis pendientes estudiar cálculo": ("agregar_pendiente", {"tarea": "estudiar cálculo"}),
            "mándale un whatsapp a mamá que ya voy": ("enviar_whatsapp", {"contacto": "mamá", "mensaje": "ya voy"}),
            "recuérdame a las 5 que saque la ropa": ("recordatorio_a_las", {"hora": "5", "mensaje": "Recordatorio: saque la ropa"}),
            "qué conectores tienes": ("listar_conectores", {}),
            "conéctate a la red Casa Lemus": ("conectar_wifi", {"nombre": "Casa Lemus"}),
        }
        for frase, esperado in casos.items():
            self.assertEqual(_accion_directa(frase), esperado, frase)

    def test_no_rompe_ordenes_anteriores(self):
        self.assertEqual(_accion_directa("busca el archivo tarea de ayer"),
                         ("buscar_archivos_locales", {"consulta": "tarea", "fecha": "ayer"}))
        self.assertIsNone(_accion_directa("cuanto tiempo hace que no hablamos"))
        self.assertIsNone(_accion_directa("qué temperatura tiene la cpu"))


class TestMCP(unittest.TestCase):
    def setUp(self):
        self.ajustes = {"comando": sys.executable, "argumentos": [os.path.join(os.path.dirname(__file__), "mcp_falso.py")]}

    def tearDown(self):
        mcp.cerrar_todo()
        REGISTRO.quitar_conector("mcp_falso")

    def test_servidor_stdio_se_vuelve_conector(self):
        resumen = mcp.registrar_servidor("falso", self.ajustes)
        self.assertEqual(resumen, "falso: 2 herramientas")  # leyó las dos páginas de tools/list
        h = REGISTRO.obtener("mcp_falso_saludar")
        self.assertFalse(h.sensible)  # readOnlyHint
        self.assertTrue(REGISTRO.obtener("mcp_falso_borrar_todo").sensible)
        # Parámetro mal nombrado y tipo como texto: el esquema JSON los corrige.
        self.assertEqual(REGISTRO.ejecutar("mcp_falso_saludar", {"nombr": "Josh", "veces": "2"}).texto, "Hola, Josh Hola, Josh")
        self.assertIn("NO autorizó", REGISTRO.ejecutar("mcp_falso_borrar_todo", {}, permiso=lambda _d: False).texto)
        self.assertTrue(REGISTRO.ejecutar("mcp_falso_borrar_todo", {}, permiso=lambda _d: True).texto.startswith("Error del servidor"))

    def test_servidor_que_falla_queda_como_no_disponible(self):
        mcp.registrar_servidor("falso", {"comando": "programa-que-no-existe-xyz"})
        estado = next(c for c in REGISTRO.estado() if c["id"] == "mcp_falso")
        self.assertFalse(estado["disponible"])

    def test_expande_variables_de_entorno(self):
        with patch.dict(os.environ, {"TOKEN_PRUEBA": "abc"}):
            self.assertEqual(mcp._expandir({"h": "Bearer ${TOKEN_PRUEBA}"}), {"h": "Bearer abc"})

    def test_formatea_contenido(self):
        texto = mcp.formatear_contenido({"content": [{"type": "text", "text": "a"}, {"type": "image", "mimeType": "image/png"}]})
        self.assertEqual(texto, "a\n[image image/png]")


class TestBucleConSugerencias(unittest.TestCase):
    def test_el_modelo_corrige_una_herramienta_inventada(self):
        from nucleo import cerebro
        respuestas = iter([
            json.dumps({"tipo": "accion", "herramienta": "obtener_tiempo_atmosferico", "parametros": {"lugar": "Pachuca"}}),
            json.dumps({"tipo": "accion", "herramienta": "clima", "parametros": {"ciudad": "Pachuca"}}),
        ])
        vistos = []

        def falso_responder(mensajes, _local):
            vistos.append(mensajes[-1]["content"])
            return next(respuestas), "prueba"

        with patch.object(cerebro, "responder", falso_responder), \
                patch.object(cerebro, "guardar_turno"), \
                patch.object(REGISTRO.herramientas["clima"], "funcion", lambda ciudad="", dia="hoy": f"{ciudad}: 18°C"):
            texto = cerebro.procesar_mensaje("oye necesito saber si salgo con chamarra en Pachuca")
        self.assertIn("no existe", vistos[1])
        self.assertIn("clima", vistos[1])
        self.assertEqual(texto, "Pachuca: 18°C")


if __name__ == "__main__":
    unittest.main()


class TestInicioWindows(unittest.TestCase):
    def test_elige_varios_elementos_con_nombres_aproximados(self):
        from acciones.comandos import _elegir_elementos_inicio
        entradas = [{"id": str(i), "nombre": n} for i, n in enumerate(["Discord", "Steam", "SteamWebHelper", "Spotify", "OneDrive"])]
        elegidos, dudas = _elegir_elementos_inicio("discord, spotifi y one drive", entradas)
        self.assertEqual([e["nombre"] for e in elegidos], ["Discord", "Spotify", "OneDrive"])
        self.assertEqual(dudas, [])
        _, dudas = _elegir_elementos_inicio("zzzz", entradas)
        self.assertTrue(dudas)

    def test_rutas_de_inicio(self):
        self.assertEqual(_accion_directa("reactiva al inicio Discord"), ("reactivar_elemento_inicio", {"nombre": "discord"}))
        self.assertEqual(_accion_directa("desactiva al inicio discord y steam"), ("desactivar_elemento_inicio", {"nombre": "discord y steam"}))
