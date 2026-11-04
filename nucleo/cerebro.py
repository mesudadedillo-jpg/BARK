"""
cerebro.py
El núcleo de Bark: bucle de decisión y herramientas.
Corre el modelo localmente via llama-cpp-python, sin servidores externos.
Mantiene memoria conversacional entre turnos via historial.py.
"""

import json
import re
import unicodedata
import threading
import ast
import difflib
import inspect
import operator
from datetime import datetime
from urllib.parse import quote_plus
try:
    from llama_cpp import Llama
except ImportError:  # Permite usar nube/UI aunque falte el respaldo local.
    Llama = None

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from acciones import apps, comandos, sistema
from nucleo.historial import historial_sesion
from nucleo.bd_manager import biblioteca_conversaciones, buscar_recuerdos, guardar_recuerdo, guardar_turno
from nucleo.proveedores import responder


# ============================================================
# CARGA DEL MODELO (se hace una sola vez al iniciar la app)
# ============================================================

_modelo = None
_bloqueo_modelo = threading.RLock()


def _crear_llama(ruta: str):
    """Instancia el modelo; si la versión de llama-cpp no admite un parámetro, lo omite."""
    argumentos = dict(
        model_path=ruta,
        n_ctx=config.CONTEXTO_MAX,
        n_threads=config.HILOS_CPU,
        n_threads_batch=config.HILOS_CPU,
        n_gpu_layers=config.GPU_LAYERS,
        n_batch=config.LOTE_LOCAL,
        use_mmap=True,
        use_mlock=False,
        verbose=False,
    )
    for opcional in ("n_threads_batch", "n_batch", "use_mmap", "use_mlock", None):
        try:
            return Llama(**argumentos)
        except TypeError:
            if opcional is None:
                raise
            argumentos.pop(opcional, None)


def cargar_modelo():
    global _modelo
    with _bloqueo_modelo:
        if _modelo is None:
            if Llama is None:
                raise RuntimeError("Falta llama-cpp-python; instala requirements.txt para habilitar el respaldo local.")
            rutas = [r for r in [config.RUTA_MODELO, *config.RUTAS_MODELO_RESPALDO] if os.path.isfile(r)]
            if not rutas:
                raise RuntimeError(f"No se encontró el modelo local: {config.RUTA_MODELO}")
            print("Cargando modelo local, esto puede tardar unos segundos...")
            ultimo_error = None
            for ruta in rutas:
                try:
                    _modelo = _crear_llama(ruta)
                    print(f"Modelo cargado: {os.path.basename(ruta)}")
                    break
                except Exception as error:  # arquitectura no soportada, RAM insuficiente…
                    ultimo_error = error
                    print(f"No pude cargar {os.path.basename(ruta)} ({error}); probando otro modelo...")
            if _modelo is None:
                raise RuntimeError(f"Ningún modelo local pudo cargarse: {ultimo_error}")
    return _modelo


# ============================================================
# DEFINICION DE HERRAMIENTAS DISPONIBLES PARA EL LLM
# ============================================================

HERRAMIENTAS_DISPONIBLES = {
    "abrir_aplicacion": comandos.abrir_aplicacion,
    "abrir_ubicacion": comandos.abrir_ubicacion,
    "buscar_y_abrir_aplicacion": comandos.buscar_y_abrir_aplicacion,
    "listar_aplicaciones_instaladas": comandos.listar_aplicaciones_instaladas,
    "listar_juegos_instalados": comandos.listar_juegos_instalados,
    "cerrar_aplicacion": comandos.cerrar_aplicacion,
    "diagnosticar_equipo": comandos.diagnosticar_equipo,
    "diagnosticar_ram_avanzado": comandos.diagnosticar_ram_avanzado,
    "liberar_ram_avanzada": comandos.liberar_ram_avanzada,
    "listar_elementos_inicio": comandos.listar_elementos_inicio,
    "desactivar_elemento_inicio": comandos.desactivar_elemento_inicio,
    "resumen_optimizacion": comandos.resumen_optimizacion,
    "limpiar_temporales_usuario": comandos.limpiar_temporales_usuario,
    "explorar_proyecto": comandos.explorar_proyecto,
    "leer_archivo_codigo": comandos.leer_archivo_codigo,
    "reemplazar_texto_archivo": comandos.reemplazar_texto_archivo,
    "verificar_archivo_python": comandos.verificar_archivo_python,
    "crear_carpeta": comandos.crear_carpeta,
    "listar_archivos": comandos.listar_archivos,
    "buscar_archivos_locales": comandos.buscar_archivos_locales,
    "abrir_archivo_encontrado": comandos.abrir_archivo_encontrado,
    "convertir_documento": comandos.convertir_documento,
    "eliminar_archivo": comandos.eliminar_archivo,
    "escribir_archivo": comandos.escribir_archivo,
    "buscar_informacion": comandos.buscar_informacion,
    "extraer_texto_pagina": comandos.extraer_texto_pagina,
    "abrir_en_navegador": comandos.abrir_en_navegador,
    "leer_documento": comandos.leer_documento,
    "crear_documento_word": comandos.crear_documento_word,
    "investigar_y_crear_word": comandos.investigar_y_crear_word,
    "tomar_captura_pantalla": comandos.tomar_captura_pantalla,
    "descargar_video": comandos.descargar_video,
    "ejecutar_script_python": comandos.ejecutar_script_python,
    "cerrar_pestana_navegador": comandos.cerrar_pestana_navegador,
    "controlar_spotify": comandos.controlar_spotify,
    "buscar_y_reproducir_spotify": comandos.buscar_y_reproducir_spotify,
    "buscar_canciones": comandos.buscar_canciones,
    "reproducir_cancion_encontrada": comandos.reproducir_cancion_encontrada,
    "crear_carpeta_resultados": comandos.crear_carpeta_resultados,
    "consultar_memoria": lambda consulta="": _formatear_recuerdos(consulta, incluir_sensibles=True),
    "consultar_biblioteca": lambda periodo="hoy", consulta="": _formatear_biblioteca(periodo, consulta),
    # --- control del equipo (acciones/sistema.py) ---
    "abrir_configuracion": sistema.abrir_configuracion,
    "controlar_volumen": sistema.controlar_volumen,
    "fijar_volumen": sistema.fijar_volumen,
    "fijar_brillo": sistema.fijar_brillo,
    "estado_bateria": sistema.estado_bateria,
    "bloquear_pc": sistema.bloquear_pc,
    "minimizar_todo": sistema.minimizar_todo,
    "apagar_pc": sistema.apagar_pc,
    "reiniciar_pc": sistema.reiniciar_pc,
    "suspender_pc": sistema.suspender_pc,
    "cancelar_apagado": sistema.cancelar_apagado,
    "listar_ventanas_abiertas": sistema.listar_ventanas_abiertas,
    "enfocar_ventana": sistema.enfocar_ventana,
    "leer_portapapeles": sistema.leer_portapapeles,
    "copiar_al_portapapeles": sistema.copiar_al_portapapeles,
    "crear_temporizador": sistema.crear_temporizador,
    "listar_temporizadores": sistema.listar_temporizadores,
    "cancelar_temporizadores": sistema.cancelar_temporizadores,
    "optimizar_pc": sistema.optimizar_pc,
    "plan_de_energia": sistema.plan_de_energia,
}

# Nombres que un modelo pequeño suele inventar → herramienta real.
SINONIMOS_HERRAMIENTAS = {
    "abrir_app": "abrir_aplicacion", "abrir_programa": "abrir_aplicacion", "open_app": "abrir_aplicacion",
    "abrir_aplicaciones": "abrir_aplicacion", "iniciar_aplicacion": "abrir_aplicacion",
    "cerrar_app": "cerrar_aplicacion", "cerrar_programa": "cerrar_aplicacion",
    "buscar_aplicacion": "listar_aplicaciones_instaladas", "buscar_app": "listar_aplicaciones_instaladas",
    "subir_volumen": "controlar_volumen", "bajar_volumen": "controlar_volumen", "volumen": "controlar_volumen",
    "temporizador": "crear_temporizador", "recordatorio": "crear_temporizador", "bateria": "estado_bateria",
}

