# Modelos locales

Los modelos no se suben al repositorio porque pesan varios GB. Colócalos aquí con esta estructura:

```
modelos/
├── qwen3-4b-instruct-2507-q4_k_m.gguf    # LLM principal (o qwen2.5-3b-instruct-q4_k_m.gguf)
├── piper/
│   ├── es_MX-claude-high.onnx            # voz local en español de México
│   └── es_MX-claude-high.onnx.json
├── vision/
│   ├── gesture_recognizer.task           # gestos de mano (MediaPipe)
│   └── face_landmarker.task              # rasgos faciales (MediaPipe)
└── whisper/                              # se descarga solo la primera vez
```

| Modelo | Cómo obtenerlo |
|---|---|
| **LLM (según tu RAM: Qwen3 1.7B … DeepSeek R1 32B)** | `python descargar_modelo.py` (`--lista` para ver todos) |
| **Voz Piper** | `python descargar_modelo.py --voces`, o a mano desde [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/es/es_MX/claude/high) — descarga el `.onnx` y su `.onnx.json` |
| **Whisper** | Automático: `faster-whisper` lo descarga en `modelos/whisper/` al primer uso |
| **MediaPipe** | [gesture_recognizer.task](https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task) · [face_landmarker.task](https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task) |

Cualquier voz `.onnx` de Piper que pongas en `piper/` aparece en el selector de voz de la interfaz.
