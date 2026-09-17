# How the Gold Coast Grew

An interactive map of all **79 gazetted localities in the City of Gold Coast**, shaded by
the era in which each one's built form was actually created — from the 1865 river port at
Nerang to the estates still going up at Pimpama today.

Open `index.html` in a browser. No build step, no server, no dependencies.

![the map](docs/preview.png)

## What the colours mean

The scale is **era of urbanisation** — the period that did most of the building, not the
date the name first appeared on a map. Those are very different things on the Gold Coast:
Pimpama had sugar mills in the 1870s and was still paddocks in 2005.

| Band | Localities | What was happening |
|---|---|---|
| Before 1900 | 4 | Colonial townships — surveyed river ports and beach villages |
| 1900–1945 | 8 | The South Coast railway and the first beach subdivisions |
| 1946–1969 | 8 | Canal estates and the post-war tourist boom |
| 1970s | 16 | The canal frontier pushes inland |
| 1980s | 7 | Master-planned communities |
| 1990s–2000s | 7 | The northern corridor opens up |
| 2010s onward | 3 | The current growth front |
| Rural / never urbanised | 26 | Farmland, forest and national park |

Twenty-six of the 79 localities have never been built out at all — and they are **55% of
the city's land area**. The Gold Coast is a thin urban ribbon on a large rural council.

Each locality also carries a **first settled** year, which is the earliest European
settlement or survey. For the farming districts of the north that is often a century
before anything suburban appeared.

## Features

- **Timeline scrubber** — drag through 1865–2025 and the city fills in. Suburbs that do
  not exist yet at the chosen year show as dashed outlines. Press **Play** to animate it.
- **Click any suburb** for its era, first settlement year, area and a note on its history.
- **Click a legend band** to isolate that era.
- **Table view** — the same data as a sortable-by-eye table, which is also the accessible
  equivalent of the map.
- Pan, wheel-zoom and pinch-zoom; light and dark themes.

## Layout

```
index.html                 the map — self-contained, loads only the data file
data/suburb-eras.csv       the era research; the editable source of truth
data/gold-coast-eras.geojson   generated: boundaries + era properties
data/gold-coast-eras.js    generated: the same payload as window.GC_DATA
scripts/localities.txt     the 79 localities that make up the LGA
scripts/build_data.py      joins boundaries to eras and writes both outputs
```

`index.html` loads `data/gold-coast-eras.js` with a plain `<script>` tag rather than
`fetch`, so the page works from a `file://` URL without a web server.

## Changing the data

The era table is meant to be argued with. Edit `data/suburb-eras.csv` — one row per
locality, with `era`, `settled` and a `note` — then rebuild:

```sh
python3 scripts/build_data.py --fetch      # clones the boundary source on first run
python3 scripts/build_data.py              # subsequent runs use the cached clone
```

The build fails loudly if a locality has no era, if an era row does not match a locality,
or if a boundary is missing, so the map and the table can never silently disagree.

Valid era values are listed in `ERA_ORDER` in `scripts/build_data.py`. Adding a band means
adding a colour step too — see the note on the ramp below.

## Sources and method

**Boundaries** are Queensland gazetted localities, from the Queensland Government /
Geoscape administrative boundaries, via
[tonywr71/GeoJson-Data](https://github.com/tonywr71/GeoJson-Data). Coordinates are rounded
to five decimal places (about a metre), which is well inside the source's own
generalisation.

**Which localities are in the LGA** is the list in `scripts/localities.txt`. Queensland's
locality dataset carries no LGA field, and the two proxies that are easy to reach —
postcode-to-LGA tables and the ABS SA4 — are both wrong at the edges here: postcode 4207
pulls in Beenleigh and Eagleby from Logan City, and SA4 "Gold Coast" pulls in Tamborine
Mountain and Beechmont from the Scenic Rim while dropping Yatala, Stapylton, Alberton and
Steiglitz. The list is therefore explicit, and checked geometrically: the union of the 79
polygons is a single contiguous region with no interior gaps, which is what the City of
Gold Coast actually is.

**Eras** are editorial. They were compiled by hand from the published history of each
locality and they are a judgement call, not an official dataset — a suburb rarely arrives
in one decade, so a band marks the period that did most of the building. Where a place has
two plausible answers (Broadbeach was subdivided in the 1920s but sat almost empty until
the 1950s) the note records both and the band follows the building.

**Colour.** The eras use a single-hue sequential blue ramp, oldest darkest, checked for
lightness monotonicity and adequate step separation in both light and dark themes.
"Rural" sits off the scale in a neutral chosen to clear the perceptual-separation floor
against the palest blue, so it never reads as "very new". Because the palest steps sit
below 3:1 against the page, every shape carries a stroke and the table view carries the
same data as text.

## Licence

The code here is yours to do as you like with. The boundary data originates with the
Queensland Government and carries its own licence terms — check the upstream source before
redistributing it.
