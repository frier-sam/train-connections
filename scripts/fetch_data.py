#!/usr/bin/env python3
"""Fetch Indian Railways station + train-schedule data and emit the two
GeoJSON files that scripts/build.py consumes (data/stations.json and
data/trains.json), keeping their exact shapes.

WHY THESE SOURCES
-----------------
There is (as of 2026) no free, bulk, *current* Indian Railways schedule dump.
Every free bulk source (DataMeet 2015, data.gov.in `isl_wise_train_detail`,
Kaggle "Indian Railways", the Spin1234 mirror) traces back to a single ~2015
CRIS release: 5,208 trains, no true Vande Bharat services. Live/fresh data
only exists behind per-request APIs (RailRadar, IndianRailAPI, CRIS APIM) that
are key-gated and cannot be bulk-downloaded on a free tier (see README notes
in the accompanying report).

So this pipeline does the best a free/legal path allows:

  * TRAIN SCHEDULES + STOP SEQUENCES  -> Spin1234/IndianRailwayOpenReference
    mirror (`schedules.json`: 417k stop records, 5,208 trains, each stop has
    day/arrival/departure) plus its `trains.json` for per-train metadata
    (type, zone, distance, duration). Same ~2015 vintage as the existing data
    but a canonical, re-downloadable URL.

  * STATION COORDINATES  -> Wikidata SPARQL (property P5696 "Indian railway
    station code" + P625 coordinates). This IS genuinely fresh (live 2026,
    CC0-licensed) and covers ~8,700 of the referenced station codes; DataMeet
    coords are used only as a fallback for the ~1.5% Wikidata lacks.

The result: fresh, license-clean coordinates joined to the best available
free schedule set, fully reproducible from live URLs.

USAGE
-----
    python3 scripts/fetch_data.py            # uses cached raw files if present
    python3 scripts/fetch_data.py --refresh  # force re-download
Then:
    python3 scripts/build.py
"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

RAW_DIR = "data/raw"
OUT_STATIONS = "data/stations.json"
OUT_TRAINS = "data/trains.json"

MIRROR = ("https://raw.githubusercontent.com/Spin1234/"
          "IndianRailwayOpenReference.github.io/HEAD")
SCHEDULES_URL = MIRROR + "/schedules.json"   # ordered stop records per train
TRAINS_META_URL = MIRROR + "/trains.json"    # per-train metadata (DataMeet lineage)
STATIONS_DM_URL = MIRROR + "/stations.json"  # DataMeet station master (fallback coords/zone)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_QUERY = """
SELECT ?code ?coord ?name ?stateLabel WHERE {
  ?s wdt:P5696 ?code .
  ?s wdt:P625 ?coord .
  OPTIONAL { ?s rdfs:label ?name . FILTER(LANG(?name)="en") }
  OPTIONAL { ?s wdt:P131 ?state . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

UA = "itrainconnections-fetch/1.0 (https://github.com/; data refresh script)"


def _get(url, headers=None, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(url, path, refresh, headers=None):
    """Download url -> path unless cached (and not --refresh)."""
    if os.path.exists(path) and not refresh and os.path.getsize(path) > 0:
        print(f"  cached {path} ({os.path.getsize(path):,} bytes)")
        return open(path, "rb").read()
    print(f"  downloading {url}")
    data = _get(url, headers=headers)
    with open(path, "wb") as f:
        f.write(data)
    print(f"  saved   {path} ({len(data):,} bytes)")
    return data


def fetch_wikidata(refresh):
    path = os.path.join(RAW_DIR, "wikidata_stations.csv")
    if os.path.exists(path) and not refresh and os.path.getsize(path) > 0:
        print(f"  cached {path} ({os.path.getsize(path):,} bytes)")
        return open(path, "rb").read().decode("utf-8", "replace")
    # NOTE: request CSV via the Accept header only. Passing format=csv as a
    # query param makes WDQS return SPARQL-XML instead, which parses to garbage.
    url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": WIKIDATA_QUERY})
    print("  querying Wikidata SPARQL (P5696 station code + P625 coords)")
    text = _get(url, headers={"Accept": "text/csv"}).decode("utf-8", "replace")
    if not text.lstrip().lower().startswith("code,"):
        raise SystemExit("Wikidata did not return CSV (got: %r...)" % text[:60])
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    n = text.count("\n")
    print(f"  saved   {path} ({len(text):,} bytes, ~{n} rows)")
    return text


def parse_point(s):
    # "Point(<lng> <lat>)"
    s = s.strip()
    if not s.startswith("Point("):
        return None
    try:
        lng, lat = s[6:-1].split()
        return round(float(lng), 5), round(float(lat), 5)
    except (ValueError, IndexError):
        return None


def stop_sort_key(rec):
    # Sort by `id` ONLY. The id column is monotonically increasing along the
    # route for 5199/5208 trains. Do NOT sort by (day, id): cabin/technical
    # stops (e.g. TUGLAKABAD EAST CABIN) carry day=None mid-journey, and any
    # None->1 coercion teleports a day-2/3 stop to the start of the route,
    # producing 2,000 km chords on the map.
    i = rec.get("id")
    return i if isinstance(i, int) else 0


def haversine_km(a, b):
    """Distance in km between two (lng, lat) points."""
    import math
    lo1, la1 = a
    lo2, la2 = b
    p = math.pi / 180
    h = (math.sin((la2 - la1) * p / 2) ** 2
         + math.cos(la1 * p) * math.cos(la2 * p)
         * math.sin((lo2 - lo1) * p / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


MAX_GAP_KM = 150.0     # consecutive stops farther apart than this are suspect
WD_DM_DISAGREE_KM = 50.0   # beyond this, Wikidata's code is a mis-assignment


def drop_spikes(seq, coord):
    """Remove stops whose coordinate is inconsistent with both neighbours.

    A 'spike' is a stop >MAX_GAP_KM from prev AND next while prev->next are
    close to each other -- i.e. a wrong coordinate, not a genuine long hop.
    Endpoints get the same treatment against their two nearest stops.
    Returns (cleaned_seq, dropped_count).
    """
    dropped = 0
    changed = True
    while changed and len(seq) >= 3:
        changed = False
        # interior spikes
        for i in range(1, len(seq) - 1):
            p, s, n = coord[seq[i - 1]], coord[seq[i]], coord[seq[i + 1]]
            if (haversine_km(p, s) > MAX_GAP_KM and haversine_km(s, n) > MAX_GAP_KM
                    and haversine_km(p, n) < MAX_GAP_KM):
                del seq[i]
                dropped += 1
                changed = True
                break
        if changed:
            continue
        # endpoint spikes: first/last stop far from a locally-consistent body
        if (haversine_km(coord[seq[0]], coord[seq[1]]) > MAX_GAP_KM
                and haversine_km(coord[seq[1]], coord[seq[2]]) < MAX_GAP_KM):
            del seq[0]
            dropped += 1
            changed = True
        elif (haversine_km(coord[seq[-1]], coord[seq[-2]]) > MAX_GAP_KM
                and haversine_km(coord[seq[-2]], coord[seq[-3]]) < MAX_GAP_KM):
            del seq[-1]
            dropped += 1
            changed = True
    return seq, dropped


def main():
    refresh = "--refresh" in sys.argv
    os.makedirs(RAW_DIR, exist_ok=True)

    # keep raw downloads out of git
    gi = os.path.join(RAW_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("# raw source dumps - regenerate with scripts/fetch_data.py\n*\n!.gitignore\n")

    print("[1/4] Fetching sources ->", RAW_DIR)
    wd_csv = fetch_wikidata(refresh)
    sched_raw = download(SCHEDULES_URL, os.path.join(RAW_DIR, "schedules.json"), refresh)
    meta_raw = download(TRAINS_META_URL, os.path.join(RAW_DIR, "trains_meta.json"), refresh)
    dm_raw = download(STATIONS_DM_URL, os.path.join(RAW_DIR, "stations_datameet.json"), refresh)

    print("[2/4] Building coordinate index (Wikidata primary, DataMeet fallback)")
    # Wikidata: fresh 2026 coords, CC0
    wd = {}
    wd_name, wd_state = {}, {}
    for row in csv.DictReader(io.StringIO(wd_csv)):
        code = (row.get("code") or "").strip().upper()
        if not code:
            continue
        pt = parse_point(row.get("coord") or "")
        if not pt:
            continue
        if code not in wd:
            wd[code] = pt
            wd_name[code] = (row.get("name") or "").strip()
            wd_state[code] = (row.get("stateLabel") or "").strip()

    # DataMeet station master: name/state/zone/address + fallback coords
    dm = {}
    for feat in json.loads(dm_raw)["features"]:
        p = feat["properties"]
        code = (p.get("code") or "").strip().upper()
        if not code:
            continue
        g = feat.get("geometry")
        coord = None
        if g and g.get("type") == "Point":
            lng, lat = g["coordinates"]
            coord = (round(lng, 5), round(lat, 5))
        dm[code] = {
            "name": p.get("name"), "state": p.get("state"),
            "zone": p.get("zone"), "address": p.get("address"), "coord": coord,
        }

    schedules = json.loads(sched_raw)
    sched_codes = {(x.get("station_code") or "").strip().upper() for x in schedules}
    sched_codes.discard("")

    # Universe = every station referenced by a schedule OR in the master list.
    universe = sched_codes | set(dm)
    coord = {}          # code -> (lng, lat)  the EXACT rounded value used everywhere
    src = defaultdict(int)
    for code in universe:
        wd_c = wd.get(code)
        dm_c = dm.get(code, {}).get("coord")
        # Wikidata P5696 has a handful of mis-assigned codes (e.g. its HMPR is
        # Hamrapur/Maharashtra while IR's HMPR is Hurmujpur Halt/UP, 1300 km
        # away). When both sources exist and disagree wildly, trust DataMeet:
        # the schedule routes were originally built against it, so its coords
        # are consistent with route geometry.
        if wd_c and dm_c and haversine_km(wd_c, dm_c) > WD_DM_DISAGREE_KM:
            coord[code] = dm_c
            src["datameet(wd-mismatch)"] += 1
        elif wd_c:
            coord[code] = wd_c
            src["wikidata"] += 1
        elif dm_c:
            coord[code] = dm_c
            src["datameet"] += 1
    print(f"  coord sources: {dict(src)}  (dropped {len(universe) - len(coord)} codes w/o coords)")
    if src["wikidata"] == 0:
        raise SystemExit("No Wikidata coordinates matched - check the SPARQL "
                         "response in data/raw/wikidata_stations.csv")

    print("[3/4] Writing", OUT_STATIONS)
    station_features = []
    for code in sorted(coord):
        lng, lat = coord[code]
        d = dm.get(code, {})
        name = d.get("name") or wd_name.get(code) or code
        state = d.get("state") or wd_state.get(code) or None
        station_features.append({
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "type": "Feature",
            "properties": {
                "state": state,
                "code": code,
                "name": name,
                "zone": d.get("zone") or "",
                "address": d.get("address"),
            },
        })
    with open(OUT_STATIONS, "w") as f:
        json.dump({"type": "FeatureCollection", "features": station_features}, f)
    print(f"  {len(station_features)} station features")

    print("[4/4] Writing", OUT_TRAINS)
    meta = {f["properties"]["number"]: f["properties"]
            for f in json.loads(meta_raw)["features"]}
    by_train = defaultdict(list)
    for x in schedules:
        by_train[x["train_number"]].append(x)

    train_features = []
    dropped = 0
    spike_stops_dropped = 0
    for number, stops in by_train.items():
        stops = sorted(stops, key=stop_sort_key)
        # ordered station-code sequence, consecutive dups collapsed
        seq = []
        for s in stops:
            c = (s.get("station_code") or "").strip().upper()
            if c in coord and (not seq or seq[-1] != c):
                seq.append(c)
        # remove stops whose coordinate is a geographic outlier vs neighbours
        seq, n_spikes = drop_spikes(seq, coord)
        spike_stops_dropped += n_spikes
        # LineString vertices == exact rounded station coords (vertex rule)
        line = [[coord[c][0], coord[c][1]] for c in seq]
        if len(line) < 2:
            dropped += 1
            continue

        m = meta.get(number, {})
        # duration: prefer metadata; else derive from first departure -> last arrival
        dh = m.get("duration_h")
        dm_ = m.get("duration_m")
        if dh is None and dm_ is None:
            dh, dm_ = derive_duration(stops)
        distance = m.get("distance")
        try:
            distance = int(distance)
        except (TypeError, ValueError):
            distance = 0

        train_features.append({
            "geometry": {"type": "LineString", "coordinates": line},
            "type": "Feature",
            "properties": {
                "number": number,
                "name": m.get("name") or stops[0].get("train_name") or number,
                "type": m.get("type") or "",
                "zone": m.get("zone") or "",
                "distance": distance,
                "duration_h": dh or 0,
                "duration_m": dm_ or 0,
                "from_station_code": seq[0],
                "to_station_code": seq[-1],
            },
        })
    with open(OUT_TRAINS, "w") as f:
        json.dump({"type": "FeatureCollection", "features": train_features}, f)
    print(f"  {len(train_features)} train features ({dropped} dropped: <2 coord-matched stops, "
          f"{spike_stops_dropped} outlier stops removed)")

    # ---- validation: consecutive-vertex gaps > MAX_GAP_KM across all trains ----
    gaps = []
    for feat in train_features:
        cs = feat["geometry"]["coordinates"]
        for a, b in zip(cs, cs[1:]):
            d = haversine_km(tuple(a), tuple(b))
            if d > MAX_GAP_KM:
                gaps.append((d, feat["properties"]["number"], tuple(a), tuple(b)))
    gaps.sort(reverse=True)
    print(f"  validation: {len(gaps)} consecutive-stop gaps > {MAX_GAP_KM:.0f} km "
          f"across all trains")
    for d, num, a, b in gaps[:5]:
        print(f"    {d:7.0f} km  train {num}  {a} -> {b}")

    print("\nDone. Now run:  python3 scripts/build.py")


def derive_duration(stops):
    """Total journey time from first departure to last arrival, using day."""
    def hms(t):
        try:
            h, m, s = (t or "").split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None
    d0 = stops[0].get("day") if isinstance(stops[0].get("day"), int) else 1
    dep = hms(stops[0].get("departure"))
    dN = stops[-1].get("day") if isinstance(stops[-1].get("day"), int) else d0
    arr = hms(stops[-1].get("arrival"))
    if dep is None or arr is None:
        return None, None
    total = (dN - d0) * 24 * 60 + (arr - dep)
    if total < 0:
        total += 24 * 60
    return total // 60, total % 60


if __name__ == "__main__":
    main()
