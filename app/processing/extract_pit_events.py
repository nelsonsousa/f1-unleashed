#!/usr/bin/env python3
"""
Extract all pit-related events across the 2025 F1 season.

Processes every cached session, detecting pit entry/exit signals from three
sources: TimingData, Position.z, and CarData.z. Outputs a single CSV at
/tmp/f1_pit_events_2025.csv for downstream analysis.

Uses numpy for vectorized track projection (batches of 10k points).

Usage:
    python3 app/processing/extract_pit_events.py
"""

from __future__ import annotations

import base64
import csv
import json
import math
import re
import sys
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import numpy as np


# =============================================================================
# Paths
# =============================================================================

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
CACHE_DIR = PROJECT_DIR / "data" / "livetiming_cache" / "2025"
SVG_DIR = PROJECT_DIR / "static" / "images" / "tracks"
OUTPUT_PATH = Path("/tmp/f1_pit_events_2025.csv")

CIRCUIT_NAME_MAP = {
    "Sakhir": "Sakhir",
    "Montréal": "Montreal",
    "São_Paulo": "Sao_Paulo",
    "Spa-Francorchamps": "Spa-Francorchamps",
    "Monaco": "Monte_Carlo",
    "Miami": "Miami_Gardens",
    "Yas_Island": "Yas_Marina",
}

PROJECTION_BATCH_SIZE = 20000
TRACK_DOWNSAMPLE_DIST = 200  # Min distance between kept points (raw F1 units)


# =============================================================================
# Track Geometry
# =============================================================================

GRID_CELL_SIZE = 2000.0  # Spatial grid cell size for fast segment lookup


@dataclass
class TrackGeometry:
    seg_starts: np.ndarray   # (N, 2) segment start points
    seg_dirs: np.ndarray     # (N, 2) segment direction vectors
    seg_len_sq: np.ndarray   # (N,)
    seg_len: np.ndarray      # (N,)
    seg_cum_dist: np.ndarray # (N,) cumulative distance at segment start
    total_dist: float
    sf_offset: float
    sector2_dist: float
    # Spatial grid for fast lookup
    grid: dict | None = None      # (cx, cy) -> list of segment indices
    grid_min: np.ndarray | None = None  # grid origin (2,)


def parse_path_d(d_attr: str) -> list[tuple[float, float]]:
    """Parse SVG path d attribute into list of (x, y) points."""
    coords = []
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