# Acciones que SIEMPRE requieren permiso explicito del usuario antes de ejecutarse
ACCIONES_SENSIBLES = {
    "eliminar_archivo",
    "escribir_archivo",
    "convertir_documento",
    "tomar_captura_pantalla",
    "descargar_video",
    "ejecutar_script_python",
    "cerrar_aplicacion",
    "desactivar_elemento_inicio",
    "limpiar_temporales_usuario",
    "liberar_ram_avanzada",
    "reemplazar_texto_archivo",
    "apagar_pc",
    "reiniciar_pc",
    "suspender_pc",
    "optimizar_pc",
    "plan_de_energia",
}

# Puede ser reemplazado por una interfaz grafica o de voz. Mantenerlo aqui
# evita que el nucleo dependa de input(), que bloquea una futura UI.
_solicitador_permiso = comandos.pedir_permiso_usuario


def configurar_solicitador_permiso(solicitador) -> None:
    """Configura una funcion que recibe una descripcion y devuelve bool."""
    global _solicitador_permiso
    _solicitador_permiso = solicitador


# ============================================================
# PERSONALIDAD Y SYSTEM PROMPT
# ============================================================

def _cargar_personalidad() -> str:
    """Lee el archivo de personalidad/comportamiento. Si no existe, usa un default minimo."""
    ruta_personalidad = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "personalidad.txt",
    )
    try:
        with open(ruta_personalidad, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Eres Bark, un asistente de IA útil y directo."


def _construir_system_prompt_completo() -> str:
    personalidad = _cargar_personalidad()

    return f"""{personalidad}

FORMATO DE RESPUESTA:
Debes responder SIEMPRE con un JSON, sin texto antes ni despues. Dos formatos posibles:

1. Para platicar, responder preguntas, o pedir un dato que falta:
{{"tipo": "respuesta_final", "mensaje": "tu respuesta aqui"}}

2. Para ejecutar una herramienta:
{{"tipo": "accion", "herramienta": "nombre_herramienta", "parametros": {{...}}}}

HERRAMIENTAS DISPONIBLES:
- abrir_aplicacion(nombre_app): abre programas instalados usando protocolo, registro, Inicio, Escritorio, carpeta conocida o PATH
- abrir_ubicacion(ruta): abre una carpeta o archivo existente sin modificarlo
- buscar_y_abrir_aplicacion(nombre): busca accesos en Escritorio y menú Inicio, y abre solo una coincidencia inequívoca
- listar_aplicaciones_instaladas(consulta): lista programas detectados en Registro, Inicio y Escritorio; consulta es opcional para filtrar
- listar_juegos_instalados(): busca juegos y launchers en Inicio, Escritorio y aplicaciones registradas de Windows
- cerrar_aplicacion(nombre_app): resuelve y cierra una aplicación/proceso activo (requiere permiso porque puede perder trabajo sin guardar)
- diagnosticar_equipo(): revisa RAM, disco y cantidad de procesos sin modificar la PC
- diagnosticar_ram_avanzado(): muestra los procesos con mayor consumo de RAM sin modificarlos
- liberar_ram_avanzada(): recorta memoria residente de procesos accesibles sin cerrarlos; requiere permiso
- listar_elementos_inicio(): muestra programas y tareas que se lanzan al encender o iniciar sesión, sin modificar nada
- desactivar_elemento_inicio(nombre): desactiva una sola entrada de inicio identificada; no desinstala la app y requiere permiso
- resumen_optimizacion(): analiza RAM, disco, temporales y el número de elementos de inicio sin modificar la PC
- limpiar_temporales_usuario(): limpia solamente archivos temporales del usuario; requiere permiso
- explorar_proyecto(ruta, profundidad): lista la estructura del proyecto local antes de editar
- leer_archivo_codigo(ruta): lee un archivo de código o configuración dentro del proyecto
- reemplazar_texto_archivo(ruta, buscar, reemplazar): modifica un bloque exacto una sola vez; requiere permiso
- verificar_archivo_python(ruta): comprueba la sintaxis de un archivo Python sin ejecutarlo
- crear_carpeta(ruta): crea una carpeta
- listar_archivos(ruta): lista contenido de una carpeta
- buscar_archivos_locales(consulta, fecha): busca documentos por nombre o fecha y ofrece opciones numeradas
- abrir_archivo_encontrado(indice): abre una opción de la última búsqueda de archivos
- convertir_documento(ruta, formato_destino): convierte DOCX a PDF o PDF a DOCX (requiere permiso porque crea un archivo)
- eliminar_archivo(ruta_archivo): borra un archivo (requiere permiso)
- escribir_archivo(ruta_archivo, contenido): crea/sobrescribe un archivo
- buscar_informacion(query): busca en internet, resultados breves
- extraer_texto_pagina(url): extrae texto de una URL especifica
- abrir_en_navegador(url): abre una URL en el navegador (requiere una URL real, nunca vacia)
- leer_documento(ruta): lee texto de un .txt, .pdf o .docx
- crear_documento_word(ruta_archivo, titulo, contenido, referencias): crea un .docx local
- investigar_y_crear_word(tema, ruta_archivo): busca fuentes web y crea un .docx con extractos y referencias
- tomar_captura_pantalla(): toma una captura de pantalla
- descargar_video(url, solo_audio): descarga de YouTube (requiere permiso)
- ejecutar_script_python(ruta_script): ejecuta un script en la sandbox (requiere permiso)
- cerrar_pestana_navegador(): cierra la pestaña activa del navegador (Ctrl+W)
- controlar_spotify(accion): controla reproduccion. accion = "play_pausa", "siguiente" o "anterior"
- buscar_y_reproducir_spotify(cancion): abre Spotify y busca una cancion
- buscar_canciones(consulta): muestra opciones de canciones para elegir por número o artista
- reproducir_cancion_encontrada(indice, artista): reproduce una canción de la última lista de opciones
- crear_carpeta_resultados(nombre_tarea): crea una carpeta "Resultados_X" en el escritorio para guardar ahi archivos de una tarea
- consultar_memoria(consulta): consulta recuerdos personales relevantes ya guardados localmente
- consultar_biblioteca(periodo, consulta): muestra conversaciones guardadas. periodo = "hoy", "semana" o "todo"

LIMITACIONES REALES (se honesto sobre esto, nunca finjas que si puedes):
- No puedes saber cual pestaña especifica esta abierta ni su titulo, solo puedes cerrar "la pestaña activa" con cerrar_pestana_navegador.
- No puedes generar archivos PDF directamente ni imagenes. Si puedes crear archivos Word .docx con crear_documento_word.
- Si el usuario pide algo que ninguna herramienta de la lista puede hacer, responde con respuesta_final explicando honestamente que no tienes esa capacidad todavia. NUNCA digas que ya lo hiciste si no existe la herramienta.

EJEMPLOS:

Usuario: "hola"
Respuesta: {{"tipo": "respuesta_final", "mensaje": "Hola, ¿en que puedo ayudarte?"}}

Usuario: "abre el navegador"
Respuesta: {{"tipo": "respuesta_final", "mensaje": "¿Que pagina quieres que abra?"}}

Usuario: "abre google"
Respuesta: {{"tipo": "accion", "herramienta": "abrir_en_navegador", "parametros": {{"url": "https://www.google.com"}}}}

Usuario: "abre Roblox Player"
Respuesta: {{"tipo": "accion", "herramienta": "abrir_aplicacion", "parametros": {{"nombre_app": "roblox player"}}}}

Usuario: "cierra esa pestaña"
Respuesta: {{"tipo": "respuesta_final", "mensaje": "Solo puedo enviar Ctrl+W a la pestaña activa; no puedo listar ni identificar las pestañas reales del navegador. ¿Confirmas que cierre la pestaña activa?"}}

Usuario: "pon musica en spotify"
Respuesta: {{"tipo": "respuesta_final", "mensaje": "¿Que cancion o artista quieres que busque?"}}

Usuario: "pon algo de queen"
Respuesta: {{"tipo": "accion", "herramienta": "buscar_y_reproducir_spotify", "parametros": {{"cancion": "queen"}}}}

Usuario: "pausa la musica"
Respuesta: {{"tipo": "accion", "herramienta": "controlar_spotify", "parametros": {{"accion": "play_pausa"}}}}

IMPORTANTE SOBRE SPOTIFY: Nunca uses "siguiente" para una solicitud de cancion. Usa buscar_y_reproducir_spotify(cancion). Las teclas multimedia solo envian el comando; nunca afirmes que la musica realmente se reprodujo o pauso.

Usuario: "investiga sobre pinguinos en formato APA y Word"
Respuesta: {{"tipo": "accion", "herramienta": "investigar_y_crear_word", "parametros": {{"tema": "pingüinos", "ruta_archivo": "resultados/investigacion_pinguinos.docx"}}}}

Usuario: "investiga sobre pinguinos y dame un pdf"
Respuesta: {{"tipo": "respuesta_final", "mensaje": "Puedo investigar sobre pinguinos y guardarte un resumen en texto dentro de una carpeta de resultados, pero todavia no puedo generar archivos PDF. ¿Quieres que lo guarde como archivo de texto en su lugar?"}}

REGLA CRITICA: Nunca respondas texto plano fuera del JSON. Nunca digas que ya ejecutaste algo si no generaste la accion correspondiente. Tras una accion, el programa mostrara el resultado REAL de la herramienta y terminara el turno: no intentes describir, completar ni inventar el resultado. Si dudas si algo es una accion o platica, prefiere responder con respuesta_final y preguntar.
"""


def _construir_system_prompt() -> str:
    """Prompt breve: los comandos frecuentes ya se resuelven sin LLM.

    Reducir miles de caracteres de instrucciones baja de forma notable el
    tiempo hasta el primer token en CPU y deja al modelo para razonamiento.
    """
    personalidad = (
        "Eres Bark, asistente local de escritorio. Habla español, directo y honesto. "
        "Nunca inventes acciones ni rutas. Para tareas de código: explora primero, lee "
        "los archivos necesarios, aplica cambios exactos sin relleno y verifica Python. "
        "No instales dependencias sin autorización; resume brevemente el resultado."
    )
    return f"""{personalidad}

Responde SOLO JSON válido: {{"tipo":"respuesta_final","mensaje":"..."}} o
{{"tipo":"accion","herramienta":"...","parametros":{{...}}}}.
No inventes; pregunta si falta dato.

Usa exactamente uno de estos nombres de herramienta, nunca categorías: abrir_aplicacion, listar_aplicaciones_instaladas,
cerrar_aplicacion, abrir_ubicacion, listar_archivos, buscar_archivos_locales, leer_documento, escribir_archivo,
eliminar_archivo, diagnosticar_equipo, diagnosticar_ram_avanzado, liberar_ram_avanzada, listar_elementos_inicio,
desactivar_elemento_inicio, resumen_optimizacion, limpiar_temporales_usuario, explorar_proyecto, leer_archivo_codigo,
reemplazar_texto_archivo, verificar_archivo_python, buscar_informacion, abrir_en_navegador, extraer_texto_pagina,
crear_documento_word, convertir_documento, abrir_configuracion, controlar_volumen, fijar_volumen, fijar_brillo,
estado_bateria, optimizar_pc, plan_de_energia(modo), bloquear_pc, minimizar_todo, apagar_pc, reiniciar_pc, suspender_pc, cancelar_apagado,
listar_ventanas_abiertas, enfocar_ventana, leer_portapapeles, copiar_al_portapapeles, crear_temporizador(minutos,mensaje).
Para abrir cualquier programa usa abrir_aplicacion con el nombre hablado tal cual (ya entiende sinónimos y apps de la Tienda).
Para código: explora primero; luego leer_archivo_codigo(ruta), reemplazar_texto_archivo(ruta,buscar,reemplazar) y verificar_archivo_python(ruta). Confirma acciones sensibles."""


def _formatear_recuerdos(consulta: str = "", incluir_sensibles: bool = False) -> str:
    recuerdos = buscar_recuerdos(consulta, incluir_sensibles=incluir_sensibles)
    if not recuerdos:
        return "No hay recuerdos personales relevantes guardados."
    return "\n".join(f"- [{r['categoria']}] {r['contenido']}" for r in recuerdos)


def _formatear_biblioteca(periodo: str = "hoy", consulta: str = "") -> str:
    turnos = biblioteca_conversaciones(periodo, consulta, limite=50)
    if not turnos:
        return "No hay conversaciones guardadas para ese periodo."
    return "\n\n".join(
        f"[{turno['creado_en']}]\nTú: {turno['usuario']}\nBark: {turno['chet']}" for turno in turnos
    )


def _detectar_recuerdo(mensaje: str) -> tuple[str, str] | None:
    """Detecta preferencias y perfil; secretos nunca se guardan automáticamente."""
    texto = _normalizar(mensaje)
    if re.search(r"(?:contrasena|password|api[ _-]?key|token|cvv|numero de tarjeta)", texto, flags=re.I):
        return None
    patrones = (
        ("nombre", r"(?:mi nombre es|me llamo)\s+(.+)$"),
        ("preferencia", r"((?:me gusta|prefiero)\s+.+)$"),
        ("perfil", r"((?:actualmente\s+)?(?:estudio|trabajo en|vivo en)\s+.+)$"),
        ("dato privado", r"(?:mi )?(?:correo|email|telefono|celular)\s+(?:es|:)?\s*(.+)$"),
        ("salud", r"((?:tengo|soy alergico a|soy alérgico a|mi diagnostico es|mi diagnóstico es)\s+.+)$"),
        ("perfil", r"(?:recuerda que|quiero que recuerdes que)\s+(.+)$"),
        ("prioridad", r"(?:para mi es importante|mi objetivo es|mi meta es)\s+(.+)$"),
    )
    for categoria, patron in patrones:
        coincidencia = re.search(patron, mensaje, flags=re.I)
        if coincidencia:
            return categoria, coincidencia.group(1).strip(" .")
    return None


def _contiene_dato_sensible(mensaje: str) -> bool:
    texto = _normalizar(mensaje)
    return bool(re.search(
        r"(?:contrasena|password|api[ _-]?key|token|cvv|tarjeta|correo|email|telefono|celular|direccion|vivo en|alerg|diagnostic|medic|salud)",
        texto,
    ))


def _guardar_recuerdo_si_corresponde(mensaje: str) -> str | None:
    recuerdo = _detectar_recuerdo(mensaje)
    texto = _normalizar(mensaje)
    sensible = _contiene_dato_sensible(texto)
    prioridad = 3 if recuerdo and recuerdo[0] == "prioridad" else (2 if sensible else 1)
    if recuerdo and guardar_recuerdo(*recuerdo, prioridad=prioridad, sensible=sensible):
        return f"Guardé este recuerdo local: {recuerdo[1]}."
    return None


def _normalizar(texto: str) -> str:
    return "".join(
        caracter for caracter in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(caracter) != "Mn"
    )


def _calcular_expresion(expresion: str) -> str | None:
    """Evalúa sólo operaciones aritméticas simples, nunca código Python."""
    operaciones = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }
    try:
        arbol = ast.parse(expresion.replace("x", "*"), mode="eval")
        def resolver(nodo):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, (int, float)):
                return nodo.value
            if isinstance(nodo, ast.UnaryOp) and type(nodo.op) in operaciones:
                return operaciones[type(nodo.op)](resolver(nodo.operand))
            if isinstance(nodo, ast.BinOp) and type(nodo.op) in operaciones:
                izquierda, derecha = resolver(nodo.left), resolver(nodo.right)
                if abs(izquierda) > 10**12 or abs(derecha) > 10**12 or (isinstance(nodo.op, ast.Pow) and abs(derecha) > 8):
                    raise ValueError("operación demasiado grande")
                return operaciones[type(nodo.op)](izquierda, derecha)
            raise ValueError("expresión no permitida")
        resultado = resolver(arbol.body)
        return str(round(resultado, 8))
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _respuesta_directa(mensaje: str) -> str | None:
    """Respuestas instantáneas que no necesitan contexto ni un LLM."""
    texto = _normalizar(mensaje).strip(" .!¿?")
    if texto in {"hola", "hola bark", "buenos dias", "buenas tardes", "buenas noches"}:
        return "Hola. Estoy listo; las órdenes comunes se ejecutan localmente al instante."
    if texto in {"gracias", "muchas gracias", "gracias bark"}:
        return "De nada."
    if texto in {"que puedes hacer", "ayuda", "que sabes hacer"}:
        return "Puedo abrir programas y ubicaciones, buscar archivos, controlar música, crear documentos, usar memoria y responder preguntas. Di, por ejemplo: “abre Discord”, “busca tarea de ayer” o “reproduce Zoé”."
    if texto in {"que hora es", "dime la hora", "hora"}:
        return f"Son las {datetime.now().strftime('%H:%M')}."
    if texto in {"que fecha es", "fecha", "que dia es", "que dia es hoy"}:
        return f"Hoy es {datetime.now().strftime('%d/%m/%Y')}."
    calculo = re.fullmatch(r"(?:cuanto es|calcula|resuelve)?\s*([0-9\s+*/().x%-]+)", texto)
    if calculo and any(simbolo in calculo.group(1) for simbolo in "+-*/%x"):
        resultado = _calcular_expresion(calculo.group(1))
        if resultado is not None:
            return f"Resultado: {resultado}."
    return None


