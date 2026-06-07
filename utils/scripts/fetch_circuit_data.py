#!/usr/bin/env python3
"""
Fetch all circuit data from OpenF1 and MultiViewer APIs.

Uses OpenF1 API to get the F1 calendar/schedule, which provides the correct
circuit_info_url for each meeting. Then fetches detailed circuit data from
MultiViewer API.

Downloads:
- F1 calendar/schedule with meeting details
- Track center line coordinates (x, y)
- Corner locations with numbers and angles
- Marshal sector positions
- Marshal light positions
- Circuit metadata (name, country, rotation, etc.)

Usage:
    python fetch_circuit_data.py                    # Fetch 2025 + 2026 circuits
    python fetch_circuit_data.py --year 2025       # Fetch specific year only
    python fetch_circuit_data.py --list            # List meetings without fetching

Output:
    utils/tracks/data/schedule_<year>.json  - Calendar/schedule for the year
    utils/tracks/data/<circuit_key>_<name>.json - Detailed circuit data
    utils/tracks/data/fetch_results.json    - Summary of fetch operation
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

import requests

# Paths
SCRIPT_DIR = Path(__file__).parent
UTILS_DIR = SCRIPT_DIR.parent
DATA_DIR = UTILS_DIR / "tracks" / "data"

# APIs
OPENF1_API_BASE = "https://api.openf1.org/v1"
MV_HEADERS = {"User-Agent": "FastF1/3.4"}

# Rate limiting
REQUEST_DELAY = 0.5  # seconds between requests


@dataclass
class Meeting:
    """F1 meeting/event from OpenF1 API."""
    meeting_key: int
    meeting_name: str
    meeting_official_name: str
    location: str
    country_key: int
    country_code: str
    country_name: str
    circuit_key: int
    circuit_short_name: str
    circuit_type: str
    circuit_info_url: str
    date_start: str
    date_end: str
    year: int
    gmt_offset: str = ""
    country_flag: str = ""
    circuit_image: str = ""


@dataclass
class TrackPosition:
    """Position on track."""
    x: float
    y: float


@dataclass
class Corner:
    """Corner marker data."""
    number: int
    letter: str
    angle: float
    length: float
    position: TrackPosition


@dataclass
class MarshalSector:
    """Marshal sector data."""
    number: int
    angle: float
    length: float
    position: TrackPosition


@dataclass
class MarshalLight:
    """Marshal light data."""
    number: int
    angle: float
    length: float
    position: TrackPosition


@dataclass
class CircuitData:
    """Complete circuit data from the MultiViewer API."""
    # Metadata
    circuit_key: int
    circuit_name: str
    country_name: str
    country_ioc_code: str
    location: str
    year: int
    round: Optional[int]
    race_date: Optional[str]
    rotation: float

    # Track outline (center line coordinates)
    track_x: list[float]
    track_y: list[float]

    # Markers
    corners: list[Corner]
    marshal_sectors: list[MarshalSector]
    marshal_lights: list[MarshalLight]

    # Meeting info (from OpenF1)
    meeting_key: Optional[int] = None
    meeting_name: Optional[str] = None

    # Timing data (if available)
    candidate_lap: Optional[dict] = None
    track_position_time: Optional[list] = None

    # Fetch metadata
    fetched_at: str = ""
    source_url: str = ""


def fetch_meetings(year: int) -> list[Meeting]:
    """Fetch F1 meetings/schedule from OpenF1 API."""
    url = f"{OPENF1_API_BASE}/meetings?year={year}"

    print(f"Fetching {year} schedule from OpenF1 API...")

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        print(f"Error fetching meetings: {e}")
        return []

    meetings = []
    for item in data:
        try:
            meetings.append(Meeting(
                meeting_key=item.get("meeting_key", 0),
                meeting_name=item.get("meeting_name", ""),
                meeting_official_name=item.get("meeting_official_name", ""),
                location=item.get("location", ""),
                country_key=item.get("country_key", 0),
                country_code=item.get("country_code", ""),
                country_name=item.get("country_name", ""),
                circuit_key=item.get("circuit_key", 0),
                circuit_short_name=item.get("circuit_short_name", ""),
                circuit_type=item.get("circuit_type", ""),
                circuit_info_url=item.get("circuit_info_url", ""),
                date_start=item.get("date_start", ""),
                date_end=item.get("date_end", ""),
                year=item.get("year", year),
                gmt_offset=item.get("gmt_offset", ""),
                country_flag=item.get("country_flag", ""),
                circuit_image=item.get("circuit_image", ""),
            ))
        except Exception as e:
            print(f"Warning: Could not parse meeting: {e}")

    print(f"Found {len(meetings)} meetings for {year}")
    return meetings


def fetch_circuit_data(circuit_info_url: str, meeting: Meeting) -> Optional[CircuitData]:
    """Fetch detailed circuit data from MultiViewer API."""
    try:
        response = requests.get(circuit_info_url, headers=MV_HEADERS, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        print(f"    Error: {e}")
        return None

    # Parse corners
    corners = []
    for c in data.get("corners", []):
        pos = c.get("trackPosition", {})
        corners.append(Corner(
            number=c.get("number", 0),
            letter=c.get("letter", ""),
            angle=c.get("angle", 0.0),
            length=c.get("length", 0.0),
            position=TrackPosition(
                x=pos.get("x", 0.0),
                y=pos.get("y", 0.0),
            ),
        ))

    # Parse marshal sectors
    marshal_sectors = []
    for s in data.get("marshalSectors", []):
        pos = s.get("trackPosition", {})
        marshal_sectors.append(MarshalSector(
            number=s.get("number", 0),
            angle=s.get("angle", 0.0),
            length=s.get("length", 0.0),
            position=TrackPosition(
                x=pos.get("x", 0.0),
                y=pos.get("y", 0.0),
            ),
        ))

    # Parse marshal lights
    marshal_lights = []
    for light in data.get("marshalLights", []):
        pos = light.get("trackPosition", {})
        marshal_lights.append(MarshalLight(
            number=light.get("number", 0),
            angle=light.get("angle", 0.0),
            length=light.get("length", 0.0),
            position=TrackPosition(
                x=pos.get("x", 0.0),
                y=pos.get("y", 0.0),
            ),
        ))

    return CircuitData(
        circuit_key=data.get("circuitKey", meeting.circuit_key),
        circuit_name=data.get("circuitName", meeting.circuit_short_name),
        country_name=data.get("countryName", meeting.country_name),
        country_ioc_code=data.get("countryIocCode", meeting.country_code),
        location=data.get("location", meeting.location),
        year=data.get("year", meeting.year),
        round=data.get("round"),
        race_date=data.get("raceDate"),
        rotation=data.get("rotation", 0.0),
        track_x=data.get("x", []),
        track_y=data.get("y", []),
        corners=corners,
        marshal_sectors=marshal_sectors,
        marshal_lights=marshal_lights,
        meeting_key=meeting.meeting_key,
        meeting_name=meeting.meeting_name,
        candidate_lap=data.get("candidateLap"),
        track_position_time=data.get("trackPositionTime"),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source_url=circuit_info_url,
    )


def dataclass_to_dict(obj):
    """Convert dataclass to dict, handling nested dataclasses."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: dataclass_to_dict(v) for k, v in asdict(obj).items()}
    elif isinstance(obj, list):
        return [dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    return obj


def save_json(data: dict, filepath: Path) -> None:
    """Save data to JSON file with pretty formatting."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved: {filepath}")


def fetch_all_circuits(years: list[int]) -> None:
    """Fetch circuit data for all meetings in the specified years."""

    # Create output directory
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all meetings
    all_meetings = []
    for year in years:
        meetings = fetch_meetings(year)
        time.sleep(REQUEST_DELAY)

        # Save schedule
        meetings_dict = [dataclass_to_dict(m) for m in meetings]
        save_json(meetings_dict, DATA_DIR / f"schedule_{year}.json")

        all_meetings.extend(meetings)

    # Deduplicate by circuit_key (keep the most recent meeting for each circuit)
    circuits_to_fetch = {}
    for meeting in all_meetings:
        key = meeting.circuit_key
        # Skip if no circuit_info_url
        if not meeting.circuit_info_url:
            continue
        # Keep the meeting with the highest year (most recent)
        if key not in circuits_to_fetch or meeting.year > circuits_to_fetch[key].year:
            circuits_to_fetch[key] = meeting

    print(f"\nFetching detailed data for {len(circuits_to_fetch)} unique circuits...")

    # Track results
    results = {
        "years": years,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "circuits": {},
    }

    success_count = 0
    fail_count = 0

    for circuit_key, meeting in sorted(circuits_to_fetch.items()):
        print(f"\n[{circuit_key}] {meeting.circuit_short_name} ({meeting.country_name}, {meeting.year})...")
        print(f"  URL: {meeting.circuit_info_url}")

        circuit_data = fetch_circuit_data(meeting.circuit_info_url, meeting)
        time.sleep(REQUEST_DELAY)

        if circuit_data:
            # Create filename
            safe_name = circuit_data.circuit_name.replace(" ", "_").replace("-", "_").replace("'", "")
            filename = f"{circuit_key}_{safe_name}_{meeting.year}.json"

            # Save individual circuit file
            circuit_dict = dataclass_to_dict(circuit_data)
            save_json(circuit_dict, DATA_DIR / filename)

            # Add to results summary
            results["circuits"][str(circuit_key)] = {
                "circuit_key": circuit_key,
                "name": circuit_data.circuit_name,
                "country": circuit_data.country_name,
                "country_ioc": circuit_data.country_ioc_code,
                "location": circuit_data.location,
                "year": circuit_data.year,
                "rotation": circuit_data.rotation,
                "track_points": len(circuit_data.track_x),
                "corners": len(circuit_data.corners),
                "marshal_sectors": len(circuit_data.marshal_sectors),
                "marshal_lights": len(circuit_data.marshal_lights),
                "meeting_name": meeting.meeting_name,
                "source_url": meeting.circuit_info_url,
            }

            print(f"  Track points: {len(circuit_data.track_x)}")
            print(f"  Corners: {len(circuit_data.corners)}")
            print(f"  Marshal sectors: {len(circuit_data.marshal_sectors)}")
            print(f"  Marshal lights: {len(circuit_data.marshal_lights)}")

            success_count += 1
        else:
            print(f"  FAILED: No data available")
            fail_count += 1

    # Save results summary
    save_json(results, DATA_DIR / "fetch_results.json")

    print(f"\n{'='*60}")
    print(f"Completed: {success_count} succeeded, {fail_count} failed")
    print(f"Data saved to: {DATA_DIR}")


def list_meetings(years: list[int]) -> None:
    """List all meetings without fetching circuit data."""
    for year in years:
        meetings = fetch_meetings(year)
        time.sleep(REQUEST_DELAY)

        print(f"\n{'='*80}")
        print(f"  {year} F1 Calendar ({len(meetings)} events)")
        print(f"{'='*80}")
        print(f"{'#':>3} {'Circuit':<20} {'Country':<15} {'Key':>4} {'Date':<12} {'Type':<20}")
        print(f"{'-'*80}")

        # Filter out testing sessions for cleaner display
        races = [m for m in meetings if "Testing" not in m.meeting_name]

        for i, m in enumerate(races, 1):
            date_str = m.date_start[:10] if m.date_start else ""
            print(f"{i:>3} {m.circuit_short_name:<20} {m.country_name:<15} {m.circuit_key:>4} {date_str:<12} {m.circuit_type:<20}")

        print(f"\nTotal: {len(races)} races + {len(meetings) - len(races)} other events")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch F1 circuit data from OpenF1 and MultiViewer APIs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fetch_circuit_data.py                  # Fetch 2025 + 2026 circuits
  python fetch_circuit_data.py --year 2025     # Fetch 2025 only
  python fetch_circuit_data.py --list          # List all meetings
        """
    )

    parser.add_argument(
        "--year", "-y",
        type=int,
        action="append",
        help="Year(s) to fetch (can specify multiple times). Default: 2025, 2026"
    )

    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List meetings without fetching circuit data"
    )

    args = parser.parse_args()

    # Default to 2025 and 2026
    years = args.year if args.year else [2025, 2026]

    if args.list:
        list_meetings(years)
    else:
        fetch_all_circuits(years)


if __name__ == "__main__":
    main()
