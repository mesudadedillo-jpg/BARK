"""Información al momento sin claves de API: clima, tipo de cambio, Wikipedia, noticias y conversión de unidades."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from urllib.parse import quote

from acciones.apps import normalizar
from conectores import similitud
from conectores._util import http_json
from conectores.registro import REGISTRO

R = REGISTRO
R.conector("informacion", "Clima, divisas y saber", "Clima (Open-Meteo), tipo de cambio, resúmenes de Wikipedia, noticias y conversión de unidades.")

_CLIMA = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado", 3: "nublado", 45: "niebla", 48: "niebla",
    51: "llovizna ligera", 53: "llovizna", 55: "llovizna intensa", 56: "llovizna helada", 57: "llovizna helada",
    61: "lluvia ligera", 63: "lluvia", 65: "lluvia fuerte", 66: "lluvia helada", 67: "lluvia helada",
    71: "nieve ligera", 73: "nieve", 75: "nevada fuerte", 77: "granizo fino", 80: "chubascos", 81: "chubascos",
    82: "chubascos fuertes", 85: "chubascos de nieve", 86: "chubascos de nieve", 95: "tormenta", 96: "tormenta con granizo",
    99: "tormenta con granizo",
}


def _ciudad_por_defecto() -> str:
    ciudad = os.getenv("BARK_CIUDAD", "").strip()
    if ciudad:
        return ciudad
    try:
        return http_json("https://ipapi.co/json/", timeout=5).get("city", "")
    except Exception:
        return ""


def _ubicar(ciudad: str) -> dict | None:
    datos = http_json("https://geocoding-api.open-meteo.com/v1/search",
                      {"name": ciudad, "count": 10, "language": "es", "format": "json"})
    resultados = datos.get("results") or []
    if not resultados:
        return None
    buscado = normalizar(ciudad.split(",")[0])
    # Nombre exacto primero; entre homónimos, la ciudad más poblada.
    return max(resultados, key=lambda r: (normalizar(r.get("name", "")) == buscado, r.get("population") or 0))


@R.herramienta("informacion", "Clima actual y pronóstico de una ciudad (dia: hoy, mañana o pasado mañana)",
               alias=("tiempo", "pronostico", "temperatura"),
               palabras=("clima", "tiempo", "temperatura", "llover", "lluvia", "calor", "frio", "pronostico", "grados"))
def clima(ciudad: str = "", dia: str = "hoy") -> str:
    ciudad = ciudad.strip() or _ciudad_por_defecto()
    if not ciudad:
        return "¿De qué ciudad? (Puedes fijar una con BARK_CIUDAD en .env)."
    lugar = _ubicar(ciudad)
    if not lugar:
        return f"No encontré la ciudad '{ciudad}'."
    d = http_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": lugar["latitude"], "longitude": lugar["longitude"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
    })
    nombre = f"{lugar['name']}, {lugar.get('admin1') or lugar.get('country', '')}"
    indice = 2 if "pasado" in normalizar(dia) else 1 if "manana" in normalizar(dia) else 0
    diario = d["daily"]
    pronostico = (f"{['Hoy', 'Mañana', 'Pasado mañana'][indice]}: {_CLIMA.get(diario['weather_code'][indice], 'variable')}, "
                  f"máx {diario['temperature_2m_max'][indice]:.0f}°C / mín {diario['temperature_2m_min'][indice]:.0f}°C, "
                  f"{diario['precipitation_probability_max'][indice] or 0}% de probabilidad de lluvia.")
    if indice:
        return f"{nombre}. {pronostico}"
    a = d["current"]
    return (f"{nombre}: {a['temperature_2m']:.0f}°C (sensación {a['apparent_temperature']:.0f}°C), "
            f"{_CLIMA.get(a['weather_code'], 'variable')}, humedad {a['relative_humidity_2m']}%, "
            f"viento {a['wind_speed_10m']:.0f} km/h. {pronostico}")


_MONEDAS = {
    "USD": ("dolar", "dolares", "dolar americano", "usd", "verdes"), "MXN": ("peso", "pesos", "peso mexicano", "mxn"),
    "EUR": ("euro", "euros", "eur"), "GBP": ("libra", "libras", "libra esterlina", "gbp"), "JPY": ("yen", "yenes", "jpy"),
    "CAD": ("dolar canadiense", "cad"), "COP": ("peso colombiano", "cop"), "ARS": ("peso argentino", "ars"),
    "BRL": ("real", "reales", "brl"), "CNY": ("yuan", "cny"), "GTQ": ("quetzal", "quetzales", "gtq"),
    "CLP": ("peso chileno", "clp"), "PEN": ("sol", "soles", "pen"), "KRW": ("won", "krw"), "CHF": ("franco suizo", "chf"),
}


def _codigo_moneda(texto: str) -> str | None:
    texto = texto.strip()
    if texto.upper() in _MONEDAS:
        return texto.upper()
    elegido, _ = similitud.elegir(texto, list(_MONEDAS.items()), clave=lambda par: par[1])
    return elegido[0] if elegido else None


@R.herramienta("informacion", "Tipo de cambio entre monedas (dólar, euro, peso…)", alias=("precio_dolar", "cambio_divisas", "divisas"),
               palabras=("dolar", "euro", "cambio", "divisa", "moneda", "pesos", "cuesta", "vale"))
def tipo_de_cambio(de: str = "USD", a: str = "MXN", cantidad: float = 1) -> str:
    origen, destino = _codigo_moneda(de), _codigo_moneda(a)
    if not origen or not destino:
        return f"No reconozco la moneda '{de if not origen else a}'."
    datos = http_json(f"https://open.er-api.com/v6/latest/{origen}")
    tasa = (datos.get("rates") or {}).get(destino)
    if not tasa:
        return "No pude obtener el tipo de cambio ahora."
    cantidad = float(cantidad or 1)
    return f"{cantidad:,.2f} {origen} = {cantidad * tasa:,.2f} {destino} (actualizado: {datos.get('time_last_update_utc', '')[:16]})."


@R.herramienta("informacion", "Resumen de Wikipedia sobre un tema; corrige el título si está mal escrito",
               alias=("wikipedia", "que_es", "quien_es", "definicion"),
               palabras=("wikipedia", "que es", "quien es", "quien fue", "definicion", "significa", "historia"))
def definir(tema: str) -> str:
    busqueda = http_json("https://es.wikipedia.org/w/api.php", {
        "action": "opensearch", "search": tema, "limit": 5, "namespace": 0, "format": "json",
    })
    titulos = busqueda[1] if len(busqueda) > 1 else []
    if not titulos:  # búsqueda de texto completo tolera faltas de ortografía
        sugerida = http_json("https://es.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": tema, "srlimit": 3, "format": "json",
        })
        titulos = [r["title"] for r in sugerida.get("query", {}).get("search", [])]
    if not titulos:
        return f"Wikipedia no tiene nada sobre '{tema}'."
    resumen = http_json(f"https://es.wikipedia.org/api/rest_v1/page/summary/{quote(titulos[0].replace(' ', '_'))}")
    extracto = resumen.get("extract") or "Sin resumen disponible."
    return f"{resumen.get('title', titulos[0])}: {extracto}\nFuente: {resumen.get('content_urls', {}).get('desktop', {}).get('page', '')}"


@R.herramienta("informacion", "Titulares recientes de Google Noticias (México); tema opcional",
               alias=("titulares", "que_paso"), palabras=("noticias", "titulares", "paso", "actualidad", "hoy"))
def noticias(tema: str = "", limite: int = 6) -> str:
    import requests
    base = "https://news.google.com/rss"
    parametros = {"hl": "es-419", "gl": "MX", "ceid": "MX:es-419"}
    if tema:
        base += "/search"
        parametros["q"] = tema
    respuesta = requests.get(base, params=parametros, timeout=10, headers={"User-Agent": "Bark-Asistente/1.0"})
    respuesta.raise_for_status()
    elementos = ET.fromstring(respuesta.content).findall(".//item")[: max(1, min(int(limite), 15))]
    if not elementos:
        return "No encontré noticias" + (f" sobre {tema}." if tema else ".")
    return "\n".join(f"• {i.findtext('title')} ({(i.findtext('pubDate') or '')[5:16]})" for i in elementos)


# ============================================================
# Unidades
# ============================================================
_UNIDADES: dict[str, dict[str, tuple[float, tuple[str, ...]]]] = {
    "longitud": {"m": (1, ("metro", "metros", "m")), "km": (1000, ("kilometro", "kilometros", "km")),
                 "cm": (0.01, ("centimetro", "centimetros", "cm")), "mm": (0.001, ("milimetro", "milimetros", "mm")),
                 "mi": (1609.344, ("milla", "millas", "mi")), "yd": (0.9144, ("yarda", "yardas")),
                 "ft": (0.3048, ("pie", "pies", "ft")), "in": (0.0254, ("pulgada", "pulgadas", "in"))},
    "masa": {"kg": (1, ("kilo", "kilos", "kilogramo", "kilogramos", "kg")), "g": (0.001, ("gramo", "gramos", "g")),
             "mg": (1e-6, ("miligramo", "miligramos", "mg")), "lb": (0.45359237, ("libra", "libras", "lb")),
             "oz": (0.028349523, ("onza", "onzas", "oz")), "t": (1000, ("tonelada", "toneladas"))},
    "volumen": {"l": (1, ("litro", "litros", "l")), "ml": (0.001, ("mililitro", "mililitros", "ml")),
                "gal": (3.785411784, ("galon", "galones", "gal")), "taza": (0.24, ("taza", "tazas")),
                "cda": (0.015, ("cucharada", "cucharadas")), "cdta": (0.005, ("cucharadita", "cucharaditas"))},
    "tiempo": {"s": (1, ("segundo", "segundos", "s")), "min": (60, ("minuto", "minutos", "min")),
               "h": (3600, ("hora", "horas", "h")), "d": (86400, ("dia", "dias")), "sem": (604800, ("semana", "semanas"))},
    "velocidad": {"km/h": (1 / 3.6, ("kilometros por hora", "km h", "kmh")), "m/s": (1, ("metros por segundo", "m s")),
                  "mph": (0.44704, ("millas por hora", "mph")), "nudo": (0.514444, ("nudo", "nudos"))},
    "datos": {"B": (1, ("byte", "bytes")), "KB": (1024, ("kilobyte", "kilobytes", "kb")), "MB": (1024**2, ("megabyte", "megabytes", "mb", "megas")),
              "GB": (1024**3, ("gigabyte", "gigabytes", "gb", "gigas")), "TB": (1024**4, ("terabyte", "terabytes", "tb"))},
}
_TEMPERATURA = {"C": ("celsius", "centigrados", "c", "grados"), "F": ("fahrenheit", "f"), "K": ("kelvin", "k")}


def _unidad(texto: str):
    candidatos = [(dim, codigo, datos) for dim, tabla in _UNIDADES.items() for codigo, datos in tabla.items()]
    candidatos += [("temperatura", codigo, (0, alias)) for codigo, alias in _TEMPERATURA.items()]
    elegido, _ = similitud.elegir(texto, candidatos, clave=lambda c: (c[1], *c[2][1]))
    return elegido


@R.herramienta("informacion", "Convierte cantidades entre unidades (km, millas, libras, °F, GB, tazas…)",
               alias=("convertir", "conversion"), palabras=("convierte", "cuantos", "cuantas", "equivale", "unidades"))
def convertir_unidades(cantidad: float, de: str, a: str) -> str:
    origen, destino = _unidad(de), _unidad(a)
    if not origen or not destino:
        return f"No reconozco la unidad '{de if not origen else a}'."
    if origen[0] != destino[0]:
        return f"No se puede convertir {origen[0]} a {destino[0]}."
    cantidad = float(cantidad)
    if origen[0] == "temperatura":
        celsius = {"C": cantidad, "F": (cantidad - 32) * 5 / 9, "K": cantidad - 273.15}[origen[1]]
        valor = {"C": celsius, "F": celsius * 9 / 5 + 32, "K": celsius + 273.15}[destino[1]]
    else:
        valor = cantidad * origen[2][0] / destino[2][0]
    texto = f"{valor:,.2f}" if abs(valor) >= 1000 else f"{valor:.4g}"
    return f"{cantidad:g} {origen[1]} = {texto} {destino[1]}"