def _nombre_documento(tema: str) -> str:
    """Devuelve un nombre de archivo corto, seguro y legible."""
    palabras = re.findall(r"[a-z0-9]+", _normalizar(tema))
    return "_".join(palabras)[:60] or "documento"


def _consulta_musical(mensaje: str) -> str:
    """Limpia muletillas mexicanas sin perder título ni artista."""
    consulta = mensaje.strip(" .!¿?")
    consulta = re.sub(
        r"^(?:pon(?:me|le)?|reproduce(?:me)?|reproducir|toca(?:me)?|escucha(?:me)?|"
        r"echame|echate|avientate|quiero\s+(?:oir|escuchar)|busca(?:me)?)(?:\s+(?:la|el|una|un))?\s+",
        "", consulta, flags=re.I,
    )
    consulta = re.sub(r"^(?:cancion|canción|rola|tema|musica|música)\s+(?:de\s+)?", "", consulta, flags=re.I)
    consulta = re.sub(r"^(?:la|el|una|un)\s+(?:cancion|canción|rola|tema|musica|música)\s+", "", consulta, flags=re.I)
    consulta = re.sub(r"\s+(?:en|por)\s+spotify\s*$", "", consulta, flags=re.I)
    return consulta.strip(" .!¿?")


_NUMEROS_ES = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "media": 0.5, "medio": 0.5,
}
_PATRON_DURACION = re.compile(
    r"(?P<n>\d+(?:[.,]\d+)?|" + "|".join(sorted(_NUMEROS_ES, key=len, reverse=True)) + r")\s*"
    r"(?P<u>horas?|hrs?|minutos?|mins?|segundos?|segs?)\b(?P<medio>\s+y\s+medi[ao])?"
)


