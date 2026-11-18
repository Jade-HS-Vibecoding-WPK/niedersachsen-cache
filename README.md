# Niedersachsen-Cache

**Karten-Daten aus OpenStreetMap für die großen Städte Niedersachsens – fertig vorbereitet, in unter einer Sekunde geladen.**

Testseite mit Karte: **https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/**

## Was ist das, und warum?

Viele unserer Apps holen Gebäude, Straßen, Grünflächen, Läden oder Haltestellen aus OpenStreetMap.
Das läuft meist über **Overpass**, einen öffentlichen Server, den sich die ganze Welt teilt.
Jede Anfrage dort wartet erst einmal in einer **Warteschlange: oft 30 bis 55 Sekunden**.
Das eigentliche Herunterladen dauert danach nur 1–2 Sekunden. Rund 98 % der Ladezeit ist also Warten.
Manchmal weist der Server uns auch ganz ab („Too Many Requests“), besonders wenn viele Studierende
über dasselbe Hochschulnetz fragen.

Dieser Cache legt die Daten **einmal pro Woche** fertig vorbereitet auf GitHub Pages ab.
Deine App fragt zuerst hier. Liegt dort nichts, lädt sie wie bisher bei Overpass.
**Deine App geht also nie kaputt**, wenn der Cache fehlt – sie wird nur schneller, wenn er da ist.

## Welche Städte?

Die Großstädte Niedersachsens (über 100.000 Einwohner) mit ihren **Randgebieten**:
Stadtgrenze plus etwa **3 km** rundherum. Dörfer und das Land dazwischen sind nicht dabei.

| Stadt | Fläche der Stadt | mit 3-km-Rand | Hinweis |
|---|---:|---:|---|
| Hannover | 204 km² | 451 km² | |
| Braunschweig | 193 km² | 418 km² | |
| Oldenburg (Oldb) | 103 km² | 264 km² | |
| Osnabrück | 120 km² | 310 km² | |
| Wolfsburg | 205 km² | 434 km² | |
| Göttingen | 117 km² | 300 km² | |
| Salzgitter | 224 km² | 515 km² | |
| Hildesheim | 92 km² | 256 km² | **Grenzfall:** Hildesheim liegt knapp an der 100.000-Grenze (zuletzt knapp darunter) und ist trotzdem dabei. |
| Wilhelmshaven | 107 km² | 271 km² | **Zusatz: Hochschulstandort** der Jade Hochschule (keine Großstadt). |

- **Datenstand:** steht auf der [Testseite](https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/), in
  [`STAND.md`](STAND.md) und in [`daten/manifest.json`](https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/daten/manifest.json).
  In deiner App: `NdsCache.stand`.
- **Aktualisierung:** jede Woche (Montag nachts) automatisch aus dem OpenStreetMap-Auszug von
  [Geofabrik](https://download.geofabrik.de/europe/germany/niedersachsen.html). Was heute in OpenStreetMap
  eingetragen wird, ist also spätestens nach gut einer Woche im Cache.

## So benutzt du es

Du brauchst nur **eine Zeile** in deiner `index.html` und tauschst deine Overpass-Abfrage gegen
`NdsCache.overpass(abfrage) || live`:

```html
<script src="https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/nds-cache.js"></script>
<script>
  async function ladeOsm(abfrage) {                       // abfrage = dein bisheriger Overpass-Text
    const ausCache = await NdsCache.overpass(abfrage);    // fertige Antwort, genau wie von Overpass
    if (ausCache) return ausCache;                        // -> sofort da
    return ladeLiveVonOverpass(abfrage);                  // null -> wie bisher live laden
  }
  // Quellenangabe auf die Karte schreiben (Pflicht, siehe unten):
  document.getElementById('quelle').textContent = NdsCache.quellenangabe;
</script>
```

- Klappt **per Doppelklick** auf deine `index.html` (file://) und auf **GitHub Pages**.
- Die Antwort sieht aus wie die von Overpass (`{elements: [...]}`), dein bisheriger Code zum Umwandeln
  in GeoJSON funktioniert also weiter. Einziger Unterschied: Wege haben kein Feld `nodes`, nur `geometry`.
- **`null` heißt:** nicht im Cache (Gebiet außerhalb, Abfrage zu kompliziert, Netz weg) → live laden.
  Warum, steht in `NdsCache.letzterGrund`.

Weitere Befehle:

| Befehl | Ergebnis |
|---|---|
| `NdsCache.overpass(abfrage)` | Overpass-Antwort oder `null` |
| `NdsCache.elemente([west, süd, ost, nord])` | **alle** Objekte im Rechteck als `{elements: [...]}` oder `null`. Mit `{ohneRouten: true}` als zweitem Wert ohne Bus-/Bahn-/Radrouten (lädt viel weniger) |
| `NdsCache.abgedeckt([west, süd, ost, nord])` | `true`, wenn das Rechteck ganz im Cache liegt |
| `NdsCache.stand` | Datenstand, z. B. `2026-11-17T21:24:30Z` |
| `NdsCache.quellenangabe` | `© OpenStreetMap-Mitwirkende, ODbL` |
| `NdsCache.geladeneBytes` | bisher heruntergeladene Bytes (zum Messen) |

### Welche Overpass-Abfragen versteht der Cache?

Die üblichen, wie sie urban-analyst, LOCUS und die meisten Apps benutzen:

```
[out:json][timeout:60];
(
  way["building"](53.51,8.10,53.53,8.12);                 // Rechteck: süd, west, nord, ost
  node["amenity"~"^(cafe|restaurant)$"](poly:"53.51 8.10 53.51 8.12 53.53 8.12");
  nwr["leisure"="park"]["access"!="private"](53.51,8.10,53.53,8.12);
);
out geom;
```

- Kopf: `[out:json]`, `[timeout:…]`, `[maxsize:…]`, `[bbox:…]`
- `node`, `way`, `relation`, `nwr` mit Filtern `["k"]`, `["k"="v"]`, `["k"!="v"]`, `["k"~"regex"]`, `["k"!~"regex"]`,
  `["k"~"regex",i]`, `[!"k"]`
- Gebiet: `(süd,west,nord,ost)`, `(poly:"lat lon lat lon …")`, `(bbox)` zusammen mit `[bbox:…]`
- Klammern `( … );` für mehrere Teile, mehrere `out` nacheinander
- Ausgabe: `out;` `out body;` `out geom;` `out geom qt;` `out skel geom;` `out tags;` `out ids;`

**Nicht** unterstützt (dann kommt `null` und deine App lädt live): `make`, `out count`, `out center`,
`out geom(…)`, `out meta`, `>`/`<` (Rekursion), `area[…]`, `around`, benannte Mengen (`->.x`), `[out:xml]`,
außerdem `out;` für Wege (dafür bräuchte man die Knoten-Nummern – nimm `out geom;`).

## Lizenz: Quellenangabe ist Pflicht

Die Daten stammen aus **OpenStreetMap** und stehen unter der
[Open Database License (ODbL)](https://www.openstreetmap.org/copyright). Der Cache ist eine
**abgeleitete Datenbank** und steht deshalb ebenfalls unter der ODbL.

Für dich heißt das:

1. **Auf jeder Karte** muss sichtbar stehen: **„© OpenStreetMap-Mitwirkende, ODbL“**
   (am besten mit Link auf https://www.openstreetmap.org/copyright). Der Text steht fertig in `NdsCache.quellenangabe`.
2. **In jedem Export** (PDF, PNG, DXF, SVG, GeoJSON …) muss derselbe Hinweis stehen, z. B. in der Ecke des Plans
   oder als Kommentar/Eigenschaft in der Datei.
3. Gibst du **Daten** weiter (nicht nur Bilder), z. B. ein GeoJSON mit Gebäuden, dann gilt für diese Daten wieder die ODbL.

Der Programmcode in diesem Repo steht unter der Apache-2.0-Lizenz (siehe [LICENSE](LICENSE)).

## Was ist drin, was nicht?

**Drin:** jedes OpenStreetMap-Objekt mit mindestens einem dieser Merkmale:
`building`, `building:use`, `highway`, `railway`, `public_transport`, `route` (Bus-, Bahn-, Radrouten …),
`leisure`, `landuse`, `natural`, `waterway`, `water`, `amenity`, `shop`, `office`, `craft`, `healthcare`,
`tourism`, `military`, `man_made`, `place`, `boundary=administrative`.
Alle Merkmale eines Objekts bleiben erhalten (Name, Adresse, Öffnungszeiten, Website, Geschosszahl …),
außer dieser **Streichliste**: fremdsprachige Namen `name:xx` (außer `name:de`), `source*`, `note*`, `fixme`,
`check_date*`, `survey*`, `mapillary`, `created_by`.

**Grenzen:**

- **Nur diese Städte** mit ihrem Rand. Alles andere (andere Orte, Bremen, ganz Deutschland) lädt deine App live.
  Liegt dein Gebiet nur zum Teil im Cache, kommt `null` → live.
- Was **nicht** in der Liste oben steht (z. B. `power`, `barrier`, `historic` ohne eins der Merkmale oben,
  reine Adresspunkte), fehlt im Cache. Fragt deine App danach, findet sie im Cache nichts.
  Im Zweifel: Ergebnis mit dem Live-Ergebnis vergleichen.
- **Datenstand** bis zu gut einer Woche alt. Wer gerade selbst in OpenStreetMap einträgt, sieht das erst nach dem
  nächsten Bau.
- Teile von sehr großen Objekten, die **außerhalb Niedersachsens** liegen (z. B. eine ICE-Route bis München),
  haben keine Geometrie – nur die Teile in Niedersachsen.
- **Gebäudehöhen vom LGLN** (amtliches 3D-Gebäudemodell) sind **geplant**, aber noch nicht drin.

## Wie funktioniert es? (für Neugierige)

- Die Daten liegen als kleine gepackte Dateien („Kacheln“, je ca. 750 × 750 m) in
  `daten/osm/15/{x}/{y}.json.gz`. Große Objekte (lange Straßen, Wälder, Grenzen) liegen ganz in
  `daten/osm-gross/12/{x}/{y}.json.gz` (Kacheln von ca. 6 km), Bus-, Bahn- und andere Routen in
  `daten/osm-routen/12/{x}/{y}.json.gz`. Was wo liegt, beschreibt `daten/manifest.json`.
- `nds-cache.js` rechnet aus, welche Dateien es braucht, lädt höchstens 6 gleichzeitig, packt sie aus
  und beantwortet deine Overpass-Abfrage selbst – mit denselben Regeln wie Overpass
  (ein Weg gehört dazu, wenn er das Gebiet berührt).
- Gebaut wird mit Python ([`bauen/bauen.py`](bauen/bauen.py), Bibliothek pyosmium) direkt aus dem
  Geofabrik-Auszug `niedersachsen-latest.osm.pbf` – **nicht** über Overpass. Das macht die GitHub Action
  [`bauen.yml`](.github/workflows/bauen.yml) jeden Montag, prüft die Größe (Abbruch über 900 MB), testet die
  Seite ([`pruefen/selbsttest.js`](pruefen/selbsttest.js)) und veröffentlicht sie. Die Daten selbst liegen
  nicht im Git, nur auf der Pages-Seite.

## Eine Stadt hinzufügen

1. In [`bauen/staedte.json`](bauen/staedte.json) einen Eintrag ergänzen, z. B.
   `{"id": "delmenhorst", "name": "Delmenhorst", "ags": "03401000"}`.
   Der **AGS** (amtlicher Gemeindeschlüssel, 8 Ziffern) steht z. B. bei Wikipedia im Kasten rechts.
   Optional `"zusatz": "…"` für einen Hinweis.
2. Änderung auf `main` hochladen (Commit + Push oder direkt auf GitHub bearbeiten).
3. Unter **Actions → Cache bauen → Run workflow** den Bau starten (dauert ca. 15–20 Minuten).
4. Die Action bricht ab, wenn die Seite größer als 900 MB würde. Dann zuerst den Rand verkleinern
   (`puffer_m` in `staedte.json`), nicht Merkmale weglassen.

Selbst bauen auf dem eigenen Rechner (Python 3.12):

```
pip install -r bauen/requirements.txt
curl -LO https://download.geofabrik.de/europe/germany/niedersachsen-latest.osm.pbf
python bauen/bauen.py --pbf niedersachsen-latest.osm.pbf --ausgabe site
copy web\* site\        (Windows)   bzw.   cp web/* site/   (Mac/Linux)
node pruefen/selbsttest.js site
```

## Pflege und Verantwortliche

- **Verantwortlich:** Kursleitung WPK Vibecoding (Jade Hochschule).
- **Pflege:** Die Action läuft von selbst. Bitte ab und zu nachsehen, ob der letzte Lauf unter **Actions** grün ist
  und ob `STAND.md` aktuell ist. GitHub schaltet den Zeitplan sonst nach 60 Tagen ohne Änderung im Repo ab –
  die Action schreibt deshalb bei jedem Lauf `STAND.md` neu.
- **Wenn etwas kaputt ist:** Issue im Repo anlegen. Die Apps merken davon nichts Schlimmes: Sie laden dann live.
- **Formatänderungen:** Apps verlassen sich auf das Format (`format: 1` im Manifest). Wer es ändert, zählt
  `format` hoch und sagt den App-Teams Bescheid.
