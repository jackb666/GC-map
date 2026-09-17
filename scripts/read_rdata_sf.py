#!/usr/bin/env python3
"""A very small reader for R `.rda` files containing an `sf` data frame.

`pyreadr` refuses these, because an `sf` object stores its geometry as a list
column of nested lists that librdata does not model. Only a narrow slice of the
RData serialisation format is needed to get at it, so that slice is implemented
here: XDR-encoded version 2/3 streams, the vector types a data frame uses, and
attribute pairlists.

The reader is deliberately selective. A national ABS boundary file holds tens of
thousands of suburbs, and materialising every coordinate costs far more memory
than the job needs, so `keep_names` restricts geometry parsing to the rows that
matter — everything else is walked for its length and discarded.

Used by build_data.py; also runnable directly for inspection:

    python3 scripts/read_rdata_sf.py path/to/suburb2021.rda --grep SOUTHPORT
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import struct
import sys

# SEXP types that appear in an sf data frame
NILSXP, SYMSXP, LISTSXP, CHARSXP = 0, 1, 2, 9
LGLSXP, INTSXP, REALSXP, STRSXP, VECSXP = 10, 13, 14, 16, 19

# serialisation-only codes
REFSXP, NILVALUE_SXP, GLOBALENV_SXP = 255, 254, 253
UNBOUNDVALUE_SXP, MISSINGARG_SXP = 252, 251
BASENAMESPACE_SXP, NAMESPACESXP, PACKAGESXP = 250, 249, 248
PERSISTSXP, CLASSREFSXP, GENERICREFSXP = 247, 246, 245
BCREPDEF, BCREPREF, EMPTYENV_SXP, BASEENV_SXP = 244, 243, 242, 241
ATTRLANGSXP, ATTRLISTSXP, ALTREP_SXP = 240, 239, 238


class Skipped:
    """Placeholder for a value that was walked past rather than built."""
    __slots__ = ()


SKIP = Skipped()


class RDataReader:
    def __init__(self, buf: bytes):
        self.b = buf
        self.i = 0
        self.refs = []

    # ----------------------------------------------------------- primitives
    def i32(self):
        v = struct.unpack_from(">i", self.b, self.i)[0]
        self.i += 4
        return v

    def f64(self, n):
        v = struct.unpack_from(">%dd" % n, self.b, self.i)
        self.i += 8 * n
        return v

    def raw(self, n):
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def length(self):
        n = self.i32()
        if n == -1:                       # long vector: two more words
            hi, lo = self.i32(), self.i32()
            return (hi << 32) | lo
        return n

    # ----------------------------------------------------------- header
    def header(self):
        magic = self.b[:7]
        if magic[:2] == b"RD" and magic[4:5] == b"\n":
            self.i = 7 if self.b[5:7] == b"X\n" else 5
        else:
            raise ValueError("not an XDR RData stream: %r" % magic)
        ver = self.i32()
        self.i32()                        # writer version
        self.i32()                        # minimum reader version
        if ver >= 3:                      # native encoding string
            self.raw(self.i32())
        return ver

    # ----------------------------------------------------------- objects
    def read(self, keep=True):
        flags = self.i32()
        t = flags & 0xFF
        has_attr = (flags >> 9) & 1
        has_tag = (flags >> 10) & 1

        if t in (NILVALUE_SXP, NILSXP):
            return None
        if t == REFSXP:
            idx = flags >> 8
            if idx == 0:
                idx = self.i32()
            return self.refs[idx - 1]
        if t in (GLOBALENV_SXP, UNBOUNDVALUE_SXP, MISSINGARG_SXP,
                 BASENAMESPACE_SXP, EMPTYENV_SXP, BASEENV_SXP):
            return None

        if t == SYMSXP:
            name = self.read()
            self.refs.append(name)
            return name

        if t == CHARSXP:
            n = self.i32()
            if n == -1:
                return None
            return self.raw(n).decode("utf-8", "replace")

        if t in (LISTSXP, ATTRLISTSXP, ATTRLANGSXP):
            # attribute pairlists are short; walk the whole chain
            out = {}
            while True:
                if has_attr:
                    self.read(False)
                tag = self.read() if has_tag else None
                val = self.read(keep)
                if tag is not None:
                    out[tag] = val
                nxt = self.i32()
                t2 = nxt & 0xFF
                if t2 in (NILVALUE_SXP, NILSXP):
                    break
                if t2 not in (LISTSXP, ATTRLISTSXP, ATTRLANGSXP):
                    self.i -= 4
                    self.read(False)
                    break
                has_attr = (nxt >> 9) & 1
                has_tag = (nxt >> 10) & 1
            return out

        if t == ALTREP_SXP:
            info = self.read(False)
            state = self.read(keep)
            self.read(False)              # attributes
            # compact_intseq / compact_realseq carry (length, start, step)
            if isinstance(state, (list, tuple)) and len(state) == 3:
                try:
                    n, start, step = int(state[0]), state[1], state[2]
                    return [start + i * step for i in range(n)] if keep else SKIP
                except (TypeError, ValueError):
                    pass
            del info
            return state if keep else SKIP

        val = None
        if t in (LGLSXP, INTSXP):
            n = self.length()
            if keep:
                val = list(struct.unpack_from(">%di" % n, self.b, self.i))
            self.i += 4 * n
        elif t == REALSXP:
            n = self.length()
            if keep:
                val = list(self.f64(n))
            else:
                self.i += 8 * n
        elif t == STRSXP:
            n = self.length()
            val = [self.read(keep) for _ in range(n)] if keep else None
            if not keep:
                for _ in range(n):
                    self.read(False)
        elif t == VECSXP:
            n = self.length()
            val = [self.read(keep) for _ in range(n)]
            if not keep:
                val = None
        else:
            raise ValueError("unsupported SEXP type %d at byte %d" % (t, self.i))

        # attributes must be materialised when the value is being kept: a
        # coordinate matrix is a flat REALSXP that only its `dim` reshapes
        attrs = self.read(keep) if has_attr else None
        if not keep:
            return SKIP
        if attrs and isinstance(attrs, dict) and "dim" in attrs and t == REALSXP:
            dim = attrs["dim"]
            if isinstance(dim, list) and len(dim) == 2:
                rows, cols = dim
                # R matrices are column-major
                return [[val[c * rows + r] for c in range(cols)] for r in range(rows)]
        return val

    # ----------------------------------------------------------- sfc column
    def read_sfc(self, keep_flags):
        """Read an sfc list column, materialising only the flagged rows."""
        flags = self.i32()
        t = flags & 0xFF
        has_attr = (flags >> 9) & 1
        if t != VECSXP:
            raise ValueError("expected a list column for geometry, got %d" % t)
        n = self.length()
        out = []
        for k in range(n):
            out.append(self.read(bool(keep_flags[k])) if k < len(keep_flags) else self.read(False))
        if has_attr:
            self.read(False)
        return out


def load_sf(path, keep_names=None, name_probe=None):
    """Return (columns, geometry) from an .rda holding one sf data frame.

    keep_names -- an upper-cased set; only those rows keep their geometry.
    name_probe -- a name expected in the suburb-name column, used to work out
                  which string column holds the names. R writes a data frame's
                  column names *after* its columns, so the names attribute is
                  not available while the columns are being read.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    # R writes .rda with gzip, bzip2 or xz compression, or none at all
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

    # .rda top level is a tagged pairlist of the objects it stores
    flags = r.i32()
    if (flags & 0xFF) != LISTSXP:
        raise ValueError("expected a pairlist at the top level")
    if (flags >> 9) & 1:
        r.read(False)
    if (flags >> 10) & 1:
        r.read()                          # object name

    # the sf data frame itself
    flags = r.i32()
    if (flags & 0xFF) != VECSXP:
        raise ValueError("stored object is not a data frame")
    ncol = r.length()

    cols, geometry, names_col = [], None, None
    for c in range(ncol):
        peek = struct.unpack_from(">i", r.b, r.i)[0] & 0xFF
        if peek == VECSXP:                # the geometry column
            if names_col is None:
                raise ValueError("no name column found before the geometry column")
            keep_flags = [nm and nm.strip().upper() in keep_names for nm in names_col] \
                if keep_names is not None else [True] * len(names_col)
            geometry = r.read_sfc(keep_flags)
        else:
            col = r.read(True)
            cols.append(col)
            if (names_col is None and name_probe and isinstance(col, list)
                    and col and isinstance(col[0], str)):
                upper = {v.strip().upper() for v in col if isinstance(v, str)}
                if name_probe.upper() in upper:
                    names_col = col
    return names_col, cols, geometry


