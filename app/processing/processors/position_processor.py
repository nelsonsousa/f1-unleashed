"""
Position Processor — car positions projected onto track geometry.

Subscribes to: Position.z, SessionInfo
Emits:
  - trackGeometry   (corners and sectors as % of lap distance) — once, persisted
  - position        { num: [x, y, distPct] } on each Position.z change —
                    persist=False (high-rate live stream, consumed live; not
                    replayed/rebuilt on seek, like liveTelemetry)

Loads the track SVG on SessionInfo to build the track polyline, then
projects each car's X,Y onto it to compute distance as % of lap. Each emit is a
full snapshot of the cars that moved; skips messages where no car has moved.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor
from app.processing.track_geometry import (
    TrackGeometry, find_svg_path, parse_svg,
    project_local, cum_dist_to_track_dist,
)

logger = logging.getLogger(__name__)


class PositionProcessor(Processor):
    """Projects car positions onto track and emits distance percentages."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._geo: Optional[TrackGeometry] = None
        self._geometry_emitted = False
        self._last_seg: dict[str, int] = {}
        self._last_pos: dict[str, tuple[float, float, float]] = {}

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle_session_info)
        self._bus.on("Position.z", self._handle_position)

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        if self._geo is not None:
            return
        if not isinstance(data, dict):
            return
        meeting = data.get("Meeting")
        if not isinstance(meeting, dict):
            return
        location = meeting.get("Location")
        if not location:
            return

        svg_path = find_svg_path(location)
        if not svg_path:
            logger.warning(f"No track SVG found for {location}")
            return

        self._geo = parse_svg(svg_path)
        logger.info(f"Loaded track geometry for {location}: {len(self._geo.points)} points")

        if not self._geometry_emitted:
            self._emit_geometry(clock_time)
            self._geometry_emitted = True

    def _emit_geometry(self, clock_time: datetime) -> None:
        """Emit track corners and sectors as % of lap distance."""
        geo = self._geo
        total = geo.total_dist
        if total <= 0:
            return

        corners = []
        for c in geo.corners:
            corners.append({
                "number": c["label"],
                "pct": round(c["dist"] / total * 100, 2),
            })

        self._bus.emit("trackGeometry", {
            "corners": corners,
            "sectors": geo.sector_boundaries,
            "trackLength": round(total, 1),
        }, clock_time)

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict) or self._geo is None:
            return

        pos_data = data.get("Position") or data
        if not isinstance(pos_data, list) or not pos_data:
            return

        latest = pos_data[-1]
        entries = latest.get("Entries") or latest
        if not isinstance(entries, dict):
            return

        geo = self._geo
        total = geo.total_dist
        cars = {}
        changed = False

        for num, pos in entries.items():
            if not isinstance(pos, dict):
                continue
            try:
                if int(num) > 99:
                    continue
            except ValueError:
                continue

            x = pos.get("X")
            y = pos.get("Y")
            if x is None or y is None:
                continue
            if x == 0 and y == 0:
                continue

            last_seg = self._last_seg.get(num)
            cum_dist, seg_idx, _ = project_local(geo, x, y, last_seg)
            self._last_seg[num] = seg_idx

            track_dist = cum_dist_to_track_dist(cum_dist, geo)
            dist_pct = round(track_dist / total * 100, 3) if total > 0 else 0.0
            rx = round(x, 1)
            ry = round(y, 1)

            prev = self._last_pos.get(num)
            if prev and prev == (rx, ry, dist_pct):
                cars[num] = [rx, ry, dist_pct]
                continue

            self._last_pos[num] = (rx, ry, dist_pct)
            cars[num] = [rx, ry, dist_pct]
            changed = True

        if changed and cars:
            self._bus.emit("position", cars, clock_time, persist=False)