def _duracion_minutos(texto: str) -> float | None:
    """«10 minutos», «una hora y media», «media hora», «30 segundos» → minutos."""
    total, hallado = 0.0, False
    for m in _PATRON_DURACION.finditer(texto):
        bruto = m.group("n")
        cantidad = _NUMEROS_ES.get(bruto) if bruto in _NUMEROS_ES else float(bruto.replace(",", "."))
        unidad = m.group("u")
        factor = 60 if unidad.startswith(("h")) else 1 / 60 if unidad.startswith("s") else 1
        total += cantidad * factor + (0.5 * factor if m.group("medio") else 0)
        hallado = True
    return total if hallado and total > 0 else None


def _accion_sistema(mensaje: str, texto: str):
    """Órdenes de control del equipo que se resuelven sin pasar por el LLM."""
    musica = any(p in texto for p in ("cancion", "rola", "tema", "musica", "spotify", "siguiente", "anterior"))

    # Temporizadores y recordatorios
    if re.search(r"\b(?:cancela|quita|borra|elimina|detener|deten)\b.*\b(?:temporizador|temporizadores|alarma|alarmas|recordatorios?)\b", texto):
        return "cancelar_temporizadores", {}
    if re.search(r"\b(?:que|cuales|lista|muestra)\b.*\b(?:temporizadores|alarmas|recordatorios)\b", texto):
        return "listar_temporizadores", {}
    avisa = re.search(r"\b(?:avisame|recuerdame|recuerdame|recuerda|dime|alertame)\s+(?:en|dentro de)\s+(.+)", texto)
    temporizador = re.search(r"\b(?:temporizador|cronometro|alarma|timer)\b.*?\b(?:de|para|en)\s+(.+)", texto)
    for coincidencia in (avisa, temporizador):
        if coincidencia:
            minutos = _duracion_minutos(coincidencia.group(1))
            if minutos:
                resto = _PATRON_DURACION.sub("", coincidencia.group(1), count=1)
                resto = re.sub(r"^\s*(?:y\s+medi[ao]\s*)?(?:que\s+|de\s+|para\s+)?", "", resto).strip(" .,")
                aviso = f"Recordatorio: {resto}" if resto and coincidencia is avisa else ""
                return "crear_temporizador", {"minutos": round(minutos, 3), "mensaje": aviso}

    # Volumen y brillo
    fijo = re.search(r"\bvolumen\s+(?:al?\s+|en\s+|a\s+)?(\d{1,3})\s*(?:%|por ciento)?", texto)
    if fijo:
        return "fijar_volumen", {"porcentaje": int(fijo.group(1))}
    brillo = re.search(r"\bbrillo\s+(?:al?\s+|en\s+|a\s+)?(\d{1,3})\s*(?:%|por ciento)?", texto)
    if brillo:
        return "fijar_brillo", {"porcentaje": int(brillo.group(1))}
    if ("volumen" in texto or "sonido" in texto or texto in {"mute", "silencio"}) and not musica:
        pasos = 10 if re.search(r"\b(?:mucho|bastante|full|al maximo)\b", texto) else 5
        if re.search(r"\b(?:silencia|silenciar|mutea|mutear|mute|silencio|quita|apaga)\b", texto):
            return "controlar_volumen", {"accion": "silenciar"}
        if re.search(r"\b(?:sube|subele|subir|aumenta|aumentale|mas alto|mas fuerte)\b", texto):
            return "controlar_volumen", {"accion": "subir", "pasos": pasos}
        if re.search(r"\b(?:baja|bajale|bajar|disminuye|reduce|mas bajo|menos)\b", texto):
            return "controlar_volumen", {"accion": "bajar", "pasos": pasos}
    if texto in {"mute", "silencio", "silenciar", "silencia el pc"}:
        return "controlar_volumen", {"accion": "silenciar"}

    # Estado y sesión
    if "bateria" in texto and re.search(r"\b(?:cuanta|cuanto|nivel|estado|como esta|tengo|queda|carga)\b", texto):
        return "estado_bateria", {}
    if re.search(r"\bbloque(?:a|ar)\b.*\b(?:pc|compu|computadora|sesion|pantalla|equipo)\b", texto):
        return "bloquear_pc", {}
    if re.search(r"\b(?:minimiza|minimizar|oculta|esconde)\s+(?:todo|todas las ventanas)\b|\bmuestra(?:me)? el escritorio\b", texto):
        return "minimizar_todo", {}
    if re.search(r"\bcancela\b.*\b(?:apagado|reinicio)\b", texto):
        return "cancelar_apagado", {}
    pc = r"(?:la\s+|mi\s+)?(?:pc|compu|computadora|equipo|laptop|maquina)"
    if re.search(rf"\b(?:apaga|apagar)\s+{pc}\b", texto):
        return "apagar_pc", {}
    if re.search(rf"\b(?:reinicia|reiniciar)\s+{pc}\b", texto):
        return "reiniciar_pc", {}
    if re.search(rf"\b(?:suspende|suspender|duerme|dormir)\s+{pc}\b", texto):
        return "suspender_pc", {}

    # Ventanas
    if re.search(r"\bventanas?\b.*\babiertas?\b|\bque (?:tengo|hay) abierto\b", texto):
        return "listar_ventanas_abiertas", {}
    cambio = re.fullmatch(r"(?:cambia|cambiate|pasa|pasate)\s+(?:a|al)\s+(?:la\s+ventana\s+de\s+|el\s+|la\s+)?(.+)", texto)
    if cambio and not musica:
        return "enfocar_ventana", {"nombre": cambio.group(1).strip()}

    # Portapapeles
    if re.search(r"\b(?:portapapeles)\b", texto) and re.search(r"\b(?:lee|leer|que tengo|que hay|muestra|dime)\b", texto):
        return "leer_portapapeles", {}
    copiar = re.match(r"copia(?:me)?\s+al\s+portapapeles\s+(.+)", mensaje.strip(), flags=re.I)
    if copiar:
        return "copiar_al_portapapeles", {"texto": copiar.group(1).strip()}

    # Configuración de Windows
    ajuste = re.fullmatch(r"(?:abre|abreme|ve a|entra a|muestra(?:me)?)\s+(?:la\s+|el\s+|los\s+|las\s+)?(?:configuracion|ajustes)\s+(?:de\s+|del\s+)?(.+)", texto)
    if ajuste:
        return "abrir_configuracion", {"seccion": ajuste.group(1).strip()}
    directo = re.fullmatch(r"(?:abre|abreme|ve a|entra a)\s+(?:la\s+|el\s+|las\s+)?(wifi|wi fi|bluetooth|actualizaciones|windows update|modo avion|vpn)", texto)
    if directo:
        return "abrir_configuracion", {"seccion": directo.group(1)}
    return None


