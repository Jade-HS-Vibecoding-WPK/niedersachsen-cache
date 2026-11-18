/* nds-cache.js – Niedersachsen-Cache für OpenStreetMap-Daten (Format 1)
 * https://github.com/Jade-HS-Vibecoding-WPK/niedersachsen-cache
 *
 * Statt bei Overpass 30–60 Sekunden in der Warteschlange zu warten, holt dieses Skript die Daten
 * vorbereitet von GitHub Pages. Es funktioniert per Doppelklick (file://) und auf GitHub Pages.
 *
 *   <script src="https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/nds-cache.js"></script>
 *   <script>
 *     async function ladeOsm(abfrage) {
 *       const ausCache = await NdsCache.overpass(abfrage);   // null = nicht im Cache
 *       if (ausCache) return ausCache;
 *       return ladeLiveVonOverpass(abfrage);                 // wie bisher
 *     }
 *   </script>
 *
 * NdsCache.abgedeckt([west, süd, ost, nord])  -> Promise<true|false>
 * NdsCache.elemente([west, süd, ost, nord])   -> Promise<{elements:[…]} | null>  (Option {ohneRouten:true})
 * NdsCache.overpass('[out:json];…;out geom;')  -> Promise<Overpass-Antwort | null>
 * NdsCache.stand, NdsCache.quellenangabe, NdsCache.geladeneBytes
 *
 * Das Skript wirft nie Fehler nach außen. Bei jedem Problem (Gebiet nicht im Cache, Abfrage nicht unterstützt,
 * Netz weg) kommt null – dann lädt die App live. Den Grund zeigt NdsCache.letzterGrund.
 * Daten: © OpenStreetMap-Mitwirkende, ODbL. Bitte auf jeder Karte und in jedem Export nennen.
 */
