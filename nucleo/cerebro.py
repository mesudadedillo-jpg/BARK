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
import operator
from datetime import datetime
from urllib.parse import quote_plus
Llama = None
ERROR_LLAMA = None
_llama_intentado = False


def _control_inteligente_activo() -> bool:
    """True si Smart App Control está en modo de aplicación (bloquea DLL sin firma)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as clave:
            valor, _tipo = winreg.QueryValueEx(clave, "VerifiedAndReputablePolicyState")
        return int(valor) == 1
    except OSError:
        return False


def _importar_llama():
    """Carga llama-cpp solo cuando hace falta y una sola vez.

    Si Windows (Control inteligente de aplicaciones) bloquea llama.dll, se suprime
    su ventana de «Imagen incorrecta» y Bark sigue funcionando con la nube.
    """
    global Llama, ERROR_LLAMA, _llama_intentado
    if _llama_intentado:
        return Llama
    _llama_intentado = True
    preferencia = os.getenv("BARK_MODELO_LOCAL", "auto").strip().lower()
    if preferencia in {"0", "no", "false", "off"}:
        ERROR_LLAMA = "desactivado con BARK_MODELO_LOCAL=0"
        return None
    if preferencia != "1" and _control_inteligente_activo():
        # Con el Control inteligente de aplicaciones activo, Windows bloquea llama.dll
        # y muestra «Imagen incorrecta» aunque se capture el error: mejor ni intentarlo.
        ERROR_LLAMA = ("Windows (Control inteligente de aplicaciones) bloquea llama.dll; "
                       "usa la nube o un motor firmado como Ollama")
        return None
    modo_anterior = None
    if sys.platform == "win32":
        import ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX: sin cuadros de error del sistema.
        modo_anterior = ctypes.windll.kernel32.SetErrorMode(0x8003)
    try:
        from llama_cpp import Llama as _Llama
        Llama = _Llama
    except Exception as error:
        ERROR_LLAMA = str(error)
    finally:
        if modo_anterior is not None:
            ctypes.windll.kernel32.SetErrorMode(modo_anterior)
    return Llama

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from acciones import apps, comandos
from nucleo.historial import historial_sesion
from nucleo.bd_manager import biblioteca_conversaciones, buscar_recuerdos, guardar_recuerdo, guardar_turno
from nucleo.proveedores import responder
from conectores import REGISTRO, mcp as conectores_mcp


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
            if _importar_llama() is None:
                raise RuntimeError(f"El modelo local no está disponible: {ERROR_LLAMA or 'falta llama-cpp-python'}")
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
# HERRAMIENTAS: viven en el registro de conectores (conectores/)
# ============================================================

class _VistaHerramientas(dict):
    """Vista de solo lectura {nombre: función} sobre el registro, incluidas las MCP que lleguen después."""

    def __getitem__(self, nombre):
        return REGISTRO.herramientas[nombre].funcion

    def __contains__(self, nombre):
        return nombre in REGISTRO.herramientas

    def __iter__(self):
        return iter(list(REGISTRO.herramientas))

    def __len__(self):
        return len(REGISTRO.herramientas)

    def keys(self):
        return list(REGISTRO.herramientas)

    def items(self):
        return [(n, h.funcion) for n, h in REGISTRO.herramientas.items()]

    def get(self, nombre, defecto=None):
        return self[nombre] if nombre in self else defecto


HERRAMIENTAS_DISPONIBLES = _VistaHerramientas()

# Herramientas que siempre se ofrecen al modelo, pida lo que pida el usuario.
HERRAMIENTAS_BASE = ("abrir_aplicacion", "abrir_en_navegador", "buscar_informacion", "buscar_herramientas")
HERRAMIENTAS_INGENIERIA = ("explorar_proyecto", "leer_archivo_codigo", "reemplazar_texto_archivo", "verificar_archivo_python")
MAX_HERRAMIENTAS_PROMPT = 12
MAX_OBSERVACIONES = 3  # reintentos tras una herramienta inexistente o buscar_herramientas
# Órdenes cuyo texto describe a otra persona o es contenido dictado, no un dato del usuario.
_SIN_RECUERDO = {"guardar_contacto", "enviar_whatsapp", "redactar_correo", "crear_nota", "agregar_pendiente",
                 "agregar_evento", "copiar_al_portapapeles", "escribir_texto"}

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


def _construir_system_prompt() -> str:
    """Prompt breve y fijo: los comandos frecuentes ya se resuelven sin LLM.

    Es idéntico en cada turno para que llama.cpp reutilice su caché de prefijo;
    la lista de herramientas, que sí cambia, va en un mensaje aparte.
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

