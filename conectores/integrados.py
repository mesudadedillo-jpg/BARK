"""
integrados.py
Registra las herramientas que Bark ya tenía (acciones/comandos.py y
acciones/sistema.py) dentro de sus conectores, con descripción, sinónimos y
palabras clave para que el buscador de herramientas las encuentre.
"""

from acciones import comandos, sistema
from conectores.registro import REGISTRO

R = REGISTRO

R.conector("aplicaciones", "Aplicaciones", "Abrir, buscar y cerrar programas instalados, también apps de la Tienda.")
R.conector("archivos", "Archivos y carpetas", "Explorar, buscar, abrir, crear, mover y comprimir archivos locales.")
R.conector("documentos", "Documentos", "Leer PDF/Word/TXT, crear Word con formato APA y convertir DOCX↔PDF.")
R.conector("web", "Web y navegador", "Buscar en internet, leer páginas y abrir sitios.")
R.conector("multimedia", "Música y multimedia", "Spotify, canciones, videos y capturas de pantalla.")
R.conector("equipo", "Control del equipo", "Volumen, brillo, energía, ventanas, portapapeles, temporizadores y ajustes.")
R.conector("optimizacion", "Optimización", "Diagnóstico de RAM y disco, programas de inicio, temporales y planes de energía.")
R.conector("codigo", "Ingeniería de software", "Explorar y editar código con reemplazos exactos y verificar Python.")
R.conector("memoria", "Memoria personal", "Recuerdos y conversaciones guardadas localmente en SQLite.")

h = R.registrar_funcion

# --- Aplicaciones ---
h(comandos.abrir_aplicacion, "aplicaciones", "Abre un programa por su nombre hablado (entiende sinónimos y apps de la Tienda)",
  alias=("abrir_app", "abrir_programa", "open_app", "abrir_aplicaciones", "iniciar_aplicacion", "lanzar_app"),
  palabras=("abre", "inicia", "lanza", "programa", "app", "ejecuta"))
h(comandos.buscar_y_abrir_aplicacion, "aplicaciones", "Busca accesos directos en Escritorio e Inicio y abre la coincidencia inequívoca",
  palabras=("juego", "acceso directo"))
h(comandos.listar_aplicaciones_instaladas, "aplicaciones", "Lista programas instalados; consulta filtra por nombre",
  alias=("buscar_aplicacion", "buscar_app", "aplicaciones_instaladas"), palabras=("instalados", "programas", "tengo"))
h(comandos.listar_juegos_instalados, "aplicaciones", "Lista juegos y launchers instalados", palabras=("juegos", "steam", "epic"))
h(comandos.cerrar_aplicacion, "aplicaciones", "Cierra una aplicación o proceso activo",
  alias=("cerrar_app", "cerrar_programa", "matar_proceso"), palabras=("cierra", "termina", "mata"), sensible=True)

# --- Archivos ---
h(comandos.abrir_ubicacion, "archivos", "Abre una carpeta o archivo existente sin modificarlo",
  alias=("abrir_archivo", "abrir_ruta"), palabras=("abre", "carpeta", "archivo", "ruta"), rutas=("ruta",))
h(comandos.listar_archivos, "archivos", "Lista el contenido de una carpeta", alias=("ver_carpeta", "contenido_carpeta"),
  palabras=("contenido", "carpeta", "lista"), rutas=("ruta",))
h(comandos.crear_carpeta, "archivos", "Crea una carpeta nueva", alias=("nueva_carpeta",), palabras=("crea", "carpeta"))
h(comandos.buscar_archivos_locales, "archivos", "Busca documentos por nombre y/o fecha (hoy, ayer, dd/mm/aaaa) y numera las opciones",
  palabras=("busca", "archivo", "documento", "ayer", "hoy"))
h(comandos.abrir_archivo_encontrado, "archivos", "Abre la opción número N de la última búsqueda de archivos",
  palabras=("opcion", "primera", "numero"))
h(comandos.escribir_archivo, "archivos", "Crea o sobrescribe un archivo de texto", palabras=("guarda", "escribe", "texto"), sensible=True)
h(comandos.eliminar_archivo, "archivos", "Borra un archivo definitivamente", alias=("borrar_archivo",), palabras=("borra", "elimina"), sensible=True)
h(comandos.crear_carpeta_resultados, "archivos", "Crea una carpeta Resultados_X en el escritorio para una tarea")