def _project_against_segs(
    points: np.ndarray,
    starts: np.ndarray,
    dirs: np.ndarray,
    len_sq: np.ndarray,
    seg_len: np.ndarray,
    cum_dist: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project M points against K segments. Returns (cum_dist, perp_dist)."""
    delta = points[:, np.newaxis, :] - starts[np.newaxis, :, :]
    dot = np.sum(delta * dirs[np.newaxis, :, :], axis=2)
    safe_len_sq = np.maximum(len_sq, 1e-10)
    t = np.clip(dot / safe_len_sq[np.newaxis, :], 0.0, 1.0)
    closest = starts[np.newaxis, :, :] + t[:, :, np.newaxis] * dirs[np.newaxis, :, :]
    diff = points[:, np.newaxis, :] - closest
    dist_sq = np.sum(diff * diff, axis=2)
    best = np.argmin(dist_sq, axis=1)
    M = points.shape[0]
    arange_M = np.arange(M)
    best_cum = cum_dist[best] + t[arange_M, best] * seg_len[best]
    best_perp = np.sqrt(dist_sq[arange_M, best])
    return best_cum, best_perp


def build_grid(geo: TrackGeometry) -> None:
    """Build spatial grid for fast segment lookup."""
    from collections import defaultdict
    N = geo.seg_starts.shape[0]
    seg_ends = geo.seg_starts + geo.seg_dirs
    seg_min = np.minimum(geo.seg_starts, seg_ends)
    seg_max = np.maximum(geo.seg_starts, seg_ends)
    grid_min = seg_min.min(axis=0) - GRID_CELL_SIZE
    geo.grid_min = grid_min
    geo.grid = defaultdict(list)
    for i in range(N):
        cx_min = int((seg_min[i, 0] - grid_min[0]) / GRID_CELL_SIZE)
        cx_max = int((seg_max[i, 0] - grid_min[0]) / GRID_CELL_SIZE)
        cy_min = int((seg_min[i, 1] - grid_min[1]) / GRID_CELL_SIZE)
        cy_max = int((seg_max[i, 1] - grid_min[1]) / GRID_CELL_SIZE)
        for cx in range(cx_min, cx_max + 1):
            for cy in range(cy_min, cy_max + 1):
                geo.grid[(cx, cy)].append(i)


def project_all(points: np.ndarray, geo: TrackGeometry) -> tuple[np.ndarray, np.ndarray]:
    """Project all points using spatial grid. Returns (raw_cum_dist, perp_dist)."""
    M = points.shape[0]
    if M == 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

    if geo.grid is None:
        build_grid(geo)

    result_cum = np.empty(M, dtype=np.float64)
    result_perp = np.full(M, np.inf, dtype=np.float64)

    # Assign each point to a grid cell — vectorized
    cell_xy = ((points - geo.grid_min) / GRID_CELL_SIZE).astype(np.int32)

    # Encode cell keys as single int for numpy grouping
    # Use a large multiplier to avoid collisions
    max_cy = cell_xy[:, 1].max() + 2 if M > 0 else 1
    cell_keys = cell_xy[:, 0].astype(np.int64) * (max_cy + 1) + cell_xy[:, 1].astype(np.int64)

    # Sort by cell key for group-by
    sort_idx = np.argsort(cell_keys)
    sorted_keys = cell_keys[sort_idx]

    # Find group boundaries
    breaks = np.where(np.diff(sorted_keys) != 0)[0] + 1
    groups = np.split(sort_idx, breaks)
    unique_keys = sorted_keys[np.concatenate([[0], breaks])]

    # Precompute neighbor segment sets for each unique cell
    for gi, key in enumerate(unique_keys):
        cx = int(key // (max_cy + 1))
        cy = int(key % (max_cy + 1))

        candidate_set = set()
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                candidate_set.update(geo.grid.get((cx + dx, cy + dy), []))
        if not candidate_set:
            candidate_set = set(range(geo.seg_starts.shape[0]))

        seg_idx = np.array(sorted(candidate_set), dtype=np.int64)
        pts_idx = groups[gi]

        cum, perp = _project_against_segs(
            points[pts_idx],
            geo.seg_starts[seg_idx],
            geo.seg_dirs[seg_idx],
            geo.seg_len_sq[seg_idx],
            geo.seg_len[seg_idx],
            geo.seg_cum_dist[seg_idx],
        )
        result_cum[pts_idx] = cum
        result_perp[pts_idx] = perp

    return result_cum, result_perp


def project_single(x: float, y: float, geo: TrackGeometry) -> tuple[float, float]:
    """Project a single point. Uses brute-force (only called during SVG parsing)."""
    pts = np.array([[x, y]], dtype=np.float64)
    cum, perp = _project_against_segs(
        pts, geo.seg_starts, geo.seg_dirs,
        geo.seg_len_sq, geo.seg_len, geo.seg_cum_dist,
    )
    return float(cum[0]), float(perp[0])


def parse_svg(svg_path: Path) -> TrackGeometry:
    """Parse track SVG → TrackGeometry with precomputed numpy arrays."""
    tree = ElementTree.parse(svg_path)
    root = tree.getroot()

    sector_paths: dict[int, list[tuple[float, float]]] = {}
    for path_el in root.iter('{http://www.w3.org/2000/svg}path'):
        cls = path_el.get('class', '')
        sector_num = path_el.get('data-sector')
        if cls == 'track' and sector_num:
            sector_paths[int(sector_num)] = parse_path_d(path_el.get('d', ''))

    if not sector_paths:
        raise ValueError(f"No track sector paths found in {svg_path}")

    sorted_sectors = sorted(sector_paths.keys())
    all_points: list[tuple[float, float]] = []
    for sn in sorted_sectors:
        pts = sector_paths[sn]
        if all_points and pts:
            last = all_points[-1]
            if abs(pts[0][0] - last[0]) < 0.01 and abs(pts[0][1] - last[1]) < 0.01:
                pts = pts[1:]
        all_points.extend(pts)

    track_pts_full = np.array(all_points, dtype=np.float64)

    # Downsample: keep points at least TRACK_DOWNSAMPLE_DIST apart
    # Always keep first and last points
    kept = [0]
    last_kept = 0
    for i in range(1, len(track_pts_full) - 1):
        dx = track_pts_full[i, 0] - track_pts_full[last_kept, 0]
        dy = track_pts_full[i, 1] - track_pts_full[last_kept, 1]
        if dx * dx + dy * dy >= TRACK_DOWNSAMPLE_DIST ** 2:
            kept.append(i)
            last_kept = i
    kept.append(len(track_pts_full) - 1)
    track_pts = track_pts_full[kept]

    seg_starts = track_pts[:-1]
    seg_dirs = track_pts[1:] - seg_starts
    seg_len_sq = np.sum(seg_dirs * seg_dirs, axis=1)
    seg_len = np.sqrt(seg_len_sq)
    seg_cum_dist = np.zeros(len(seg_len), dtype=np.float64)
    seg_cum_dist[1:] = np.cumsum(seg_len[:-1])
    total_dist = float(np.sum(seg_len))

    geo = TrackGeometry(
        seg_starts=seg_starts, seg_dirs=seg_dirs,
        seg_len_sq=seg_len_sq, seg_len=seg_len,
        seg_cum_dist=seg_cum_dist,
        total_dist=total_dist, sf_offset=0.0, sector2_dist=0.0,
    )

    for g_el in root.iter('{http://www.w3.org/2000/svg}g'):
        if g_el.get('id') == 'start-finish':
            sf_x = float(g_el.get('data-track-x', '0'))
            sf_y = float(g_el.get('data-track-y', '0'))
            geo.sf_offset, _ = project_single(sf_x, sf_y, geo)
            break

    if len(sorted_sectors) > 1:
        s2_pts = sector_paths[sorted_sectors[1]]
        if s2_pts:
            raw_d, _ = project_single(s2_pts[0][0], s2_pts[0][1], geo)
            d = raw_d - geo.sf_offset
            geo.sector2_dist = (d % total_dist + total_dist) % total_dist

    return geo


# =============================================================================
# Data Helpers
# =============================================================================

def decompress_z(encoded: str) -> dict | list:
    decoded = base64.b64decode(encoded)
    return json.loads(zlib.decompress(decoded, -zlib.MAX_WBITS))


CSV_COLUMNS = [
    'session', 'timestamp', 'car_number', 'tla', 'source', 'event',
    'track_dist', 'track_dist_pct', 'dist_to_centerline',
    'x', 'y', 'speed', 'number_of_laps', 'in_pit', 'pit_out',
]

# Message types in order-of-processing
# Position samples are collected first, projected in bulk, then events are emitted
# in original message order alongside timing/telemetry events.

# We use a tagged-union approach: all messages are parsed into a flat event stream
# with an order index, then processed sequentially.

MSG_TIMING = 0
MSG_POSITION = 1
MSG_CARDATA = 2
MSG_DRIVERLIST = 3


@dataclass
class DriverState:
    last_track_dist: float | None = None
    last_speed: int | None = None
    last_centerline_dist: float | None = None
    last_x: float | None = None
    last_y: float | None = None
    last_number_of_laps: int | None = None
    last_in_pit: bool | None = None
    last_pit_out: bool | None = None


def process_session(
    session_path: Path,
    geo: TrackGeometry,
    session_label: str,
    csv_writer: csv.writer,
) -> int:
    """Process one session via two passes: collect+project, then detect events."""
    jsonl_path = session_path / "live.jsonl"
    if not jsonl_path.exists():
        return 0

    # ── Pass 1: Parse all messages, collect position coordinates ──
    messages = []    # [(order, msg_type, payload)]
    pos_coords = []  # [(flat_idx, x, y)]  flat_idx = index into pos_samples
    pos_samples = [] # [(order, timestamp, car_num, x, y, flat_idx)]

    order = 0
    with open(jsonl_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get('Type', '')
            msg_time = msg.get('DateTime', '')

            if msg_type == 'DriverList':
                messages.append((order, MSG_DRIVERLIST, msg.get('Json', {})))
                order += 1

            elif msg_type == 'TimingData':
                messages.append((order, MSG_TIMING, (msg_time, msg.get('Json', {}))))
                order += 1

            elif msg_type == 'Position.z':
                try:
                    data = decompress_z(msg.get('Json', ''))
                except Exception:
                    continue
                positions = data.get('Position', data) if isinstance(data, dict) else data
                if not isinstance(positions, list):
                    continue

                for entry in positions:
                    timestamp = entry.get('Timestamp', msg_time)
                    entries = entry.get('Entries', {})
                    batch_entries = []
                    for car_num, pos in entries.items():
                        if not isinstance(pos, dict):
                            continue
                        x = pos.get('X')
                        y = pos.get('Y')
                        if x is None or y is None or (x == 0 and y == 0):
                            continue
                        flat_idx = len(pos_coords)
                        pos_coords.append((flat_idx, float(x), float(y)))
                        batch_entries.append((car_num, float(x), float(y), flat_idx))

                    if batch_entries:
                        messages.append((order, MSG_POSITION, (timestamp, batch_entries)))
                        order += 1

            elif msg_type == 'CarData.z':
                try:
                    data = decompress_z(msg.get('Json', ''))
                except Exception:
                    continue
                car_entries = data.get('Entries', []) if isinstance(data, dict) else []
                if not isinstance(car_entries, list):
                    continue
                for entry in car_entries:
                    timestamp = entry.get('Utc', msg_time)
                    cars = entry.get('Cars', {})
                    car_speeds = {}
                    for car_num, cd in cars.items():
                        if isinstance(cd, dict):
                            channels = cd.get('Channels', {})
                            car_speeds[car_num] = channels.get('2', 0)
                    if car_speeds:
                        messages.append((order, MSG_CARDATA, (timestamp, car_speeds)))
                        order += 1

    # ── Batch projection of all position coordinates ──
    n_pos = len(pos_coords)
    if n_pos > 0:
        all_xy = np.array([(c[1], c[2]) for c in pos_coords], dtype=np.float64)
        all_cum_dist, all_perp_dist = project_all(all_xy, geo)

        # Convert to track distance
        all_track_dist = (all_cum_dist - geo.sf_offset) % geo.total_dist
        all_track_dist = (all_track_dist + geo.total_dist) % geo.total_dist
        all_track_dist_pct = all_track_dist / geo.total_dist * 100
    else:
        all_track_dist = np.array([], dtype=np.float64)
        all_track_dist_pct = np.array([], dtype=np.float64)
        all_perp_dist = np.array([], dtype=np.float64)

    # ── Pass 2: Process events in order ──
    driver_tla: dict[str, str] = {}
    driver_state: dict[str, DriverState] = {}
    latest_speed: dict[str, int] = {}
    event_count = 0

    def write_event(
        timestamp, car_number, source, event,
        track_dist=None, track_dist_pct=None, dist_to_centerline=None,
        x=None, y=None, speed=None,
        number_of_laps=None, in_pit=None, pit_out=None,
    ):
        nonlocal event_count
        tla = driver_tla.get(car_number, car_number)
        csv_writer.writerow([
            session_label, timestamp, car_number, tla, source, event,
            f'{track_dist:.1f}' if track_dist is not None else '',
            f'{track_dist_pct:.2f}' if track_dist_pct is not None else '',
            f'{dist_to_centerline:.1f}' if dist_to_centerline is not None else '',
            f'{x:.1f}' if x is not None else '',
            f'{y:.1f}' if y is not None else '',
            speed if speed is not None else '',
            number_of_laps if number_of_laps is not None else '',
            in_pit if in_pit is not None else '',
            pit_out if pit_out is not None else '',
        ])
        event_count += 1

    for _, msg_kind, payload in messages:

        if msg_kind == MSG_DRIVERLIST:
            for car_num, info in payload.items():
                if isinstance(info, dict) and 'Tla' in info:
                    driver_tla[car_num] = info['Tla']

        elif msg_kind == MSG_TIMING:
            msg_time, data = payload
            lines = data.get('Lines', {})
            for car_num, timing in lines.items():
                if not isinstance(timing, dict):
                    continue
                state = driver_state.setdefault(car_num, DriverState())

                if 'NumberOfLaps' in timing:
                    new_val = timing['NumberOfLaps']
                    if state.last_number_of_laps is None or new_val != state.last_number_of_laps:
                        state.last_number_of_laps = new_val
                        write_event(
                            msg_time, car_num, 'timing', 'timing:number_of_laps',
                            track_dist=state.last_track_dist,
                            track_dist_pct=(state.last_track_dist / geo.total_dist * 100) if state.last_track_dist is not None else None,
                            dist_to_centerline=state.last_centerline_dist,
                            x=state.last_x, y=state.last_y,
                            speed=latest_speed.get(car_num),
                            number_of_laps=new_val,
                            in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                        )

                if 'InPit' in timing:
                    new_val = timing['InPit']
                    state.last_in_pit = new_val
                    write_event(
                        msg_time, car_num, 'timing',
                        'timing:in_pit_true' if new_val else 'timing:in_pit_false',
                        track_dist=state.last_track_dist,
                        track_dist_pct=(state.last_track_dist / geo.total_dist * 100) if state.last_track_dist is not None else None,
                        dist_to_centerline=state.last_centerline_dist,
                        x=state.last_x, y=state.last_y,
                        speed=latest_speed.get(car_num),
                        number_of_laps=state.last_number_of_laps,
                        in_pit=new_val, pit_out=state.last_pit_out,
                    )

                if 'PitOut' in timing:
                    new_val = timing['PitOut']
                    state.last_pit_out = new_val
                    write_event(
                        msg_time, car_num, 'timing',
                        'timing:pit_out_true' if new_val else 'timing:pit_out_false',
                        track_dist=state.last_track_dist,
                        track_dist_pct=(state.last_track_dist / geo.total_dist * 100) if state.last_track_dist is not None else None,
                        dist_to_centerline=state.last_centerline_dist,
                        x=state.last_x, y=state.last_y,
                        speed=latest_speed.get(car_num),
                        number_of_laps=state.last_number_of_laps,
                        in_pit=state.last_in_pit, pit_out=new_val,
                    )

        elif msg_kind == MSG_POSITION:
            timestamp, batch_entries = payload
            for car_num, x, y, flat_idx in batch_entries:
                track_dist = float(all_track_dist[flat_idx])
                track_dist_pct = float(all_track_dist_pct[flat_idx])
                centerline_dist = float(all_perp_dist[flat_idx])

                state = driver_state.setdefault(car_num, DriverState())
                spd = latest_speed.get(car_num)

                if state.last_track_dist is not None:
                    prev_td = state.last_track_dist
                    prev_cl = state.last_centerline_dist
                    td = geo.total_dist

                    # S/F crossing
                    if (prev_td / td * 100) > 80 and track_dist_pct < 20:
                        write_event(
                            timestamp, car_num, 'position', 'pos:sf_crossing',
                            track_dist=track_dist, track_dist_pct=track_dist_pct,
                            dist_to_centerline=centerline_dist,
                            x=x, y=y, speed=spd,
                            number_of_laps=state.last_number_of_laps,
                            in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                        )

                    # Sector 2 crossing
                    if geo.sector2_dist > 0 and prev_td < geo.sector2_dist <= track_dist:
                        if track_dist - prev_td < td * 0.5:
                            write_event(
                                timestamp, car_num, 'position', 'pos:sector2_crossing',
                                track_dist=track_dist, track_dist_pct=track_dist_pct,
                                dist_to_centerline=centerline_dist,
                                x=x, y=y, speed=spd,
                                number_of_laps=state.last_number_of_laps,
                                in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                            )

                    # Centerline jump > 50
                    if prev_cl is not None and abs(centerline_dist - prev_cl) > 50:
                        write_event(
                            timestamp, car_num, 'position', 'pos:centerline_jump',
                            track_dist=track_dist, track_dist_pct=track_dist_pct,
                            dist_to_centerline=centerline_dist,
                            x=x, y=y, speed=spd,
                            number_of_laps=state.last_number_of_laps,
                            in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                        )

                    # Backwards > 10%
                    td_delta = track_dist - prev_td
                    if td_delta < -td * 0.1:
                        unwrapped = td_delta + td
                        if unwrapped > td * 0.2:
                            write_event(
                                timestamp, car_num, 'position', 'pos:backwards',
                                track_dist=track_dist, track_dist_pct=track_dist_pct,
                                dist_to_centerline=centerline_dist,
                                x=x, y=y, speed=spd,
                                number_of_laps=state.last_number_of_laps,
                                in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                            )

                state.last_track_dist = track_dist
                state.last_centerline_dist = centerline_dist
                state.last_x = x
                state.last_y = y

        elif msg_kind == MSG_CARDATA:
            timestamp, car_speeds = payload
            for car_num, speed in car_speeds.items():
                state = driver_state.setdefault(car_num, DriverState())
                prev_speed = state.last_speed
                latest_speed[car_num] = speed

                if prev_speed is not None:
                    if prev_speed > 0 and speed == 0:
                        write_event(
                            timestamp, car_num, 'telemetry', 'tel:speed_to_zero',
                            track_dist=state.last_track_dist,
                            track_dist_pct=(state.last_track_dist / geo.total_dist * 100) if state.last_track_dist is not None else None,
                            dist_to_centerline=state.last_centerline_dist,
                            x=state.last_x, y=state.last_y, speed=speed,
                            number_of_laps=state.last_number_of_laps,
                            in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                        )
                    if prev_speed == 0 and speed > 0:
                        write_event(
                            timestamp, car_num, 'telemetry', 'tel:speed_from_zero',
                            track_dist=state.last_track_dist,
                            track_dist_pct=(state.last_track_dist / geo.total_dist * 100) if state.last_track_dist is not None else None,
                            dist_to_centerline=state.last_centerline_dist,
                            x=state.last_x, y=state.last_y, speed=speed,
                            number_of_laps=state.last_number_of_laps,
                            in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                        )
                    if state.last_track_dist is not None:
                        td_pct = state.last_track_dist / geo.total_dist * 100
                        if td_pct > 90 and prev_speed - speed > 30:
                            write_event(
                                timestamp, car_num, 'telemetry', 'tel:speed_drop_end_of_lap',
                                track_dist=state.last_track_dist, track_dist_pct=td_pct,
                                dist_to_centerline=state.last_centerline_dist,
                                x=state.last_x, y=state.last_y, speed=speed,
                                number_of_laps=state.last_number_of_laps,
                                in_pit=state.last_in_pit, pit_out=state.last_pit_out,
                            )

                state.last_speed = speed

    return event_count


# =============================================================================
# Circuit Name Mapping
# =============================================================================

def normalize_location(name: str) -> str:
    if name in CIRCUIT_NAME_MAP:
        return CIRCUIT_NAME_MAP[name]
    normalized = unicodedata.normalize('NFD', name)
    ascii_str = normalized.encode('ascii', 'ignore').decode('ascii')
    return ascii_str.replace(' ', '_')


def find_svg_for_event(event_dir: str) -> Path | None:
    match = re.match(r'\d+_(.*)', event_dir)
    if not match:
        return None
    svg_name = normalize_location(match.group(1))
    svg_path = SVG_DIR / f"{svg_name}.svg"
    return svg_path if svg_path.exists() else None


# =============================================================================
# Main
# =============================================================================

def main():
    if not CACHE_DIR.exists():
        print(f"Cache directory not found: {CACHE_DIR}")
        sys.exit(1)

    event_dirs = sorted(d for d in CACHE_DIR.iterdir() if d.is_dir())
    print(f"Found {len(event_dirs)} events in {CACHE_DIR}")

    total_events = 0
    total_sessions = 0
    start_time = time.time()

    with open(OUTPUT_PATH, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(CSV_COLUMNS)

        for event_dir in event_dirs:
            event_name = event_dir.name
            svg_path = find_svg_for_event(event_name)
            if svg_path is None:
                print(f"  SKIP {event_name}: no SVG found")
                continue

            try:
                geo = parse_svg(svg_path)
            except Exception as e:
                print(f"  SKIP {event_name}: SVG parse error: {e}")
                continue

            for session_dir in sorted(d for d in event_dir.iterdir() if d.is_dir()):
                session_label = f"{event_name}/{session_dir.name}"
                if not (session_dir / "live.jsonl").exists():
                    continue

                t0 = time.time()
                count = process_session(session_dir, geo, session_label, writer)
                elapsed = time.time() - t0
                total_events += count
                total_sessions += 1
                print(f"  {session_label}: {count} events ({elapsed:.1f}s)")

    elapsed_total = time.time() - start_time
    print(f"\nDone: {total_sessions} sessions, {total_events} events in {elapsed_total:.1f}s")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == '__main__':
    main()