Usa solo herramientas de la lista HERRAMIENTAS que llega después, con su nombre exacto.
Si ninguna encaja, llama buscar_herramientas(consulta) antes de rendirte; si tampoco hay, dilo con honestidad.
Los nombres aproximados están bien (apps, archivos, carpetas, contactos, redes Wi-Fi, sitios): Bark busca el más parecido.
Para abrir cualquier programa usa abrir_aplicacion con el nombre hablado tal cual.
Las herramientas marcadas [pide permiso] preguntan solas al usuario; no pidas confirmación tú."""


def _herramientas_para(mensaje: str, contexto: str = "", modo_ingenieria: bool = False) -> list:
    """Elige las herramientas relevantes para este turno (como la búsqueda de herramientas de Claude)."""
    elegidas = [REGISTRO.obtener(n) for n in HERRAMIENTAS_BASE]
    if modo_ingenieria:
        elegidas += [REGISTRO.obtener(n) for n in HERRAMIENTAS_INGENIERIA]
    for h in REGISTRO.buscar(mensaje, limite=MAX_HERRAMIENTAS_PROMPT) + REGISTRO.buscar(contexto, limite=3):
        elegidas.append(h)
    unicas = {h.nombre: h for h in elegidas if h is not None and not REGISTRO.disponible(h)}
    return list(unicas.values())[: MAX_HERRAMIENTAS_PROMPT + len(HERRAMIENTAS_BASE)]


def _mensaje_herramientas(mensaje: str, contexto: str = "", modo_ingenieria: bool = False) -> str:
    return "HERRAMIENTAS:\n" + REGISTRO.catalogo(_herramientas_para(mensaje, contexto, modo_ingenieria))


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


def consultar_memoria(consulta: str = "") -> str:
    return _formatear_recuerdos(consulta, incluir_sensibles=True)


def consultar_biblioteca(periodo: str = "hoy", consulta: str = "") -> str:
    return _formatear_biblioteca(periodo, consulta)


REGISTRO.registrar_funcion(consultar_memoria, "memoria", "Consulta recuerdos personales guardados localmente",
                           palabras=("recuerdas", "memoria", "recuerdos", "sabes de mi"))
REGISTRO.registrar_funcion(consultar_biblioteca, "memoria", "Conversaciones guardadas; periodo = hoy, semana o todo",
                           palabras=("historial", "conversaciones", "hablamos"))

# Compatibilidad: el permiso real lo decide la marca «sensible» de cada herramienta.
ACCIONES_SENSIBLES = {n for n, h in REGISTRO.herramientas.items() if h.sensible}


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
        return ("Tengo conectores para apps, archivos, documentos, música, red y Wi-Fi, instalar programas, notas, "
                "pendientes, agenda, contactos y WhatsApp, clima, divisas, noticias, Git y servidores MCP. "
                "Prueba: “abre Discord”, “qué clima hace mañana”, “anota comprar tortillas” o “qué conectores tienes”.")
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


_MONEDAS_HABLADAS = r"(dolar(?:es)?|euros?|pesos?(?: mexicanos?| colombianos?| argentinos?| chilenos?)?|yen(?:es)?|libras?|reales?|yuan(?:es)?|quetzal(?:es)?)"
_CANTIDAD = r"(\d+(?:[.,]\d+)?)"


def _original(mensaje: str, texto: str, coincidencia, grupo: int = 1) -> str:
    """Recupera un fragmento con mayúsculas y acentos originales: al quitar acentos el largo no cambia."""
    valor = coincidencia.group(grupo) or ""
    if len(mensaje) == len(texto):
        return mensaje[coincidencia.start(grupo):coincidencia.end(grupo)].strip(" .,")
    return valor.strip(" .,")


def _accion_conectores(mensaje: str, texto: str):
    """Órdenes de los conectores nuevos que no necesitan pasar por el LLM."""
    # Whisper suele añadir «¿…?» o un punto final; se quitan de ambos para que los índices coincidan.
    mensaje = mensaje.strip(" .,;!?¿¡\n\t")
    texto = _normalizar(mensaje)

    # Núcleo
    if re.search(r"\b(?:conectores|integraciones)\b", texto) and re.search(r"\b(?:que|cuales|muestra|lista|ver|tus|mis)\b", texto):
        return "listar_conectores", {}
    if re.search(r"\b(?:recarga|reconecta|actualiza)\b.*\b(?:conectores|mcp)\b", texto):
        return "recargar_conectores_mcp", {}

    # Clima
    if re.search(r"\b(?:clima|pronostico|va a llover|llovera|hara (?:frio|calor)|que temperatura hace)\b|\bque tiempo hace\b", texto) \
            and not re.search(r"\b(?:cpu|procesador|gpu|pc|compu)\b", texto):
        dia = "pasado manana" if "pasado manana" in texto else "manana" if re.search(r"\bmanana\b", texto) else "hoy"
        lugar = re.search(r"\b(?:en|de|para)\s+(?!hoy\b|manana\b|pasado\b|la semana\b)([a-z][a-z .]{2,40}?)(?:\s+(?:hoy|manana|pasado manana|ahorita|ahora))?\s*$", texto)
        return "clima", {"ciudad": _original(mensaje, texto, lugar) if lugar else "", "dia": dia}

    # Divisas (antes que unidades: «100 dólares a pesos»)
    divisa = re.search(rf"{_CANTIDAD}\s*{_MONEDAS_HABLADAS}\s+(?:a|en)\s+{_MONEDAS_HABLADAS}", texto)
    if divisa:
        return "tipo_de_cambio", {"de": divisa.group(2), "a": divisa.group(3), "cantidad": float(divisa.group(1).replace(",", "."))}
    precio = re.search(rf"\b(?:tipo de cambio(?: del?)?|a como (?:esta|anda) el|cuanto (?:esta|vale|cuesta) el|precio del)\s+{_MONEDAS_HABLADAS}", texto)
    if precio:
        return "tipo_de_cambio", {"de": precio.group(1), "a": "MXN", "cantidad": 1}

    # Unidades
    unidades = re.search(rf"^(?:convierte|pasa|cuanto es|cuantos son|cuantas son)\s+{_CANTIDAD}\s*([a-z°/ ]+?)\s+(?:a|en)\s+([a-z°/ ]+)$", texto)
    if unidades:
        return "convertir_unidades", {"cantidad": float(unidades.group(1).replace(",", ".")), "de": unidades.group(2), "a": unidades.group(3)}
    unidades = re.search(rf"^cuant[oa]s\s+([a-z/ ]+?)\s+(?:son|hay en|tiene)\s+{_CANTIDAD}\s*([a-z°/ ]+)$", texto)
    if unidades:
        return "convertir_unidades", {"cantidad": float(unidades.group(2).replace(",", ".")), "de": unidades.group(3), "a": unidades.group(1)}

    # Red
    if re.search(r"\bmi ip\b|\bip publica\b", texto):
        return ("ip_publica", {}) if re.search(r"\bpublica\b|\bexterna\b", texto) else ("estado_red", {})
    if re.search(r"\b(?:tengo|hay|estoy sin|no tengo|no hay)\s+(?:internet|conexion|red)\b|\bestoy conectado\b|\bestado de (?:la )?red\b", texto):
        return "estado_red", {}
    ping = re.search(r"\b(?:haz(?:me|le)?\s+)?(?:un\s+)?ping\s+(?:a\s+)?([a-z0-9.\-]+\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})\b", texto)
    if ping:
        return "hacer_ping", {"host": ping.group(1)}
    if re.search(r"\bredes?\b.*\b(?:wifi|wi fi|inalambricas?)\b|\b(?:wifi|wi fi)\b.*\b(?:disponibles?|cerca|hay)\b", texto) \
            and not re.search(r"\bconecta", texto):
        return "redes_wifi", {}
    wifi = re.fullmatch(r"(?:conectate|conecta(?:me)?(?: la pc)?)\s+(?:a|al)\s+(?:la\s+)?(?:red|wifi|wi fi|internet)?\s*(?:de\s+)?(.+)", texto)
    if wifi and wifi.group(1).strip() not in {"internet", "red", "wifi"}:
        return "conectar_wifi", {"nombre": _original(mensaje, texto, wifi)}
    if re.search(r"\bbluetooth\b", texto) and re.search(r"\b(?:dispositivos|emparejados|conectados|que tengo)\b", texto):
        return "dispositivos_bluetooth", {}

    # Programas (winget)
    instala = re.fullmatch(r"(?:instala(?:me|le)?|descarga(?:me)? e instala)\s+(?:el programa |la app |la aplicacion |el juego )?(.+)", texto)
    if instala:
        return "instalar_programa", {"nombre": _original(mensaje, texto, instala)}
    desinstala = re.fullmatch(r"(?:desinstala(?:me|le)?)\s+(?:el programa |la app |la aplicacion |el juego )?(.+)", texto)
    if desinstala:
        return "desinstalar_programa", {"nombre": _original(mensaje, texto, desinstala)}
    if re.search(r"\bactualiza(?:r)?\s+(?:todos\s+)?(?:mis\s+|los\s+)?(?:programas|apps|aplicaciones)\b|\bactualiza todo\b", texto):
        return "actualizar_programas", {"nombre": ""}
    if re.search(r"\b(?:que|cuales)\s+(?:programas|apps|aplicaciones)\b.*\bactualiza|\bhay actualizaciones\b|\bactualizaciones pendientes\b", texto):
        return "listar_actualizaciones", {}

    # Información del equipo
    if re.search(r"\b(?:especificaciones|specs|que procesador|que tarjeta (?:de video|grafica)|que grafica|cuanta ram tengo|"
                 r"caracteristicas de (?:mi|la) (?:pc|compu|computadora|laptop)|que (?:pc|computadora|laptop) tengo)\b", texto):
        return "info_sistema", {}
    if re.search(r"\buso de (?:la )?(?:cpu|procesador)\b|\bque tan (?:cargada|ocupada)\b|\bcuanto (?:cpu|procesador) (?:uso|usa|ocupa)\b", texto):
        return "uso_recursos", {}
    if re.search(r"\b(?:que\s+)?procesos\s+(?:hay\s+)?(?:abiertos|activos|corriendo)\b|\bque procesos\b", texto):
        return "listar_procesos", {}
    if re.search(r"\bcuanto tiempo lleva (?:encendida|prendida|encendido|prendido)\b", texto):
        return "tiempo_encendido", {}
    if re.search(r"\bcuanto espacio\b|\bespacio (?:libre|en (?:el |mi )?disco)\b|\bdisco (?:lleno|duro)\b", texto) \
            and not re.search(r"\blibera\b", texto):
        return "espacio_en_disco", {}

    # Recordatorio a una hora concreta («recuérdame a las 5 que…»)
    hora = re.search(r"\b(?:recuerdame|avisame|alarma|despiertame|alertame)\b.*?\ba las?\s+"
                     r"(\d{1,2}(?:[: ]\d{2})?(?:\s*(?:am|pm|a m|p m))?(?:\s+y\s+(?:media|cuarto))?(?:\s+de la (?:manana|tarde|noche))?)"
                     r"(?:\s+(?:que|para|de que)\s+(.+))?\s*$", texto)
    if hora:
        aviso = _original(mensaje, texto, hora, 2) if hora.group(2) else ""
        return "recordatorio_a_las", {"hora": hora.group(1), "mensaje": f"Recordatorio: {aviso}" if aviso else ""}

    # Pendientes (antes que notas: «apunta en pendientes…»)
    pendiente = re.fullmatch(r"(?:agrega|anade|pon|apunta|mete)\s+(?:a|en)\s+(?:mis\s+|los\s+|la lista de\s+)?(?:pendientes|tareas por hacer|to do)\s*[:,]?\s*(?:que\s+)?(.+)", texto) \
        or re.fullmatch(r"(?:agrega|anade|apunta)\s+(?:el |un |como )?pendiente\s*[:,]?\s*(?:que\s+)?(.+)", texto)
    if pendiente:
        return "agregar_pendiente", {"tarea": _original(mensaje, texto, pendiente)}
    if re.search(r"\b(?:mis pendientes|que (?:tengo )?pendientes?|que tengo pendiente|lista de pendientes|que me falta hacer)\b", texto):
        return "listar_pendientes", {}
    hecho = re.fullmatch(r"(?:ya\s+(?:termine|hice|acabe)|marca como (?:hecho|hecha|terminado|terminada)|tacha)\s+(?:el pendiente\s+)?(.+)", texto)
    if hecho:
        return "completar_pendiente", {"tarea": _original(mensaje, texto, hecho)}

    # Notas
    nota = re.fullmatch(r"(?:anota(?:me)?|apunta(?:me)?|toma nota(?: de)?|crea una nota|guarda una nota|nota)\s*[:,]?\s*(?:que\s+)?(.+)", texto)
    if nota and len(nota.group(1)) >= 2:
        return "crear_nota", {"texto": _original(mensaje, texto, nota)}
    if re.search(r"\b(?:mis notas|lee mis notas|muestra(?:me)? (?:mis )?notas|que notas tengo|que anote)\b", texto):
        return "listar_notas", {}

    # Agenda
    if re.search(r"\b(?:proximos eventos|mi semana|que tengo esta semana|agenda de la semana)\b", texto):
        return "proximos_eventos", {}
    agenda = re.fullmatch(r"(?:que tengo|mi agenda|que hay en mi agenda|eventos)\s+(?:para\s+|de\s+)?(hoy|manana|pasado manana|el \w+|este \w+)(?:\s+en (?:la|mi) agenda)?", texto)
    if agenda:
        return "eventos_de", {"fecha": agenda.group(1)}

    # Contactos y mensajes
    contacto = re.fullmatch(r"guarda(?:r)?\s+(?:a\s+|el contacto(?: de)?\s+)(.+?)\s+con\s+(?:el\s+)?(numero|telefono|cel(?:ular)?|correo|email)\s+(.+)", texto)
    if contacto:
        nombre = _original(mensaje, texto, contacto, 1)
        dato = _original(mensaje, texto, contacto, 3)
        if contacto.group(2) in {"correo", "email"}:
            return "guardar_contacto", {"nombre": nombre, "correo": dato}
        return "guardar_contacto", {"nombre": nombre, "telefono": dato}
    whats = re.fullmatch(r"(?:manda|envia|escribe)(?:le)?\s+(?:un\s+)?(?:whats(?:app)?|wasap|wats|mensaje(?: de whatsapp| por whatsapp)?)\s+a\s+(.+?)\s+(?:que diga|diciendo|que|:)\s*(.+)", texto)
    if whats:
        return "enviar_whatsapp", {"contacto": _original(mensaje, texto, whats, 1), "mensaje": _original(mensaje, texto, whats, 2)}
    chat = re.fullmatch(r"abre (?:el\s+)?(?:whatsapp|chat|whats)\s+de\s+(.+)", texto)
    if chat:
        return "enviar_whatsapp", {"contacto": _original(mensaje, texto, chat), "mensaje": ""}
    correo = re.fullmatch(r"(?:manda|envia|escribe|redacta)(?:le)?\s+un\s+(?:correo|mail|email)\s+a\s+(.+?)\s+(?:que diga|diciendo|que|sobre|:)\s*(.+)", texto)
    if correo:
        cuerpo = _original(mensaje, texto, correo, 2)
        return "redactar_correo", {"destinatario": _original(mensaje, texto, correo, 1),
                                   "asunto": " ".join(cuerpo.split()[:7]), "cuerpo": cuerpo}

    # Archivos y carpetas por nombre aproximado
    if not re.search(r"\s(?:de|del)\s+(?:hoy|ayer|\d{1,2}[/-])", texto):
        archivo = re.fullmatch(r"(?:busca(?:me)?|encuentra(?:me)?|donde (?:esta|quedo|estan))\s+(?:el |la |mi |mis |los |las )?(archivo|carpeta|documento)s?\s+(?:de |llamad[oa] |que se llama )?(.+)", texto)
        if archivo:
            return "buscar_archivo_parecido", {"nombre": _original(mensaje, texto, archivo, 2), "solo_carpetas": archivo.group(1) == "carpeta"}
    carpeta = re.fullmatch(r"(?:abre|abreme|ve a|entra a)\s+(?:la |mi )?carpeta\s+(?:de |del |llamada )?(.+)", texto)
    if carpeta:
        return "abrir_carpeta", {"nombre": _original(mensaje, texto, carpeta)}
    if re.search(r"\b(?:archivos recientes|ultimos archivos|que archivos (?:use|abri|modifique|edite))\b", texto):
        return "archivos_recientes", {}

    # Web: noticias, Wikipedia y búsquedas en sitios
    noticias = re.fullmatch(r"(?:dame |dime |cuales son |muestrame |que )?(?:las )?(?:ultimas )?(?:noticias|titulares)(?: de hoy)?(?:\s+(?:de|sobre|del)\s+(.+))?", texto)
    if noticias:
        return "noticias", {"tema": _original(mensaje, texto, noticias) if noticias.group(1) else ""}
    wiki = re.fullmatch(r"(?:busca en wikipedia|wikipedia|definicion de|que significa)\s+(.+)", texto)
    if wiki:
        return "definir", {"tema": _original(mensaje, texto, wiki)}
    en_sitio = re.fullmatch(r"busca(?:me)?\s+(.+?)\s+en\s+(amazon|mercado libre|maps|google maps|github|wikipedia|tiktok|pinterest|"
                            r"spotify|imagenes|reddit|twitch|netflix|stack overflow)", texto)
    if en_sitio:
        return "buscar_en_sitio", {"consulta": _original(mensaje, texto, en_sitio, 1), "sitio": en_sitio.group(2)}
    mapa = re.fullmatch(r"(?:donde queda|como llego a|ubicacion de)\s+(.+)", texto)
    if mapa:
        return "buscar_en_sitio", {"consulta": _original(mensaje, texto, mapa), "sitio": "Google Maps"}

    # Atajos de teclado explícitos
    atajo = re.fullmatch(r"(?:presiona|pulsa|aprieta)\s+(?:las teclas\s+|la tecla\s+|el atajo\s+)?(.+)", texto)
    if atajo:
        return "presionar_atajo", {"atajo": atajo.group(1)}
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
    accion_conector = _accion_conectores(mensaje, texto)
    if accion_conector:
        return accion_conector
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
        # Un contacto, nota o mensaje dictado habla de otros: no es un recuerdo sobre el usuario.
        aviso_recuerdo = None if accion[0] in _SIN_RECUERDO else _guardar_recuerdo_si_corresponde(mensaje_usuario)
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
    herramientas_ingenieria = set(HERRAMIENTAS_INGENIERIA)

    contexto_previo = historial_sesion.obtener_mensajes_contexto(incluir_sensibles=False)
    ultimo_usuario = next((m["content"] for m in reversed(contexto_previo) if m["role"] == "user"), "")

    mensajes = [{"role": "system", "content": _construir_system_prompt()},
                {"role": "system", "content": _mensaje_herramientas(mensaje_usuario, ultimo_usuario, modo_ingenieria)}]
    # Al proveedor de nube solo se le dan recuerdos no sensibles y relevantes.
    recuerdos = _formatear_recuerdos(mensaje_usuario, incluir_sensibles=False)
    if not recuerdos.startswith("No hay"):
        mensajes.append({"role": "system", "content": f"MEMORIA PERSONAL RELEVANTE:\n{recuerdos}"})
    # El historial sensible no sale de Bark: ni siquiera cuando la consulta actual es inocua.
    mensajes.extend(contexto_previo)
    mensajes.append({"role": "user", "content": mensaje_usuario})

    observaciones = 0
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

            resultado = REGISTRO.ejecutar(nombre_herramienta, parametros, _solicitador_permiso)
            # Vuelve al modelo cuando la herramienta no existía (con sugerencias), faltaban
            # datos, era buscar_herramientas, o es un paso de una tarea de código.
            encadenar = (resultado.observar and observaciones < MAX_OBSERVACIONES) or (
                modo_ingenieria and resultado.herramienta in herramientas_ingenieria)
            if encadenar:
                observaciones += 1
                mensajes.append({"role": "assistant", "content": respuesta_llm})
                mensajes.append({
                    "role": "user",
                    "content": (
                        "OBSERVACIÓN REAL DE LA HERRAMIENTA:\n"
                        f"{resultado.texto[:config.MAX_CARACTERES_OBSERVACION]}\n\n"
                        "Si ahora tienes la herramienta correcta, úsala con su nombre exacto. "
                        "Continúa solo si hace falta; de lo contrario responde con respuesta_final y resume el resultado."
                    ),
                })
                continue
            # Un modelo pequeño no debe reinterpretar ni adornar el resultado
            # de una acción aislada. Mostrar el dato observado evita alucinaciones.
            respuesta_final_texto = resultado.texto[:config.MAX_CARACTERES_OBSERVACION]
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
    return REGISTRO.resolver(nombre)[0]


def _ajustar_parametros(funcion, parametros) -> dict:
    """Si el modelo llama al parámetro con otro nombre («app» en vez de «nombre_app»), lo reasigna."""
    herramienta = next((h for h in REGISTRO.herramientas.values() if h.funcion is funcion), None)
    if herramienta is None:
        from conectores.registro import Herramienta
        herramienta = Herramienta(getattr(funcion, "__name__", "f"), funcion, "", "nucleo")
    return REGISTRO.ajustar_parametros(herramienta, parametros)


def _ejecutar_herramienta(nombre: str, parametros: dict) -> str:
    """Ejecuta la herramienta solicitada, pidiendo permiso si es sensible."""
    return REGISTRO.ejecutar(nombre, parametros, _solicitador_permiso).texto


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
    conectores_mcp.conectar_en_segundo_plano()
    if _modelo_local_necesario():
        threading.Thread(target=_precalentar_modelo, name="bark-precalentar-llm", daemon=True).start()
