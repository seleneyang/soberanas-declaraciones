#!/usr/bin/env python3
"""Recolector diario de declaraciones.

Corre una vez al día, arma el padrón desde Wikidata, busca frases
entrecomilladas en la cobertura de las últimas 24 horas, y le pide al modelo
que resuelva tres cosas por cada frase: si las palabras son de la persona o de
alguien más citado en la misma oración, si contienen una afirmación verificable,
y en qué lugar se dijo.

Todo sale a data/candidatos-FECHA.json con estado "pendiente". Nada entra a
data/expedientes.json sin que alguien lo lea y lo confirme, porque el trabajo
editorial es el que decide qué recorte de prensa documenta el caso y esa
decisión no se automatiza.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

UA = "expedientes-cr/0.1 (herramienta de investigación)"
WDQS = "https://query.wikidata.org/sparql"
GDELT = "https://api.gdeltproject.org/api/v2/context/context"
API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MODELO", "claude-sonnet-5")
SALIDA = Path("data")

PROVINCIAS = [
    "San José", "Alajuela", "Cartago", "Heredia",
    "Guanacaste", "Puntarenas", "Limón", "Exterior",
]

SPARQL = """
SELECT DISTINCT ?p ?pLabel ?posLabel ?distLabel WHERE {
  ?p wdt:P31 wd:Q5 ; wdt:P102 wd:Q135553930 .
  OPTIONAL {
    ?p p:P39 ?st . ?st ps:P39 ?pos .
    FILTER NOT EXISTS { ?st pq:P582 ?fin }
    OPTIONAL { ?st pq:P768 ?dist }
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "es,en". }
} LIMIT 300
"""

PROMPT = """Estás preparando material para un archivo de investigación sobre discurso \
político costarricense. Te doy una oración tomada de cobertura periodística que menciona \
a una persona con cargo público.

Resolvé tres cosas:
1. Atribución. ¿Las palabras entrecomilladas son de esa persona, o la oración cita a otra \
persona (un crítico, un analista, un documento) y solo la menciona a ella? Las oraciones \
periodísticas hacen lo segundo muy seguido.
2. Afirmación verificable. Si las palabras son suyas, ¿contienen una afirmación empírica \
sobre el mundo (cifras, hechos, leyes, historia, posiciones atribuibles)? Las opiniones, \
promesas, predicciones y preguntas retóricas no lo son.
3. Lugar. ¿La oración o su contexto dice dónde se hizo la declaración? Puede ser un lugar \
en Costa Rica o en el exterior. Si no lo dice, dejalo vacío en vez de suponerlo.

Provincias válidas: {provincias}. Usá "Exterior" para cualquier lugar fuera de Costa Rica.

Respondé solo JSON, sin preámbulo ni backticks:
{{"atribucion": "persona" | "otro" | "incierto",
  "afirmacion_verificable": true | false,
  "afirmacion": "reformulación neutral, o cadena vacía",
  "lugar_nombre": "nombre del lugar, o cadena vacía",
  "lugar_provincia": "una de las provincias válidas, o cadena vacía",
  "lugar_pais": "país, o cadena vacía",
  "articulo_sugerido": "título de artículo de Wikipedia en español que cubra el tema"}}