(function (global) {
  'use strict';

  var ONLINE = 'https://jade-hs-vibecoding-wpk.github.io/niedersachsen-cache/';
  var skript = typeof document !== 'undefined' && document.currentScript && document.currentScript.src;
  // Liegt das Skript auf einem Webserver, stehen die Daten daneben. Sonst (file://, kopiert) die Online-Adresse.
  var basis = global.NDS_CACHE_BASIS || (skript && /^https?:/.test(skript) ? skript.replace(/[^\/]*$/, '') : ONLINE);

  var Z = 15, ZG = 12;
  var hat = Object.prototype.hasOwnProperty;

  var NdsCache = {
    version: '1.0.0',
    basis: basis,
    stand: null,                     // Datenstand (OSM-Zeitstempel), sobald das Manifest geladen ist
    quellenangabe: '© OpenStreetMap-Mitwirkende, ODbL',
    quellenlink: 'https://www.openstreetmap.org/copyright',
    geladeneBytes: 0,                // übertragene (komprimierte) Bytes, zum Messen
    gleichzeitig: 6,                 // höchstens so viele Downloads gleichzeitig
    zeitlimit: 15000,                // ms je Datei
    maxKacheln: 400,                 // größere Gebiete (ca. 220 km²) -> null, dann lieber live
    letzterGrund: '',                // warum zuletzt null kam
    manifest: null
  };

  function Fehler(grund) { this.grund = grund; }
  function nein(grund) { throw new Fehler(grund); }
  function merkeGrund(e) {
    NdsCache.letzterGrund = e instanceof Fehler ? e.grund : 'Fehler: ' + (e && e.message ? e.message : e);
    if (global.console && console.info) console.info('NdsCache: ' + NdsCache.letzterGrund + ' – bitte live laden');
    return null;
  }

  // ------------------------------------------------------------ Laden (max. 6 gleichzeitig, 15 s, Merkzettel)
  var aktiv = 0, warteschlange = [];
  function platz() {
    while (aktiv < NdsCache.gleichzeitig && warteschlange.length) {
      aktiv++;
      warteschlange.shift()();
    }
  }
  function holen(url) {
    return new Promise(function (ok, fehler) {
      warteschlange.push(function () {
        var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var uhr = setTimeout(function () { if (ctrl) ctrl.abort(); }, NdsCache.zeitlimit);
        fetch(url, ctrl ? { signal: ctrl.signal } : {}).then(function (r) {
          if (!r.ok) throw new Fehler('HTTP ' + r.status + ' für ' + url);
          return r.arrayBuffer();
        }).then(function (buf) {
          clearTimeout(uhr);
          NdsCache.geladeneBytes += buf.byteLength;
          return entpacken(buf);
        }).then(function (text) {
          aktiv--; platz(); ok(JSON.parse(text));
        }, function (e) {
          clearTimeout(uhr); aktiv--; platz();
          fehler(e && e.name === 'AbortError' ? new Fehler('Zeitlimit für ' + url) : e);
        });
      });
      platz();
    });
  }
  // gzip-Dateien selbst auspacken. Hat der Server schon entpackt, fehlt die gzip-Kennung (1f 8b).
  function entpacken(buf) {
    var b = new Uint8Array(buf, 0, Math.min(2, buf.byteLength));
    if (b[0] === 0x1f && b[1] === 0x8b) {
      if (typeof DecompressionStream === 'undefined') nein('Browser kann kein gzip (DecompressionStream fehlt)');
      var strom = new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'));
      return new Response(strom).text();
    }
    return new TextDecoder().decode(buf);
  }

  // Routen-Ebene: Dort steht die Geometrie jedes Wegs nur einmal je Datei ("wege"). Wieder in die Mitglieder einsetzen,
  // damit die Relationen genau so aussehen wie bei Overpass "out geom".
  function wegeEinsetzen(js) {
    var w = js.wege, fertig = {};
    if (!w) return js;
    (js.elements || []).forEach(function (el) {
      (el.members || []).forEach(function (m) {
        if (m.type === 'way' && !m.geometry && hat.call(w, m.ref)) {
          m.geometry = fertig[m.ref] || (fertig[m.ref] = w[m.ref].map(function (p) { return { lat: p[0], lon: p[1] }; }));
        }
      });
    });
    return js;
  }

  var merkzettel = {};   // url -> Promise<[Elemente]>
  var merkReihe = [];
  function kachelLaden(url) {
    if (!merkzettel[url]) {
      merkzettel[url] = holen(url).then(function (js) { return wegeEinsetzen(js).elements || []; });
      merkzettel[url].catch(function () { delete merkzettel[url]; });  // beim nächsten Mal neu versuchen
      merkReihe.push(url);
      if (merkReihe.length > 600) delete merkzettel[merkReihe.shift()];
    }
    return merkzettel[url];
  }

  var manifestVersprechen = null;
  function manifestLaden() {
    if (!manifestVersprechen) {
      var orte = [NdsCache.basis];
      if (NdsCache.basis !== ONLINE) orte.push(ONLINE);
      manifestVersprechen = orte.reduce(function (vorher, ort) {
        return vorher.then(function (m) {
          if (m) return m;
          return holen(ort + 'daten/manifest.json').then(function (js) {
            if (!js || js.format !== 1) return null;
            NdsCache.basis = ort;
            return js;
          }).catch(function () { return null; });
        });
      }, Promise.resolve(null)).then(function (m) {
        if (!m) { manifestVersprechen = null; nein('Cache nicht erreichbar'); }
        vorbereiten(m);
        return m;
      });
    }
    return manifestVersprechen;
  }

  var abgedeckteKacheln = null;  // "x/y" -> true
  function vorbereiten(m) {
    if (NdsCache.manifest === m) return;
    NdsCache.manifest = m;
    NdsCache.stand = m.datenstand || null;
    if (m.lizenz && m.lizenz.quellenangabe) NdsCache.quellenangabe = m.lizenz.quellenangabe;
    abgedeckteKacheln = {};
    (m.staedte || []).forEach(function (s) {
      var k = s.kacheln, bin = atob(k.bits);
      for (var i = 0; i < k.breite * k.hoehe; i++) {
        if (bin.charCodeAt(i >> 3) & (1 << (i & 7))) abgedeckteKacheln[(k.x0 + i % k.breite) + '/' + (k.y0 + Math.floor(i / k.breite))] = true;
      }
    });
  }

  // ------------------------------------------------------------ Kacheln und Geometrie
  function kx(lon, z) { return (lon + 180) / 360 * Math.pow(2, z); }
  function ky(lat, z) {
    var r = lat * Math.PI / 180;
    return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z);
  }
  function kachelWest(x) { return x / Math.pow(2, Z) * 360 - 180; }
  function kachelNord(y) { var n = Math.PI - 2 * Math.PI * y / Math.pow(2, Z); return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))); }

  // Gebiet: {rahmen:[w,s,o,n], poly:[[lat,lon]…] oder null}
  function gebietRahmen(w, s, o, n) { return { rahmen: [w, s, o, n], poly: null, schluessel: [w, s, o, n].join(',') }; }
  function gebietPoly(pts) {
    var w = Infinity, s = Infinity, o = -Infinity, n = -Infinity;
    pts.forEach(function (p) { if (p[1] < w) w = p[1]; if (p[1] > o) o = p[1]; if (p[0] < s) s = p[0]; if (p[0] > n) n = p[0]; });
    return { rahmen: [w, s, o, n], poly: pts, schluessel: 'poly:' + pts.join(' ') };
  }

  // Alle z15-Kacheln, die das Gebiet berührt; null, wenn eine davon nicht abgedeckt ist.
  function gebietKacheln(g) {
    var r = g.rahmen, liste = [];
    if (!(r[0] <= r[2] && r[1] <= r[3])) nein('ungültiges Gebiet');
    var x0 = Math.floor(kx(r[0], Z)), x1 = Math.floor(kx(r[2], Z));
    var y0 = Math.floor(ky(r[3], Z)), y1 = Math.floor(ky(r[1], Z));
    if ((x1 - x0 + 1) * (y1 - y0 + 1) > NdsCache.maxKacheln * 4) return null;
    for (var x = x0; x <= x1; x++) {
      for (var y = y0; y <= y1; y++) {
        if (g.poly) {
          var w = kachelWest(x), o = kachelWest(x + 1), n = kachelNord(y), s = kachelNord(y + 1);
          if (!rahmenTrifftPoly(w, s, o, n, g.poly)) continue;
        }
        if (!abgedeckteKacheln[x + '/' + y]) return null;
        liste.push([x, y]);
      }
    }
    return liste;
  }

  function punktImPoly(lat, lon, poly) {
    var drin = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var a = poly[i], b = poly[j];
      if ((a[0] > lat) !== (b[0] > lat) && lon < (b[1] - a[1]) * (lat - a[0]) / (b[0] - a[0]) + a[1]) drin = !drin;
    }
    return drin;
  }
  function kreuz(ax, ay, bx, by, cx, cy) { return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax); }
  function streckenSchneiden(a1, a2, b1, b2) {   // Punkte als [lat, lon]; Berührung zählt
    var d1 = kreuz(b1[1], b1[0], b2[1], b2[0], a1[1], a1[0]), d2 = kreuz(b1[1], b1[0], b2[1], b2[0], a2[1], a2[0]);
    var d3 = kreuz(a1[1], a1[0], a2[1], a2[0], b1[1], b1[0]), d4 = kreuz(a1[1], a1[0], a2[1], a2[0], b2[1], b2[0]);
    if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return true;
    function auf(p, q, r) { return Math.min(p[1], q[1]) <= r[1] && r[1] <= Math.max(p[1], q[1]) && Math.min(p[0], q[0]) <= r[0] && r[0] <= Math.max(p[0], q[0]); }
    return (d1 === 0 && auf(b1, b2, a1)) || (d2 === 0 && auf(b1, b2, a2)) || (d3 === 0 && auf(a1, a2, b1)) || (d4 === 0 && auf(a1, a2, b2));
  }
  function streckeTrifftRahmen(a, b, w, s, o, n) {  // Liang-Barsky
    var t0 = 0, t1 = 1, dx = b[1] - a[1], dy = b[0] - a[0];
    var p = [-dx, dx, -dy, dy], q = [a[1] - w, o - a[1], a[0] - s, n - a[0]];
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return false; }
      else {
        var t = q[i] / p[i];
        if (p[i] < 0) { if (t > t1) return false; if (t > t0) t0 = t; }
        else { if (t < t0) return false; if (t < t1) t1 = t; }
      }
    }
    return true;
  }
  function rahmenTrifftPoly(w, s, o, n, poly) {
    for (var i = 0; i < poly.length; i++) if (poly[i][0] >= s && poly[i][0] <= n && poly[i][1] >= w && poly[i][1] <= o) return true;
    if (punktImPoly(s, w, poly) || punktImPoly(n, o, poly)) return true;
    for (var j = 0, k = poly.length - 1; j < poly.length; k = j++) if (streckeTrifftRahmen(poly[k], poly[j], w, s, o, n)) return true;
    return false;
  }

  // Overpass-Regeln: Knoten im Gebiet; Weg, wenn ein Punkt darin liegt oder eine Strecke es schneidet;
  // Relation, wenn ein Mitglied (Knoten oder Weg) das tut.
  function linieTrifft(geo, g) {
    if (!geo || !geo.length) return false;
    var r = g.rahmen, i;
    if (!g.poly) {
      for (i = 0; i < geo.length; i++) if (geo[i] && geo[i].lat >= r[1] && geo[i].lat <= r[3] && geo[i].lon >= r[0] && geo[i].lon <= r[2]) return true;
      for (i = 1; i < geo.length; i++) if (geo[i - 1] && geo[i] && streckeTrifftRahmen([geo[i - 1].lat, geo[i - 1].lon], [geo[i].lat, geo[i].lon], r[0], r[1], r[2], r[3])) return true;
      return false;
    }
    for (i = 0; i < geo.length; i++) if (geo[i] && punktImPoly(geo[i].lat, geo[i].lon, g.poly)) return true;
    var P = g.poly;
    for (i = 1; i < geo.length; i++) {
      if (!geo[i - 1] || !geo[i]) continue;
      var a = [geo[i - 1].lat, geo[i - 1].lon], b = [geo[i].lat, geo[i].lon];
      if (Math.max(a[1], b[1]) < r[0] || Math.min(a[1], b[1]) > r[2] || Math.max(a[0], b[0]) < r[1] || Math.min(a[0], b[0]) > r[3]) continue;
      for (var j = 0, k = P.length - 1; j < P.length; k = j++) if (streckenSchneiden(a, b, P[k], P[j])) return true;
    }
    return false;
  }
  function punktTrifft(lat, lon, g) {
    var r = g.rahmen;
    if (lat < r[1] || lat > r[3] || lon < r[0] || lon > r[2]) return false;
    return g.poly ? punktImPoly(lat, lon, g.poly) : true;
  }
  function trifft(el, g) {
    var r = g.rahmen, b = el.bounds;
    if (el.type === 'node') return punktTrifft(el.lat, el.lon, g);
    if (b && (b.maxlon < r[0] || b.minlon > r[2] || b.maxlat < r[1] || b.minlat > r[3])) return false;
    if (el.type === 'way') return linieTrifft(el.geometry, g);
    var ms = el.members || [];
    for (var i = 0; i < ms.length; i++) {
      var m = ms[i];
      if (m.type === 'node' && m.lat != null && punktTrifft(m.lat, m.lon, g)) return true;
      if (m.type === 'way' && linieTrifft(m.geometry, g)) return true;
    }
    return false;
  }

  // ------------------------------------------------------------ Elemente laden
  function urlsFuer(kacheln, mitRouten) {
    var m = NdsCache.manifest, urls = [], eltern = {};
    kacheln.forEach(function (k) {
      urls.push(NdsCache.basis + 'daten/' + m.kacheln.pfad.replace('{x}', k[0]).replace('{y}', k[1]));
      eltern[(k[0] >> (Z - ZG)) + '/' + (k[1] >> (Z - ZG))] = [k[0] >> (Z - ZG), k[1] >> (Z - ZG)];
    });
    Object.keys(eltern).forEach(function (s) {
      var e = eltern[s];
      urls.push(NdsCache.basis + 'daten/' + m.gross.pfad.replace('{x}', e[0]).replace('{y}', e[1]));
      if (mitRouten && m.routen) urls.push(NdsCache.basis + 'daten/' + m.routen.pfad.replace('{x}', e[0]).replace('{y}', e[1]));
    });
    return urls;
  }
  function rang(t) { return t === 'node' ? 0 : t === 'way' ? 1 : 2; }
  function sortieren(liste) { return liste.sort(function (a, b) { return rang(a.type) - rang(b.type) || a.id - b.id; }); }

  function ladenUndMischen(urls) {
    return Promise.all(urls.map(kachelLaden)).then(function (teile) {
      var gesehen = {}, alle = [];
      teile.forEach(function (els) {
        for (var i = 0; i < els.length; i++) {
          var s = els[i].type.charAt(0) + els[i].id;
          if (!gesehen[s]) { gesehen[s] = true; alle.push(els[i]); }
        }
      });
      return alle;
    });
  }
  function kopie(el) { var k = {}; for (var f in el) k[f] = el[f]; return k; }
  function alsBbox(b) {
    if (b && typeof b.getWest === 'function') return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
    if (!b || b.length !== 4) nein('bbox als [West, Süd, Ost, Nord] angeben');
    var z = b.map(Number);
    if (z.some(isNaN)) nein('bbox enthält keine Zahlen');
    return z;
  }

  NdsCache.abgedeckt = function (bbox) {
    return Promise.resolve().then(function () {
      var b = alsBbox(bbox);
      return manifestLaden().then(function () {
        var k = gebietKacheln(gebietRahmen(b[0], b[1], b[2], b[3]));
        return !!k && k.length <= NdsCache.maxKacheln;
      });
    }).catch(function (e) { merkeGrund(e); return false; });
  };

  // optionen.ohneRouten = true: Bus-/Bahn-/Radrouten (Relationen type=route) weglassen, spart viel Download
  NdsCache.elemente = function (bbox, optionen) {
    return Promise.resolve().then(function () {
      var b = alsBbox(bbox);
      return manifestLaden().then(function () {
        var g = gebietRahmen(b[0], b[1], b[2], b[3]);
        var k = gebietKacheln(g);
        if (!k) nein('Gebiet nicht (ganz) im Cache');
        if (k.length > NdsCache.maxKacheln) nein('Gebiet zu groß (' + k.length + ' Kacheln)');
        return ladenUndMischen(urlsFuer(k, !(optionen && optionen.ohneRouten))).then(function (alle) {
          return { elements: sortieren(alle.filter(function (el) { return trifft(el, g); }).map(kopie)) };
        });
      });
    }).catch(merkeGrund);
  };

  // ------------------------------------------------------------ Overpass-Abfragen (Teilmenge) selbst auswerten
  var TYPEN = { node: ['node'], way: ['way'], rel: ['relation'], relation: ['relation'], nwr: ['node', 'way', 'relation'],
    nw: ['node', 'way'], nr: ['node', 'relation'], wr: ['way', 'relation'] };

  function zerlegen(q) {
    var i = 0, n = q.length, einst = { bbox: null, json: false };
    function leer() {
      for (;;) {
        while (i < n && /\s/.test(q.charAt(i))) i++;
        if (q.substr(i, 2) === '//') { var j = q.indexOf('\n', i); i = j < 0 ? n : j + 1; continue; }
        if (q.substr(i, 2) === '/*') { var k = q.indexOf('*/', i + 2); if (k < 0) nein('Kommentar nicht geschlossen'); i = k + 2; continue; }
        return;
      }
    }
    function erwarte(c) { leer(); if (q.charAt(i) !== c) nein('erwartet "' + c + '" an Stelle ' + i); i++; }
    function wort() { leer(); var m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(q.slice(i, i + 40)); if (!m) return ''; i += m[0].length; return m[0]; }
    function text() {
      leer();
      var c = q.charAt(i), s = '';
      if (c === '"' || c === "'") {
        i++;
        while (i < n && q.charAt(i) !== c) {
          if (q.charAt(i) === '\\') {
            var e = q.charAt(i + 1);
            if (e === 'n') s += '\n'; else if (e === 't') s += '\t';
            else if (e === 'u') { s += String.fromCharCode(parseInt(q.substr(i + 2, 4), 16)); i += 4; }
            else s += e;
            i += 2;
          } else s += q.charAt(i++);
        }
        if (i >= n) nein('Zeichenkette nicht geschlossen');
        i++;
        return s;
      }
      var m = /^[^\s\[\]=!~,"'();]+/.exec(q.slice(i, i + 200));
      if (!m) nein('Wert erwartet an Stelle ' + i);
      i += m[0].length;
      return m[0];
    }
    function zahl() {
      leer();
      var m = /^-?\d+(\.\d+)?([eE][-+]?\d+)?/.exec(q.slice(i, i + 40));
      if (!m) nein('Zahl erwartet an Stelle ' + i);
      i += m[0].length;
      return parseFloat(m[0]);
    }
    function regex(muster, gross) {
      try { return new RegExp(muster, gross ? 'i' : ''); } catch (e) { nein('regulärer Ausdruck nicht lesbar: ' + muster); }
    }

    // Kopf: [out:json][timeout:…][maxsize:…][bbox:…];
    leer();
    if (q.charAt(i) === '[') {
      while (leer(), q.charAt(i) === '[') {
        i++;
        var name = wort();
        erwarte(':');
        if (name === 'out') { if (wort() !== 'json') nein('nur [out:json] wird unterstützt'); einst.json = true; }
        else if (name === 'timeout' || name === 'maxsize') zahl();
        else if (name === 'bbox') {
          var s = zahl(); erwarte(','); var w = zahl(); erwarte(','); var nn = zahl(); erwarte(','); var o = zahl();
          einst.bbox = gebietRahmen(w, s, o, nn);
        } else nein('Einstellung [' + name + ':…] nicht unterstützt');
        erwarte(']');
      }
      erwarte(';');
    }
    if (!einst.json) nein('Abfrage ohne [out:json]');

    function filterTeil() {   // nach "[" – Merkmalsfilter
      leer();
      var f = {};
      if (q.charAt(i) === '!') { i++; f.art = 'nicht'; f.k = text(); erwarte(']'); return f; }
      if (q.charAt(i) === '~') nein('Schlüssel als regulärer Ausdruck nicht unterstützt');
      f.k = text(); leer();
      var c = q.charAt(i), c2 = q.substr(i, 2);
      if (c === ']') { i++; f.art = 'hat'; return f; }
      if (c2 === '!=') { i += 2; f.art = 'ungleich'; }
      else if (c2 === '!~') { i += 2; f.art = 'passtnicht'; }
      else if (c === '=') { i++; f.art = 'gleich'; }
      else if (c === '~') { i++; f.art = 'passt'; }
      else nein('Filter nicht unterstützt an Stelle ' + i);
      f.v = text(); leer();
      var gross = false;
      if (q.charAt(i) === ',') { i++; if (wort() !== 'i') nein('Filter-Zusatz nicht unterstützt'); gross = true; }
      if (f.art === 'passt' || f.art === 'passtnicht') f.re = regex(f.v, gross);
      else if (gross) nein(',i nur bei ~ unterstützt');
      erwarte(']');
      return f;
    }
    function gebietTeil() {   // nach "(" – Gebietsfilter
      leer();
      var save = i, w = wort();
      if (w === 'poly') {
        erwarte(':');
        var z = text().trim().split(/\s+/).map(Number);
        if (z.length < 6 || z.length % 2 || z.some(isNaN)) nein('poly nicht lesbar');
        var pts = [];
        for (var k = 0; k < z.length; k += 2) pts.push([z[k], z[k + 1]]);
        erwarte(')');
        return gebietPoly(pts);
      }
      if (w === 'bbox') { erwarte(')'); if (!einst.bbox) nein('(bbox) ohne [bbox:…]'); return einst.bbox; }
      if (w) nein('Filter (' + w + ':…) nicht unterstützt');
      i = save;
      var s = zahl(); erwarte(','); var ww = zahl(); erwarte(','); var nn = zahl(); erwarte(','); var o = zahl(); erwarte(')');
      return gebietRahmen(ww, s, o, nn);
    }
    function anweisung(inUnion) {
      leer();
      if (q.charAt(i) === '(') {
        i++;
        var teile = [];
        for (;;) {
          leer();
          if (q.charAt(i) === ')') { i++; erwarte(';'); break; }
          if (i >= n) nein('Klammer nicht geschlossen');
          teile.push(anweisung(true));
        }
        return { art: 'union', teile: teile };
      }
      var w = wort();
      if (w === 'out') {
        if (inUnion) nein('out innerhalb von ( … )');
        var a = { art: 'out', menge: 'body', geom: false };
        for (;;) {
          leer();
          if (q.charAt(i) === ';') { i++; break; }
          var x = wort();
          if (x === 'body' || x === 'skel' || x === 'ids' || x === 'tags') a.menge = x;
          else if (x === 'geom') { leer(); if (q.charAt(i) === '(') nein('out geom(…) nicht unterstützt'); a.geom = true; }
          else if (x === 'qt' || x === 'asc') a.sort = x;
          else nein('out ' + (x || q.substr(i, 10)) + ' nicht unterstützt');
        }
        if (a.menge === 'tags' && a.geom) nein('out tags geom nicht unterstützt');
        return a;
      }
      if (!hat.call(TYPEN, w)) nein('Anweisung "' + (w || q.substr(i, 12)) + '" nicht unterstützt');
      var st = { art: 'abfrage', typen: TYPEN[w], filter: [], gebiete: [] };
      for (;;) {
        leer();
        var c = q.charAt(i);
        if (c === ';') { i++; break; }
        if (c === '[') { i++; st.filter.push(filterTeil()); }
        else if (c === '(') { i++; st.gebiete.push(gebietTeil()); }
        else nein('Teil "' + q.substr(i, 12) + '" nicht unterstützt');
      }
      if (!st.gebiete.length) { if (einst.bbox) st.gebiete.push(einst.bbox); else nein('Abfrage ohne Gebiet'); }
      return st;
    }
    var liste = [];
    for (;;) { leer(); if (i >= n) break; liste.push(anweisung(false)); }
    return liste;
  }

  function filterPasst(f, t) {
    var da = hat.call(t, f.k), v = da ? t[f.k] : undefined;
    switch (f.art) {
      case 'hat': return da;
      case 'nicht': return !da;
      case 'gleich': return da && v === f.v;
      case 'ungleich': return !(da && v === f.v);
      case 'passt': return da && f.re.test(v);
      case 'passtnicht': return !(da && f.re.test(v));
    }
    return false;
  }

  function abfragenSammeln(liste, ziel) {
    liste.forEach(function (a) {
      if (a.art === 'abfrage') ziel.push(a);
      else if (a.art === 'union') abfragenSammeln(a.teile, ziel);
    });
    return ziel;
  }
  // Braucht eine Anweisung die Routen-Ebene? Nur, wenn sie Relationen liefern kann und jeder verlangte Schlüssel
  // auch in Routen-Relationen vorkommt.
  function brauchtRouten(st, routenSchluessel) {
    if (st.typen.indexOf('relation') < 0) return false;
    return st.filter.every(function (f) {
      return !(f.art === 'hat' || f.art === 'gleich' || f.art === 'passt') || hat.call(routenSchluessel, f.k);
    });
  }

  function ausgeben(el, a) {
    var o = { type: el.type, id: el.id };
    if (a.menge === 'ids') return o;
    if (a.menge === 'tags') { if (el.tags) o.tags = el.tags; return o; }
    if (el.type === 'node') { o.lat = el.lat; o.lon = el.lon; }
    else if (el.type === 'way') {
      if (!a.geom) nein('Wege ohne "geom" (Knotenliste) kann der Cache nicht liefern');
      o.bounds = el.bounds; o.geometry = el.geometry;
    } else {
      if (a.geom) { o.bounds = el.bounds; o.members = el.members; }
      else o.members = (el.members || []).map(function (m) { return { type: m.type, ref: m.ref, role: m.role }; });
    }
    if (a.menge === 'body' && el.tags) o.tags = el.tags;
    return o;
  }

  NdsCache.overpass = function (abfrage) {
    return Promise.resolve().then(function () {
      var q = String(abfrage || '');
      if (/^data=/.test(q)) q = decodeURIComponent(q.slice(5).replace(/\+/g, ' '));
      var liste = zerlegen(q);
      if (!liste.some(function (a) { return a.art === 'out'; })) nein('Abfrage ohne out');
      return manifestLaden().then(function (m) {
        var abfr = abfragenSammeln(liste, []);
        var routenSchluessel = {};
        ((m.routen && m.routen.schluessel) || []).forEach(function (k) { routenSchluessel[k] = true; });
        var kacheln = {}, mitRouten = false;
        abfr.forEach(function (st) {
          st.gebiete.forEach(function (g) {
            var k = gebietKacheln(g);
            if (!k) nein('Gebiet nicht (ganz) im Cache');
            k.forEach(function (xy) { kacheln[xy[0] + '/' + xy[1]] = xy; });
          });
          if (brauchtRouten(st, routenSchluessel)) mitRouten = true;
        });
        var kl = Object.keys(kacheln).map(function (s) { return kacheln[s]; });
        if (kl.length > NdsCache.maxKacheln) nein('Gebiet zu groß (' + kl.length + ' Kacheln)');
        return ladenUndMischen(urlsFuer(kl, mitRouten)).then(function (alle) {
          var treffer = {};   // Gebiet -> Ergebnis je Element (merken, weil viele Anweisungen dasselbe Gebiet haben)
          function ausfuehren(st) {
            if (st.art === 'union') {
              var gesehen = {}, erg = [];
              st.teile.forEach(function (t) {
                ausfuehren(t).forEach(function (el) {
                  var s = el.type.charAt(0) + el.id;
                  if (!gesehen[s]) { gesehen[s] = true; erg.push(el); }
                });
              });
              return erg;
            }
            var ergebnis = [];
            for (var i = 0; i < alle.length; i++) {
              var el = alle[i];
              if (st.typen.indexOf(el.type) < 0) continue;
              var t = el.tags || {}, ok = true;
              for (var f = 0; f < st.filter.length && ok; f++) ok = filterPasst(st.filter[f], t);
              if (!ok) continue;
              for (var g = 0; g < st.gebiete.length && ok; g++) {
                var geb = st.gebiete[g], tr = treffer[geb.schluessel] || (treffer[geb.schluessel] = {});
                if (tr[i] === undefined) tr[i] = trifft(el, geb);
                ok = tr[i];
              }
              if (ok) ergebnis.push(el);
            }
            return ergebnis;
          }
          var menge = [], ausgabe = [];
          liste.forEach(function (a) {
            if (a.art === 'out') sortieren(menge.slice()).forEach(function (el) { ausgabe.push(ausgeben(el, a)); });
            else menge = ausfuehren(a);
          });
          return {
            version: 0.6,
            generator: 'nds-cache.js ' + NdsCache.version + ' (Niedersachsen-Cache)',
            osm3s: {
              timestamp_osm_base: m.datenstand,
              copyright: 'The data included in this document is from www.openstreetmap.org. The data is made available under ODbL.'
            },
            nds_cache: true,
            elements: ausgabe
          };
        });
      });
    }).catch(merkeGrund);
  };

  NdsCache.bereit = function () {
    return manifestLaden().then(function () { return true; }, function (e) { merkeGrund(e); return false; });
  };

  global.NdsCache = NdsCache;
  // Manifest schon einmal holen, damit NdsCache.stand früh bekannt ist (klein, ca. 50 KB).
  if (!global.NDS_CACHE_NICHT_VORLADEN) manifestLaden().catch(function () {});
})(typeof window !== 'undefined' ? window : globalThis);
