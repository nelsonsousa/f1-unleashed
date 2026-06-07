#!/usr/bin/env python3
"""
Generate SVG track maps from circuit data.

Creates SVG files with:
- Track outline (single stroke)
- Track segments colored by marshal sector
- Corner markers (circles) - hidden by default
- Marshal sector markers (triangles) - hidden by default
- Marshal light markers (squares) - hidden by default
- Car markers group (empty, for JavaScript to populate)

All coordinates in the SVG are raw F1 coordinates. A single global transform
on the root <g> element handles rotation, scaling, centering, and y-flip.
Car markers share the same coordinate space as track paths.

Usage:
    python generate_track_svgs.py                    # Generate all tracks
    python generate_track_svgs.py --circuit 2       # Generate single track
    python generate_track_svgs.py --output-dir /tmp  # Custom output directory
"""

from __future__ import annotations

import argparse
import json
import math
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR = Path(__file__).parent
UTILS_DIR = SCRIPT_DIR.parent
DATA_DIR = UTILS_DIR / "tracks" / "data"
SCHEDULE_FILE = DATA_DIR / "schedule_2026.json"
DEFAULT_OUTPUT_DIR = UTILS_DIR.parent / "static" / "images" / "tracks"

# SVG generation settings
SVG_PADDING = 80  # Padding around track in SVG units (needs to accommodate start/finish markers)
TARGET_WIDTH = 800  # Target width for SVG in pixels

# Marker sizes (in SVG units after transformation)
CORNER_RADIUS = 8
MARSHAL_SECTOR_SIZE = 6
MARSHAL_LIGHT_SIZE = 5

# Start/finish marker sizes (in SVG units)
CHECKER_SQUARE_SIZE = 6
CHECKER_LONG_SIDE = 5
CHECKER_SHORT_SIDE = 3
SIDE_OFFSET = 24
CHEVRON_GAP = 30
CHEVRON_SIZE = 10
CHEVRON_SPACING = 8


# =============================================================================
# Data Structures
# =============================================================================

class Point(NamedTuple):
    """2D point with x, y coordinates."""
    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        """Calculate Euclidean distance to another point."""
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)


class Bounds(NamedTuple):
    """Axis-aligned bounding box."""
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return Point(
            (self.min_x + self.max_x) / 2,
            (self.min_y + self.max_y) / 2
        )

    def expanded(self, padding: float) -> Bounds:
        """Return new bounds expanded by padding on all sides."""
        return Bounds(
            self.min_x - padding,
            self.min_y - padding,
            self.max_x + padding,
            self.max_y + padding
        )

    @staticmethod
    def from_points(points: list[Point]) -> Bounds:
        """Create bounds from a list of points."""
        if not points:
            return Bounds(0, 0, 1, 1)
        xs = [p.x for p in points]
        ys = [p.y for p in points]
        return Bounds(min(xs), min(ys), max(xs), max(ys))


@dataclass
class Marker:
    """Track marker (corner, marshal sector, or marshal light)."""
    position: Point
    number: int
    letter: str = ""
    angle: float = 0.0
    length: float = 0.0