def _accion_directa(mensaje: str):
    """Atajos deterministas para controles comunes que un LLM pequeño confunde."""
    texto = _normalizar(mensaje).strip()
    sitios = {
        "google": "https://www.google.com", "youtube": "https://www.youtube.com",
        "gmail": "https://mail.google.com", "correo": "https://mail.google.com",
        "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
        "netflix": "https://www.netflix.com", "spotify web": "https://open.spotify.com",
        "whatsapp web": "https://web.whatsapp.com", "chatgpt": "https://chatgpt.com",
        "github": "https://github.com", "google maps": "https://maps.google.com",
        "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
        "classroom": "https://classroom.google.com", "google classroom": "https://classroom.google.com",
        "tiktok": "https://www.tiktok.com", "linkedin": "https://www.linkedin.com",
        "amazon": "https://www.amazon.com.mx", "mercado libre": "https://www.mercadolibre.com.mx",
        "wikipedia": "https://es.wikipedia.org", "traductor": "https://translate.google.com",
        "canva": "https://www.canva.com", "twitter": "https://x.com", "outlook web": "https://outlook.live.com",
    }
    abrir_sitio = re.fullmatch(r"(?:abre|abreme|ve a|entra a)\s+(?:el |la |mi )?(.+)", texto)
    if abrir_sitio and abrir_sitio.group(1).strip() in sitios:
        return "abrir_en_navegador", {"url": sitios[abrir_sitio.group(1).strip()]}
    if abrir_sitio and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?", abrir_sitio.group(1).strip()):
        return "abrir_en_navegador", {"url": "https://" + abrir_sitio.group(1).strip()}
    accion_sistema = _accion_sistema(mensaje, texto)
    if accion_sistema:
        return accion_sistema
    if texto in {"abre el navegador", "abre navegador", "abre internet"}:
        return "abrir_en_navegador", {"url": "https://www.google.com"}

    # Mantenimiento: estas frases no dependen del modelo local para que el
    # usuario reciba datos reales aun cuando el equipo tenga poca RAM.
    if any(frase in texto for frase in (
        "programas de inicio", "aplicaciones de inicio", "tareas de inicio",
        "que se abre al iniciar", "que se inicia al encender", "tareas que se activan al encender", "arranque de windows",
        "inicio de windows", "mostrar inicio", "iniciar la pc", "iniciar mi pc",
        "se abren al iniciar", "se abran al iniciar", "abren al encender",
    )):
        return "listar_elementos_inicio", {}
    if any(frase in texto for frase in (
        "que consume ram", "que está consumiendo ram", "que esta consumiendo ram",
        "diagnostico de ram", "diagnóstico de ram", "analiza la ram", "analizar ram",
    )):
        return "diagnosticar_ram_avanzado", {}
    if any(frase in texto for frase in (
        "libera ram avanzada", "liberar ram avanzada", "libera la ram",
        "optimiza la ram", "optimizar la ram",
    )):
        return "liberar_ram_avanzada", {}
    if any(frase in texto for frase in (
        "herramientas de optimizacion", "analisis de optimizacion", "analiza el rendimiento",
        "que puedo optimizar", "recomendaciones de optimizacion",
    )):
        return "resumen_optimizacion", {}
    if re.search(r"\b(?:optimiza|optimizar|acelera|acelerar|limpia|limpiar|mejora|mejorar|arregla)\b.*\b(?:pc|compu|computadora|equipo|sistema|rendimiento|lenta|laptop)\b", texto) \
            or re.search(r"\b(?:pc|compu|computadora|laptop)\b.*\b(?:lenta|lento|va lenta|esta lenta)\b", texto):
        return "optimizar_pc", {}
    plan = re.search(r"\b(?:modo|plan)\s+(?:de\s+)?(alto rendimiento|rendimiento|maximo rendimiento|juego|ahorro(?: de energia)?|economico|equilibrado)\b", texto)
    if plan:
        return "plan_de_energia", {"modo": plan.group(1)}
    if re.search(r"\bplan de energia\b", texto) and re.search(r"\b(?:cual|que|muestra|dime)\b", texto):
        return "plan_de_energia", {}
    if any(frase in texto for frase in (
        "limpia temporales", "limpiar temporales", "borra temporales",
        "libera espacio temporal", "limpia archivos temporales",
    )):
        return "limpiar_temporales_usuario", {}
    inicio = re.fullmatch(r"(?:desactiva|quita|elimina)\s+(?:(?:al|del)\s+)?(?:inicio|arranque)\s+(.+)", texto)
    if inicio:
        return "desactivar_elemento_inicio", {"nombre": inicio.group(1).strip()}

    # Música: reconoce formas coloquiales mexicanas y abre Spotify con el
    # título/artista completo, sin hacer pasar la petición por el LLM.
    if re.search(r"\b(?:pausa|pausale|pausala|deten|detenla|detenlo)\b", texto) and any(p in texto for p in ("musica", "mus", "rola", "cancion", "spotify")):
        return "controlar_spotify", {"accion": "play_pausa"}
    if re.search(r"\b(?:reanuda|sigue|continua|continu[aá]le|dale)\b", texto) and any(p in texto for p in ("musica", "mus", "rola", "cancion", "spotify")):
        return "controlar_spotify", {"accion": "play_pausa"}
    if re.search(r"\b(?:siguiente|otra|cambiala|cambiale|saltala|saltate|brincate)\b", texto) and any(p in texto for p in ("cancion", "rola", "tema", "musica", "spotify", "esta")):
        return "controlar_spotify", {"accion": "siguiente"}
    if re.search(r"\b(?:anterior|regresa|regresate|pon\s+la\s+pasada)\b", texto) and any(p in texto for p in ("cancion", "rola", "tema", "musica", "spotify")):
        return "controlar_spotify", {"accion": "anterior"}
    busqueda_directa_musica = re.match(
        r"(?:busca(?:me)?\s+(?:la\s+)?(?:cancion|canción|rola|tema|musica|música)|"
        r"pon(?:me|le)?|reproduce(?:me)?|reproducir|toca(?:me)?|escucha(?:me)?|"
        r"echame|echate|avientate|quiero\s+(?:oir|escuchar))\b.+",
        mensaje.strip(), flags=re.I,
    )
    if busqueda_directa_musica and not texto.startswith("pon en marcha "):
        consulta = _consulta_musical(mensaje)
        # "Busca canciones de…" conserva el selector de resultados. Una
        # canción, rola o tema concreto se reproduce automáticamente.
        if re.match(r"^(?:canciones|rolas|temas)\s+(?:de|por)\s+", consulta, flags=re.I):
            artista = re.sub(r"^(?:canciones|rolas|temas)\s+(?:de|por)\s+", "", consulta, flags=re.I).strip()
            if artista:
                return "buscar_canciones", {"consulta": artista}
        if consulta and len(consulta) >= 2 and not re.fullmatch(r"(?:musica|música|algo|una)", _normalizar(consulta)):
            return "buscar_y_reproducir_spotify", {"cancion": consulta}

    # Documentos: frases cotidianas crean una investigación Word con un nombre
    # automático. La petición explícita de PDF se mantiene en conversión.
    documento = re.search(
        r"(?:haz(?:me)?|crea(?:me)?|armame|preparame|generame|elabora(?:me)?)\s+"
        r"(?:un\s+)?(?:documento|word|archivo|trabajo|investigacion|investigación)\s+"
        r"(?:de|sobre|acerca\s+de)\s+(.+)",
        mensaje, flags=re.I,
    )
    investigar = re.search(
        r"(?:investiga|busca\s+informacion|busca\s+información)\s+(?:de|sobre)?\s*(.+?)"
        r"(?:\s+(?:en|y)\s+(?:word|documento|pdf))?$",
        mensaje, flags=re.I,
    )
    tema_documento = (documento.group(1) if documento else investigar.group(1) if investigar else "").strip(" .!¿?")
    tema_documento = re.sub(r"\s+(?:y\s+)?(?:pasalo|pásalo|guardalo|guárdalo|conviertelo|conviértelo).*$", "", tema_documento, flags=re.I).strip()
    if tema_documento and (documento or re.search(r"\b(?:word|documento|investiga|investigacion|investigación)\b", texto)):
        ruta = os.path.join("resultados", f"{_nombre_documento(tema_documento)}.docx")
        return "investigar_y_crear_word", {"tema": tema_documento, "ruta_archivo": ruta}

    busqueda_web = re.fullmatch(r"(?:busca|buscame|busca me)\s+(.+?)\s+(?:en|por)\s+(google|youtube)", texto)
    if busqueda_web:
        consulta, sitio = busqueda_web.groups()
        base = "https://www.youtube.com/results?search_query=" if sitio == "youtube" else "https://www.google.com/search?q="
        return "abrir_en_navegador", {"url": base + quote_plus(consulta)}
    if texto in {"mi historial hoy", "historial de hoy", "que hablamos hoy"}:
        return "consultar_biblioteca", {"periodo": "hoy"}
    if texto in {"mi historial de la semana", "historial semanal", "que hablamos esta semana"}:
        return "consultar_biblioteca", {"periodo": "semana"}
    if texto in {"mis recuerdos", "muestrame mis recuerdos", "muestra mi memoria", "que recuerdas de mi", "que recuerdas"}:
        return "consultar_memoria", {"consulta": ""}
    if texto in {"revisa mi pc", "revisa la pc", "como esta mi pc", "rendimiento de la pc", "estado del equipo"}:
        return "diagnosticar_equipo", {}
    conversion = re.search(r"(?:convierte|transforma)\s+([a-zA-Z]:\\.+?\.(?:docx|pdf))\s+(?:a|en)\s+(pdf|docx|word)\b", mensaje, flags=re.I)
    if conversion:
        destino = "docx" if _normalizar(conversion.group(2)) == "word" else conversion.group(2).lower()
        return "convertir_documento", {"ruta": conversion.group(1).strip(), "formato_destino": destino}
    ruta = mensaje.strip().strip('"')
    # Las rutas pegadas desde el Explorador son una instrucción suficiente.
    # Evita que un modelo pequeño vuelva a pedir una ruta que ya recibió.
    if re.fullmatch(r"[a-zA-Z]:[\\/].*", ruta):
        return "abrir_ubicacion", {"ruta": ruta}
    ubicaciones = {
        "abre mis descargas": "Downloads", "abre descargas": "Downloads",
        "abre mis documentos": "Documents", "abre documentos": "Documents",
        "abre mi escritorio": "Desktop", "abre el escritorio": "Desktop",
        "abre mis imagenes": "Pictures", "abre imagenes": "Pictures", "abre fotos": "Pictures",
        "abre mi musica": "Music", "abre musica": "Music", "abre videos": "Videos",
    }
    if texto in ubicaciones:
        return "abrir_ubicacion", {"ruta": os.path.join(os.path.expanduser("~"), ubicaciones[texto])}
    if any(frase in texto for frase in (
        "que juegos tengo", "que juegos hay", "mis juegos", "juegos instalados",
        "juegos tengo",
    )):
        return "listar_juegos_instalados", {}
    if any(frase in texto for frase in (
        "que programas tengo", "mis programas", "programas instalados",
        "que aplicaciones tengo", "mis aplicaciones", "aplicaciones instaladas",
        "aplicaciones del sistema",
    )):
        return "listar_aplicaciones_instaladas", {}
    coincidencia = re.fullmatch(r"(?:busca|encuentra|lista)\s+(?:la\s+)?(?:aplicacion|aplicación|programa)\s+(.+)", texto)
    if coincidencia:
        return "listar_aplicaciones_instaladas", {"consulta": coincidencia.group(1).strip()}
    if texto in {"y en el escritorio", "en el escritorio", "mi escritorio"} or re.search(r"\b(?:en|el) escritorio\b", texto):
        return "listar_archivos", {"ruta": os.path.join(os.path.expanduser("~"), "Desktop")}
    seleccion = re.search(r"\b(?:la\s+)?(?:primera|opcion\s+1|numero\s+1)\b", texto)
    if re.search(r"\b(?:abre|abreme|abrir)\b", texto) and seleccion:
        return "abrir_archivo_encontrado", {"indice": 1}
    archivo = re.search(r"(?:busca|encuentra|abre)\s+(?:el\s+)?(?:archivo\s+)?(.+?)\s+(?:de|del)\s+(hoy|ayer|\d{1,2}[/-]\d{1,2}[/-]\d{4})\s*$", mensaje, flags=re.I)
    if archivo:
        return "buscar_archivos_locales", {"consulta": archivo.group(1).strip(), "fecha": archivo.group(2).strip()}
    busqueda_musica = re.match(r"(?:busca\s+)?(?:canciones|musica|música)\s+(?:de|por)\s+(.+)$", mensaje, flags=re.I)
    if busqueda_musica:
        return "buscar_canciones", {"consulta": busqueda_musica.group(1).strip()}
    opcion_musica = re.search(r"\b(?:quiero|pon|reproduce)\s+(?:la\s+)?(primera|segunda|tercera|\d+)\b", texto)
    if opcion_musica:
        mapa = {"primera": 1, "segunda": 2, "tercera": 3}
        indice = mapa.get(opcion_musica.group(1), int(opcion_musica.group(1)) if opcion_musica.group(1).isdigit() else 0)
        return "reproducir_cancion_encontrada", {"indice": indice}
    artista_opcion = re.fullmatch(r"(?:quiero|pon|reproduce)\s+la\s+de\s+(.+)", mensaje, flags=re.I)
    if artista_opcion:
        return "reproducir_cancion_encontrada", {"artista": artista_opcion.group(1).strip()}
    coincidencia = re.search(r"(?:quiero\s+)?jugar\s+(?:a\s+)?(gta|grand theft auto|roblox)\b", texto)
    if coincidencia:
        nombre = "gta" if coincidencia.group(1) in ("gta", "grand theft auto") else "roblox"
        return "buscar_y_abrir_aplicacion", {"nombre": nombre}
    if re.search(r"\b(pausa|pausar|reanuda|reanudar|continua|continuar)\b", texto) and "mus" in texto:
        return "controlar_spotify", {"accion": "play_pausa"}
    coincidencia = re.search(r"(?:busca\s+y\s+)?reproduc(?:e|ir)\s+(.+)$", mensaje, flags=re.I)
    if coincidencia:
        cancion = re.sub(r"\s+en\s+spotify\s*$", "", coincidencia.group(1), flags=re.I).strip()
        if cancion:
            return "buscar_y_reproducir_spotify", {"cancion": cancion}
    if texto.startswith(("reproduce ", "reproducir ", "pon ", "ponle ", "toca ", "escucha ")) and not texto.startswith("pon en marcha "):
        cancion = re.sub(r"^(reproduce|reproducir|pon|ponle|toca|escucha)\s+", "", mensaje, flags=re.I)
        cancion = re.sub(r"\s+en\s+spotify\s*$", "", cancion, flags=re.I).strip()
        if cancion and not re.fullmatch(r"(play|pausa|musica|música)", _normalizar(cancion)):
            return "buscar_y_reproducir_spotify", {"cancion": cancion}
    if texto in {"cierralo", "ciérralo", "cierrala", "ciérrala", "cierralo ya", "ciérralo ya"}:
        ultima = comandos.ultima_aplicacion_abierta()
        if ultima:
            return "cerrar_aplicacion", {"nombre_app": ultima}
    coincidencia = re.fullmatch(r"cierra\s+(.+)", texto)
    if coincidencia:
        return "cerrar_aplicacion", {"nombre_app": coincidencia.group(1)}
    coincidencia = re.fullmatch(r"(?:fuerza el cierre de|forzar cierre de|cierra forzado)\s+(.+)", texto)
    if coincidencia:
        return "cerrar_aplicacion", {"nombre_app": coincidencia.group(1)}
    coincidencia = re.match(r"abre\s+(steam|spotify|roblox(?: player)?|configuracion|configuración|whatsapp)\b", texto)
    if coincidencia:
        nombre = coincidencia.group(1)
        if nombre == "roblox":
            nombre = "roblox player"
        return "abrir_aplicacion", {"nombre_app": nombre}
    # Para apps no conocidas de antemano no depende del LLM: la herramienta
    # inspecciona los orígenes locales de Windows antes de rendirse.
    coincidencia = re.fullmatch(r"(?:abre|abreme|abrirme|inicia|inicializa|lanza|ejecuta|pon en marcha|corre)\s+(?:la\s+|el\s+)?(.+)", texto)
    if coincidencia:
        nombre = coincidencia.group(1).strip(" .")
        if nombre and nombre not in {"navegador", "explorador", "internet", "google"}:
            return "abrir_aplicacion", {"nombre_app": nombre}
    if any(frase in texto for frase in ("pc saturada", "pc esta saturada", "computadora saturada", "diagnostica la pc", "diagnostica mi pc", "estado de la pc")):
        return "diagnosticar_equipo", {}
    if ("investiga" in texto or "investigacion" in texto or "investigación" in texto) and "word" in texto:
        tema = re.sub(r"^(ahora\s+)?haz\s+una\s+investigacion\s+(de|sobre)\s+", "", mensaje, flags=re.I)
        tema = re.sub(r"\s+(y\s+)?(dame|guardalo|guárdalo).*$", "", tema, flags=re.I).strip()
        if tema:
            nombre = "_".join(re.findall(r"[a-z0-9]+", _normalizar(tema)))[:60]
            return "investigar_y_crear_word", {"tema": tema, "ruta_archivo": f"resultados/{nombre}.docx"}
    return None


