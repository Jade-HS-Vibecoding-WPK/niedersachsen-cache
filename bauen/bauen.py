"""Niedersachsen-Cache bauen.

Liest einen OpenStreetMap-Auszug (Geofabrik, *.osm.pbf) und schreibt für die Städte aus staedte.json
gzip-Kacheln im Format der Overpass-Antwort "out geom":

  daten/osm/15/{x}/{y}.json.gz        kleine Objekte, je z15-Kachel (ca. 0,75 km)
  daten/osm-gross/12/{x}/{y}.json.gz  große Objekte (Umriss über mehr als 2x2 z15-Kacheln), ganz, je z12-Kachel
  daten/manifest.json                 Datenstand, Städte, Kachelliste, Lizenz

Aufruf (im Repo-Ordner):
  python bauen/bauen.py --pbf niedersachsen-latest.osm.pbf --ausgabe site

Ablauf (4 Lesedurchgänge, alle über dieselbe Datei; nur Python + pyosmium, kein Overpass):
  1. Relationen: Stadtgrenzen (über den amtlichen Gemeindeschlüssel) und alle Relationen mit passenden Schlüsseln
  2. Wege der Stadtgrenzen und Mitglieds-Wege ohne eigene passende Schlüssel (nur Knoten-Nummern)
  3. Koordinaten dieser Wege und der Mitglieds-Knoten -> Stadtfläche + Puffer -> Liste der z15-Kacheln
  4. Hauptdurchgang: Knoten und Wege mit passenden Schlüsseln -> Kacheln
  5. Relationen mit Mitglieds-Geometrie zusammensetzen -> Kacheln (kein eigener Lesedurchgang)
"""
import argparse
import array
import base64
import gzip
import json
import math
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict

import numpy
import osmium
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, box, mapping
from shapely.ops import linemerge, polygonize, unary_union
import shapely
from shapely.prepared import prep

FORMAT = 1
Z = 15    # Zoomstufe der Kacheln (ca. 0,75 km in Niedersachsen)
ZG = 12   # Zoomstufe der Groß-Ebene (ca. 6 km)

# Objekte mit mindestens einem dieser Schlüssel kommen in den Cache.
SCHLUESSEL = [
    "building", "building:use", "highway", "railway", "public_transport", "route", "leisure", "landuse",
    "natural", "waterway", "water", "amenity", "shop", "office", "craft", "healthcare", "tourism",
    "military", "man_made", "place",
]
# "boundary" nur mit dem Wert "administrative"
SCHLUESSEL_MIT_WERT = {"boundary": "administrative"}
_ALLE_SCHLUESSEL = set(SCHLUESSEL)
_FILTER_SCHLUESSEL = SCHLUESSEL + list(SCHLUESSEL_MIT_WERT)

# Diese Merkmale werden gestrichen (sie helfen keiner App und machen die Dateien groß):
STREICHLISTE = [
    "name:xx (fremdsprachige Namen; name:de bleibt)",
    "source, source:*, source_*",
    "note, note:*, note_*",
    "fixme, FIXME, fixme:*",
    "check_date, check_date:*",
    "survey, survey:*, survey_*",
    "mapillary",
    "created_by",
]
STREICH = re.compile(
    r"^(name:(?!de$).+|(source|note|check_date|survey)([:_].*)?|(fixme|FIXME)(:.*)?|mapillary|created_by)$"
)

ERDE = 40075016.686  # Erdumfang am Äquator in m


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ Merkmale
def aufnehmen(tags):
    """tags: dict. True, wenn das Objekt in den Cache gehört."""
    for k in tags:
        if k in _ALLE_SCHLUESSEL:
            return True
    for k, v in SCHLUESSEL_MIT_WERT.items():
        if tags.get(k) == v:
            return True
    return False


def merkmale(tags):
    return {k: v for k, v in tags.items() if not STREICH.match(k)}


# ------------------------------------------------------------------ Kacheln
def kx(lon, z=Z):
    return (lon + 180.0) / 360.0 * (1 << z)