@dataclass
class Transform:
    """Transformation parameters for converting F1 coordinates to SVG."""
    scale: float
    translate_x: float
    translate_y: float
    rotate_deg: float
    rotate_cx: float  # rotation center x (in original coords)
    rotate_cy: float  # rotation center y (in original coords)
    flip_y: bool
    flip_y_offset: float  # for y-flip: new_y = flip_y_offset - y

    def apply(self, p: Point) -> Point:
        """Apply transformation to a point."""
        x, y = p.x, p.y

        # 1. Rotate around center (in original coordinates)
        if self.rotate_deg != 0:
            angle_rad = math.radians(self.rotate_deg)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            dx, dy = x - self.rotate_cx, y - self.rotate_cy
            x = dx * cos_a - dy * sin_a + self.rotate_cx
            y = dx * sin_a + dy * cos_a + self.rotate_cy

        # 2. Translate
        x += self.translate_x
        y += self.translate_y

        # 3. Scale
        x *= self.scale
        y *= self.scale

        # 4. Y-flip (SVG y increases down, F1 y increases up)
        if self.flip_y:
            y = self.flip_y_offset - y

        return Point(x, y)

    def to_svg_transform(self) -> str:
        """Generate SVG transform attribute value for car markers.

        This transform converts raw F1 coordinates to match the transformed track.
        """
        # Build transform string (applied in reverse order in SVG)
        parts = []

        # 4. Y-flip
        if self.flip_y:
            parts.append(f"translate(0, {self.flip_y_offset:.2f})")
            parts.append("scale(1, -1)")

        # 3. Scale
        parts.append(f"scale({self.scale:.6f})")

        # 2. Translate
        parts.append(f"translate({self.translate_x:.2f}, {self.translate_y:.2f})")

        # 1. Rotate
        if self.rotate_deg != 0:
            parts.append(f"rotate({self.rotate_deg}, {self.rotate_cx:.2f}, {self.rotate_cy:.2f})")

        return " ".join(parts)


@dataclass
class TrackData:
    """Processed circuit data ready for SVG generation."""
    name: str
    track_points: list[Point]
    corners: list[Marker]
    marshal_sectors: list[Marker]
    marshal_lights: list[Marker]
    rotation: float = 0.0
    sector_indices: list[tuple[int, int]] = field(default_factory=list)

    @classmethod
    def from_circuit_json(cls, data: dict) -> TrackData:
        """Load track data from circuit JSON."""
        name = data.get("circuit_name", "Unknown")
        rotation = data.get("rotation", 0)

        # Extract track coordinates
        track_x = list(data.get("track_x", []))
        track_y = list(data.get("track_y", []))

        if not track_x or not track_y:
            raise ValueError("No track data available")

        track_points = [Point(x, y) for x, y in zip(track_x, track_y)]

        # Extract markers
        corners = [
            Marker(
                position=Point(c["position"]["x"], c["position"]["y"]),
                number=c.get("number", 0),
                letter=c.get("letter", ""),
                angle=c.get("angle", 0),
                length=c.get("length", 0),
            )
            for c in data.get("corners", [])
        ]

        marshal_sectors = [
            Marker(
                position=Point(s["position"]["x"], s["position"]["y"]),
                number=s.get("number", 0),
                angle=s.get("angle", 0),
                length=s.get("length", 0),
            )
            for s in data.get("marshal_sectors", [])
        ]

        marshal_lights = [
            Marker(
                position=Point(l["position"]["x"], l["position"]["y"]),
                number=l.get("number", 0),
                angle=l.get("angle", 0),
            )
            for l in data.get("marshal_lights", [])
        ]

        track_data = cls(
            name=name,
            track_points=track_points,
            corners=corners,
            marshal_sectors=marshal_sectors,
            marshal_lights=marshal_lights,
            rotation=rotation,
        )

        # Process sectors
        track_data.process_sectors()

        return track_data

    def get_bounds(self) -> Bounds:
        """Calculate bounds of track points."""
        return Bounds.from_points(self.track_points)

    def process_sectors(self) -> None:
        """Insert sector points into track and build sector indices."""
        self.marshal_sectors.sort(key=lambda m: m.number)

        # Insert marshal sector positions into track
        track_x = [p.x for p in self.track_points]
        track_y = [p.y for p in self.track_points]

        new_x, new_y, sector_to_index = _insert_sector_points(
            track_x, track_y, self.marshal_sectors
        )

        self.track_points = [Point(x, y) for x, y in zip(new_x, new_y)]
        self.sector_indices = [
            (s.number, sector_to_index[s.number])
            for s in self.marshal_sectors
        ]
        self.sector_indices.sort(key=lambda x: x[1])


# =============================================================================
# Geometry Utilities
# =============================================================================