# ============================================================
# BUCLE REACT: PENSAR -> ACTUAR -> OBSERVAR
# ============================================================

def procesar_mensaje(mensaje_usuario: str, max_iteraciones: int = None) -> str:
    """
    Punto de entrada principal. Recibe texto del usuario (ya sea por voz
    transcrita o escrito), corre el bucle ReAct con contexto del historial
    de la sesion, y devuelve la respuesta final. Al terminar, guarda el
    turno limpio en el historial.
    """
    mensaje_sensible = _contiene_dato_sensible(mensaje_usuario)
    respuesta_directa = _respuesta_directa(mensaje_usuario)
    if respuesta_directa:
        historial_sesion.agregar_turno(mensaje_usuario, respuesta_directa, sensible=mensaje_sensible)
        guardar_turno(mensaje_usuario, respuesta_directa)
        return respuesta_directa
    accion = _accion_directa(mensaje_usuario)
    if accion:
        resultado = _ejecutar_herramienta(*accion)
        aviso_recuerdo = _guardar_recuerdo_si_corresponde(mensaje_usuario)
        if aviso_recuerdo:
            resultado = f"{resultado}\n{aviso_recuerdo}"
        historial_sesion.agregar_turno(mensaje_usuario, resultado, sensible=mensaje_sensible)
        guardar_turno(mensaje_usuario, resultado)
        return resultado

    limite = max_iteraciones or config.MAX_ITERACIONES_DEFAULT
    # En tareas de ingeniería el agente necesita observar cada paso antes de
    # decidir el siguiente. Las demás conversaciones conservan una sola acción
    # para que abrir apps, música y consultas sigan siendo inmediatas.
    modo_ingenieria = bool(re.search(
        r"\b(?:codigo|código|programa|proyecto|python|archivo|bug|error|funcion|función|refactor|interfaz)\b",
        _normalizar(mensaje_usuario),
    ))
    herramientas_ingenieria = {
        "explorar_proyecto", "leer_archivo_codigo", "reemplazar_texto_archivo", "verificar_archivo_python",
    }

    mensajes = [{"role": "system", "content": _construir_system_prompt()}]
    # Al proveedor de nube solo se le dan recuerdos no sensibles y relevantes.
    recuerdos = _formatear_recuerdos(mensaje_usuario, incluir_sensibles=False)
    if not recuerdos.startswith("No hay"):
        mensajes.append({"role": "system", "content": f"MEMORIA PERSONAL RELEVANTE:\n{recuerdos}"})
    # El historial sensible no sale de Bark: ni siquiera cuando la consulta actual es inocua.
    mensajes.extend(historial_sesion.obtener_mensajes_contexto(incluir_sensibles=False))
    mensajes.append({"role": "user", "content": mensaje_usuario})

    respuesta_final_texto = None

    for iteracion in range(limite):
        if mensaje_sensible:
            respuesta_llm, _origen = _llamar_modelo_local(mensajes), "local"
        else:
            respuesta_llm, _origen = responder(mensajes, _llamar_modelo_local)
        decision = _parsear_respuesta_json(respuesta_llm)

        if decision is None:
            mensajes.append({"role": "assistant", "content": respuesta_llm})
            mensajes.append({
                "role": "user",
                "content": (
                    "Tu respuesta anterior no fue un JSON valido. "
                    "Responde UNICAMENTE con el JSON correcto, sin texto adicional."
                ),
            })
            continue

        if decision.get("tipo") == "respuesta_final":
            respuesta_final_texto = decision.get("mensaje", "Tarea completada.")
            break

        elif decision.get("tipo") == "accion":
            nombre_herramienta = decision.get("herramienta")
            parametros = decision.get("parametros", {})

            resultado = _ejecutar_herramienta(nombre_herramienta, parametros)
            if modo_ingenieria and nombre_herramienta in herramientas_ingenieria:
                mensajes.append({"role": "assistant", "content": respuesta_llm})
                mensajes.append({
                    "role": "user",
                    "content": (
                        "OBSERVACIÓN REAL DE LA HERRAMIENTA:\n"
                        f"{str(resultado)[:config.MAX_CARACTERES_OBSERVACION]}\n\n"
                        "Continúa solo si hace falta inspeccionar, editar o verificar; "
                        "de lo contrario responde con respuesta_final y resume el resultado."
                    ),
                })
                continue
            # Un modelo pequeño no debe reinterpretar ni adornar el resultado
            # de una acción aislada. Mostrar el dato observado evita alucinaciones.
            respuesta_final_texto = str(resultado)[:config.MAX_CARACTERES_OBSERVACION]
            break

        else:
            respuesta_final_texto = "El modelo devolvio un formato no reconocido."
            break

    if respuesta_final_texto is None:
        respuesta_final_texto = f"Se alcanzo el limite de {limite} iteraciones sin completar la tarea."

    historial_sesion.agregar_turno(mensaje_usuario, respuesta_final_texto, sensible=mensaje_sensible)
    aviso_recuerdo = _guardar_recuerdo_si_corresponde(mensaje_usuario)
    if aviso_recuerdo and aviso_recuerdo not in respuesta_final_texto:
        respuesta_final_texto = f"{respuesta_final_texto}\n{aviso_recuerdo}"
    guardar_turno(mensaje_usuario, respuesta_final_texto)

    return respuesta_final_texto


