/**
 * Circuit name normalization utilities.
 *
 * Provides functions to normalize circuit/event/location names to their canonical form,
 * allowing lookups to work regardless of which API variant was used.
 */

(function() {
    let circuitsData = null;

    /**
     * Normalize a string for comparison: lowercase, no accents, underscores for spaces.
     */
    function normalizeString(s) {
        return s
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase()
            .replace(/\s+/g, '_')
            .replace(/-/g, '_');
    }

    /**
     * Load circuits data from JSON file.
     */
    async function loadCircuits() {
        if (circuitsData) return circuitsData;

        try {
            const response = await fetch('/static/data/circuits.json');
            const data = await response.json();
            circuitsData = data.circuits;
            return circuitsData;
        } catch (e) {
            console.warn('Failed to load circuits data:', e);
            return [];
        }
    }

    /**
     * Find a circuit by matching name against principal or variant names.
     *
     * @param {string} name - The name to search for
     * @param {string} [field] - Optional field to search in ('event_name', 'location', 'circuit', 'country')
     * @returns {Object|null} The matching circuit object, or null if not found
     */
    async function findCircuit(name, field = null) {
        const circuits = await loadCircuits();
        const normalizedName = normalizeString(name);

        const fieldsToSearch = field ? [field] : ['event_name', 'location', 'circuit', 'country'];

        for (const circuit of circuits) {
            for (const f of fieldsToSearch) {
                // Check principal value
                const principal = circuit[f] || '';
                if (normalizeString(principal) === normalizedName) {
                    return circuit;
                }

                // Check variants
                const variants = (circuit.variants || {})[f] || [];
                for (const variant of variants) {
                    if (normalizeString(variant) === normalizedName) {
                        return circuit;
                    }
                }
            }
        }

        return null;
    }

    /**
     * Get the canonical (principal) name for a given variant.
     *
     * @param {string} name - The name to normalize (could be a variant)
     * @param {string} field - The field type ('event_name', 'location', 'circuit', 'country')
     * @returns {Promise<string>} The canonical name, or the original name if no match found
     */
    async function getCanonicalName(name, field) {
        const circuit = await findCircuit(name, field);
        if (circuit) {
            return circuit[field] || name;
        }
        return name;
    }

    /**
     * Get the canonical location name for a given location variant.
     */
    async function getCanonicalLocation(name) {
        return getCanonicalName(name, 'location');
    }

    /**
     * Get the canonical event name for a given event variant.
     */
    async function getCanonicalEvent(name) {
        return getCanonicalName(name, 'event_name');
    }

    /**
     * Get the SVG filename for a location (synchronous version for track_map.js).
     * Must call loadCircuits() first during initialization.
     *
     * @param {string} location - Location name (e.g., 'Bahrain', 'Monaco')
     * @returns {string|null} The SVG filename without extension, or null if no mapping found
     */
    function getSvgFilenameSync(location) {
        if (!circuitsData) return null;

        const normalizedName = normalizeString(location);

        for (const circuit of circuitsData) {
            // Check principal location
            if (normalizeString(circuit.location) === normalizedName) {
                return circuit.location.replace(/\s+/g, '_');
            }

            // Check location variants
            const variants = (circuit.variants || {}).location || [];
            for (const variant of variants) {
                if (normalizeString(variant) === normalizedName) {
                    return circuit.location.replace(/\s+/g, '_');
                }
            }
        }

        return null;
    }

    // Export to window
    window.CircuitUtils = {
        loadCircuits,
        findCircuit,
        getCanonicalName,
        getCanonicalLocation,
        getCanonicalEvent,
        getSvgFilenameSync,
        normalizeString,
    };
})();
