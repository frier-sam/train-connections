# iTrainConnections

A FlightConnections-style interactive map of India's passenger rail network.
Pick a station to see everywhere it reaches, or pick two to find direct trains
and one-change routes through major junctions — all drawn on real track geometry.

**Design system — "the station board":** Indian station name boards (yellow
enamel, black caps) name the places; signal aspects colour the trip (green =
proceed/from, red = stop/to, amber = the junction). Display type is Bricolage
Grotesque, timetable figures are IBM Plex Mono — both vendored in
`web/vendor/fonts/`, so the app stays fully self-contained. Search matches
city *and station* names with full keyboard support, journeys are shareable
via `#from=…&to=…` URLs, and well-known metro clusters carry their real city
names (Mumbai, Delhi, Kolkata, Bengaluru, …). See `AUDIT.md` for the full
audit and what was fixed.

## Features
- **8,189 stations clustered into 3,371 cities · 5,199 trains** on one dark map.
  A city groups nearby stations (Delhi = New Delhi + Nizamuddin + Anand Vihar +
  Sarai Rohilla + …), so the graph is city-to-city and a transfer may arrive at
  one station and leave from another in the same city.
- Cities coloured by **cities reachable direct** (cool teal→cyan ramp); **major
  hub cities** (real interchanges — many trains AND ≥4 lines) drawn as **amber**
  markers sized by traffic.
- **From only** → routes fan out, side panel lists every train serving the city
  (click one to isolate its route).
- **From + To** → two lists:
  - **Direct trains** (no change) serving both cities.
  - **One change, via a hub city** — ranked by least geographic detour, showing
    the two trains and the hub's traffic. Click one to draw both legs
    (green → amber) through the hub.
- **Filter by train type** (Express, Superfast, Duronto, Rajdhani, Passenger…).

## Data
Source: [DataMeet/railways](https://github.com/datameet/railways) (open).
`trains.json` stores each train as a GeoJSON **LineString following the real
track**, and every vertex sits exactly on a station coordinate — so the full
stop sequence and the connection graph are recoverable from one file.
`schedules.json` (82 MB timings) is not needed for the route map.

`build.py` also clusters stations into cities (greedy, ~10 km around the most-
connected anchor; there's no clean city field in the data, so proximity is the
robust general approach) and builds the reachability graph at the city level.
Outputs `cities.json` plus a `city` column on `stations.json`. Routes are still
drawn on per-station geometry.

## Develop
```bash
# 1. fetch raw data (once)
curl -sL https://raw.githubusercontent.com/datameet/railways/master/stations.json -o data/stations.json
curl -sL https://raw.githubusercontent.com/datameet/railways/master/trains.json   -o data/trains.json

# 2. build compact frontend data -> web/data/
npm run build          # == python3 scripts/build.py

# 3. serve locally
npm run dev            # http://localhost:8777
```

## Deploy (Cloudflare Pages)
The app is fully static — `web/` is the deploy directory. Everything it needs
(Leaflet, data JSON) is vendored/prebuilt; only the CARTO basemap tiles load
externally at runtime.

```bash
npm run build                    # regenerate web/data/ from data/
npx wrangler pages deploy web    # or: npm run deploy
```
Or via the dashboard: connect the repo, set **build command** `python3
scripts/build.py` and **output directory** `web` (or commit `web/data/*` and use
no build step). `web/_headers` sets long cache lifetimes for `/vendor` and
`/data`. Largest asset is `data/trains.json` (~2.2 MB, ~600 KB gzipped) — well
within Pages limits.

**Cache busting:** because of the long cache lifetimes, bump the version query
when you ship changes: `app.js?v=N` in `index.html` for code, and `DATA_V` at
the top of `app.js` (used on the `data/*.json` fetches) when you rebuild data.
Bumping `DATA_V` only when data actually changes lets browsers keep the cached
JSON across code-only deploys.

## Layout
```
data/            raw DataMeet GeoJSON (downloaded, not deployed)
scripts/build.py preprocessing -> compact index-based JSON
web/             deploy root (static, no build step at serve time)
  index.html · style.css · app.js
  _headers                       Cloudflare cache rules
  vendor/leaflet/                vendored Leaflet 1.9.4
  data/{stations,trains,cities}.json  generated
```

## Possible next steps
- True curved track-shape geometry (RailRadar GeoJSON / OSM rail).
- Live train positions (moving dots) — needs a live API (RailRadar).
- Multi-hop (2+ changes) routing with departure/arrival times (uses
  `schedules.json`), shareable per-journey URLs.