def _llamar_modelo_local(mensajes) -> str:
    """Envia la conversación al modelo local; se carga solo si hace falta."""
    modelo = cargar_modelo()
    argumentos = {
        "messages": mensajes,
        "temperature": config.TEMPERATURA,
        "max_tokens": config.MAX_TOKENS_RESPUESTA,
        "response_format": {"type": "json_object"},
    }
    # llama.cpp no garantiza seguridad al generar desde dos hilos sobre la
    # misma instancia. Serializar sólo la inferencia evita cierres aleatorios.
    with _bloqueo_modelo:
        try:
            respuesta = modelo.create_chat_completion(**argumentos)
        except TypeError:  # Compatibilidad con versiones antiguas de llama-cpp.
            argumentos.pop("response_format")
            respuesta = modelo.create_chat_completion(**argumentos)
    return respuesta["choices"][0]["message"]["content"].strip()


def _parsear_respuesta_json(texto: str):
    """Intenta convertir la respuesta del LLM en un diccionario JSON."""
    try:
        texto = texto.strip()
        if not (texto.startswith("{") and texto.endswith("}")):
            return None
        decision = json.loads(texto)
        if not isinstance(decision, dict):
            return None
        return decision
    except (json.JSONDecodeError, ValueError):
        return None


def _resolver_herramienta(nombre) -> str | None:
    """Corrige nombres casi correctos que genera un modelo pequeño («abrir_app»)."""
    if nombre in HERRAMIENTAS_DISPONIBLES:
        return nombre
    if not isinstance(nombre, str):
        return None
    clave = _normalizar(nombre).strip().replace(" ", "_").replace("-", "_")
    if clave in HERRAMIENTAS_DISPONIBLES:
        return clave
    if clave in SINONIMOS_HERRAMIENTAS:
        return SINONIMOS_HERRAMIENTAS[clave]
    cercano = difflib.get_close_matches(clave, list(HERRAMIENTAS_DISPONIBLES), n=1, cutoff=0.8)
    return cercano[0] if cercano else None