# --- Documentos ---
h(comandos.leer_documento, "documentos", "Lee el texto de un .txt, .pdf o .docx", alias=("leer_pdf", "leer_word"),
  palabras=("lee", "pdf", "word", "docx", "resume"), rutas=("ruta",))
h(comandos.crear_documento_word, "documentos", "Crea un .docx con título, contenido y referencias", palabras=("word", "docx", "documento"))
h(comandos.investigar_y_crear_word, "documentos", "Investiga un tema en la web y crea un Word APA con fuentes",
  palabras=("investiga", "investigacion", "apa", "tarea", "word"))
h(comandos.convertir_documento, "documentos", "Convierte DOCX a PDF o PDF a DOCX", alias=("convertir_pdf", "pasar_a_pdf"),
  palabras=("convierte", "pdf", "docx", "word"), sensible=True, rutas=("ruta",))

# --- Web ---
h(comandos.buscar_informacion, "web", "Busca en internet y devuelve resultados breves", alias=("buscar_web", "buscar_en_internet", "google"),
  palabras=("busca", "internet", "informacion", "investiga"))
h(comandos.extraer_texto_pagina, "web", "Extrae el texto de una URL", alias=("leer_pagina",), palabras=("pagina", "url", "lee"))
h(comandos.abrir_en_navegador, "web", "Abre una URL completa en el navegador", alias=("abrir_url", "abrir_pagina", "abrir_web"),
  palabras=("url", "navegador", "pagina", "sitio"))
h(comandos.cerrar_pestana_navegador, "web", "Cierra la pestaña activa del navegador (Ctrl+W)", palabras=("pestana", "cierra"))

# --- Multimedia ---
h(comandos.controlar_spotify, "multimedia", "Controla la reproducción: accion = play_pausa, siguiente o anterior",
  alias=("pausar_musica", "siguiente_cancion", "control_musica"), palabras=("pausa", "siguiente", "anterior", "musica", "spotify"))
h(comandos.buscar_y_reproducir_spotify, "multimedia", "Busca y reproduce una canción o artista en Spotify",
  alias=("reproducir_musica", "poner_musica", "reproducir_cancion"), palabras=("pon", "reproduce", "cancion", "musica", "spotify", "rola"))
h(comandos.buscar_canciones, "multimedia", "Muestra opciones de canciones para elegir", palabras=("canciones", "busca"))
h(comandos.reproducir_cancion_encontrada, "multimedia", "Reproduce una canción de la última lista por número o artista")
h(comandos.descargar_video, "multimedia", "Descarga un video o solo el audio de YouTube y otros sitios",
  alias=("bajar_video",), palabras=("descarga", "youtube", "video", "mp3", "audio"), sensible=True)
h(comandos.tomar_captura_pantalla, "multimedia", "Toma una captura de pantalla", alias=("captura", "screenshot"),
  palabras=("captura", "pantalla", "screenshot"), sensible=True)

# --- Control del equipo ---
h(sistema.abrir_configuracion, "equipo", "Abre una sección de la Configuración de Windows (wifi, bluetooth, sonido…)",
  alias=("configuracion", "ajustes"), palabras=("configuracion", "ajustes", "wifi", "bluetooth"))
h(sistema.controlar_volumen, "equipo", "Sube, baja o silencia el volumen: accion = subir|bajar|silenciar",
  alias=("subir_volumen", "bajar_volumen", "volumen", "silenciar"), palabras=("volumen", "sonido", "silencia", "mute"))
h(sistema.fijar_volumen, "equipo", "Pone el volumen en un porcentaje exacto", palabras=("volumen", "porcentaje"))
h(sistema.fijar_brillo, "equipo", "Pone el brillo de la pantalla en un porcentaje", alias=("brillo",), palabras=("brillo", "pantalla"))
h(sistema.estado_bateria, "equipo", "Nivel y estado de la batería", alias=("bateria",), palabras=("bateria", "carga"))
h(sistema.bloquear_pc, "equipo", "Bloquea la sesión", palabras=("bloquea", "sesion"))
h(sistema.minimizar_todo, "equipo", "Minimiza todas las ventanas", palabras=("minimiza", "escritorio"))
h(sistema.apagar_pc, "equipo", "Apaga el equipo tras unos segundos", palabras=("apaga",), sensible=True)
h(sistema.reiniciar_pc, "equipo", "Reinicia el equipo tras unos segundos", palabras=("reinicia",), sensible=True)
h(sistema.suspender_pc, "equipo", "Suspende el equipo", palabras=("suspende", "dormir"), sensible=True)
h(sistema.cancelar_apagado, "equipo", "Cancela un apagado o reinicio programado", palabras=("cancela", "apagado"))
h(sistema.listar_ventanas_abiertas, "equipo", "Lista las ventanas abiertas", palabras=("ventanas", "abiertas"))
h(sistema.enfocar_ventana, "equipo", "Trae al frente la ventana más parecida al nombre", alias=("cambiar_ventana",),
  palabras=("cambia", "ventana", "enfoca"))
