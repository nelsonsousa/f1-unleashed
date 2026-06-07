"""
Circuit name normalization utilities.

Provides functions to normalize circuit/event/location names to their canonical form,
allowing file lookups to work regardless of which API variant was used.
"""

import json
from pathlib import Path
from functools import lru_cache


def _normalize_string(s: str) -> str:
    """Normalize a string for comparison: lowercase, no accents, underscores for spaces."""
    import unicodedata
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return s.lower().replace(' ', '_').replace('-', '_')


@lru_cache(maxsize=1)
def _load_circuits() -> list:
    """Load circuits data from JSON file."""
    json_path = Path(__file__).parent.parent.parent / 'static' / 'data' / 'circuits.json'
    with open(json_path) as f:
        data = json.load(f)
    return data['circuits']


def find_circuit(name: str, field: str = None) -> dict | None:
    """
    Find a circuit by matching name against principal or variant names.

    Args:
        name: The name to search for (event name, location, circuit, or country)
        field: Optional field to search in ('event_name', 'location', 'circuit', 'country')
               If None, searches all fields.

    Returns:
        The matching circuit dict, or None if not found.
    """
    circuits = _load_circuits()
    normalized_name = _normalize_string(name)

    fields_to_search = [field] if field else ['event_name', 'location', 'circuit', 'country']

    for circuit in circuits:
        for f in fields_to_search:
            # Check principal value
            principal = circuit.get(f, '')
            if _normalize_string(principal) == normalized_name:
                return circuit

            # Check variants
            variants = circuit.get('variants', {}).get(f, [])
            for variant in variants:
                if _normalize_string(variant) == normalized_name:
                    return circuit

    return None


def get_canonical_name(name: str, field: str) -> str:
    """
    Get the canonical (principal) name for a given variant.

    Args:
        name: The name to normalize (could be a variant)
        field: The field type ('event_name', 'location', 'circuit', 'country')

    Returns:
        The canonical name, or the original name if no match found.
    """
    circuit = find_circuit(name, field)
    if circuit:
        return circuit.get(field, name)
    return name


def get_canonical_location(name: str) -> str:
    """Get the canonical location name for a given location variant."""
    return get_canonical_name(name, 'location')


def get_canonical_event(name: str) -> str:
    """Get the canonical event name for a given event variant."""
    return get_canonical_name(name, 'event_name')


def get_svg_filename(location: str) -> str:
    """
    Get the SVG filename for a location.

    Args:
        location: Location name (e.g., 'Bahrain', 'Monaco')

    Returns:
        The SVG filename without extension (e.g., 'Sakhir', 'Monte_Carlo')
    """
    canonical = get_canonical_location(location)
    # Normalize for filesystem: remove accents, replace spaces with underscores
    return _normalize_string(canonical).replace('_', '_').title().replace(' ', '_')


def get_cache_path_components(event_name: str, session_type: str) -> tuple[str, str]:
    """
    Get normalized components for cache directory path.

    Args:
        event_name: Event name (e.g., 'Bahrain Grand Prix', 'Pre-Season Testing')
        session_type: Session type (e.g., 'Practice 1', 'Race')

    Returns:
        Tuple of (event_dir_name, session_dir_name) for cache path construction.
    """
    circuit = find_circuit(event_name, 'event_name')

    if circuit:
        # Use canonical event name, formatted for directory
        canonical = circuit['event_name']
        # Remove "Grand Prix" suffix for directory name
        event_dir = canonical.replace(' Grand Prix', '').replace(' ', '_')
    else:
        event_dir = event_name.replace(' ', '_')

    session_dir = session_type.replace(' ', '_')

    return event_dir, session_dir