def _ajustar_parametros(funcion, parametros) -> dict:
    """Si el modelo llama al parámetro con otro nombre («app» en vez de «nombre_app»), lo reasigna por posición."""
    if not isinstance(parametros, dict):
        return {}
    try:
        firma = inspect.signature(funcion).parameters
    except (TypeError, ValueError):
        return parametros
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in firma.values()):
        return parametros
    validos = {k: v for k, v in parametros.items() if k in firma}
    sobrantes = [v for k, v in parametros.items() if k not in firma]
    libres = [n for n in firma if n not in validos]
    for nombre_param, valor in zip(libres, sobrantes):
        validos[nombre_param] = valor
    return validos


def _ejecutar_herramienta(nombre: str, parametros: dict) -> str:
    """Ejecuta la herramienta solicitada, pidiendo permiso si es sensible."""
    resuelto = _resolver_herramienta(nombre)
    if resuelto is None:
        return f"Herramienta '{nombre}' no existe. No finjas haberla ejecutado."
    nombre = resuelto
    parametros = _ajustar_parametros(HERRAMIENTAS_DISPONIBLES[nombre], parametros)

    if nombre in ACCIONES_SENSIBLES:
        descripcion = f"{nombre}({parametros})"
        if not _solicitador_permiso(descripcion):
            return "El usuario NO autorizo esta accion."

    try:
        funcion = HERRAMIENTAS_DISPONIBLES[nombre]
        return funcion(**parametros)
    except TypeError as e:
        return f"Parametros incorrectos para '{nombre}': {e}"
    except Exception as e:
        return f"Error al ejecutar '{nombre}': {e}"


# --- Zona de pruebas ---
if __name__ == "__main__":
    print("Bark - modo consola de prueba (escribe 'salir' para terminar)\n")
    while True:
        entrada = input("Tu: ")
        if entrada.lower() == "salir":
            break
        respuesta = procesar_mensaje(entrada)
        print(f"Bark: {respuesta}\n")


# ============================================================
# ARRANQUE RÁPIDO
# ============================================================

def _modelo_local_necesario() -> bool:
    modo = config.PRECALENTAR_MODELO
    if modo in {"1", "true", "si", "sí", "on"}:
        return True
    if modo in {"0", "false", "no", "off"}:
        return False
    # auto: solo si no hay nube a la que recurrir (el modelo local pesa ~2 GB de RAM).
    return config.PROVEEDOR_PREFERIDO == "local" or not (config.GEMINI_API_KEY or config.OPENAI_API_KEY)


def _precalentar_modelo() -> None:
    try:
        modelo = cargar_modelo()
        with _bloqueo_modelo:
            # Procesa el prompt del sistema una vez: las órdenes siguientes reutilizan ese prefijo.
            modelo.create_chat_completion(
                messages=[{"role": "system", "content": _construir_system_prompt()}, {"role": "user", "content": "hola"}],
                max_tokens=1, temperature=0.0,
            )
    except Exception:
        pass  # El respaldo local se cargará bajo demanda si esto falla.


def precalentar() -> None:
    """Prepara en segundo plano el índice de apps y (si hace falta) el modelo local."""
    apps.precalentar_indice()
    if _modelo_local_necesario():
        threading.Thread(target=_precalentar_modelo, name="bark-precalentar-llm", daemon=True).start()
