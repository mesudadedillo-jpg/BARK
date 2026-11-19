"""Servidor MCP mínimo por stdio para las pruebas del cliente de Bark."""

import json
import sys

HERRAMIENTAS = [
    {"name": "saludar", "description": "Saluda a alguien por su nombre.",
     "inputSchema": {"type": "object", "properties": {"nombre": {"type": "string"}, "veces": {"type": "integer"}},
                     "required": ["nombre"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "borrar_todo", "description": "Borra datos (debe pedir permiso).",
     "inputSchema": {"type": "object", "properties": {}}},
]


def responder(id_, resultado):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": resultado}) + "\n")
    sys.stdout.flush()


print("aviso que no es JSON y debe ignorarse", flush=True)
for linea in sys.stdin:
    mensaje = json.loads(linea)
    metodo, id_ = mensaje.get("method"), mensaje.get("id")
    if id_ is None:
        continue  # notificación
    if metodo == "initialize":
        responder(id_, {"protocolVersion": mensaje["params"]["protocolVersion"], "capabilities": {"tools": {}},
                        "serverInfo": {"name": "falso", "version": "0"}})
    elif metodo == "tools/list":
        if not mensaje.get("params", {}).get("cursor"):
            responder(id_, {"tools": HERRAMIENTAS[:1], "nextCursor": "p2"})
        else:
            responder(id_, {"tools": HERRAMIENTAS[1:]})
    elif metodo == "tools/call":
        argumentos = mensaje["params"].get("arguments", {})
        if mensaje["params"]["name"] == "saludar":
            texto = " ".join([f"Hola, {argumentos['nombre']}"] * int(argumentos.get("veces", 1)))
            responder(id_, {"content": [{"type": "text", "text": texto}]})
        else:
            responder(id_, {"content": [{"type": "text", "text": "no se pudo"}], "isError": True})
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "no existe"}}) + "\n")
        sys.stdout.flush()
