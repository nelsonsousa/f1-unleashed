"""Precipitation contours from the raw ``dbz_u8`` radar composite.

Turns the 512x512 reflectivity grid into a small set of SVG-ready band polygons
(in composite pixel coords, 0..COMPOSITE_PX) plus a rain-direction alert. Computed
once per 10-min refresh and cached as JSON next to the raw tile, so replay renders
animated SVG contours over the track map without ever touching the raster.

Bands (dBZ thresholds):  clear >= -32 (base, no contour) · mist >= 5 · light >= 15
· moderate >= 30 · heavy >= 45  → contour levels 5/15/30/45 → 4 nested regions.

Contours via ``contourpy`` (already installed through matplotlib — zero new deps).
"""

import math
import re
from pathlib import Path

import contourpy
import numpy as np

# dBZ band thresholds. "clear" is the base (dBZ < 5) and gets no contour.
BANDS: tuple[tuple[str, int], ...] = (
    ("mist", 5),
    ("light", 15),
    ("moderate", 30),
    ("heavy", 45),
)
BAND_LEVELS: tuple[int, ...] = tuple(lvl for _, lvl in BANDS)
_NAME_FOR = {lvl: name for name, lvl in BANDS}

# Rough is fine (the user's spec): decimate polylines and drop specks so the JSON
# stays small. Tolerance + min ring size are in composite pixels.
SIMPLIFY_TOL = 1.5
MIN_RING_PTS = 4
MIN_RING_AREA = 16.0


def dbz_from_red(red: np.ndarray) -> np.ndarray:
    """dbz_u8 red channel → signed dBZ grid (low 7 bits = dBZ + 32)."""
    return (np.asarray(red) & 0x7F).astype(np.int16) - 32


def _rdp(pts: np.ndarray, tol: float) -> np.ndarray:
    """Iterative Douglas-Peucker simplification of an (N,2) polyline."""
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        seg = b - a
        length = math.hypot(seg[0], seg[1])
        span = pts[i + 1:j] - a
        if length == 0:
            d = np.hypot(span[:, 0], span[:, 1])
        else:
            d = np.abs(seg[0] * span[:, 1] - seg[1] * span[:, 0]) / length
        k = int(np.argmax(d))
        if d[k] > tol:
            idx = i + 1 + k
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return pts[keep]


