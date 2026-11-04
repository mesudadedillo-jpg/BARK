"""Punto de entrada de Bark en modo consola.

Ejecuta: python main.py
Escribe /salir para terminar, /limpiar para vaciar el contexto de esta sesion
o /olvidar para borrar tambien el historial local guardado.
"""

import sys

from acciones import comandos
from nucleo.cerebro import configurar_solicitador_permiso, precalentar, procesar_mensaje
from nucleo.escuchar import VozNoDisponible, ejecutar_modo_voz, solicitar_permiso_por_voz
from nucleo.servicio import ejecutar_servicio, iniciar_en_segundo_plano
from nucleo.historial import historial_sesion
from nucleo.bd_manager import buscar_recuerdos, cargar_turnos, limpiar_memoria


def main() -> None:
    # Recupera contexto local de sesiones anteriores sin necesidad de nube.
    for turno in cargar_turnos(limite=10):
        historial_sesion.agregar_turno(turno["usuario"], turno["chet"])

    if "--diagnostico-voz" not in sys.argv:
        precalentar()
        import threading
        from nucleo import voz_local
        threading.Thread(target=voz_local.precargar, name="bark-precarga-voz", daemon=True).start()

    if "--asistente" in sys.argv:
        ejecutar_servicio()
        return

    if "--segundo-plano" in sys.argv:
        print(iniciar_en_segundo_plano())
        return

    if "--ui" in sys.argv:
        from interfaz.ui import iniciar_interfaz
        iniciar_interfaz()
        return

    if "--voz" in sys.argv:
        try:
            configurar_solicitador_permiso(solicitar_permiso_por_voz)
            ejecutar_modo_voz(procesar_mensaje)
        except VozNoDisponible as error:
            print(f"Bark: {error}")
        return

    if "--diagnostico-voz" in sys.argv:
        from nucleo.escuchar import diagnosticar_microfono
        print(diagnosticar_microfono())
        return

    print("Bark listo. Escribe /salir para terminar o /voz para usar micrófono.")
    while True:
        try:
            entrada = input("\nTú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta luego.")
            return

        if not entrada:
            continue
        if entrada.lower() == "/salir":
            return
        if entrada.lower() == "/voz":
            try:
                configurar_solicitador_permiso(solicitar_permiso_por_voz)
                ejecutar_modo_voz(procesar_mensaje)
            except VozNoDisponible as error:
                print(f"Bark: {error}")
            finally:
                configurar_solicitador_permiso(comandos.pedir_permiso_usuario)
            continue
        if entrada.lower() == "/limpiar":
            historial_sesion.limpiar()
            print("Bark: Contexto de esta sesión limpiado.")
            continue
        if entrada.lower() == "/olvidar":
            historial_sesion.limpiar()
            limpiar_memoria()
            print("Bark: Historial local borrado.")
            continue
        if entrada.lower().startswith("/memoria"):
            consulta = entrada.removeprefix("/memoria").strip()
            recuerdos = buscar_recuerdos(consulta, limite=100)
            if not recuerdos:
                print("Bark: No hay recuerdos guardados todavía.")
            else:
                print("Bark:\n" + "\n".join(f"- [{r['categoria']}] {r['contenido']}" for r in recuerdos))
            continue

        print(f"Bark: {procesar_mensaje(entrada)}")


if __name__ == "__main__":
    main()
