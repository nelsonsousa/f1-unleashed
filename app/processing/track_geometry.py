"""
Track Geometry — SVG parsing, position projection, track distance computation.

Parses track SVGs to build a polyline with precomputed segment arrays for fast
point projection. Used by TelemetryProcessor for S/F crossing detection and
lap time prediction.

No downsampling: full SVG resolution is kept for precise S/F detection.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

logger = logging.getLogger(__name__)

SVG_DIR = Path(__file__).parent.parent.parent / "static" / "images" / "tracks"

CIRCUIT_NAME_MAP = {
    "Bahrain": "Sakhir",
    "Sakhir": "Sakhir",
    "Montréal": "Montreal",
    "São_Paulo": "Sao_Paulo",
    "Spa-Francorchamps": "Spa-Francorchamps",
    "Monaco": "Monte_Carlo",
    "Miami": "Miami_Gardens",
    "Yas_Island": "Yas_Marina",
    "Yas Island": "Yas_Marina",
    # Location "Budapest" ≠ Circuit.ShortName "Hungaroring"; the client fetches
    # the SVG by ShortName, so the asset is named Hungaroring.svg — map the
    # server's Location lookup onto it so both sides resolve the same file.
    "Budapest": "Hungaroring",
}

LOCAL_WINDOW = 80  # Segments forward to search locally


@dataclass
class TrackGeometry:
    points: list[tuple[float, float]]  # Full-resolution polyline
    seg_starts: np.ndarray    # (N, 2)
    seg_dirs: np.ndarray      # (N, 2)
    seg_len_sq: np.ndarray    # (N,)
    seg_len: np.ndarray       # (N,)
    seg_cum_dist: np.ndarray  # (N,) cumulative distance at segment start
    total_dist: float

    sf_offset: float          # cumDist of S/F line
    lap_distance: float       # from SVG data-lap-distance

    corners: list[dict] = field(default_factory=list)
    first_corner_dist: float = 0.0
    last_corner_dist: float = 0.0

    sector_boundaries: list[dict] = field(default_factory=list)
    prediction_dists: list[float] = field(default_factory=list)


def parse_path_d(d_attr: str) -> list[tuple[float, float]]:
    """Parse SVG path d attribute into list of (x, y) points."""
    coords: list[tuple[float, float]] = []
    tokens = d_attr.strip().split()
    i = 0
    while i < len(tokens):
        if tokens[i] in ('M', 'L'):
            coords.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        else:
            try:
                coords.append((float(tokens[i]), float(tokens[i + 1])))
                i += 2
            except (ValueError, IndexError):
                i += 1
    return coords


def _project_brute(
    px: float, py: float,
    seg_starts: np.ndarray,
    seg_dirs: np.ndarray,
    seg_len_sq: np.ndarray,
    seg_len: np.ndarray,
    seg_cum_dist: np.ndarray,
) -> tuple[float, float, int]:
    """Project single point against all segments. Returns (cum_dist, perp_dist_sq, best_idx)."""
    pt = np.array([px, py], dtype=np.float64)
    delta = pt - seg_starts
    dot = np.sum(delta * seg_dirs, axis=1)
    safe_len_sq = np.maximum(seg_len_sq, 1e-10)
    t = np.clip(dot / safe_len_sq, 0.0, 1.0)
    closest = seg_starts + t[:, np.newaxis] * seg_dirs
    diff = pt - closest
    dist_sq = np.sum(diff * diff, axis=1)
    best = int(np.argmin(dist_sq))
    best_cum = float(seg_cum_dist[best] + t[best] * seg_len[best])
    return best_cum, float(dist_sq[best]), best


def _np_project(
    px: float, py: float,
    starts: np.ndarray, dirs: np.ndarray,
    len_sq: np.ndarray, seg_len: np.ndarray,
    cum_dist: np.ndarray,
    indices: np.ndarray | None = None,
) -> tuple[float, int, float]:
    """Vectorized projection of a single point against segment arrays.

    If indices is provided, projects against those segments only and returns
    the original segment index.
    """
    pt = np.array([px, py], dtype=np.float64)
    if indices is not None:
        s = starts[indices]
        d = dirs[indices]
        lsq = len_sq[indices]
        sl = seg_len[indices]
        cd = cum_dist[indices]
    else:
        s = starts
        d = dirs
        lsq = len_sq
        sl = seg_len
        cd = cum_dist

    delta = pt - s
    dot = np.sum(delta * d, axis=1)
    safe = np.maximum(lsq, 1e-10)
    t = np.clip(dot / safe, 0.0, 1.0)
    closest = s + t[:, np.newaxis] * d
    diff = pt - closest
    dist_sq = np.sum(diff * diff, axis=1)

    best_local = int(np.argmin(dist_sq))
    best_dsq = float(dist_sq[best_local])
    if indices is not None:
        best_seg = int(indices[best_local])
    else:
        best_seg = best_local
    best_cum = float(cd[best_local] + t[best_local] * sl[best_local])
    return best_cum, best_seg, best_dsq


def project_local(
    geo: TrackGeometry,
    x: float, y: float,
    last_seg_idx: int | None,
    window: int = LOCAL_WINDOW,
) -> tuple[float, int, float]:
    """Project point with local forward search from last known position.

    Returns (cum_dist, seg_idx, perp_dist_sq).
    Falls back to global search if no prior position or local match is poor.
    Uses numpy vectorization for performance.
    """
    n_seg = len(geo.seg_starts)

    if last_seg_idx is not None:
        w = min(window, n_seg)
        indices = np.arange(w)
        indices = (last_seg_idx + indices) % n_seg
        cum, idx, dsq = _np_project(
            x, y, geo.seg_starts, geo.seg_dirs,
            geo.seg_len_sq, geo.seg_len, geo.seg_cum_dist,
            indices,
        )
        if dsq <= 10000:
            return cum, idx, dsq

    # Global fallback
    return _np_project(
        x, y, geo.seg_starts, geo.seg_dirs,
        geo.seg_len_sq, geo.seg_len, geo.seg_cum_dist,
    )


def cum_dist_to_track_dist(cum_dist: float, geo: TrackGeometry) -> float:
    """Convert raw cumulative distance to track distance (0 = S/F line)."""
    return ((cum_dist - geo.sf_offset) % geo.total_dist + geo.total_dist) % geo.total_dist


def parse_svg(svg_path: Path) -> TrackGeometry:
    """Parse track SVG -> TrackGeometry with precomputed numpy arrays."""
    tree = ElementTree.parse(svg_path)
    root = tree.getroot()
    ns = '{http://www.w3.org/2000/svg}'

    # Parse sector paths in order
    sector_paths: dict[int, list[tuple[float, float]]] = {}
    for path_el in root.iter(f'{ns}path'):
        cls = path_el.get('class', '')
        sector_num = path_el.get('data-sector')
        if cls == 'track' and sector_num:
            sector_paths[int(sector_num)] = parse_path_d(path_el.get('d', ''))

    if not sector_paths:
        raise ValueError(f"No track sector paths found in {svg_path}")

    # Concatenate sectors, deduplicating shared endpoints
    sorted_sectors = sorted(sector_paths.keys())
    all_points: list[tuple[float, float]] = []
    for sn in sorted_sectors:
        pts = sector_paths[sn]
        if all_points and pts:
            last = all_points[-1]
            if abs(pts[0][0] - last[0]) < 0.1 and abs(pts[0][1] - last[1]) < 0.1:
                pts = pts[1:]
        all_points.extend(pts)

    # Build numpy arrays (no downsampling)
    track_pts = np.array(all_points, dtype=np.float64)
    seg_starts = track_pts[:-1]
    seg_dirs = track_pts[1:] - seg_starts
    seg_len_sq = np.sum(seg_dirs * seg_dirs, axis=1)
    seg_len = np.sqrt(seg_len_sq)
    seg_cum_dist = np.zeros(len(seg_len), dtype=np.float64)
    seg_cum_dist[1:] = np.cumsum(seg_len[:-1])
    total_dist = float(np.sum(seg_len))

    geo = TrackGeometry(
        points=all_points,
        seg_starts=seg_starts, seg_dirs=seg_dirs,
        seg_len_sq=seg_len_sq, seg_len=seg_len,
        seg_cum_dist=seg_cum_dist,
        total_dist=total_dist,
        sf_offset=0.0,
        lap_distance=0.0,
    )

    # S/F line from SVG
    for g_el in root.iter(f'{ns}g'):
        if g_el.get('id') == 'start-finish':
            sf_x = float(g_el.get('data-track-x', '0'))
            sf_y = float(g_el.get('data-track-y', '0'))
            geo.lap_distance = float(g_el.get('data-lap-distance', '0'))
            cum, _, _ = _project_brute(
                sf_x, sf_y,
                geo.seg_starts, geo.seg_dirs,
                geo.seg_len_sq, geo.seg_len, geo.seg_cum_dist,
            )
            geo.sf_offset = cum
            break

    # Corners
    corners: list[dict] = []
    for g_el in root.iter(f'{ns}g'):
        if g_el.get('id') == 'corners':
            for marker in g_el:
                if 'corner-marker' not in (marker.get('class') or ''):
                    continue
                label = marker.get('data-corner', '')
                dist = float(marker.get('data-length', '0'))
                if label:
                    corners.append({"label": label, "dist": dist})
            break

    geo.corners = corners
    if corners:
        geo.first_corner_dist = corners[0]["dist"]
        geo.last_corner_dist = max(c["dist"] for c in corners)
    else:
        geo.first_corner_dist = geo.lap_distance * 0.1
        geo.last_corner_dist = geo.lap_distance * 0.9

    # Marshal sector start distances for prediction update points (sector >= 3)
    pred_dists: list[float] = []
    for g_el in root.iter(f'{ns}g'):
        if g_el.get('id') == 'marshal-sectors':
            for marker in g_el:
                if 'marshal-sector-marker' not in (marker.get('class') or ''):
                    continue
                sector_num = int(marker.get('data-sector', '0'))
                if sector_num >= 3:
                    dist = float(marker.get('data-length', '0'))
                    pred_dists.append(dist)
            break
    pred_dists.sort()
    geo.prediction_dists = pred_dists

    # Sector start distances (% of total)
    sector_boundaries: list[dict] = []
    cum = 0.0
    for sn in sorted_sectors:
        pts = sector_paths[sn]
        start_pct = (cum / total_dist * 100) if total_dist > 0 else 0.0
        # Compute sector length
        sector_len = 0.0
        for j in range(len(pts) - 1):
            dx = pts[j + 1][0] - pts[j][0]
            dy = pts[j + 1][1] - pts[j][1]
            sector_len += (dx * dx + dy * dy) ** 0.5
        end_pct = ((cum + sector_len) / total_dist * 100) if total_dist > 0 else 0.0
        sector_boundaries.append({
            "sector": sn,
            "startPct": round(start_pct, 2),
            "endPct": round(end_pct, 2),
        })
        cum += sector_len
    geo.sector_boundaries = sector_boundaries

    logger.info(
        f"Parsed track SVG {svg_path.name}: {len(all_points)} points, "
        f"totalDist={total_dist:.0f}, lapDist={geo.lap_distance:.0f}, "
        f"{len(corners)} corners, {len(sector_boundaries)} sectors, "
        f"{len(pred_dists)} prediction sectors"
    )

    return geo


def normalize_location(name: str) -> str:
    """Normalize circuit location name for SVG filename lookup."""
    if name in CIRCUIT_NAME_MAP:
        return CIRCUIT_NAME_MAP[name]
    normalized = unicodedata.normalize('NFD', name)
    ascii_str = normalized.encode('ascii', 'ignore').decode('ascii')
    return ascii_str.replace(' ', '_')


def find_svg_path(location: str, svg_dir: Path | None = None) -> Path | None:
    """Find SVG file for a circuit location name."""
    if svg_dir is None:
        svg_dir = SVG_DIR
    svg_name = normalize_location(location)
    svg_path = svg_dir / f"{svg_name}.svg"
    return svg_path if svg_path.exists() else None