def ky(lat, z=Z):
    r = math.radians(lat)
    return (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (1 << z)


def kachel_west(x, z=Z):
    return x / (1 << z) * 360.0 - 180.0


def kachel_nord(y, z=Z):
    n = math.pi - 2.0 * math.pi * y / (1 << z)
    return math.degrees(math.atan(math.sinh(n)))


def kachel_rechteck(x, y, z=Z):
    """(west, süd, ost, nord) einer Kachel in Grad."""
    return kachel_west(x, z), kachel_nord(y + 1, z), kachel_west(x + 1, z), kachel_nord(y, z)


def kachel_flaeche_km2(x, y, z=Z):
    w, s, o, n = kachel_rechteck(x, y, z)
    breite = ERDE * math.cos(math.radians((s + n) / 2)) / (1 << z)
    return breite * breite / 1e6


def segment_kacheln(lat0, lon0, lat1, lon1, ziel):
    """Alle z15-Kacheln, die die Strecke (gerade in Grad, wie bei Overpass) berührt."""
    x0, y0 = int(kx(lon0)), int(ky(lat0))
    x1, y1 = int(kx(lon1)), int(ky(lat1))
    ziel.add((x0, y0))
    if x0 == x1 and y0 == y1:
        return
    ziel.add((x1, y1))
    ts = [0.0, 1.0]
    if x0 != x1:
        for k in range(min(x0, x1) + 1, max(x0, x1) + 1):
            ts.append((kachel_west(k) - lon0) / (lon1 - lon0))
    if y0 != y1:
        for k in range(min(y0, y1) + 1, max(y0, y1) + 1):
            ts.append((kachel_nord(k) - lat0) / (lat1 - lat0))
    ts.sort()
    for i in range(len(ts) - 1):
        t = (ts[i] + ts[i + 1]) / 2.0
        ziel.add((int(kx(lon0 + t * (lon1 - lon0))), int(ky(lat0 + t * (lat1 - lat0)))))


def linie_kacheln(pts):
    """pts: Liste von (lat, lon). Ergibt (Kachelmenge, gross?)."""
    xs = [int(kx(p[1])) for p in pts]
    ys = [int(ky(p[0])) for p in pts]
    xa, xb, ya, yb = min(xs), max(xs), min(ys), max(ys)
    if xb - xa <= 1 and yb - ya <= 1:
        # passt in 2x2 Kacheln: alle Kacheln des Rechtecks nehmen (höchstens 4, etwas großzügig ist unschädlich)
        return {(x, y) for x in range(xa, xb + 1) for y in range(ya, yb + 1)}, (xa, ya, xb, yb)
    k = set()
    for i in range(len(pts) - 1):
        segment_kacheln(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], k)
    if len(pts) == 1:
        k.add((xs[0], ys[0]))
    return k, (xa, ya, xb, yb)


def ist_gross(rahmen, schwelle):
    xa, ya, xb, yb = rahmen
    return xb - xa + 1 > schwelle or yb - ya + 1 > schwelle


# ------------------------------------------------------------------ JSON-Bausteine (Format wie Overpass "out geom")
def packen(pts):
    """Punkte platzsparend merken: ganze Zahlen in 1e-7 Grad (genau wie OSM sie speichert)."""
    a = array.array("i")
    for lat, lon in pts:
        a.append(round(lat * 1e7))
        a.append(round(lon * 1e7))
    return a


def auspacken(a):
    return [(a[i] / 1e7, a[i + 1] / 1e7) for i in range(0, len(a), 2)]


def geom_json(pts):
    return "[" + ",".join('{"lat":%r,"lon":%r}' % p for p in pts) + "]"


def bounds_json(pts):
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return '{"minlat":%r,"minlon":%r,"maxlat":%r,"maxlon":%r}' % (min(lats), min(lons), max(lats), max(lons))


def tags_json(tags):
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ Stadtgrenzen
def flaeche_aus_wegen(aussen, innen):
    def ringe(linien):
        if not linien:
            return []
        m = linemerge(linien)
        teile = list(m.geoms) if hasattr(m, "geoms") else [m]
        # Kleine Lücken in der Grenze (Fehler in OSM, z. B. Meppen: 2 m) schließen, wenn Anfang und Ende
        # eines offenen Stücks höchstens ca. 100 m auseinander liegen
        luecken = [LineString([l.coords[-1], l.coords[0]]) for l in teile
                   if not l.is_closed and math.dist(l.coords[0], l.coords[-1]) < 0.0015]
        if luecken:
            m = linemerge(teile + luecken)
        return list(polygonize(m))

    a = ringe(aussen)
    if not a:
        return None
    g = unary_union(a)
    i = ringe(innen)
    if i:
        g = g.difference(unary_union(i))
    return g


def poly_lesen(pfad):
    """Geofabrik-*.poly lesen (Abschnitte mit Koordinaten, '!' = Loch). Ergibt eine shapely-Fläche."""
    aussen, loecher, cur, loch = [], [], None, False
    zeilen = [z.strip() for z in open(pfad, encoding="utf-8") if z.strip()]
    for z in zeilen[1:]:
        if z == "END":
            if cur is not None:
                (loecher if loch else aussen).append(Polygon(cur))
                cur = None
            continue
        if cur is None:
            loch, cur = z.startswith("!"), []
            continue
        x, y = z.split()[:2]
        cur.append((float(x), float(y)))
    g = unary_union(aussen)
    return g.difference(unary_union(loecher)) if loecher else g


