"""Sitios web por nombre aproximado y búsquedas dentro de sitios concretos."""

from __future__ import annotations

import webbrowser
from urllib.parse import quote_plus

from conectores import similitud
from conectores.registro import REGISTRO

R = REGISTRO

SITIOS = {
    "Google": "https://www.google.com", "YouTube": "https://www.youtube.com", "Gmail": "https://mail.google.com",
    "Google Drive": "https://drive.google.com", "Google Docs": "https://docs.google.com", "Google Maps": "https://maps.google.com",
    "Google Classroom": "https://classroom.google.com", "Google Calendar": "https://calendar.google.com",
    "Google Fotos": "https://photos.google.com", "Traductor": "https://translate.google.com",
    "Facebook": "https://www.facebook.com", "Instagram": "https://www.instagram.com", "TikTok": "https://www.tiktok.com",
    "X / Twitter": "https://x.com", "WhatsApp Web": "https://web.whatsapp.com", "Telegram Web": "https://web.telegram.org",
    "Netflix": "https://www.netflix.com", "Disney+": "https://www.disneyplus.com", "Prime Video": "https://www.primevideo.com",
    "Spotify Web": "https://open.spotify.com", "Twitch": "https://www.twitch.tv", "Crunchyroll": "https://www.crunchyroll.com",
    "Amazon": "https://www.amazon.com.mx", "Mercado Libre": "https://www.mercadolibre.com.mx", "Temu": "https://www.temu.com",
    "Shein": "https://www.shein.com.mx", "GitHub": "https://github.com", "Stack Overflow": "https://stackoverflow.com",
    "ChatGPT": "https://chatgpt.com", "Claude": "https://claude.ai", "Gemini": "https://gemini.google.com",
    "Canva": "https://www.canva.com", "Figma": "https://www.figma.com", "Notion": "https://www.notion.so",
    "LinkedIn": "https://www.linkedin.com", "Outlook": "https://outlook.live.com", "Wikipedia": "https://es.wikipedia.org",
    "Roblox": "https://www.roblox.com", "Steam": "https://store.steampowered.com", "Discord": "https://discord.com/app",
    "Pinterest": "https://www.pinterest.com", "Reddit": "https://www.reddit.com", "Duolingo": "https://www.duolingo.com",
    "SAT": "https://www.sat.gob.mx", "IMSS": "https://www.imss.gob.mx", "Railway": "https://railway.app",
}

BUSCADORES = {
    "Google": "https://www.google.com/search?q={}", "YouTube": "https://www.youtube.com/results?search_query={}",
    "Google Maps": "https://www.google.com/maps/search/{}", "Imágenes": "https://www.google.com/search?tbm=isch&q={}",
    "Amazon": "https://www.amazon.com.mx/s?k={}", "Mercado Libre": "https://listado.mercadolibre.com.mx/{}",
    "Wikipedia": "https://es.wikipedia.org/w/index.php?search={}", "GitHub": "https://github.com/search?q={}",
    "Spotify": "https://open.spotify.com/search/{}", "TikTok": "https://www.tiktok.com/search?q={}",
    "Stack Overflow": "https://stackoverflow.com/search?q={}", "Pinterest": "https://www.pinterest.com/search/pins/?q={}",
    "Reddit": "https://www.reddit.com/search/?q={}", "Twitch": "https://www.twitch.tv/search?term={}",
    "Netflix": "https://www.netflix.com/search?q={}", "Traductor": "https://translate.google.com/?sl=auto&tl=es&text={}",
}


@R.herramienta("web", "Abre un sitio web por su nombre aproximado (YouTube, Classroom, Mercado Libre…)",
               alias=("abrir_web_por_nombre", "ir_a_sitio"), palabras=("abre", "pagina", "sitio", "web", "entra"))
def abrir_sitio(nombre: str) -> str:
    elegido, sugerencias = similitud.elegir(nombre, list(SITIOS))
    if elegido:
        webbrowser.open(SITIOS[elegido])
        return f"Abrí {elegido}."
    if sugerencias:
        return similitud.texto_sugerencias(nombre, sugerencias, "un sitio")
    webbrowser.open(BUSCADORES["Google"].format(quote_plus(nombre)))
    return f"No conozco el sitio '{nombre}'; lo busqué en Google."


@R.herramienta("web", "Busca algo dentro de un sitio: YouTube, Maps, Amazon, Mercado Libre, GitHub, imágenes…",
               alias=("buscar_en_youtube", "buscar_en_maps", "buscar_en"),
               palabras=("busca", "youtube", "maps", "amazon", "mercado", "imagenes", "github", "donde", "comprar"))
def buscar_en_sitio(consulta: str, sitio: str = "Google") -> str:
    elegido, _ = similitud.elegir(sitio, list(BUSCADORES))
    elegido = elegido or "Google"
    webbrowser.open(BUSCADORES[elegido].format(quote_plus(consulta)))
    return f"Busqué «{consulta}» en {elegido}."