h(sistema.leer_portapapeles, "equipo", "Lee el texto del portapapeles", palabras=("portapapeles", "copiado"))
h(sistema.copiar_al_portapapeles, "equipo", "Copia un texto al portapapeles", palabras=("copia", "portapapeles"))
h(sistema.crear_temporizador, "equipo", "Avisa dentro de N minutos (0.5 = 30 s) con un mensaje",
  alias=("temporizador", "recordatorio", "alarma", "timer"), palabras=("temporizador", "avisame", "recuerdame", "alarma", "minutos"))
h(sistema.listar_temporizadores, "equipo", "Lista los temporizadores activos", palabras=("temporizadores",))
h(sistema.cancelar_temporizadores, "equipo", "Cancela todos los temporizadores", palabras=("cancela", "temporizadores"))

# --- Optimización ---
h(comandos.diagnosticar_equipo, "optimizacion", "Revisa RAM, disco y procesos sin modificar nada", palabras=("diagnostico", "rendimiento", "lenta"))
h(comandos.diagnosticar_ram_avanzado, "optimizacion", "Procesos que más RAM consumen", palabras=("ram", "memoria", "consume"))
h(comandos.liberar_ram_avanzada, "optimizacion", "Libera RAM sin cerrar programas", palabras=("libera", "ram"), sensible=True)
h(comandos.listar_elementos_inicio, "optimizacion", "Programas y tareas que arrancan con Windows", palabras=("inicio", "arranque"))
h(comandos.desactivar_elemento_inicio, "optimizacion", "Desactiva uno o varios programas de inicio (nombres aproximados, separados por coma); reversible",
  alias=("quitar_del_inicio",), palabras=("inicio", "desactiva", "arranque", "encender"), sensible=True)
h(comandos.reactivar_elemento_inicio, "optimizacion", "Vuelve a activar programas de inicio desactivados",
  alias=("activar_al_inicio",), palabras=("inicio", "reactiva", "activa", "arranque"), sensible=True)
h(comandos.resumen_optimizacion, "optimizacion", "Analiza qué se puede optimizar sin cambiar nada", palabras=("optimizacion", "analiza"))
h(comandos.limpiar_temporales_usuario, "optimizacion", "Limpia archivos temporales del usuario", palabras=("temporales", "limpia", "espacio"), sensible=True)
h(sistema.optimizar_pc, "optimizacion", "Optimización completa: RAM, temporales, DNS y recomendaciones",
  palabras=("optimiza", "acelera", "lenta", "rendimiento"), sensible=True)
h(sistema.plan_de_energia, "optimizacion", "Muestra o cambia el plan de energía (alto rendimiento, equilibrado, ahorro)",
  palabras=("energia", "rendimiento", "ahorro"), sensible=True)

# --- Código ---
h(comandos.explorar_proyecto, "codigo", "Lista la estructura del proyecto antes de editar", palabras=("proyecto", "codigo", "estructura"))
h(comandos.leer_archivo_codigo, "codigo", "Lee un archivo de código o configuración del proyecto", palabras=("codigo", "lee", "archivo"))
h(comandos.reemplazar_texto_archivo, "codigo", "Reemplaza un bloque exacto una sola vez", palabras=("edita", "reemplaza", "codigo"), sensible=True)
h(comandos.verificar_archivo_python, "codigo", "Comprueba la sintaxis de un archivo Python", palabras=("verifica", "sintaxis", "python"))
h(comandos.ejecutar_script_python, "codigo", "Ejecuta un script en la carpeta sandbox", palabras=("ejecuta", "script", "python"), sensible=True)
