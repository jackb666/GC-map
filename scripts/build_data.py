#!/usr/bin/env python3
"""Build the Gold Coast suburb-age dataset.

Joins gazetted Queensland locality boundaries to the curated development-era
table in ``data/suburb-eras.csv`` and writes:

  data/gold-coast-eras.geojson   the portable data artifact
  data/gold-coast-eras.js        the same payload as ``window.GC_DATA``, so
                                 index.html works from a file:// URL

Boundaries come from the Queensland locality set published in
https://github.com/tonywr71/GeoJson-Data (derived from the Queensland
Government / Geoscape administrative boundaries). Run with --fetch to clone it
into a cache directory, or point --source at an existing copy.

Usage:
    python3 scripts/build_data.py --fetch
    python3 scripts/build_data.py --source /path/to/suburb-10-qld.geojson
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCALITIES = ROOT / "scripts" / "localities.txt"
ERAS = ROOT / "data" / "suburb-eras.csv"
OUT_GEOJSON = ROOT / "data" / "gold-coast-eras.geojson"
OUT_JS = ROOT / "data" / "gold-coast-eras.js"

SOURCE_REPO = "https://github.com/tonywr71/GeoJson-Data"
SOURCE_FILE = "suburb-10-qld.geojson"
NAME_FIELD = "qld_loca_2"

# The City of Gold Coast sits well inside this box. Queensland reuses some
# locality names elsewhere in the state (there is another Gilberton up near
# Georgetown), so names alone are not a safe selector.
REGION = (152.95, -28.45, 153.65, -27.60)  # minx, miny, maxx, maxy

# Ordered oldest -> newest. Kept in one place so the map, the legend and the
# timeline all agree on the order.
ERA_ORDER = [
    "pre1900",
    "1900-1945",
    "1946-1969",
    "1970s",
    "1980s",
    "1990s-2000s",
    "2010s+",
    "rural",
]

# The year each band is treated as "arrived" by the timeline scrubber.
ERA_START = {
    "pre1900": 1865,
    "1900-1945": 1920,
    "1946-1969": 1950,
    "1970s": 1970,
    "1980s": 1980,
    "1990s-2000s": 1990,
    "2010s+": 2010,
    "rural": None,
}

ERA_LABEL = {
    "pre1900": "Before 1900",
    "1900-1945": "1900–1945",
    "1946-1969": "1946–1969",
    "1970s": "1970s",
    "1980s": "1980s",
    "1990s-2000s": "1990s–2000s",
    "2010s+": "2010s onward",
    "rural": "Rural / never urbanised",
}

ERA_BLURB = {
    "pre1900": "Colonial townships — surveyed river ports and beach villages.",
    "1900-1945": "The railway and the first beach subdivisions.",
    "1946-1969": "Canal estates and the post-war tourist boom.",
    "1970s": "The canal frontier pushes inland.",
    "1980s": "Master-planned communities.",
    "1990s-2000s": "The northern corridor opens up.",
    "2010s+": "The current growth front.",
    "rural": "Farmland, forest and national park — never built out.",
}


def ring_points(geom):
    if geom["type"] == "Polygon":
        return [r for r in geom["coordinates"]]
    return [r for poly in geom["coordinates"] for r in poly]


def ring_area(ring):
    """Signed planar area of a ring in square degrees."""
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def geom_area_km2(geom):
    """Approximate area, good enough at this latitude for a tooltip figure."""
    total = 0.0
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        for i, ring in enumerate(poly):
            lat = sum(p[1] for p in ring) / len(ring)
            kx = 111.320 * math.cos(math.radians(lat))
            ky = 110.574
            a = abs(ring_area(ring)) * kx * ky
            total += a if i == 0 else -a
    return total


def bbox(geom):
    pts = [p for ring in ring_points(geom) for p in ring]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def in_region(geom):
    minx, miny, maxx, maxy = bbox(geom)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return REGION[0] <= cx <= REGION[2] and REGION[1] <= cy <= REGION[3]


def largest_ring(geom):
    best, best_a = None, -1.0
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        a = abs(ring_area(poly[0]))
        if a > best_a:
            best, best_a = poly[0], a
    return best


def label_point(geom):
    """A point inside the suburb, biased toward the middle of its widest part.

    Grid-samples the largest ring's bounding box and keeps the interior point
    furthest from the edge, so labels avoid narrow necks and coastal slivers.
    """
    ring = largest_ring(geom)
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    best, best_d = None, -1.0
    steps = 24
    for i in range(1, steps):
        for j in range(1, steps):
            px = minx + (maxx - minx) * i / steps
            py = miny + (maxy - miny) * j / steps
            if not point_in_ring(px, py, ring):
                continue
            d = min_edge_distance(px, py, ring)
            if d > best_d:
                best, best_d = (px, py), d
    if best is None:
        return [round(sum(xs) / len(xs), 5), round(sum(ys) / len(ys), 5)]
    return [round(best[0], 5), round(best[1], 5)]


def point_in_ring(px, py, ring):
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        if (y1 > py) != (y2 > py):
            xin = x1 + (py - y1) / (y2 - y1) * (x2 - x1)
            if px < xin:
                inside = not inside
    return inside


def min_edge_distance(px, py, ring):
    best = float("inf")
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            d = math.hypot(px - x1, py - y1)
        else:
            t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
            d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
        best = min(best, d)
    return best


def round_geom(geom, nd=5):
    def rr(ring):
        out = [[round(p[0], nd), round(p[1], nd)] for p in ring]
        # drop consecutive duplicates introduced by rounding
        dedup = [out[0]]
        for p in out[1:]:
            if p != dedup[-1]:
                dedup.append(p)
        if dedup[0] != dedup[-1]:
            dedup.append(dedup[0])
        return dedup if len(dedup) >= 4 else out

    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rr(r) for r in geom["coordinates"]]}
    return {
        "type": "MultiPolygon",
        "coordinates": [[rr(r) for r in poly] for poly in geom["coordinates"]],
    }


def resolve_source(args) -> pathlib.Path:
    if args.source:
        return pathlib.Path(args.source)
    cache = pathlib.Path(args.cache).expanduser()
    target = cache / SOURCE_FILE
    if target.exists():
        return target
    if not args.fetch:
        sys.exit(
            f"No source boundaries found at {target}.\n"
            f"Re-run with --fetch to clone {SOURCE_REPO}, or pass --source."
        )
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / ".git").exists():
        print(f"cloning {SOURCE_REPO} -> {cache}")
        subprocess.run(
            ["git", "clone", "--depth", "1", SOURCE_REPO, str(cache)], check=True
        )
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="path to a Queensland locality GeoJSON")
    ap.add_argument("--fetch", action="store_true", help="clone the source repo if missing")
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "qld-boundaries"))
    args = ap.parse_args()

    wanted = [ln.strip() for ln in LOCALITIES.read_text().splitlines() if ln.strip()]
    eras = {r["locality"]: r for r in csv.DictReader(ERAS.open())}

    missing = [w for w in wanted if w not in eras]
    if missing:
        sys.exit(f"localities with no era assigned: {missing}")
    extra = [k for k in eras if k not in wanted]
    if extra:
        sys.exit(f"era rows that are not Gold Coast localities: {extra}")

    src = resolve_source(args)
    print(f"reading {src}")
    raw = json.loads(src.read_text())

    picked = {}
    for f in raw["features"]:
        geom = f.get("geometry")
        props = f.get("properties", {})
        if not geom or props.get("dt_retire"):
            continue
        name = (props.get(NAME_FIELD) or "").strip().upper()
        if name not in eras or not in_region(geom):
            continue
        if name in picked:
            sys.exit(f"{name} matched more than one polygon inside the region")
        picked[name] = geom

    absent = [w for w in wanted if w not in picked]
    if absent:
        sys.exit(f"no boundary found for: {absent}")

    features = []
    for name in wanted:
        geom = picked[name]
        row = eras[name]
        era = row["era"]
        if era not in ERA_ORDER:
            sys.exit(f"{name}: unknown era {era!r}")
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": name.title()
                    .replace("'S", "'s")
                    .replace(" Bc", " BC"),
                    "era": era,
                    "eraLabel": ERA_LABEL[era],
                    "eraStart": ERA_START[era],
                    "settled": int(row["settled"]),
                    "note": row["note"],
                    "areaKm2": round(geom_area_km2(geom), 1),
                    "label": label_point(geom),
                },
                "geometry": round_geom(geom),
            }
        )

    fc = {
        "type": "FeatureCollection",
        "name": "City of Gold Coast — suburbs by era of development",
        "meta": {
            "boundaries": "Queensland gazetted localities (Queensland Government / "
            "Geoscape administrative boundaries) via " + SOURCE_REPO,
            "eras": "Curated in data/suburb-eras.csv — editorial, not an official dataset.",
            "eraOrder": ERA_ORDER,
            "eraLabels": ERA_LABEL,
            "eraBlurbs": ERA_BLURB,
            "eraStart": ERA_START,
            "localityCount": len(features),
        },
        "features": features,
    }

    OUT_GEOJSON.write_text(json.dumps(fc, separators=(",", ":")) + "\n")
    OUT_JS.write_text(
        "/* Generated by scripts/build_data.py - do not edit by hand. */\n"
        "window.GC_DATA = " + json.dumps(fc, separators=(",", ":")) + ";\n"
    )

    counts = {}
    for f in features:
        counts[f["properties"]["era"]] = counts.get(f["properties"]["era"], 0) + 1
    print(f"wrote {len(features)} localities -> {OUT_GEOJSON.name} "
          f"({OUT_GEOJSON.stat().st_size / 1024:.0f} KB), {OUT_JS.name}")
    for e in ERA_ORDER:
        print(f"  {ERA_LABEL[e]:<26} {counts.get(e, 0):>3}")


if __name__ == "__main__":
    main()