def sfg_to_geojson(g):
    """Convert one parsed sfg (nested lists of coordinate matrices) to GeoJSON."""
    if g is None or isinstance(g, Skipped):
        return None

    def is_ring(x):
        return (isinstance(x, list) and x and isinstance(x[0], list)
                and x[0] and isinstance(x[0][0], float))

    if is_ring(g):                                        # POLYGON is [ring, ...]
        return None
    if isinstance(g, list) and g and is_ring(g[0]):       # POLYGON
        return {"type": "Polygon", "coordinates": [[[p[0], p[1]] for p in r] for r in g]}
    if isinstance(g, list) and g and isinstance(g[0], list) and g[0] and is_ring(g[0][0]):
        return {"type": "MultiPolygon",
                "coordinates": [[[[p[0], p[1]] for p in r] for r in poly] for poly in g]}
    return None


if __name__ == "__main__":
    path = sys.argv[1]
    probe = None
    if "--grep" in sys.argv:
        probe = sys.argv[sys.argv.index("--grep") + 1]
    names, cols, geom = load_sf(path, keep_names={probe} if probe else set(),
                                name_probe=probe)
    print("rows:", len(names) if names else 0, " non-geometry columns:", len(cols))
    if probe and names:
        for k, nm in enumerate(names):
            if nm and nm.strip().upper() == probe.upper():
                gj = sfg_to_geojson(geom[k])
                pts = 0
                if gj:
                    for ring in (gj["coordinates"] if gj["type"] == "Polygon"
                                 else [r for p in gj["coordinates"] for r in p]):
                        pts += len(ring)
                print(f"  {nm}: {gj['type'] if gj else None}, {pts} vertices")
