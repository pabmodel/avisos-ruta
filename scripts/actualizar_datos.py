#!/usr/bin/env python3
"""
Ruta Clara · actualizador de datos

Descarga:
  1. Incidencias de tráfico de la DGT (Punto de Acceso Nacional, DATEX II v3.7).
     Es la misma fuente oficial que alimenta Etraffic.
  2. Avisos meteorológicos de AEMET (paquete CAP con todos los avisos activos).

y los guarda como JSON ligeros que lee la app:
  <salida>/incidencias.json
  <salida>/avisos.json

Uso:  python3 actualizar_datos.py [carpeta_salida]
Variable opcional PREV_BASE: URL de la web publicada (p.ej. https://usuario.github.io/ruta-clara/)
para reutilizar los datos anteriores si una fuente falla en esta pasada.
"""
import datetime as dt
import io
import json
import os
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

DGT_URL = "https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml"
AEMET_RSS = "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAE_RSS.xml"
UA = {"User-Agent": "RutaClara/1.0 (app personal; GitHub Pages)"}
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

NOW = dt.datetime.now(dt.timezone.utc)


def get(url, timeout=90):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ln(tag):
    """Nombre local de una etiqueta XML, sin espacio de nombres."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_time(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t
    except ValueError:
        return None


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def in_spain(lat, lon):
    return 26.0 <= lat <= 45.0 and -19.5 <= lon <= 5.5


def pairs_from_poslist(txt):
    """posList GML: pares de números. Detecta si vienen como lat lon o lon lat."""
    nums = []
    for tok in txt.replace(",", " ").split():
        try:
            nums.append(float(tok))
        except ValueError:
            pass
    out = []
    for i in range(0, len(nums) - 1, 2):
        a, b = nums[i], nums[i + 1]
        if in_spain(a, b):
            out.append([a, b])
        elif in_spain(b, a):
            out.append([b, a])
    return out


# ---------------------------------------------------------------- DGT

TYPE_FIELDS_EXTRA = {"overallSeverity", "severity", "complianceOption", "validityStatus"}


def parse_record(rec, situation_sev):
    leaves = {}
    pts = []
    kms = []
    comments = []
    lat = None
    for el in rec.iter():
        name = ln(el.tag)
        txt = (el.text or "").strip()
        if name == "latitude":
            try:
                lat = float(txt)
            except ValueError:
                lat = None
        elif name == "longitude" and lat is not None:
            try:
                lon = float(txt)
                if in_spain(lat, lon):
                    pts.append([round(lat, 6), round(lon, 6)])
            except ValueError:
                pass
            lat = None
        elif name == "posList" and txt:
            pts.extend(pairs_from_poslist(txt))
        elif name == "kilometerPoint" and txt:
            kms.append(txt)
        elif name == "value" and txt:
            if txt not in comments:
                comments.append(txt)
        elif len(el) == 0 and txt:
            leaves.setdefault(name, [])
            if txt not in leaves[name]:
                leaves[name].append(txt)

    # quitar puntos duplicados consecutivos
    clean = []
    for p in pts:
        if not clean or clean[-1] != p:
            clean.append(p)
    if not clean:
        return None

    def first(*names):
        for n in names:
            if leaves.get(n):
                return leaves[n][0]
        return None

    status = first("validityStatus")
    if status and status.lower() == "suspended":
        return None
    start = parse_time(first("overallStartTime"))
    end = parse_time(first("overallEndTime"))
    if end and end < NOW:
        return None
    if start and start > NOW + dt.timedelta(hours=12):
        return None  # programada para más adelante

    tags = []
    for k, vals in leaves.items():
        kl = k.lower()
        if k.endswith("Type") or k in TYPE_FIELDS_EXTRA or "level" in kl or "service" in kl:
            for v in vals:
                if v not in tags:
                    tags.append(v)

    rtype = ln(rec.get(XSI_TYPE, "")).split(":")[-1]
    return {
        "id": rec.get("id") or "",
        "rt": rtype,
        "c": first("causeType") or "",
        "tags": tags,
        "road": first("roadName", "roadNumber") or "",
        "dest": first("roadDestination", "destination") or "",
        "dir": first("tpegDirectionRoad", "tpegDirection", "directionRelativeOnLinearSection") or "",
        "mun": first("municipality") or "",
        "prov": first("province") or "",
        "ccaa": first("autonomousCommunity") or "",
        "km": kms[:2],
        "pts": clean[:60],
        "start": iso(start),
        "end": iso(end),
        "sev": first("overallSeverity", "severity") or situation_sev or "",
        "planned": bool(start and start > NOW),
        "cm": " · ".join(comments[:3])[:400],
    }


def fetch_dgt():
    raw = get(DGT_URL)
    root = ET.fromstring(raw)
    items = []
    pub_time = None
    for el in root.iter():
        if ln(el.tag) == "publicationTime" and el.text:
            pub_time = parse_time(el.text)
            break
    for sit in root.iter():
        if ln(sit.tag) != "situation":
            continue
        sit_sev = None
        for ch in sit:
            if ln(ch.tag) == "overallSeverity" and ch.text:
                sit_sev = ch.text.strip()
        for rec in sit:
            if ln(rec.tag) != "situationRecord":
                continue
            r = parse_record(rec, sit_sev)
            if r:
                r["sid"] = sit.get("id") or ""
                items.append(r)
    return {
        "source": "DGT · Punto de Acceso Nacional (DATEX II v3.7)",
        "url": DGT_URL,
        "published": iso(pub_time),
        "updated": iso(NOW),
        "count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------- AEMET


def children(el, name):
    return [c for c in el if ln(c.tag) == name]


def child_text(el, name):
    for c in el:
        if ln(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_cap(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if child_text(root, "msgType").lower() == "cancel":
        return None
    infos = children(root, "info")
    if not infos:
        return None
    info = next((i for i in infos if child_text(i, "language").lower().startswith("es")), infos[0])

    params = {}
    for p in children(info, "parameter"):
        params[child_text(p, "valueName")] = child_text(p, "value")
    fen = ""
    for ec in children(info, "eventCode"):
        if "fenomeno" in child_text(ec, "valueName").lower():
            fen = child_text(ec, "value")
    nivel = ""
    for k, v in params.items():
        if "nivel" in k.lower():
            nivel = v.lower()
    if not nivel or nivel == "verde":
        return None

    onset = parse_time(child_text(info, "onset") or child_text(info, "effective"))
    expires = parse_time(child_text(info, "expires"))
    if expires and expires < NOW:
        return None

    areas = []
    for a in children(info, "area"):
        polys = []
        for pg in children(a, "polygon"):
            ring = []
            for pair in (pg.text or "").split():
                try:
                    la, lo = pair.split(",")[:2]
                    ring.append([round(float(la), 3), round(float(lo), 3)])
                except ValueError:
                    pass
            if len(ring) >= 3:
                polys.append(ring)
        zona = ""
        for gc in children(a, "geocode"):
            zona = child_text(gc, "value")
        if polys:
            areas.append({"d": child_text(a, "areaDesc"), "z": zona, "poly": polys})
    if not areas:
        return None

    param = ""
    prob = ""
    for k, v in params.items():
        kl = k.lower()
        if "parametro" in kl and not param:
            param = v
        if "probabilidad" in kl:
            prob = v
    return {
        "id": child_text(root, "identifier"),
        "ev": child_text(info, "event"),
        "head": child_text(info, "headline"),
        "desc": child_text(info, "description")[:500],
        "instr": child_text(info, "instruction")[:300],
        "nivel": nivel,
        "fen": fen,
        "param": param[:200],
        "prob": prob,
        "onset": iso(onset),
        "expires": iso(expires),
        "areas": areas,
    }


def iter_tar_xml(raw):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            data = f.read()
            name = m.name.lower()
            if name.endswith(".xml"):
                yield data
            elif name.endswith((".tar", ".tar.gz", ".tgz", ".gz")):
                try:
                    yield from iter_tar_xml(data)
                except tarfile.TarError:
                    pass


def fetch_aemet():
    rss = ET.fromstring(get(AEMET_RSS))
    link = None
    for item in rss.iter():
        if ln(item.tag) != "item":
            continue
        for c in item:
            if ln(c.tag) == "link" and c.text and c.text.strip().endswith((".tar.gz", ".tar", ".tgz")):
                link = c.text.strip()
                break
        if link:
            break
    if not link:
        raise RuntimeError("No encuentro el paquete de avisos en el RSS de AEMET")
    items, seen = [], set()
    for data in iter_tar_xml(get(link)):
        cap = parse_cap(data)
        if cap and cap["id"] not in seen:
            seen.add(cap["id"])
            items.append(cap)
    return {
        "source": "AEMET · Meteoalerta (CAP)",
        "url": link,
        "updated": iso(NOW),
        "count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------- main


def previous(name):
    base = os.environ.get("PREV_BASE", "").strip()
    if not base:
        return None
    try:
        return json.loads(get(base.rstrip("/") + "/data/" + name, timeout=30))
    except Exception:
        return None


def run(name, fn, out_dir):
    path = os.path.join(out_dir, name)
    try:
        data = fn()
        print(f"{name}: {data['count']} elementos")
    except Exception as e:  # la fuente ha fallado: reutilizar los datos anteriores
        print(f"AVISO {name}: {e}", file=sys.stderr)
        data = previous(name) or {"items": [], "count": 0, "updated": None}
        data["error"] = f"No se pudo actualizar ({type(e).__name__}) a las {iso(NOW)}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    os.makedirs(out_dir, exist_ok=True)
    run("incidencias.json", fetch_dgt, out_dir)
    run("avisos.json", fetch_aemet, out_dir)


if __name__ == "__main__":
    main()