def _point_to_segment_distance(px: float, py: float,
                                x1: float, y1: float,
                                x2: float, y2: float) -> float:
    """Distance from point (px, py) to line segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1

    if dx == 0 and dy == 0:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    closest_x, closest_y = x1 + t * dx, y1 + t * dy

    return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)


def _find_empty_side(p1: Point, track_points: list[Point],
                      search_distance: float = 500) -> int:
    """Find which side of track has more space at P1.

    Returns 1 for left, -1 for right.
    """
    look_ahead = min(10, len(track_points) - 1)
    p_ahead = track_points[look_ahead]

    dx, dy = p_ahead.x - p1.x, p_ahead.y - p1.y
    length = math.sqrt(dx * dx + dy * dy)
    if length == 0:
        return 1

    # Perpendicular vector
    perp_x, perp_y = -dy / length, dx / length

    left_end = Point(p1.x + perp_x * search_distance, p1.y + perp_y * search_distance)
    right_end = Point(p1.x - perp_x * search_distance, p1.y - perp_y * search_distance)

    left_min = right_min = search_distance
    n = len(track_points)

    for i in range(20, n - 21):
        intersects, dist = _line_segment_intersection(
            p1, left_end, track_points[i], track_points[i + 1]
        )
        if intersects and 1 < dist < left_min:
            left_min = dist

        intersects, dist = _line_segment_intersection(
            p1, right_end, track_points[i], track_points[i + 1]
        )
        if intersects and 1 < dist < right_min:
            right_min = dist

    return 1 if left_min >= right_min else -1


def _line_segment_intersection(p1: Point, p2: Point,
                                p3: Point, p4: Point) -> tuple[bool, float]:
    """Check if segments p1-p2 and p3-p4 intersect."""
    d1x, d1y = p2.x - p1.x, p2.y - p1.y
    d2x, d2y = p4.x - p3.x, p4.y - p3.y

    cross = d1x * d2y - d1y * d2x
    if abs(cross) < 1e-10:
        return False, float('inf')

    dx, dy = p3.x - p1.x, p3.y - p1.y
    t = (dx * d2y - dy * d2x) / cross
    u = (dx * d1y - dy * d1x) / cross

    if 0 <= t <= 1 and 0 <= u <= 1:
        return True, t * math.sqrt(d1x * d1x + d1y * d1y)

    return False, float('inf')


def _find_closest_track_index(marker_pos: Point,
                               track_x: list[float],
                               track_y: list[float]) -> tuple[int, float]:
    """Find index of closest track point to marker."""
    min_dist = float('inf')
    closest_idx = 0

    for i, (tx, ty) in enumerate(zip(track_x, track_y)):
        dist = math.sqrt((marker_pos.x - tx) ** 2 + (marker_pos.y - ty) ** 2)
        if dist < min_dist:
            min_dist = dist
            closest_idx = i

    return closest_idx, min_dist


def _insert_sector_points(track_x: list[float], track_y: list[float],
                           marshal_sectors: list[Marker],
                           tolerance: float = 0.5
                           ) -> tuple[list[float], list[float], dict[int, int]]:
    """Insert marshal sector positions into track coordinates."""
    new_x, new_y = list(track_x), list(track_y)
    insertions = []

    for sector in marshal_sectors:
        closest_idx, dist = _find_closest_track_index(sector.position, new_x, new_y)

        if dist <= tolerance:
            continue

        sx, sy = sector.position.x, sector.position.y
        n = len(new_x)
        prev_idx, next_idx = (closest_idx - 1) % n, (closest_idx + 1) % n

        dist_before = _point_to_segment_distance(
            sx, sy, new_x[prev_idx], new_y[prev_idx], new_x[closest_idx], new_y[closest_idx]
        )
        dist_after = _point_to_segment_distance(
            sx, sy, new_x[closest_idx], new_y[closest_idx], new_x[next_idx], new_y[next_idx]
        )

        insert_pos = closest_idx + 1 if dist_after < dist_before else closest_idx
        insertions.append((sector.number, insert_pos, sx, sy))

    # Insert from end to preserve indices
    for _, pos, sx, sy in sorted(insertions, key=lambda x: x[1], reverse=True):
        new_x.insert(pos, sx)
        new_y.insert(pos, sy)

    # Build sector -> index map
    sector_to_index = {
        sector.number: _find_closest_track_index(sector.position, new_x, new_y)[0]
        for sector in marshal_sectors
    }

    return new_x, new_y, sector_to_index


# =============================================================================
# SVG Generation
# =============================================================================

class SVGBuilder:
    """Builds SVG content with transformed coordinates."""

    def __init__(self, track_data: TrackData, target_width: float = TARGET_WIDTH):
        self.track = track_data
        self.target_width = target_width
        self.transform = self._compute_transform()
        self.inv_scale = 1.0 / self.transform.scale

        # Transform all track points (needed only for viewBox bounds calculation)
        self.transformed_points = [self.transform.apply(p) for p in track_data.track_points]
        self.transformed_bounds = Bounds.from_points(self.transformed_points).expanded(SVG_PADDING)

    def _compute_transform(self) -> Transform:
        """Compute transformation to fit track in target size."""
        bounds = self.track.get_bounds()
        center = bounds.center

        # First, compute bounds after rotation (to determine scale)
        rotation = self.track.rotation
        if rotation != 0:
            angle_rad = math.radians(rotation)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            rotated_points = []
            for p in self.track.track_points:
                dx, dy = p.x - center.x, p.y - center.y
                rx = dx * cos_a - dy * sin_a + center.x
                ry = dx * sin_a + dy * cos_a + center.y
                rotated_points.append(Point(rx, ry))
            rotated_bounds = Bounds.from_points(rotated_points)
        else:
            rotated_bounds = bounds

        # Calculate scale to fit target width
        scale = self.target_width / max(rotated_bounds.width, rotated_bounds.height)

        # Calculate translation to center at origin before scaling
        translate_x = -center.x
        translate_y = -center.y

        # After scale, the bounds will be centered at origin
        # We need to translate to positive coordinates and add padding
        final_half_width = rotated_bounds.width * scale / 2
        final_half_height = rotated_bounds.height * scale / 2

        # Y-flip offset (after all other transforms)
        flip_y_offset = final_half_height * 2 + SVG_PADDING * 2

        return Transform(
            scale=scale,
            translate_x=translate_x,
            translate_y=translate_y,
            rotate_deg=rotation,
            rotate_cx=center.x,
            rotate_cy=center.y,
            flip_y=True,
            flip_y_offset=flip_y_offset,
        )

    def _compute_lap_distance(self) -> float:
        """Compute total lap distance by summing segment lengths of the closed track polyline."""
        points = self.track.track_points
        total = 0.0
        for i in range(len(points) - 1):
            total += points[i].distance_to(points[i + 1])
        # Close the loop: last point back to first
        if len(points) > 1:
            total += points[-1].distance_to(points[0])
        return total

    def generate(self) -> str:
        """Generate complete SVG content."""
        # Generate SVG elements with raw F1 coordinates
        outline_paths = self._generate_sector_paths("track-outline")
        sector_paths = self._generate_sector_paths("track")
        corner_markers = self._generate_corner_markers()
        sector_markers = self._generate_sector_markers()
        light_markers = self._generate_light_markers()
        start_finish = self._generate_start_finish()

        # ViewBox (from transformed bounds, for proper display sizing)
        b = self.transformed_bounds
        viewbox = f"{b.min_x:.1f} {b.min_y:.1f} {b.width:.1f} {b.height:.1f}"

        # Global transform: raw F1 coordinates -> SVG display coordinates
        transform_str = self.transform.to_svg_transform()

        # S/F position is the 1st track point (raw F1 coordinates)
        sf = self.track.track_points[0]
        lap_distance = self._compute_lap_distance()

        return f'''<?xml version="1.0" encoding="utf-8"?>
<?xml-stylesheet type="text/css" href="../../css/tracks.css"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">
  <title>{self.track.name}</title>

  <g id="track-root" transform="{transform_str}" data-scale="{self.transform.scale:.6f}" data-rotation="{self.transform.rotate_deg:.1f}">

    <g id="track-outline">
{chr(10).join(outline_paths)}
    </g>

    <g id="track-sectors">
{chr(10).join(sector_paths)}
    </g>

    <g id="marshal-lights">
{chr(10).join(light_markers)}
    </g>

    <g id="marshal-sectors">
{chr(10).join(sector_markers)}
    </g>

    <g id="corners">
{chr(10).join(corner_markers)}
    </g>

    <g id="start-finish" data-track-x="{sf.x:.1f}" data-track-y="{sf.y:.1f}" data-lap-distance="{lap_distance:.1f}">
{start_finish}
    </g>

    <g id="car-markers"></g>

  </g>
</svg>
'''

    def _generate_sector_paths(self, css_class: str) -> list[str]:
        """Generate path elements for each sector using raw F1 coordinates."""
        paths = []
        num_sectors = len(self.track.sector_indices)
        raw_points = self.track.track_points

        for i in range(num_sectors):
            sector_num, start_idx = self.track.sector_indices[i]
            _, end_idx = self.track.sector_indices[(i + 1) % num_sectors]

            if end_idx > start_idx:
                sector_points = raw_points[start_idx:end_idx + 1]
            else:
                sector_points = (
                    raw_points[start_idx:] +
                    raw_points[:end_idx + 1]
                )

            if sector_points:
                path_d = self._path_d(sector_points)
                paths.append(
                    f'      <path d="{path_d}" class="{css_class}" data-sector="{sector_num}"/>'
                )

        return paths

    def _generate_corner_markers(self) -> list[str]:
        """Generate corner marker SVG elements using raw F1 coordinates."""
        markers = []
        radius = CORNER_RADIUS * self.inv_scale
        for corner in sorted(self.track.corners, key=lambda c: c.number):
            pos = corner.position
            label = f"{corner.number}{corner.letter}" if corner.letter else str(corner.number)
            markers.append(f'''      <g class="corner-marker" data-corner="{label}" data-length="{corner.length:.1f}">
        <circle cx="{pos.x:.1f}" cy="{pos.y:.1f}" r="{radius:.1f}" class="corner-bg"/>
        <text x="{pos.x:.1f}" y="{pos.y:.1f}" class="corner-label">{label}</text>
      </g>''')
        return markers

    def _generate_sector_markers(self) -> list[str]:
        """Generate marshal sector marker SVG elements using raw F1 coordinates."""
        markers = []
        size = MARSHAL_SECTOR_SIZE * self.inv_scale
        for sector in sorted(self.track.marshal_sectors, key=lambda s: s.number):
            pos = sector.position
            x1, y1 = pos.x, pos.y - size
            x2, y2 = pos.x - size * 0.866, pos.y + size * 0.5
            x3, y3 = pos.x + size * 0.866, pos.y + size * 0.5
            markers.append(f'''      <g class="marshal-sector-marker" data-sector="{sector.number}" data-length="{sector.length:.1f}">
        <polygon points="{x1:.1f},{y1:.1f} {x2:.1f},{y2:.1f} {x3:.1f},{y3:.1f}" class="marshal-sector-bg"/>
        <text x="{pos.x:.1f}" y="{pos.y:.1f}" class="marshal-sector-label">{sector.number}</text>
      </g>''')
        return markers

    def _generate_light_markers(self) -> list[str]:
        """Generate marshal light marker SVG elements using raw F1 coordinates."""
        markers = []
        size = MARSHAL_LIGHT_SIZE * self.inv_scale
        for light in sorted(self.track.marshal_lights, key=lambda l: l.number):
            pos = light.position
            x, y = pos.x - size / 2, pos.y - size / 2
            markers.append(f'''      <g class="marshal-light-marker" data-light="{light.number}">
        <rect x="{x:.1f}" y="{y:.1f}" width="{size:.1f}" height="{size:.1f}" class="marshal-light-bg"/>
        <text x="{pos.x:.1f}" y="{pos.y:.1f}" class="marshal-light-label">{light.number}</text>
      </g>''')
        return markers

    def _generate_start_finish(self) -> str:
        """Generate start/finish marker with checkerboard and chevrons.

        Uses raw F1 coordinates. All dimensional constants are scaled by inv_scale
        so they appear the correct size after the global transform.
        """
        if len(self.track.track_points) < 2:
            return ""

        # Use raw F1 coordinates for start point and direction
        p1 = self.track.track_points[0]
        look_ahead = min(10, len(self.track.track_points) - 1)
        p_ahead = self.track.track_points[look_ahead]

        dx, dy = p_ahead.x - p1.x, p_ahead.y - p1.y
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0:
            return ""

        # Unit vectors
        r_x, r_y = dx / length, dy / length  # Racing direction
        p_x, p_y = -r_y, r_x  # Perpendicular

        side = _find_empty_side(self.track.track_points[0], self.track.track_points)
        # No side flip needed — we're in raw F1 coordinates (global transform handles y-flip)

        # Scale all dimensional constants to F1 coordinate space
        s = self.inv_scale
        side_offset = SIDE_OFFSET * s
        sq_size = CHECKER_SQUARE_SIZE * s
        chevron_gap = CHEVRON_GAP * s
        chevron_size = CHEVRON_SIZE * s
        chevron_spacing = CHEVRON_SPACING * s

        # Center point for checkerboard
        x_pt = Point(p1.x + p_x * side * side_offset, p1.y + p_y * side * side_offset)

        checker_long = CHECKER_LONG_SIDE * sq_size
        checker_short = CHECKER_SHORT_SIDE * sq_size
        # Flag ENDS at S/F line (placed over starting grid, behind S/F)
        corner_x = x_pt.x - r_x * checker_long - p_x * side * checker_short / 2
        corner_y = x_pt.y - r_y * checker_long - p_y * side * checker_short / 2

        elements = []

        # Checkerboard border (white outline)
        border_pts = [
            (corner_x, corner_y),
            (corner_x + r_x * checker_long, corner_y + r_y * checker_long),
            (corner_x + r_x * checker_long + p_x * side * checker_short,
             corner_y + r_y * checker_long + p_y * side * checker_short),
            (corner_x + p_x * side * checker_short, corner_y + p_y * side * checker_short),
        ]
        border_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in border_pts)
        elements.append(f'      <polygon points="{border_str}" class="checker-border"/>')

        # Checkerboard squares
        for col in range(CHECKER_LONG_SIDE):
            for row in range(CHECKER_SHORT_SIDE):
                color_class = "checker-black" if (col + row) % 2 == 0 else "checker-white"
                sq_x = corner_x + r_x * col * sq_size + p_x * side * row * sq_size
                sq_y = corner_y + r_y * col * sq_size + p_y * side * row * sq_size

                pts = [
                    (sq_x, sq_y),
                    (sq_x + r_x * sq_size, sq_y + r_y * sq_size),
                    (sq_x + r_x * sq_size + p_x * side * sq_size,
                     sq_y + r_y * sq_size + p_y * side * sq_size),
                    (sq_x + p_x * side * sq_size, sq_y + p_y * side * sq_size),
                ]
                points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                elements.append(f'      <polygon points="{points_str}" class="{color_class}"/>')

        # Chevrons (ahead of S/F line in racing direction)
        chevron_center_x = x_pt.x + r_x * chevron_gap
        chevron_center_y = x_pt.y + r_y * chevron_gap

        for i in range(3):
            cx = chevron_center_x + r_x * i * chevron_spacing
            cy = chevron_center_y + r_y * i * chevron_spacing

            tip = (cx + r_x * chevron_size, cy + r_y * chevron_size)
            top = (cx + p_x * side * chevron_size, cy + p_y * side * chevron_size)
            bot = (cx - p_x * side * chevron_size, cy - p_y * side * chevron_size)

            elements.append(
                f'      <polyline points="{top[0]:.1f},{top[1]:.1f} {tip[0]:.1f},{tip[1]:.1f} '
                f'{bot[0]:.1f},{bot[1]:.1f}" class="chevron"/>'
            )

        return "\n".join(elements)

    @staticmethod
    def _path_d(points: list[Point]) -> str:
        """Generate SVG path d attribute."""
        if not points:
            return ""
        parts = [f"M {points[0].x:.1f} {points[0].y:.1f}"]
        parts.extend(f"L {p.x:.1f} {p.y:.1f}" for p in points[1:])
        return " ".join(parts)


# =============================================================================
# File I/O
# =============================================================================

def normalize_location(name: str) -> str:
    """Normalize location name for filenames."""
    normalized = unicodedata.normalize('NFD', name)
    ascii_str = normalized.encode('ascii', 'ignore').decode('ascii')
    return ascii_str.replace(' ', '_')


def load_schedule() -> dict[int, dict]:
    """Load schedule and create circuit_key -> meeting data mapping."""
    if not SCHEDULE_FILE.exists():
        return {}

    with open(SCHEDULE_FILE) as f:
        schedule = json.load(f)

    return {
        meeting['circuit_key']: meeting
        for meeting in schedule
        if meeting.get('circuit_key')
    }


def load_circuit_data(filepath: Path) -> dict:
    """Load circuit data from JSON file."""
    with open(filepath) as f:
        return json.load(f)


def generate_track_svg(circuit_data: dict, output_path: Path) -> bool:
    """Generate SVG file for a circuit."""
    try:
        track = TrackData.from_circuit_json(circuit_data)
    except ValueError as e:
        print(f"  Error: {e}")
        return False

    # Generate SVG
    builder = SVGBuilder(track)
    svg_content = builder.generate()

    # Write file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg_content)

    bounds = track.get_bounds()
    print(f"  Saved: {output_path}")
    print(f"    Track points: {len(track.track_points)}")
    print(f"    Marshal sectors: {len(track.marshal_sectors)}")
    print(f"    Corners: {len(track.corners)}")
    print(f"    Original bounds: X[{bounds.min_x:.0f}, {bounds.max_x:.0f}] Y[{bounds.min_y:.0f}, {bounds.max_y:.0f}]")
    print(f"    SVG size: {builder.transformed_bounds.width:.0f} x {builder.transformed_bounds.height:.0f}")

    return True


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate SVG track maps from circuit data",
    )
    parser.add_argument("--circuit", "-c", type=int, help="Generate only this circuit key")
    parser.add_argument("--output-dir", "-o", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--data-dir", "-d", type=Path, default=DATA_DIR)

    args = parser.parse_args()

    # Find circuit data files
    data_files = [
        f for f in args.data_dir.glob("*_*_202*.json")
        if not f.name.startswith(("schedule_", "fetch_"))
    ]

    if not data_files:
        print(f"No circuit data files found in {args.data_dir}")
        return

    if args.circuit:
        data_files = [f for f in data_files if f.name.startswith(f"{args.circuit}_")]
        if not data_files:
            print(f"No data file found for circuit {args.circuit}")
            return

    # Load schedule
    schedule_map = load_schedule()
    print(f"Loaded schedule with {len(schedule_map)} circuits")
    print(f"Generating SVGs for {len(data_files)} circuits...")
    print(f"Output directory: {args.output_dir}")

    success, fail = 0, 0

    for data_file in sorted(data_files):
        parts = data_file.stem.split("_")
        circuit_key = int(parts[0]) if parts[0].isdigit() else None

        print(f"\n[{circuit_key}] {data_file.name}...")

        try:
            circuit_data = load_circuit_data(data_file)

            # Determine output filename
            if circuit_key and circuit_key in schedule_map:
                location = schedule_map[circuit_key].get('location', '')
                if location:
                    output_name = normalize_location(location)
                    print(f"  Using location: {location} -> {output_name}")
                else:
                    output_name = circuit_data.get("circuit_name", "Unknown").replace(" ", "_")
            else:
                output_name = circuit_data.get("circuit_name", "Unknown").replace(" ", "_")

            output_path = args.output_dir / f"{output_name}.svg"

            if generate_track_svg(circuit_data, output_path):
                success += 1
            else:
                fail += 1

        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    print(f"\n{'=' * 60}")
    print(f"Completed: {success} succeeded, {fail} failed")


if __name__ == "__main__":
    main()