def _ring_area(r: np.ndarray) -> float:
    x, y = r[:, 0], r[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def extract_bands(dbz: np.ndarray, levels: tuple[int, ...] = BAND_LEVELS) -> list[dict]:
    """Filled band polygons for each dBZ level, as nested rings in composite px.

    Each band = every region where ``dbz >= level`` (outer ring + holes). Bands
    are independent and nest (heavy ⊂ moderate ⊂ …); the client stacks them back
    to front. Ring 0 of each polygon is the exterior, the rest are holes
    (render with fill-rule evenodd)."""
    z = dbz.astype(float)
    gen = contourpy.contour_generator(z=z, fill_type=contourpy.FillType.OuterOffset)
    bands: list[dict] = []
    for lvl in levels:
        points, offsets = gen.filled(float(lvl), float(np.inf))
        polys: list[list] = []
        for pts, off in zip(points, offsets):
            rings = []
            for k in range(len(off) - 1):
                ring = pts[off[k]:off[k + 1]]
                if len(ring) < MIN_RING_PTS:
                    continue
                ring = _rdp(ring, SIMPLIFY_TOL)
                if len(ring) < MIN_RING_PTS or _ring_area(ring) < MIN_RING_AREA:
                    continue
                rings.append([[round(float(px), 1), round(float(py), 1)]
                              for px, py in ring])
            if rings:
                polys.append(rings)
        bands.append({"name": _NAME_FOR[lvl], "level": int(lvl), "polygons": polys})
    return bands


def rain_alert(dbz: np.ndarray, geometry: dict, mist_level: int = 5) -> dict:
    """Whether rain exists anywhere in the composite and, if so, the bearing +
    distance from the circuit to the nearest rain. Uses the FULL grid (not just
    the visible viewport) so approaching rain outside the map still alerts.

    bearing_deg: 0=N, 90=E, 180=S, 270=W (tiles are north-up; +y is south)."""
    h, w = dbz.shape
    mask = dbz >= mist_level
    cx = geometry["circuit_frac_x"] * w
    cy = geometry["circuit_frac_y"] * h
    if not mask.any():
        return {"rain": False}
    ci = int(np.clip(round(cy), 0, h - 1))
    cj = int(np.clip(round(cx), 0, w - 1))
    ys, xs = np.nonzero(mask)
    d2 = (xs - cx) ** 2 + (ys - cy) ** 2
    # Closest rain wins the alert direction; among equally-close pixels the
    # heaviest (max dBZ) breaks the tie.
    near = np.nonzero(d2 <= d2.min() + 2.0)[0]
    k = int(near[np.argmax(dbz[ys[near], xs[near]])])
    dx, dy = xs[k] - cx, ys[k] - cy
    m_per_px = geometry["width_m"] / w
    bearing = math.degrees(math.atan2(dx, -dy)) % 360.0
    return {
        "rain": True,
        "over_circuit": bool(mask[ci, cj]),
        "distance_m": round(math.hypot(dx, dy) * m_per_px),
        "bearing_deg": round(bearing),
    }


def build_contour_json(red: np.ndarray, geometry: dict) -> dict:
    """Full per-snapshot contour payload from a raw dbz_u8 red-channel grid."""
    dbz = dbz_from_red(red)
    geo = {k: geometry[k] for k in
           ("width_m", "circuit_frac_x", "circuit_frac_y", "zoom", "tiles")}
    return {
        "geometry": geo,
        "levels": {name: lvl for name, lvl in BANDS},
        "bands": extract_bands(dbz),
        "alert": rain_alert(dbz, geometry),
    }


# ── SVG generation in the TRACK's coordinate system (server-side; card Q046I51N) ──
# The client stays thin: the server converts composite-px contours into the
# track's raw coordinate system and ships ready-to-inject SVG, so the single
# #track-root transform (rotate·scale·y-flip) drives BOTH the track and the rain.
#
# Intensity reads from DROP DENSITY, not colour: every band draws the same drop
# in the same colour. The bands nest (mist ⊃ light ⊃ moderate ⊃ heavy), so each
# gives its own drop a different position within the tile — a heavy cell stacks
# all four (≈4× density), a misty cell just one. The streaks point the way the
# cloud is travelling (bearing+180); because the pattern lives in the track's
# raw frame, the track transform carries that heading to screen for free — no
# per-circuit rotation, and the drops read as "coming from / going to".
RAIN_DROP_FILL = "#dbe9ff"
RAIN_DROP_STROKE = "#5b7ea6"
_RAIN_TILE_RAW = 900.0        # raindrop tile in RAW units (1 unit = 0.1 m) → ~90 m
# Density ladder (SME): each level shows twice the rain of the one below it.
# Bands nest (heavy ⊂ moderate ⊂ light ⊂ mist), so a cell draws its band PLUS
# every shallower one and the drop counts accumulate. Each band gets its own
# drops at tile fractions chosen so they fall exactly between the shallower
# levels' — so nothing overlaps and the count doubles cleanly:
#   mist     1 drop  @ 0.5                        → cell total 1  (1×)
#   light    1 drop  @ 0.0        (offset 50%)    → cell total 2  (2×)
#   moderate 2 drops @ 0.25,0.75  (offset 25%)    → cell total 4  (4×)
#   heavy    4 drops @ .125….875  (offset 12.5%)  → cell total 8  (8×)
# The stacked fractions land on an even 8-step lattice {0,.125,.25,…,.875}.
BAND_DROPS = {
    "mist":     [0.5],
    "light":    [0.0],
    "moderate": [0.25, 0.75],
    "heavy":    [0.125, 0.375, 0.625, 0.875],
}


def track_pivot(svg_path) -> tuple[float, float] | None:
    """Parse the (cx, cy) rotation pivot (= the raw bbox centre) from a track
    SVG's #track-root ``rotate(deg, cx, cy)`` transform."""
    try:
        txt = Path(svg_path).read_text()
    except OSError:
        return None
    m = re.search(r"rotate\([-\d.]+,\s*([-\d.]+),\s*([-\d.]+)\)", txt)
    return (float(m.group(1)), float(m.group(2))) if m else None


def contours_to_track_svg(payload: dict, pivot: tuple[float, float]) -> str:
    """Composite-px contours → an SVG ``<g>`` body (defs + paths) in the track's
    RAW coordinate system. The circuit-fraction point maps to the pivot;
    composite is north-up (+px=E, +py=S); raw is +x=E, +y=N (the #track-root
    scale(1,-1) then flips it to display, same as the track)."""
    geo = payload["geometry"]
    k = (geo["width_m"] / 512.0) * 10.0            # raw units (0.1 m) per composite px
    fx512, fy512 = geo["circuit_frac_x"] * 512, geo["circuit_frac_y"] * 512
    pvx, pvy = pivot
    rawx = lambda x: pvx + (x - fx512) * k
    rawy = lambda y: pvy - (y - fy512) * k

    tile = _RAIN_TILE_RAW
    sc = tile / 16.0
    s = lambda v: round(v * sc)
    streak = (f"M0,0 C{s(-0.75)},{s(2.8)} {s(-0.75)},{s(6.6)} 0,{s(8)} "
              f"C{s(0.75)},{s(6.6)} {s(0.75)},{s(2.8)} 0,0 Z")

    # Streaks fall the way the cloud is travelling (bearing = nearest-rain
    # direction, +180 = heading). The raw frame is geographic (+y = North), so a
    # compass heading φ rotates the drop by -φ; the #track-root transform then
    # carries it to the correct on-screen angle for any circuit rotation.
    alert = payload.get("alert") or {}
    heading = (alert.get("bearing_deg", 0) + 180) % 360 if alert.get("rain") else 0
    drop_rot = -heading

    sw = max(1, round(0.3 * sc))

    def pattern(name, fractions):
        # Drops spread across the tile: x on the interleaved lattice, y offset so
        # the tile reads as a field rather than a diagonal line.
        drops = "".join(
            f'<g transform="translate({tile*f:.0f},{tile*((f+0.5)%1.0):.0f})">'
            f'<path d="{streak}" fill="{RAIN_DROP_FILL}" fill-opacity="0.9" '
            f'stroke="{RAIN_DROP_STROKE}" stroke-width="{sw}"/></g>'
            for f in fractions)
        return (f'<pattern id="rain-{name}" width="{tile:.0f}" height="{tile:.0f}" '
                f'patternUnits="userSpaceOnUse" patternTransform="rotate({drop_rot:.0f})">'
                f'{drops}'
                f'<animateTransform attributeName="patternTransform" type="translate" '
                f'additive="sum" calcMode="discrete" values="0 0;0 {tile/3:.0f};0 {2*tile/3:.0f}" '
                f'dur="0.6s" repeatCount="indefinite"/></pattern>')

    defs = "".join(pattern(n, BAND_DROPS[n]) for n, _ in BANDS)
    body = ""
    for band in payload.get("bands", []):
        if not band.get("polygons"):
            continue
        d = ""
        for rings in band["polygons"]:
            for ring in rings:
                d += "M" + "L".join(f"{rawx(x):.0f},{rawy(y):.0f}" for x, y in ring) + "Z"
        if d:
            body += f'<path d="{d}" fill="url(#rain-{band["name"]})" fill-rule="evenodd"/>'
    return f"<defs>{defs}</defs>{body}"
