// Selbsttest: prüft die frisch gebaute Seite mit nds-cache.js, bevor sie veröffentlicht wird.
// Aufruf: node pruefen/selbsttest.js site
// Startet einen kleinen Webserver auf dem Ordner und stellt ein paar Abfragen. Bricht mit Fehler ab, wenn etwas nicht stimmt.
'use strict';
const fs = require('fs'), path = require('path'), http = require('http');
const SITE = path.resolve(process.argv[2] || 'site');

const server = http.createServer((req, res) => {
  fs.readFile(path.join(SITE, decodeURIComponent(req.url.split('?')[0])), (e, d) => {
    if (e) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Access-Control-Allow-Origin': '*' }); res.end(d);
  });
});

let fehler = 0;
function pruefe(name, ok, info) {
  console.log((ok ? 'OK    ' : 'FEHLER') + ' ' + name + (info !== undefined ? ' – ' + info : ''));
  if (!ok) fehler++;
}

server.listen(0, '127.0.0.1', async () => {
  try {
    globalThis.NDS_CACHE_BASIS = 'http://127.0.0.1:' + server.address().port + '/';
    require(path.join(SITE, 'nds-cache.js'));
    const C = globalThis.NdsCache;
    pruefe('Manifest geladen', await C.bereit(), C.stand);
    pruefe('Datenstand bekannt', !!C.stand, C.stand);

    const hannover = [9.732, 52.370, 9.745, 52.378];
    pruefe('Hannover-Mitte abgedeckt', await C.abgedeckt(hannover) === true);
    pruefe('Bremen nicht abgedeckt', await C.abgedeckt([8.80, 53.07, 8.81, 53.08]) === false);
    pruefe('falsche bbox gibt false', await C.abgedeckt('Unsinn') === false);

    const el = await C.elemente(hannover);
    pruefe('elemente(Hannover-Mitte)', el && el.elements.length > 500, el ? el.elements.length + ' Elemente' : C.letzterGrund);

    const geb = await C.overpass('[out:json][timeout:60];(way["building"](52.370,9.732,52.378,9.745);relation["building"](52.370,9.732,52.378,9.745););out geom;');
    const wege = geb ? geb.elements.filter(e => e.type === 'way') : [];
    pruefe('Gebäude Hannover-Mitte', wege.length > 100, geb ? geb.elements.length + ' Elemente' : C.letzterGrund);
    pruefe('Wege mit geometry und bounds', wege.length && wege.every(w => w.geometry && w.geometry.length > 1 && w.bounds));
    pruefe('Datenstand in osm3s', geb && geb.osm3s && geb.osm3s.timestamp_osm_base === C.stand);

    const whv = '53.5185 8.103 53.5185 8.1181 53.5275 8.1181 53.5275 8.103';
    const laeden = await C.overpass(`[out:json];node["shop"](poly:"${whv}");out;`);
    pruefe('Läden Wilhelmshaven (poly)', laeden && laeden.elements.length > 10, laeden ? laeden.elements.length : C.letzterGrund);

    const routen = await C.overpass('[out:json];relation["type"="route"]["route"="bus"](53.136,8.2065,53.145,8.2215);out geom;');
    pruefe('Buslinien Oldenburg', routen && routen.elements.length > 5, routen ? routen.elements.length : C.letzterGrund);

    pruefe('make -> null', await C.overpass('[out:json];way["highway"](52.37,9.73,52.38,9.74);out geom;make x;out;') === null);
    pruefe('out count -> null', await C.overpass('[out:json];way["highway"](52.37,9.73,52.38,9.74);out count;') === null);
    pruefe('außerhalb -> null', await C.overpass('[out:json];way["highway"](53.07,8.80,53.08,8.81);out geom;') === null);
    pruefe('ohne [out:json] -> null', await C.overpass('way["highway"](52.37,9.73,52.38,9.74);out geom;') === null);
    pruefe('Wege mit out body -> null', await C.overpass('[out:json];way["highway"](52.37,9.73,52.38,9.74);out;') === null);
    console.log('geladen: ' + Math.round(C.geladeneBytes / 1024) + ' KB');
  } catch (e) {
    console.error(e); fehler++;
  }
  server.close();
  if (fehler) { console.error(fehler + ' Prüfung(en) fehlgeschlagen'); process.exit(1); }
  console.log('Alle Prüfungen bestanden');
});
