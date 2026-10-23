"""Live map (Leaflet): live NEXRAD radar, route, airports colored by flight
category, active AIRMET/SIGMET/G-AIRMET polygons and PIREPs.

Radar tiles: Iowa Environmental Mesonet NEXRAD base reflectivity composite,
refreshed every ~5 minutes.
"""
from __future__ import annotations

import json
from html import escape
from datetime import datetime

CAT_COLORS = {"VFR": "#16a34a", "MVFR": "#2563eb", "IFR": "#dc2626", "LIFR": "#c026d3"}
HAZARD_COLORS = {
    "CONVECTIVE": "#dc2626", "IFR": "#7c3aed", "MT_OBSC": "#92400e", "MTN OBSCN": "#92400e",
    "ICE": "#0284c7", "TURB": "#ea580c", "TURB-LO": "#ea580c", "LLWS": "#0d9488", "SFC_WND": "#0d9488",
}
RADAR_URL = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png"


def live_map_html(route_paths: list[list[tuple[float, float]]], airports: list[dict],
                  advisories: list, pireps: list, now: datetime, height: int = 540) -> str:
    lats = [p[0] for path in route_paths for p in path] or [33.0]
    lons = [p[1] for path in route_paths for p in path] or [-83.0]
    lat_lo, lat_hi, lon_lo, lon_hi = min(lats) - 3, max(lats) + 3, min(lons) - 3, max(lons) + 3

    polys = []
    for a in advisories:
        if a.hazard in ("FZLVL", "TURB-HI") or not a.active(now):
            continue
        if not any(lat_lo <= la <= lat_hi and lon_lo <= lo <= lon_hi for la, lo in a.polygon):
            continue
        color = HAZARD_COLORS.get(a.hazard, HAZARD_COLORS.get(a.hazard.split("-")[0], "#64748b"))
        band = ""
        if a.base_ft is not None or a.top_ft is not None:
            band = f"{'SFC' if not a.base_ft else f'{a.base_ft:,.0f}'}–{'' if a.top_ft is None else f'{a.top_ft:,.0f}'} ft"
        until = a.valid_to.strftime("%H:%MZ") if a.valid_to else ""
        polys.append({"pts": [[la, lo] for la, lo in a.polygon], "color": color,
                      "label": f"<b>{a.kind} {a.hazard}</b><br>{band}<br>valid until {until}<br>{escape(a.raw[:160])}"})

    pts = []
    for p in pireps:
        if not (lat_lo <= p.lat <= lat_hi and lon_lo <= p.lon <= lon_hi):
            continue
        pts.append({"lat": p.lat, "lon": p.lon, "urgent": p.urgent,
                    "label": f"<b>{'URGENT ' if p.urgent else ''}PIREP</b> {escape(p.aircraft)} "
                             f"{'' if p.alt_ft is None else f'{p.alt_ft:,.0f} ft'}<br>"
                             f"{('Turb: ' + escape(p.turb) + '<br>') if p.turb else ''}"
                             f"{('Ice: ' + escape(p.icing) + '<br>') if p.icing else ''}<code>{escape(p.raw)}</code>"})

    airports = [{**a, "raw": escape(str(a.get("raw", "")))} for a in airports]
    data = {"routes": [[[la, lo] for la, lo in path] for path in route_paths],
            "airports": airports, "polys": polys, "pireps": pts, "cats": CAT_COLORS,
            "radar": RADAR_URL}
    js_data = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{{margin:0;height:100%;font-family:system-ui,sans-serif;background:#0b1220}}#m{{height:{height}px;border-radius:10px}}
.legend{{background:#152033;color:#e2e8f0;padding:6px 8px;border-radius:6px;font-size:12px;line-height:18px;box-shadow:0 1px 4px #0003}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}</style></head>
<body><div id="m"></div><script>
const D = {js_data};
const map = L.map('m', {{zoomControl:true}});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
  {{attribution:'&copy; OpenStreetMap &copy; CARTO', maxZoom:12}}).addTo(map);
const radar = L.tileLayer(D.radar + '?t=' + Date.now(), {{opacity:0.7, attribution:'NEXRAD via Iowa Environmental Mesonet'}}).addTo(map);
const adv = L.layerGroup().addTo(map), pir = L.layerGroup().addTo(map), rte = L.layerGroup().addTo(map);
D.polys.forEach(p => L.polygon(p.pts, {{color:p.color, weight:2, fillOpacity:0.12}}).bindPopup(p.label).addTo(adv));
const all = [];
D.routes.forEach(r => {{ L.polyline(r, {{color:'#38bdf8', weight:4}}).addTo(rte); r.forEach(x => all.push(x)); }});
D.airports.forEach(a => {{
  const c = D.cats[a.cat] || '#64748b';
  L.circleMarker([a.lat, a.lon], {{radius:9, color:'#e2e8f0', weight:1, fillColor:c, fillOpacity:0.95}})
   .bindPopup('<b>' + a.ident + '</b> ' + (a.cat||'no report') + '<br><code>' + (a.raw||'') + '</code>')
   .bindTooltip(a.ident + ' ' + (a.cat||''), {{permanent:true, direction:'right', offset:[8,0]}}).addTo(rte);
  all.push([a.lat, a.lon]);
}});
D.pireps.forEach(p => L.circleMarker([p.lat, p.lon], {{radius:5, color: p.urgent ? '#dc2626' : '#0f172a',
  weight:2, fillColor:'#fde047', fillOpacity:1}}).bindPopup(p.label).addTo(pir));
if (all.length) map.fitBounds(all, {{padding:[30,30]}}); else map.setView([33,-83], 6);
L.control.layers(null, {{'Radar (NEXRAD)':radar, 'AIRMET/SIGMET/G-AIRMET':adv, 'PIREPs':pir, 'Route':rte}},
  {{collapsed:false}}).addTo(map);
const st = document.createElement('style'); st.textContent = '.leaflet-tooltip{{background:#152033;color:#e2e8f0;border-color:#334155}} .leaflet-popup-content-wrapper,.leaflet-popup-tip{{background:#152033;color:#e2e8f0}} .leaflet-control-layers{{background:#152033;color:#e2e8f0}}'; document.head.appendChild(st);
const lg = L.control({{position:'bottomleft'}});
lg.onAdd = () => {{ const d = L.DomUtil.create('div','legend');
  d.innerHTML = Object.entries(D.cats).map(([k,v]) => '<i style="background:'+v+'"></i>'+k).join('&nbsp; ')
    + '<br><i style="background:#fde047;border:1px solid #000"></i>PIREP &nbsp; shaded = advisory';
  return d; }};
lg.addTo(map);
</script></body></html>"""