Persona: {nombre}
Cargo: {cargo}
Oración: {oracion}"""


def sparql(query: str) -> list[dict]:
    r = requests.get(
        WDQS, params={"query": query, "format": "json"},
        headers={"User-Agent": UA, "Accept": "application/sparql-results+json"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def padron() -> list[dict]:
    gente: dict[str, dict] = {}
    for fila in sparql(SPARQL):
        qid = fila["p"]["value"].rsplit("/", 1)[-1]
        entrada = gente.setdefault(qid, {"qid": qid, "nombre": fila["pLabel"]["value"],
                                         "cargo": "", "provincia": ""})
        if "posLabel" in fila and not entrada["cargo"]:
            entrada["cargo"] = fila["posLabel"]["value"]
        if "distLabel" in fila and not entrada["provincia"]:
            entrada["provincia"] = fila["distLabel"]["value"]
    return [g for g in gente.values() if not re.fullmatch(r"Q\d+", g["nombre"])]


def cita(oracion: str) -> str | None:
    """Saca el fragmento entrecomillado más largo de la oración."""
    trozos = re.findall(r'[«"\u201c]([^»"\u201d]{25,600})[»"\u201d]', oracion)
    return max(trozos, key=len).strip() if trozos else None


def frases(persona: dict, sesion: requests.Session) -> list[dict]:
    try:
        r = sesion.get(GDELT, params={
            "query": f'"{persona["nombre"]}"', "mode": "artlist", "format": "json",
            "timespan": "24H", "maxrecords": "40", "isquote": "1",
        }, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        articulos = r.json().get("articles", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  gdelt falló para {persona['nombre']}: {exc}", file=sys.stderr)
        return []

    apellido = persona["nombre"].split()[-1].lower()
    vistas, salida = set(), []
    for art in articulos:
        oracion = (art.get("context") or art.get("fragment") or "").strip()
        if not oracion or apellido not in oracion.lower():
            continue
        texto = cita(oracion)
        if not texto:
            continue
        clave = re.sub(r"\W+", "", texto.lower())[:150]
        if clave in vistas:
            continue
        vistas.add(clave)
        salida.append({
            "texto": texto, "oracion": oracion,
            "url": art.get("url", ""), "medio": art.get("domain", ""),
            "idioma": art.get("language", ""), "visto": art.get("seendate", ""),
        })
    return salida


def modelo(prompt: str) -> dict | None:
    clave = os.environ.get("ANTHROPIC_API_KEY")
    if not clave:
        raise RuntimeError("falta ANTHROPIC_API_KEY")
    try:
        r = requests.post(API, headers={
            "x-api-key": clave, "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={"model": MODEL, "max_tokens": 1200,
                 "messages": [{"role": "user", "content": prompt}]}, timeout=120)
        r.raise_for_status()
        bloques = r.json().get("content", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  modelo falló: {exc}", file=sys.stderr)
        return None
    texto = "".join(b.get("text", "") for b in bloques if b.get("type") == "text").strip()
    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.MULTILINE).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return None


def main() -> int:
    hoy = date.today().isoformat()
    gente = padron()
    print(f"padrón: {len(gente)} personas en wikidata")
    if not gente:
        print("wikidata no devolvió a nadie; puede que falten declaraciones P102",
              file=sys.stderr)

    sesion = requests.Session()
    candidatos = []

    for persona in gente:
        if len(persona["nombre"].split()) < 2:
            continue
        for f in frases(persona, sesion):
            lectura = modelo(PROMPT.format(
                provincias=", ".join(PROVINCIAS),
                nombre=persona["nombre"], cargo=persona["cargo"] or "sin cargo registrado",
                oracion=f["oracion"],
            ))
            if not lectura or lectura.get("atribucion") != "persona":
                continue
            if not lectura.get("afirmacion_verificable"):
                continue

            prov = lectura.get("lugar_provincia", "")
            candidatos.append({
                "id": f"C{int(time.time()*1000)}",
                "estado": "pendiente",
                "recolectado": datetime.now(timezone.utc).isoformat(),
                "fileNo": "",
                "speaker": {"qid": persona["qid"], "name": persona["nombre"],
                            "position": persona["cargo"],
                            "provinciaElectoral": persona["provincia"],
                            "party": "Pueblo Soberano", "image": "", "wiki": ""},
                "said": {"quote": f["texto"], "key": "", "date": hoy,
                         "outlet": f["medio"], "url": f["url"],
                         "lugar": {"nombre": lectura.get("lugar_nombre", ""),
                                   "provincia": prov if prov in PROVINCIAS else "",
                                   "pais": lectura.get("lugar_pais", ""),
                                   "lat": None, "lng": None}},
                "documented": {"headline": "", "outlet": "", "date": "", "url": "",
                               "excerpt": "", "key": ""},
                "recorded": {"lang": "es", "title": lectura.get("articulo_sugerido", ""),
                             "datum": "", "datumLabel": ""},
                "note": "",
                "afirmacion_detectada": lectura.get("afirmacion", ""),
            })
        time.sleep(1.5)

    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / f"candidatos-{hoy}.json"
    destino.write_text(json.dumps(candidatos, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(candidatos)} candidatos pendientes en {destino}")
    print("Falta a mano: el recorte de prensa, el pin en el mapa y las frases a hilar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
