#!/usr/bin/env python3
"""Build the Gold Coast suburb-development dataset.

Joins gazetted locality boundaries to the dated development table in
``data/suburb-development.csv`` and writes:

  data/gold-coast-eras.geojson   the portable data artifact
  data/gold-coast-eras.js        the same payload as ``window.GC_DATA``, so
                                 index.html works from a file:// URL

Each locality carries four facts rather than a single era band: the year it was
first settled, the year its present urban development began, the year it was
substantially built out, and how confident that dating is. A locality that was
never urbanised has no development window at all.

Boundaries come from the Queensland locality set published in
https://github.com/tonywr71/GeoJson-Data (derived from the Queensland
Government / Geoscape administrative boundaries). Run with --fetch to clone it,
or point --source at an existing copy.

ABS SAL boundaries, which clip to the coastline instead of running out over the
water, can be used instead by passing --abs path/to/suburb2021.rda (from the
absmapsdata R package); see scripts/read_rdata_sf.py.

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
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCALITIES = ROOT / "scripts" / "localities.txt"
DEV_TABLE = ROOT / "data" / "suburb-development.csv"
OUT_GEOJSON = ROOT / "data" / "gold-coast-eras.geojson"
OUT_JS = ROOT / "data" / "gold-coast-eras.js"

SOURCE_REPO = "https://github.com/tonywr71/GeoJson-Data"
SOURCE_FILE = "suburb-10-qld.geojson"
NAME_FIELD = "qld_loca_2"

# The City of Gold Coast sits well inside this box. Queensland reuses some
# locality names elsewhere in the state (there is another Gilberton up near
# Georgetown), so names alone are not a safe selector.
REGION = (152.95, -28.45, 153.65, -27.60)  # minx, miny, maxx, maxy

# The year the scale and the timeline start from, and the year they end at.
YEAR_MIN = 1865
YEAR_MAX = 2025

CONFIDENCE = {
    "high": "Well documented — a survey, a subdivision or an opening date.",
    "medium": "The decade is solid; the exact years are an estimate.",
    "low": "Approximate. Acreage released gradually, with no single date.",
}


def ring_points(geom):
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
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
            a = abs(ring_area(ring)) * kx * 110.574
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
            pxx = minx + (maxx - minx) * i / steps
            pyy = miny + (maxy - miny) * j / steps
            if not point_in_ring(pxx, pyy, ring):
                continue
            d = min_edge_distance(pxx, pyy, ring)
            if d > best_d:
                best, best_d = (pxx, pyy), d
    if best is None:
        return [round(sum(xs) / len(xs), 5), round(sum(ys) / len(ys), 5)]
    return [round(best[0], 5), round(best[1], 5)]


def round_geom(geom, nd=5):
    def rr(ring):
        out = [[round(p[0], nd), round(p[1], nd)] for p in ring]
        dedup = [out[0]]
        for p in out[1:]:
            if p != dedup[-1]:
                dedup.append(p)
        if dedup[0] != dedup[-1]:
            dedup.append(dedup[0])
        return dedup if len(dedup) >= 4 else out

    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rr(r) for r in geom["coordinates"]]}
    return {"type": "MultiPolygon",
            "coordinates": [[rr(r) for r in poly] for poly in geom["coordinates"]]}


def resolve_source(args) -> pathlib.Path:
    if args.source:
        return pathlib.Path(args.source)
    cache = pathlib.Path(args.cache).expanduser()
    target = cache / SOURCE_FILE
    if target.exists():
        return target
    if not args.fetch:
        sys.exit(f"No source boundaries found at {target}.\n"
                 f"Re-run with --fetch to clone {SOURCE_REPO}, or pass --source.")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / ".git").exists():
        print(f"cloning {SOURCE_REPO} -> {cache}")
        subprocess.run(["git", "clone", "--depth", "1", SOURCE_REPO, str(cache)], check=True)
    return target


def load_boundaries(args, wanted):
    """Return {LOCALITY NAME: geometry} for the localities we want."""
    if args.abs:
        sys.path.insert(0, str(ROOT / "scripts"))
        from read_rdata_sf import RDataReader, sfg_to_geojson
        import bz2, gzip, lzma, struct

        blob = pathlib.Path(args.abs).read_bytes()
        if blob[:2] == b"\x1f\x8b":
            buf = gzip.decompress(blob)
        elif blob[:3] == b"BZh":
            buf = bz2.decompress(blob)
        elif blob[:6] == b"\xfd7zXZ\x00":
            buf = lzma.decompress(blob)
        else:
            buf = blob

        r = RDataReader(buf)
        r.header()
        flags = r.i32()
        if (flags >> 9) & 1:
            r.read(False)
        if (flags >> 10) & 1:
            r.read()
        r.i32()
        ncol = r.length()
        cols = []
        while len(cols) < ncol:
            if (struct.unpack_from(">i", r.b, r.i)[0] & 0xFF) == 19:
                break
            cols.append(r.read(True))
        names, states = cols[0], cols[3]
        # ABS disambiguates repeated names: "Southport (Qld)", "Gilberton (Gold Coast - Qld)"
        strip = re.compile(r"\s*\([^)]*\)\s*$")
        norm = [strip.sub("", n).strip().upper() if n else "" for n in names]
        keep = [norm[i] in wanted and states[i] == "Queensland" for i in range(len(names))]
        geoms = r.read_sfc(keep)
        out = {}
        for i, k in enumerate(keep):
            if not k:
                continue
            gj = sfg_to_geojson(geoms[i])
            if gj is None:
                continue
            if norm[i] in out:
                sys.exit(f"{norm[i]} matched more than one ABS row")
            out[norm[i]] = gj
        return out

    src = resolve_source(args)
    print(f"reading {src}")
    raw = json.loads(src.read_text())
    out = {}
    for f in raw["features"]:
        geom = f.get("geometry")
        props = f.get("properties", {})
        if not geom or props.get("dt_retire"):
            continue
        name = (props.get(NAME_FIELD) or "").strip().upper()
        if name not in wanted or not in_region(geom):
            continue
        if name in out:
            sys.exit(f"{name} matched more than one polygon inside the region")
        out[name] = geom
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="path to a Queensland locality GeoJSON")
    ap.add_argument("--abs", help="path to an absmapsdata suburb20XX.rda instead")
    ap.add_argument("--fetch", action="store_true", help="clone the source repo if missing")
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "qld-boundaries"))
    args = ap.parse_args()

    wanted = [ln.strip() for ln in LOCALITIES.read_text().splitlines() if ln.strip()]
    rows = {r["locality"]: r for r in csv.DictReader(DEV_TABLE.open())}

    missing = [w for w in wanted if w not in rows]
    if missing:
        sys.exit(f"localities with no development row: {missing}")
    extra = [k for k in rows if k not in wanted]
    if extra:
        sys.exit(f"development rows that are not Gold Coast localities: {extra}")

    picked = load_boundaries(args, set(wanted))
    absent = [w for w in wanted if w not in picked]
    if absent:
        sys.exit(f"no boundary found for: {absent}")

    features = []
    for name in wanted:
        row = rows[name]
        founded = int(row["founded"])
        start = int(row["start"]) if row["start"] else None
        end = int(row["end"]) if row["end"] else None
        conf = row["confidence"]

        if conf not in CONFIDENCE:
            sys.exit(f"{name}: unknown confidence {conf!r}")
        if start is None and end is not None:
            sys.exit(f"{name}: has a completion year but no start year")
        if start is not None and start < founded:
            sys.exit(f"{name}: development starts before it was settled")
        if end is not None and end < start:
            sys.exit(f"{name}: finished building before it started")
        if not (YEAR_MIN <= founded <= YEAR_MAX):
            sys.exit(f"{name}: founded {founded} is outside {YEAR_MIN}-{YEAR_MAX}")

        # For an unfinished suburb the window runs to the present day.
        closed = end if end is not None else YEAR_MAX
        mid = (start + closed) // 2 if start is not None else None

        geom = picked[name]
        features.append({
            "type": "Feature",
            "properties": {
                "name": name.title(),
                "founded": founded,
                "start": start,
                "end": end,
                "mid": mid,
                "years": (closed - start) if start is not None else None,
                "ongoing": start is not None and end is None,
                "rural": start is None,
                "confidence": conf,
                "note": row["note"],
                "areaKm2": round(geom_area_km2(geom), 1),
                "label": label_point(geom),
            },
            "geometry": round_geom(geom),
        })

    urban = [f for f in features if not f["properties"]["rural"]]
    fc = {
        "type": "FeatureCollection",
        "name": "City of Gold Coast — suburbs by date of development",
        "meta": {
            "boundaries": ("ABS SAL (absmapsdata)" if args.abs else
                           "Queensland gazetted localities (Queensland Government / "
                           "Geoscape administrative boundaries) via " + SOURCE_REPO),
            "dates": "Curated in data/suburb-development.csv — editorial, not an official dataset.",
            "yearMin": YEAR_MIN,
            "yearMax": YEAR_MAX,
            "confidence": CONFIDENCE,
            "localityCount": len(features),
            "urbanCount": len(urban),
        },
        "features": features,
    }

    OUT_GEOJSON.write_text(json.dumps(fc, separators=(",", ":")) + "\n")
    OUT_JS.write_text("/* Generated by scripts/build_data.py - do not edit by hand. */\n"
                      "window.GC_DATA = " + json.dumps(fc, separators=(",", ":")) + ";\n")

    starts = sorted(f["properties"]["start"] for f in urban)
    spans = sorted(f["properties"]["years"] for f in urban)
    print(f"wrote {len(features)} localities -> {OUT_GEOJSON.name} "
          f"({OUT_GEOJSON.stat().st_size / 1024:.0f} KB), {OUT_JS.name}")
    print(f"  urban {len(urban)}, rural {len(features) - len(urban)}")
    print(f"  development starts {starts[0]}–{starts[-1]}, median {starts[len(starts) // 2]}")
    print(f"  build-out length {spans[0]}–{spans[-1]} years, median {spans[len(spans) // 2]}")
    by_decade = {}
    for f in urban:
        d = f["properties"]["start"] // 10 * 10
        by_decade[d] = by_decade.get(d, 0) + 1
    for d in sorted(by_decade):
        print(f"    {d}s  {'█' * by_decade[d]} {by_decade[d]}")


if __name__ == "__main__":
    main()
