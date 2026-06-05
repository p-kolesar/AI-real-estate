"""Geo derivation for real-estate sweeps.

The results payload does not carry the listing's locality, but each sweep
targets exactly one locality slug, so city/district/region can be derived from
the slug itself. Slugs look like ``bratislava-ruzinov`` (city-borough) or a bare
corridor town like ``stupava``.
"""

from __future__ import annotations

# Bratislava boroughs -> okres (district). Region is always Bratislavský for the
# BA + Stupava corridor this workshop covers.
_BA_BOROUGHS = {
    "stare-mesto": "Bratislava I",
    "ruzinov": "Bratislava II",
    "vrakuna": "Bratislava II",
    "podunajske-biskupice": "Bratislava II",
    "nove-mesto": "Bratislava III",
    "raca": "Bratislava III",
    "vajnory": "Bratislava III",
    "karlova-ves": "Bratislava IV",
    "dubravka": "Bratislava IV",
    "lamac": "Bratislava IV",
    "devin": "Bratislava IV",
    "devinska-nova-ves": "Bratislava IV",
    "zahorska-bystrica": "Bratislava IV",
    "petrzalka": "Bratislava V",
    "jarovce": "Bratislava V",
    "rusovce": "Bratislava V",
    "cunovo": "Bratislava V",
}

# Corridor towns outside the city proper.
_CORRIDOR = {
    "stupava": ("Stupava", "Malacky", "Bratislavský"),
    "malacky": ("Malacky", "Malacky", "Bratislavský"),
    "marianka": ("Marianka", "Malacky", "Bratislavský"),
    "borinka": ("Borinka", "Malacky", "Bratislavský"),
}


def _titlecase(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def locality_geo(slug: str) -> dict:
    """Map a locality slug to ``{city, district, region}``.

    Always returns a city (falls back to a title-cased slug) so the geo fields
    are never null for a successfully targeted sweep.
    """
    s = slug.strip().lower().strip("/")

    if s in _CORRIDOR:
        city, district, region = _CORRIDOR[s]
        return {"city": city, "district": district, "region": region}

    if s.startswith("bratislava-"):
        borough = s[len("bratislava-"):]
        return {
            "city": "Bratislava",
            "district": _BA_BOROUGHS.get(borough, "Bratislava"),
            "region": "Bratislavský",
        }

    if s == "bratislava":
        return {"city": "Bratislava", "district": "Bratislava", "region": "Bratislavský"}

    # Unknown locality: still give a non-null city so downstream joins work.
    return {"city": _titlecase(s), "district": _titlecase(s), "region": None}