def bitmaske(kacheln):
    xs = [k[0] for k in kacheln]
    ys = [k[1] for k in kacheln]
    x0, y0 = min(xs), min(ys)
    b, h = max(xs) - x0 + 1, max(ys) - y0 + 1
    bits = bytearray((b * h + 7) // 8)
    for x, y in kacheln:
        i = (y - y0) * b + (x - x0)
        bits[i >> 3] |= 1 << (i & 7)
    return {"x0": x0, "y0": y0, "breite": b, "hoehe": h, "bits": base64.b64encode(bytes(bits)).decode()}


# ------------------------------------------------------------------ Hauptprogramm
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True, help="Geofabrik-Auszug *.osm.pbf")
    ap.add_argument("--ausgabe", default="site", help="Zielordner (daten/ wird darin angelegt)")
    ap.add_argument("--staedte-datei", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "staedte.json"))
    ap.add_argument("--staedte", default="", help="nur diese Städte (ids, Komma-getrennt), z. B. für Tests")
    ap.add_argument("--puffer", type=float, default=None, help="Randgebiet in m (Standard: aus staedte.json)")
    ap.add_argument("--budget-mb", type=float, default=None,
                    help="höchstens so viele MB Daten (gepackt); Umland-Gemeinden darüber bleiben draußen (Standard: aus staedte.json)")
    ap.add_argument("--gross-schwelle", type=int, default=2,
                    help="Objekte, deren Umriss mehr als N x N z15-Kacheln überspannt, kommen in die Groß-Ebene")
    ap.add_argument("--auszug-poly", default="",
                    help="Umriss des Auszugs (Geofabrik *.poly). Kacheln, die nicht ganz darin liegen, gelten als nicht "
                         "abgedeckt, weil dort Daten jenseits der Landesgrenze fehlen.")
    ap.add_argument("--quelle-url", default="https://download.geofabrik.de/europe/germany/niedersachsen-latest.osm.pbf")
    args = ap.parse_args()

    t_start = time.time()
    konf = json.load(open(args.staedte_datei, encoding="utf-8"))
    puffer = args.puffer if args.puffer is not None else konf.get("puffer_m", 3000)
    budget_mb = args.budget_mb if args.budget_mb is not None else konf.get("budget_mb")
    # Großstädte (immer drin), dann Umland-Gemeinden nach Entfernung von Oldenburg (drin, solange das Budget reicht)
    staedte = [dict(s, gruppe="grossstadt", puffer_m=s.get("puffer_m", puffer)) for s in konf["staedte"]]
    umland = konf.get("umland") or {}
    staedte += [dict(s, gruppe="umland", puffer_m=s.get("puffer_m", umland.get("puffer_m", puffer)))
                for s in umland.get("orte", [])]
    if args.staedte:
        wahl = set(args.staedte.split(","))
        staedte = [s for s in staedte if s["id"] in wahl]
    if args.puffer is not None:
        for s in staedte:
            s["puffer_m"] = args.puffer
    ags_zu_stadt = {s["ags"]: s for s in staedte}
    log(f"{sum(s['gruppe'] == 'grossstadt' for s in staedte)} Großstädte, {sum(s['gruppe'] == 'umland' for s in staedte)} "
        f"Umland-Gemeinden, Budget {budget_mb} MB")

    rd = osmium.io.Reader(args.pbf, osmium.osm.NOTHING)
    kopf = rd.header()
    datenstand = kopf.get("osmosis_replication_timestamp") or kopf.get("timestamp") or ""
    rd.close()
    log("Datenstand des Auszugs:", datenstand or "(unbekannt)")

    # ---------------- 1. Relationen
    log("1/5 Relationen lesen …")
    rel = {}               # id -> (tags_gefiltert, [(typ, ref, rolle)])
    stadt_rel = {}         # stadt-id -> (rel-id, [(ref, rolle)])
    rel_wege, rel_knoten = set(), set()
    for r in osmium.FileProcessor(args.pbf, osmium.osm.RELATION):
        tags = dict(r.tags)
        ags = tags.get("de:amtlicher_gemeindeschluessel")
        if tags.get("boundary") == "administrative" and ags in ags_zu_stadt:
            s = ags_zu_stadt[ags]
            alt = stadt_rel.get(s["id"])
            # Bei doppelten Grenzen (z. B. Stufe 6 und 8) die mit mehr Mitgliedern nehmen
            if alt is None or len(r.members) > len(alt[1]):
                stadt_rel[s["id"]] = (r.id, [(m.ref, m.role) for m in r.members if m.type == "w"])
        if not aufnehmen(tags):
            continue
        mitglieder = [(m.type, m.ref, m.role) for m in r.members]
        rel[r.id] = (merkmale(tags), mitglieder)
        for typ, ref, _ in mitglieder:
            if typ == "w":
                rel_wege.add(ref)
            elif typ == "n":
                rel_knoten.add(ref)
    fehlend = [s["id"] for s in staedte if s["id"] not in stadt_rel and s["gruppe"] == "grossstadt"]
    if fehlend:
        sys.exit("Stadtgrenze nicht gefunden (AGS prüfen): " + ", ".join(fehlend))
    ohne_grenze = [s["name"] for s in staedte if s["id"] not in stadt_rel]
    if ohne_grenze:   # Umland: nicht schlimm, die Gemeinde fehlt dann eben
        log(f"   Hinweis: keine Grenze in OSM für {len(ohne_grenze)} Umland-Gemeinde(n), übersprungen: {', '.join(ohne_grenze)}")
        staedte = [s for s in staedte if s["id"] in stadt_rel]
    log(f"   {len(rel)} Relationen mit passenden Schlüsseln, {len(rel_wege)} Mitglieds-Wege")

    # ---------------- 2. Wege: Stadtgrenzen + Mitglieds-Wege, die der Hauptdurchgang nicht sieht
    log("2/5 Wege der Stadtgrenzen und Mitglieds-Wege lesen …")
    grenz_wege = {ref for _, (_, ms) in stadt_rel.items() for ref, _ in ms}
    wege_knoten = {}       # Weg-id -> array('q') Knoten-ids
    gesucht = set(rel_knoten)  # Knoten, deren Koordinaten wir brauchen
    for w in osmium.FileProcessor(args.pbf, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(rel_wege | grenz_wege)):
        if w.id in grenz_wege or not any(k in w.tags for k in _FILTER_SCHLUESSEL):
            # Stadtgrenze, oder Mitglieds-Weg, der im Hauptdurchgang nicht vorbeikommt
            ids = array.array("q", (n.ref for n in w.nodes))
            wege_knoten[w.id] = ids
            gesucht.update(ids)
    log(f"   {len(wege_knoten)} Wege gemerkt, {len(gesucht)} Knoten gesucht")

    # ---------------- 3. Stadtflächen
    log("3/5 Knoten-Koordinaten lesen, Stadtflächen und Kacheln berechnen …")
    knoten_ort = {}        # Knoten-id -> (lat, lon)
    for n in osmium.FileProcessor(args.pbf, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(gesucht)):
        if n.location.valid():
            knoten_ort[n.id] = (n.location.lat, n.location.lon)
    del gesucht
    _utm = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    _wgs = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

    def zu_utm(g):
        return shapely.transform(g, lambda c: numpy.column_stack(_utm.transform(c[:, 0], c[:, 1])))

    def zu_wgs(g):
        return shapely.transform(g, lambda c: numpy.column_stack(_wgs.transform(c[:, 0], c[:, 1])))
    abgedeckt = {}         # (x, y) -> [stadt-ids]
    stadt_info = []
    stadt_kacheln = {}     # stadt-id -> [(x, y)]
    grenzen = {}           # stadt-id -> GeoJSON der Grenze (eigene Datei, damit das Manifest klein bleibt)
    auszug = prep(poly_lesen(args.auszug_poly)) if args.auszug_poly else None
    if auszug is None:
        log("   Hinweis: ohne --auszug-poly wird nicht geprüft, ob Kacheln über den Auszug hinausragen")
    for s in staedte:
        rid, ms = stadt_rel[s["id"]]
        aussen, innen = [], []
        for ref, rolle in ms:
            pts = [knoten_ort[k][::-1] for k in wege_knoten.get(ref, []) if k in knoten_ort]
            if len(pts) >= 2:
                (innen if rolle == "inner" else aussen).append(LineString(pts))
        stadt = flaeche_aus_wegen(aussen, innen)
        if stadt is None or stadt.is_empty:
            if s["gruppe"] == "umland":
                log(f"   Hinweis: Grenze von {s['name']} ließ sich nicht zusammensetzen (Relation {rid}), übersprungen")
                continue
            sys.exit(f"Stadtgrenze von {s['name']} ließ sich nicht zusammensetzen (Relation {rid})")
        stadt_utm = zu_utm(stadt)
        mit_puffer = zu_wgs(stadt_utm.buffer(s["puffer_m"], quad_segs=16))
        pp = prep(mit_puffer)
        w, su, o, n = mit_puffer.bounds
        kacheln, gekappt = [], 0
        for x in range(int(kx(w)), int(kx(o)) + 1):
            for y in range(int(ky(n)), int(ky(su)) + 1):
                r = box(*kachel_rechteck(x, y))
                if pp.intersects(r):
                    if auszug is not None and not auszug.contains(r):
                        gekappt += 1  # ragt über den Auszug hinaus (andere Bundesländer): Daten wären unvollständig
                        continue
                    kacheln.append((x, y))
                    abgedeckt.setdefault((x, y), []).append(s["id"])
        if not kacheln:
            log(f"   Hinweis: {s['name']} liegt ganz außerhalb des Auszugs, übersprungen")
            continue
        stadt_kacheln[s["id"]] = kacheln
        bb = [min(kachel_rechteck(x, y)[0] for x, y in kacheln), min(kachel_rechteck(x, y)[1] for x, y in kacheln),
              max(kachel_rechteck(x, y)[2] for x, y in kacheln), max(kachel_rechteck(x, y)[3] for x, y in kacheln)]
        info = {
            "id": s["id"], "name": s["name"], "ags": s["ags"], "osm_relation": rid, "gruppe": s["gruppe"],
            "einwohner_ca": s.get("einwohner_ca"),
            "flaeche_stadt_km2": round(stadt_utm.area / 1e6, 1),
            "puffer_m": s["puffer_m"],
            "flaeche_mit_puffer_km2": round(zu_utm(mit_puffer).area / 1e6, 1),
            "flaeche_kacheln_km2": round(sum(kachel_flaeche_km2(x, y) for x, y in kacheln), 1),
            "bbox": [round(v, 6) for v in bb],
            "anzahl_kacheln": len(kacheln),
            "kacheln_ausserhalb_auszug": gekappt,
            "kacheln": bitmaske(kacheln),
        }
        grenzen[s["id"]] = mapping(stadt.simplify(0.0005, preserve_topology=True))
        if s.get("km_von_oldenburg") is not None:
            info["km_von_oldenburg"] = s["km_von_oldenburg"]
        if s.get("zusatz"):
            info["zusatz"] = s["zusatz"]
        if gekappt:
            info["hinweis"] = (f"Randgebiet an der Grenze des Niedersachsen-Auszugs abgeschnitten ({gekappt} Kacheln "
                               "fehlen, dort lädt die App live)")
        stadt_info.append(info)
        log(f"   {s['name']}: Stadt {info['flaeche_stadt_km2']} km², mit Puffer {info['flaeche_mit_puffer_km2']} km², "
            f"{len(kacheln)} Kacheln ({info['flaeche_kacheln_km2']} km²)")
    for ref in grenz_wege - rel_wege:
        wege_knoten.pop(ref, None)
    eltern = {(x >> (Z - ZG), y >> (Z - ZG)) for x, y in abgedeckt}
    log(f"   zusammen {len(abgedeckt)} Kacheln z15, {len(eltern)} Kacheln z12")

    # grobe Vorauswahl über das z12-Raster (schnell, auch bei Hunderten Gemeinden):
    # Liegt der Rahmen eines Objekts in keiner z12-Kachel über einer abgedeckten z15-Kachel, ist es nicht dabei.
    def im_rahmen(w, s, o, n):
        x0, x1 = int(kx(w, ZG)), int(kx(o, ZG))
        y0, y1 = int(ky(n, ZG)), int(ky(s, ZG))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 64:
            return any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in eltern)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                if (x, y) in eltern:
                    return True
        return False

    # ---------------- Sammelstellen
    texte = []             # JSON-Text je Element
    sortier = []           # (typ-rang, id) je Element
    klein = defaultdict(list)   # z15-Kachel -> Element-Nummern
    gross = defaultdict(list)   # z12-Kachel -> Element-Nummern
    routen = defaultdict(list)  # z12-Kachel -> Element-Nummern (Relationen type=route)
    routen_schluessel = set()   # alle Schlüssel, die in Routen-Relationen vorkommen (für nds-cache.js)
    routen_wege = {}            # Element-Nummer -> Mitglieds-Wege mit Geometrie (nur Routen-Ebene)
    zaehler = Counter()

    def ablegen(text, rang, oid, kacheln, rahmen_k, typ, route=False):
        drin = [k for k in kacheln if k in abgedeckt]
        if not drin:
            return False
        nr = len(texte)
        texte.append(text)
        sortier.append((rang, oid))
        if route:
            for z12 in {(x >> (Z - ZG), y >> (Z - ZG)) for x, y in drin}:
                routen[z12].append(nr)
            zaehler["routen"] += 1
        elif ist_gross(rahmen_k, args.gross_schwelle):
            for z12 in {(x >> (Z - ZG), y >> (Z - ZG)) for x, y in drin}:
                gross[z12].append(nr)
            zaehler[typ + "_gross"] += 1
        else:
            for k in drin:
                klein[k].append(nr)
            zaehler[typ] += 1
        return True

    # ---------------- 4. Hauptdurchgang
    log("4/5 Knoten und Wege lesen (Hauptdurchgang) …")
    weg_geom = {}          # Weg-id -> Liste (lat, lon), nur für Relations-Mitglieder
    fp = (osmium.FileProcessor(args.pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(osmium.filter.KeyFilter(*_FILTER_SCHLUESSEL)))
    gesehen = Counter()
    for obj in fp:
        if obj.is_node():
            gesehen["knoten"] += 1
            loc = obj.location
            if not loc.valid():
                continue
            lat, lon = loc.lat, loc.lon
            if not im_rahmen(lon, lat, lon, lat):
                continue
            tags = dict(obj.tags)
            if not aufnehmen(tags):
                continue
            k = (int(kx(lon)), int(ky(lat)))
            if k not in abgedeckt:
                continue
            text = '{"type":"node","id":%d,"lat":%r,"lon":%r,"tags":%s}' % (obj.id, lat, lon, tags_json(merkmale(tags)))
            ablegen(text, 0, obj.id, [k], (k[0], k[1], k[0], k[1]), "knoten")
        else:
            gesehen["wege"] += 1
            pts = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
            if len(pts) < 2:
                continue
            if obj.id in rel_wege:
                weg_geom[obj.id] = packen(pts)
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            if not im_rahmen(min(lons), min(lats), max(lons), max(lats)):
                continue
            tags = dict(obj.tags)
            if not aufnehmen(tags):
                continue
            kacheln, rk = linie_kacheln(pts)
            if not any(k in abgedeckt for k in kacheln):
                continue
            text = '{"type":"way","id":%d,"bounds":%s,"geometry":%s,"tags":%s}' % (
                obj.id, bounds_json(pts), geom_json(pts), tags_json(merkmale(tags)))
            ablegen(text, 1, obj.id, kacheln, rk, "wege")
    log(f"   gesehen: {dict(gesehen)}; im Cache: {dict(zaehler)}")

    # ---------------- 5. Relationen zusammensetzen
    log("5/5 Relationen zusammensetzen …")

    ort = knoten_ort.get  # Knoten-id -> (lat, lon) oder None (fehlt im Auszug)

    def weg_punkte(wid):
        """Punkte (lat, lon) eines Mitglieds-Wegs oder None, wenn er im Auszug fehlt."""
        a = weg_geom.get(wid)
        if a is not None:
            return auspacken(a)
        if wid in wege_knoten:
            pts = [p for p in (ort(n) for n in wege_knoten[wid]) if p]
            if len(pts) >= 2:
                weg_geom[wid] = packen(pts)
                return pts
        return None

    def weg_kacheln(wid):
        """(Kachelmenge im Stadt-Rahmen, Rahmen der Kacheln) eines Mitglieds-Wegs, gemerkt."""
        if wid not in weg_kach:
            pts = weg_punkte(wid)
            if pts is None:
                weg_kach[wid] = None
            else:
                lats = [p[0] for p in pts]
                lons = [p[1] for p in pts]
                if im_rahmen(min(lons), min(lats), max(lons), max(lats)):
                    weg_kach[wid] = linie_kacheln(pts)
                else:  # weit weg: nur den Rahmen merken (für die Groß-Einteilung)
                    weg_kach[wid] = (set(), (int(kx(min(lons))), int(ky(max(lats))), int(kx(max(lons))), int(ky(min(lats)))))
        return weg_kach[wid]

    weg_kach = {}
    fehlende_mitglieder = 0
    for rid in sorted(rel):
        tags, ms = rel[rid]
        kacheln = set()
        xa = ya = 1 << 30
        xb = yb = -1
        for typ, ref, _ in ms:
            if typ == "w":
                wk = weg_kacheln(ref)
                if wk is None:
                    continue
                kacheln |= wk[0]
                r = wk[1]
            elif typ == "n":
                p = ort(ref)
                if p is None:
                    continue
                k = (int(kx(p[1])), int(ky(p[0])))
                kacheln.add(k)
                r = (k[0], k[1], k[0], k[1])
            else:
                continue
            xa, ya, xb, yb = min(xa, r[0]), min(ya, r[1]), max(xb, r[2]), max(yb, r[3])
        if not any(k in abgedeckt for k in kacheln):
            continue
        # Routen-Ebene: Relationen type=route, die außer "route" keinen Schlüssel aus der Liste haben.
        # Dort steht die Geometrie jedes Mitglieds-Wegs nur einmal je Datei (Feld "wege"), sonst wie Overpass.
        ist_route = tags.get("type") == "route" and not aufnehmen({k: v for k, v in tags.items() if k != "route"})
        teile, alle_pts, weg_refs = [], [], []
        for typ, ref, rolle in ms:
            rolle_j = json.dumps(rolle, ensure_ascii=False)
            if typ == "w":
                pts = weg_punkte(ref)
                if pts is None:
                    fehlende_mitglieder += 1
                    teile.append('{"type":"way","ref":%d,"role":%s}' % (ref, rolle_j))
                    continue
                alle_pts.extend(pts)
                if ist_route:
                    weg_refs.append(ref)
                    teile.append('{"type":"way","ref":%d,"role":%s}' % (ref, rolle_j))
                else:
                    teile.append('{"type":"way","ref":%d,"role":%s,"geometry":%s}' % (ref, rolle_j, geom_json(pts)))
            elif typ == "n":
                p = ort(ref)
                if p is None:
                    fehlende_mitglieder += 1
                    teile.append('{"type":"node","ref":%d,"role":%s}' % (ref, rolle_j))
                    continue
                alle_pts.append(p)
                teile.append('{"type":"node","ref":%d,"role":%s,"lat":%r,"lon":%r}' % (ref, rolle_j, p[0], p[1]))
            else:
                teile.append('{"type":"relation","ref":%d,"role":%s}' % (ref, rolle_j))
        text = '{"type":"relation","id":%d,"bounds":%s,"members":[%s],"tags":%s}' % (
            rid, bounds_json(alle_pts), ",".join(teile), tags_json(tags))
        if ablegen(text, 2, rid, kacheln, (xa, ya, xb, yb), "relationen", route=ist_route) and ist_route:
            routen_schluessel.update(tags)
            routen_wege[len(texte) - 1] = array.array("q", weg_refs)
    log(f"   im Cache: {dict(zaehler)}; Mitglieder ohne Geometrie (außerhalb des Auszugs): {fehlende_mitglieder}")
    del wege_knoten, knoten_ort, weg_kach

    # ---------------- Schreiben
    log("Dateien schreiben …")
    ziel = os.path.join(args.ausgabe, "daten")
    if os.path.isdir(ziel):
        shutil.rmtree(ziel)
    datei_gr = {}          # (art, x, y) -> (Bytes gepackt, Bytes roh, Pfad)

    def schreiben(pfad, nummern, art, x, y):
        nummern = sorted(set(nummern), key=lambda i: sortier[i])
        roh = '{"elements":[\n' + ",\n".join(texte[i] for i in nummern) + "\n]"
        if art == "osm-routen":
            refs = sorted({r for i in nummern for r in routen_wege.get(i, ())})
            roh += ',\n"wege":{\n' + ",\n".join(
                '"%d":[%s]' % (r, ",".join("[%r,%r]" % p for p in auspacken(weg_geom[r]))) for r in refs) + "\n}"
        roh = (roh + "}\n").encode("utf-8")
        gz = gzip.compress(roh, compresslevel=9, mtime=0)
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        with open(pfad, "wb") as f:
            f.write(gz)
        datei_gr[(art, x, y)] = (len(gz), len(roh), pfad)

    for (x, y) in sorted(abgedeckt):
        schreiben(os.path.join(ziel, "osm", str(Z), str(x), f"{y}.json.gz"), klein.get((x, y), []), "osm", x, y)
    for (x, y) in sorted(eltern):
        schreiben(os.path.join(ziel, "osm-gross", str(ZG), str(x), f"{y}.json.gz"), gross.get((x, y), []), "osm-gross", x, y)
        schreiben(os.path.join(ziel, "osm-routen", str(ZG), str(x), f"{y}.json.gz"), routen.get((x, y), []), "osm-routen", x, y)

    # ---------------- Budget: Großstädte immer, dann Umland-Gemeinden der Reihe nach, solange es reicht
    behalten, k15, k12, summe = [], set(), set(), 0
    budget_info = None
    for info in stadt_info:
        neu15 = [k for k in stadt_kacheln[info["id"]] if k not in k15]
        neu12 = {(x >> (Z - ZG), y >> (Z - ZG)) for x, y in neu15} - k12
        plus = (sum(datei_gr[("osm",) + k][0] for k in neu15)
                + sum(datei_gr[(a,) + e][0] for e in neu12 for a in ("osm-gross", "osm-routen")))
        if budget_mb and info["gruppe"] == "umland" and summe + plus > budget_mb * 1e6:
            draussen = [i["name"] for i in stadt_info[len(behalten):]]
            budget_info = {"mb": budget_mb, "genutzt_mb": round(summe / 1e6, 1),
                           "umland_aufgenommen": sum(i["gruppe"] == "umland" for i in behalten),
                           "letzte_aufgenommen": behalten[-1]["name"],
                           "radius_km": behalten[-1].get("km_von_oldenburg"),
                           "erste_nicht_aufgenommen": f"{info['name']} (+{plus / 1e6:.1f} MB)",
                           "nicht_aufgenommen": len(draussen)}
            log(f"   Budget {budget_mb} MB erreicht bei {summe / 1e6:.1f} MB: {info['name']} (+{plus / 1e6:.1f} MB) "
                f"und {len(draussen) - 1} weitere bleiben draußen")
            break
        summe += plus
        k15.update(neu15)
        k12 |= neu12
        behalten.append(info)
        info["mb"] = round(plus / 1e6, 2)
    if budget_info is None:
        budget_info = {"mb": budget_mb, "genutzt_mb": round(summe / 1e6, 1),
                       "umland_aufgenommen": sum(i["gruppe"] == "umland" for i in behalten), "nicht_aufgenommen": 0,
                       "hinweis": "Alle Gemeinden der Liste passen ins Budget."}
    if len(behalten) < len(stadt_info):
        for (art, x, y), (_, _, pfad) in list(datei_gr.items()):
            if (x, y) not in (k15 if art == "osm" else k12):
                os.remove(pfad)
                del datei_gr[(art, x, y)]
        for wurzel, ordner, namen in os.walk(ziel, topdown=False):
            if not namen and not ordner:
                os.rmdir(wurzel)
        zaehler = Counter()   # neu zählen: nur, was in den verbliebenen Dateien steht
        for i in set().union(*(klein.get(k, ()) for k in k15)):
            zaehler[("knoten", "wege", "relationen")[sortier[i][0]]] += 1
        for i in set().union(*(gross.get(e, ()) for e in k12)):
            zaehler[("knoten", "wege", "relationen")[sortier[i][0]] + "_gross"] += 1
        zaehler["routen"] = len(set().union(*(routen.get(e, ()) for e in k12)))
    stadt_info = behalten
    groessen, dateien, max_datei = Counter(), Counter(), {}
    for (art, _, _), (gz, roh, pfad) in datei_gr.items():
        groessen[art] += gz
        groessen[art + "_roh"] += roh
        dateien[art] += 1
        if gz > max_datei.get(art, (0, ""))[0]:
            max_datei[art] = (gz, pfad)
    with open(os.path.join(ziel, "grenzen.json"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"id": i["id"], "name": i["name"], "gruppe": i["gruppe"]}, "geometry": grenzen[i["id"]]}
            for i in stadt_info]}, f, ensure_ascii=False, separators=(",", ":"))

    manifest = {
        "format": FORMAT,
        "name": "Niedersachsen-Cache",
        "beschreibung": "OpenStreetMap-Daten der Großstädte Niedersachsens (+ Zusatz: Wilhelmshaven, Delmenhorst) mit Randgebiet "
                        "und der Gemeinden über 5000 Einwohner rund um Oldenburg (so viele, wie ins Budget passen), "
                        "vorgeschnitten in Kacheln im Format der Overpass-Antwort 'out geom'.",
        "datenstand": datenstand,
        "erstellt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "quellen": [{
            "name": "OpenStreetMap",
            "auszug": "Geofabrik niedersachsen-latest.osm.pbf",
            "url": args.quelle_url,
            "datenstand": datenstand,
            "umriss": os.path.basename(args.auszug_poly) if args.auszug_poly else None,
            "hinweis": "Der Geofabrik-Auszug Niedersachsen enthält auch das Land Bremen (wichtig für Delmenhorst). "
                       "Kacheln, die über den Umriss des Auszugs hinausragen (andere Bundesländer), sind nicht abgedeckt.",
        }],
        "lizenz": {
            "daten": "ODbL 1.0",
            "quellenangabe": "© OpenStreetMap-Mitwirkende, ODbL",
            "link": "https://www.openstreetmap.org/copyright",
            "hinweis": "Der Cache ist eine abgeleitete Datenbank der OpenStreetMap-Daten und steht selbst unter der "
                       "Open Database License (ODbL 1.0). Karten und Exporte daraus müssen "
                       "'© OpenStreetMap-Mitwirkende, ODbL' nennen.",
        },
        "kacheln": {
            "zoom": Z,
            "pfad": "osm/" + str(Z) + "/{x}/{y}.json.gz",
            "hinweis": "Für jede abgedeckte z15-Kachel gibt es eine Datei (auch wenn sie leer ist). "
                       "Welche Kacheln abgedeckt sind, steht je Stadt in staedte[].kacheln (Bitmaske, "
                       "Bit i = (y-y0)*breite + (x-x0), niedrigstes Bit zuerst, base64).",
        },
        "gross": {
            "zoom": ZG,
            "pfad": "osm-gross/" + str(ZG) + "/{x}/{y}.json.gz",
            "schwelle": f"Umriss (Rahmen der berührten z15-Kacheln) breiter oder höher als {args.gross_schwelle} Kacheln",
            "hinweis": "Große Objekte liegen ganz und ungeschnitten in jeder z12-Kachel, in der sie eine abgedeckte "
                       "z15-Kachel berühren. Für jede z12-Kachel über einer abgedeckten z15-Kachel gibt es eine Datei.",
        },
        "routen": {
            "zoom": ZG,
            "pfad": "osm-routen/" + str(ZG) + "/{x}/{y}.json.gz",
            "schluessel": sorted(routen_schluessel),
            "hinweis": "Relationen mit type=route (Bus-, Bahn-, Rad-, Wanderrouten …), die außer 'route' keinen der "
                       "Cache-Schlüssel tragen, liegen ganz und ungeschnitten in einer eigenen z12-Ebene, weil sie den "
                       "größten Teil der Datenmenge ausmachen und nur selten gebraucht werden. Anders als in den "
                       "übrigen Kacheln steht die Geometrie der Mitglieds-Wege nur einmal je Datei im Feld "
                       "'wege' ({\"Weg-id\": [[lat, lon], …]}); Mitglieder in 'elements' haben dort keine 'geometry'. "
                       "nds-cache.js setzt sie beim Laden wieder ein, danach sehen die Relationen aus wie bei Overpass. "
                       "'schluessel' listet alle Merkmals-Schlüssel dieser Relationen: Eine Abfrage, die einen anderen "
                       "Schlüssel verlangt, braucht diese Ebene nicht.",
        },
        "elemente": {
            "format": "wie Overpass API [out:json] mit 'out geom'",
            "node": "{type, id, lat, lon, tags}",
            "way": "{type, id, bounds, geometry:[{lat,lon}…], tags} – das Feld 'nodes' (Knoten-Nummern) fehlt",
            "relation": "{type, id, bounds, members:[{type:'way', ref, role, geometry} | {type:'node', ref, role, lat, lon}"
                        " | {type:'relation', ref, role}], tags}",
            "abweichungen": [
                "Wege haben kein Feld 'nodes'.",
                "Mitglieder von Relationen, die außerhalb des Niedersachsen-Auszugs liegen, haben keine Geometrie.",
                "Merkmale aus der Streichliste fehlen.",
                "Routen-Ebene (osm-routen): Geometrie der Mitglieds-Wege steht je Datei nur einmal im Feld 'wege' "
                "(siehe routen.hinweis).",
                "Ein Objekt steht in jeder Kachel, die es berührt; über mehrere Kacheln doppelte Objekte (type+id) "
                "muss der Leser entfernen (nds-cache.js macht das).",
            ],
            "reihenfolge": "Knoten, dann Wege, dann Relationen, jeweils nach id",
        },
        "schluessel": SCHLUESSEL + ["boundary=administrative"],
        "schluessel_hinweis": "Enthalten ist jedes Objekt mit mindestens einem dieser Schlüssel (boundary nur mit "
                              "dem Wert administrative). building:use ist zusätzlich drin, weil LOCUS danach fragt.",
        "streichliste": STREICHLISTE,
        "streichliste_regex": STREICH.pattern,
        "budget": dict(budget_info, hinweis_regel=(
            "Großstädte sind immer drin. Danach kommen die Umland-Gemeinden (über 5000 Einwohner) nach Entfernung "
            "von Oldenburg dazu, bis die Daten-Dateien zusammen 'mb' Megabyte (gepackt) erreichen; staedte[].mb = "
            "zusätzliche MB durch diese Gemeinde.")),
        "grenzen": {"pfad": "grenzen.json",
                    "hinweis": "Gemeindegrenzen (vereinfacht, ca. 50 m) als GeoJSON, nur zum Anzeigen. "
                               "Abgedeckt ist die Grenze plus staedte[].puffer_m, auf Kacheln gerundet."},
        "staedte": stadt_info,
        "statistik": {
            "elemente": dict(zaehler),
            "dateien": dict(dateien),
            "bytes_gz": {k: v for k, v in groessen.items() if not k.endswith("_roh")},
            "bytes_roh": {k[:-4]: v for k, v in groessen.items() if k.endswith("_roh")},
            "groesste_datei": {k: {"bytes": v[0], "pfad": os.path.relpath(v[1], ziel).replace(os.sep, "/")}
                               for k, v in max_datei.items()},
            "bauzeit_s": round(time.time() - t_start),
        },
    }
    with open(os.path.join(ziel, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    log(f"Fertig: {sum(dateien.values())} Dateien, "
        f"osm {groessen['osm'] / 1e6:.1f} MB (roh {groessen['osm_roh'] / 1e6:.0f} MB), "
        f"osm-gross {groessen['osm-gross'] / 1e6:.1f} MB (roh {groessen['osm-gross_roh'] / 1e6:.0f} MB), "
        f"osm-routen {groessen['osm-routen'] / 1e6:.1f} MB (roh {groessen['osm-routen_roh'] / 1e6:.0f} MB), "
        f"{time.time() - t_start:.0f} s")
    log("Größte Dateien:", {k: v for k, v in manifest["statistik"]["groesste_datei"].items()})


if __name__ == "__main__":
    main()
