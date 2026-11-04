# Bark

Asistente personal para Windows con voz, memoria y herramientas locales.

## Inicio

Ejecuta `Iniciar_Bark.bat`. La palabra de activación es **Bark**.

## Modelos

Por defecto Bark intenta Gemini, luego ChatGPT y finalmente Qwen local. Si no hay red, usa directamente el modelo local. Copia `config/.env.example` a `.env` y coloca únicamente claves nuevas y revocadas; `.env` no se versiona.

`BARK_PROVEEDOR=local` fuerza privacidad y operación sin conexión. La interfaz muestra el estado de proveedores configurados.

## Voz

Whisper escucha localmente. En la interfaz, el panel **Configuración de voz** permite seleccionar el motor, modelo y voz de Gemini, el respaldo de OpenAI, la voz local Piper, el modelo Whisper y el micrófono. La casilla **Leer respuestas del chat** activa la respuesta hablada también para mensajes escritos.

Con `BARK_VOZ=auto` la cadena es **Gemini TTS → OpenAI TTS → Piper local**. Si Gemini u OpenAI responde con error, límite o no tiene una clave disponible, Bark continúa con la siguiente alternativa y el modo voz no se detiene. Puedes forzar `gemini`, `openai` o `local` con `BARK_VOZ`. Las selecciones de la interfaz duran la sesión; para fijar el arranque, usa `GEMINI_MODELO_TTS`, `GEMINI_VOZ`, `OPENAI_MODELO_TTS` y `OPENAI_VOZ` en `.env`.

Para más voces locales, coloca modelos `.onnx` de Piper (por ejemplo una voz femenina española) y su `.onnx.json` en `modelos/piper`; aparecerán en el selector al reiniciar.

## Control del equipo (nuevo)

Estas órdenes se resuelven localmente, sin pasar por el modelo (respuesta inmediata):

- **Abrir apps por nombre hablado**: «abre la store de microsoft», «abre la calculadora», «abre el bloc de notas», «abre discord». Bark entiende sinónimos (Tienda/Store), apps de la Tienda de Windows sin acceso directo, errores de dictado («spotifi») y, si hay dos coincidencias parecidas, pregunta cuál. El índice de apps se guarda en `bd_memoria/indice_apps.json` y se refresca solo cada 12 h.
- **Volumen / brillo**: «sube el volumen», «baja mucho el volumen», «silencia», «volumen al 40», «brillo al 70».
- **Temporizadores**: «temporizador de 10 minutos», «recuérdame en 5 minutos que saque la pizza», «cancela los temporizadores».
- **Sistema**: «cuánta batería tengo», «bloquea la pc», «minimiza todo», «cambia a chrome», «qué ventanas tengo abiertas», «apaga/reinicia la pc» (pide permiso; «cancela el apagado» lo detiene), «abre la configuración de wifi».
- **Portapapeles**: «qué hay en el portapapeles», «copia al portapapeles …».

## Velocidad del modelo local

- `BARK_PRECALENTAR=auto|1|0`: precarga el modelo al iniciar (por defecto solo si no hay claves de nube).
- `BARK_GPU_LAYERS=-1`: usa GPU si tu llama-cpp-python la soporta. `BARK_HILOS_CPU`, `BARK_CONTEXTO_MAX` y `BARK_MAX_TOKENS` siguen funcionando.

## Optimizar el PC

- «optimiza mi pc», «acelera la compu», «mi pc va lenta»: libera RAM sin cerrar apps, borra temporales del usuario, vacía la caché DNS y te dice qué consume más y qué programas de inicio conviene revisar (pide permiso una sola vez; no cierra ni desinstala nada).
- «modo alto rendimiento» / «modo ahorro» / «modo equilibrado»: cambia el plan de energía de Windows.
- «analiza el rendimiento»: solo informe, sin cambiar nada.

## Modelo de lenguaje local

`python descargar_modelo.py` baja **Qwen3-4B-Instruct-2507** (Q4_K_M, ~2.5 GB) a `modelos/`. Bark usa automáticamente el modelo más capaz que encuentre y, si no carga (p. ej. llama-cpp-python antiguo: `pip install -U llama-cpp-python`), vuelve solo al Qwen2.5-3B. Para forzar uno: `BARK_MODELO_LLM=archivo.gguf` en `.env`.

## Diagnóstico de voz

Si algo falla, pon `BARK_VOZ_DEBUG=1` en `.env`: Bark guarda en `resultados/ultima_voz.wav` el audio exacto que recibe Whisper; si suena bien, el micrófono está correcto. `BARK_VOZ_UMBRAL_RMS` ya es solo un mínimo: el umbral real se adapta al ruido del cuarto. Para más precisión (con más consumo) usa `BARK_MODELO_WHISPER=small`.

## Voz local más natural

La voz Piper ahora habla **por frases en streaming** (empieza tras la primera frase mientras sintetiza la siguiente), limpia lo que no se debe leer (markdown, emojis, enlaces, rutas, listas largas: lee el encabezado y deja el detalle en pantalla), pronuncia cifras y unidades en español, recorta silencios y mete pausas naturales. Ajustes en `.env`: `BARK_VOZ_RITMO_PIPER` (0.93; menor = más rápido), `BARK_VOZ_VARIACION_PIPER` (0.75) y `BARK_VOZ_RITMO_VARIABLE_PIPER` (0.9); súbelos para más expresividad, bájalos a 0.667/0.8 (valores de fábrica) si oyes artefactos.
