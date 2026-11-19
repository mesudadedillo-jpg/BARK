"""Comunicación: agenda de contactos local, WhatsApp y correo.

Bark nunca envía mensajes por su cuenta: prepara el chat o el correo con el
texto escrito y tú pulsas Enviar. Así un error de dictado no llega a nadie.
"""

from __future__ import annotations

import re
import uuid
import webbrowser
from urllib.parse import quote

from conectores import similitud
from conectores._util import Almacen
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("comunicacion", "Contactos, WhatsApp y correo", "Agenda local de contactos; prepara mensajes de WhatsApp y correos para que tú los envíes.")

CONTACTOS = Almacen("contactos")
LADA_PAIS = "52"  # México; los números de 10 dígitos se completan con esta lada


def _telefono(texto: str) -> str:
    digitos = re.sub(r"\D", "", texto or "")
    return LADA_PAIS + digitos if len(digitos) == 10 else digitos


def _buscar(nombre: str) -> tuple[dict | None, list[dict]]:
    return similitud.elegir(nombre, CONTACTOS.leer(), clave=lambda c: (c["nombre"], *c.get("alias", [])))


@R.herramienta("comunicacion", "Guarda o actualiza un contacto con teléfono y/o correo", alias=("agregar_contacto", "nuevo_contacto"),
               palabras=("contacto", "guarda", "numero", "telefono", "correo"))
def guardar_contacto(nombre: str, telefono: str = "", correo: str = "", alias: str = "") -> str:
    nombre = nombre.strip()
    if not nombre or not (telefono or correo):
        return "Necesito el nombre y al menos un teléfono o correo."
    contactos = CONTACTOS.leer()
    existente = next((c for c in contactos if similitud.normalizar(c["nombre"]) == similitud.normalizar(nombre)), None)
    if existente is None:
        existente = {"id": uuid.uuid4().hex[:8], "nombre": nombre, "alias": []}
        contactos.append(existente)
    if telefono:
        existente["telefono"] = _telefono(telefono)
    if correo:
        existente["correo"] = correo.strip()
    if alias:
        existente["alias"] = sorted(set(existente.get("alias", [])) | {a.strip() for a in alias.split(",") if a.strip()})
    CONTACTOS.guardar(contactos)
    return f"Guardé a {nombre}."


@R.herramienta("comunicacion", "Busca un contacto por nombre o apodo aproximado", alias=("ver_contacto", "contactos"),
               palabras=("contacto", "contactos", "numero", "telefono", "correo"))
def buscar_contacto(nombre: str = "") -> str:
    contactos = CONTACTOS.leer()
    if not contactos:
        return "Tu agenda está vacía. Di «guarda a Mamá con el número …»."
    if nombre:
        contactos = [c for _, c in similitud.mejores(nombre, contactos, clave=lambda c: (c["nombre"], *c.get("alias", [])), umbral=0.5)]
        if not contactos:
            return f"No tengo a nadie parecido a '{nombre}'."
    return "\n".join(
        f"{c['nombre']}: {c.get('telefono', '—')}  {c.get('correo', '')}".rstrip() for c in contactos[:20]
    )


@R.herramienta("comunicacion", "Abre WhatsApp con el chat del contacto y el mensaje escrito (tú lo envías)",
               alias=("mandar_whatsapp", "whatsapp", "enviar_mensaje", "mensaje_whatsapp"),
               palabras=("whatsapp", "mensaje", "manda", "envia", "escribele", "chat"))
def enviar_whatsapp(contacto: str, mensaje: str = "") -> str:
    numero = _telefono(contacto) if len(re.sub(r"\D", "", contacto)) >= 10 else ""
    nombre = contacto
    if not numero:
        elegido, sugerencias = _buscar(contacto)
        if not elegido:
            return similitud.texto_sugerencias(contacto, [c["nombre"] for c in sugerencias], "un contacto") + \
                " Puedes guardarlo con «guarda a … con el número …»."
        if not elegido.get("telefono"):
            return f"{elegido['nombre']} no tiene teléfono guardado."
        numero, nombre = elegido["telefono"], elegido["nombre"]
    webbrowser.open(f"https://wa.me/{numero}?text={quote(mensaje)}")
    return f"Abrí WhatsApp con {nombre}" + (" y el mensaje escrito; revísalo y pulsa Enviar." if mensaje else ".")


@R.herramienta("comunicacion", "Abre tu programa de correo con un correo listo para revisar y enviar",
               alias=("enviar_correo", "mandar_correo", "email", "correo"),
               palabras=("correo", "email", "mail", "manda", "envia", "redacta"))
def redactar_correo(destinatario: str, asunto: str = "", cuerpo: str = "") -> str:
    correo = destinatario.strip()
    if "@" not in correo:
        elegido, sugerencias = _buscar(destinatario)
        if not elegido or not elegido.get("correo"):
            if elegido:
                return f"{elegido['nombre']} no tiene correo guardado."
            return similitud.texto_sugerencias(destinatario, [c["nombre"] for c in sugerencias], "un contacto")
        correo = elegido["correo"]
    webbrowser.open(f"mailto:{correo}?subject={quote(asunto)}&body={quote(cuerpo)}")
    return f"Preparé un correo para {correo}; revísalo y envíalo cuando quieras."
