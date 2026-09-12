"""Region and country codes expanded to airport lists.

`fli` ships city aliases but nothing at country or region level, and a
request like "somewhere in the USA" needs one. These are long-haul gateways,
not exhaustive airport lists: a scan costs one request per destination, so
a wide-but-shallow list beats a complete one. Edit freely — this is a
judgement call about coverage versus cost, not a fact about the world.
"""

from __future__ import annotations

# Ordered by likely relevance: the planner trims from the end when a search
# would exceed its request budget.
REGIONS: dict[str, list[str]] = {
    "US": [
        "JFK", "EWR", "LAX", "SFO", "SEA", "ORD",
        "IAD", "BOS", "IAH", "ATL", "DFW", "MIA",
    ],
    "US_WEST": ["LAX", "SFO", "SEA", "LAS", "SAN", "PDX"],
    "US_EAST": ["JFK", "EWR", "BOS", "IAD", "MIA", "ATL"],
    "CANADA": ["YYZ", "YVR", "YUL"],
    "UK": ["LHR", "LGW", "MAN"],
    "EUROPE": ["LHR", "CDG", "AMS", "FRA", "MUC", "MAD", "BCN", "FCO", "ZRH", "CPH"],
    "JAPAN": ["NRT", "HND", "KIX"],
    "AUSTRALIA": ["SYD", "MEL", "BNE", "PER"],
    "SOUTHEAST_ASIA": ["SIN", "BKK", "KUL", "CGK", "MNL", "HAN", "SGN"],
}

ALIASES: dict[str, str] = {
    "USA": "US",
    "UNITED STATES": "US",
    "AMERICA": "US",
    "WEST COAST": "US_WEST",
    "EAST COAST": "US_EAST",
    "GB": "UK",
    "UNITED KINGDOM": "UK",
    "EU": "EUROPE",
    "JP": "JAPAN",
    "AU": "AUSTRALIA",
    "CA": "CANADA",
    "SEA": "SOUTHEAST_ASIA",
    "ASEAN": "SOUTHEAST_ASIA",
}


class UnknownRegionError(ValueError):
    """Raised when a region name has no airport list."""


def expand_region(name: str) -> list[str]:
    """Expand a region or country name into its gateway airport codes."""
    key = name.strip().upper().replace("-", "_")
    key = ALIASES.get(key, key)
    if key not in REGIONS:
        known = ", ".join(sorted(set(REGIONS) | set(ALIASES)))
        raise UnknownRegionError(f"Unknown region {name!r}. Known regions: {known}")
    return list(REGIONS[key])


def known_regions() -> list[str]:
    """Every region name the planner accepts, aliases included."""
    return sorted(set(REGIONS) | set(ALIASES))
