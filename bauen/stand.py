"""Schreibt STAND.md (Datenstand, Größe, Städte) aus dem Manifest. Wird von der Action nach jedem Bau aufgerufen.

Aufruf: python bauen/stand.py site > STAND.md
"""
import json
import os
import sys

seite = sys.argv[1] if len(sys.argv) > 1 else "site"
m = json.load(open(os.path.join(seite, "daten", "manifest.json"), encoding="utf-8"))
groesse = dateien = 0
for wurzel, _, namen in os.walk(seite):
    for n in namen:
        groesse += os.path.getsize(os.path.join(wurzel, n))
        dateien += 1
lauf = os.environ.get("LAUF_URL", "")
st = m["statistik"]

print("# Stand des Niedersachsen-Caches")
print()
print("Diese Datei schreibt die GitHub Action bei jedem Bau neu (auch damit GitHub den wöchentlichen Zeitplan "
      "nicht nach 60 Tagen ohne Aktivität abschaltet).")
print()
print("| | |")
print("|---|---|")
print(f"| Datenstand (OpenStreetMap) | {m['datenstand']} |")
print(f"| gebaut | {m['erstellt']} |")
print(f"| Größe der Seite | {groesse / 1e6:.0f} MB in {dateien} Dateien |")
print(f"| Kacheln z15 / Groß-Ebene z12 / Routen z12 | {st['dateien'].get('osm', 0)} / {st['dateien'].get('osm-gross', 0)} / "
      f"{st['dateien'].get('osm-routen', 0)} |")
print(f"| Bauzeit | {st['bauzeit_s'] // 60} min |")
if lauf:
    print(f"| Action-Lauf | {lauf} |")
print()
print("| Stadt | Fläche Stadt | mit 3-km-Rand | Kacheln |")
print("|---|---:|---:|---:|")
for s in m["staedte"]:
    if s.get("gruppe") == "umland":
        continue
    zusatz = " *(" + s["zusatz"].split(":")[0] + ")*" if s.get("zusatz") else ""
    print(f"| {s['name']}{zusatz} | {s['flaeche_stadt_km2']:.0f} km² | {s['flaeche_mit_puffer_km2']:.0f} km² | {s['anzahl_kacheln']} |")
print()
umland = [s for s in m["staedte"] if s.get("gruppe") == "umland"]
if umland:
    b = m.get("budget", {})
    print(f"## Umland Oldenburg: {len(umland)} Gemeinden über 5.000 Einwohner")
    print()
    print(f"Nach Entfernung von Oldenburg aufgenommen, bis die Daten das Budget von {b.get('mb')} MB erreichen "
          f"(genutzt: {b.get('genutzt_mb')} MB). Rand je Gemeinde ca. 1 km."
          + (f" Als Erste nicht mehr drin: {b['erste_nicht_aufgenommen']}, dazu {b['nicht_aufgenommen'] - 1} weitere."
             if b.get("erste_nicht_aufgenommen") else ""))
    print()
    print("| Gemeinde | km von Oldenburg | Einwohner | Fläche | zusätzliche MB |")
    print("|---|---:|---:|---:|---:|")
    for s in umland:
        print(f"| {s['name']} | {s.get('km_von_oldenburg', 0):.0f} | {s.get('einwohner_ca') or 0:,} | "
              f"{s['flaeche_stadt_km2']:.0f} km² | {s.get('mb', 0):.1f} |".replace(",", "."))
    print()
print("Daten: © OpenStreetMap-Mitwirkende, ODbL")
