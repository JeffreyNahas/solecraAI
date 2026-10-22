"""Which permits become leads (decided with Jeff, 2026-10-22; see issue #7).

Kept: new construction, major renovation, change of use, commercial fit-out, condo conversion,
on business-likely buildings (multi-residential with enough units, commercial, office,
institutional), within a radius of downtown Montréal. Single-family homes are skipped: the
permit data has no owner contact, so there is nobody we may email under CASL.
"""

import math

from app.config import Settings
from app.permits.model import BuildingCategory, Permit, WorkCategory

DOWNTOWN = (45.5017, -73.5673)  # Place Ville Marie

KEEP_WORK = frozenset(
    {
        WorkCategory.NEW_CONSTRUCTION,
        WorkCategory.MAJOR_RENOVATION,
        WorkCategory.CHANGE_OF_USE,
        WorkCategory.COMMERCIAL_FITOUT,
        WorkCategory.CONDO_CONVERSION,
    }
)
KEEP_BUILDINGS = frozenset(
    {BuildingCategory.COMMERCIAL, BuildingCategory.OFFICE, BuildingCategory.INSTITUTIONAL}
)


def distance_km(lat: float, lon: float, origin: tuple[float, float] = DOWNTOWN) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(origin[0]), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lon - origin[1])
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def reject_reason(permit: Permit, settings: Settings) -> str | None:
    """None if the permit should become a lead, else a short reason (for counts and logs)."""
    if permit.work not in KEEP_WORK:
        return f"work:{permit.work.value}"
    if permit.building == BuildingCategory.MULTI_RESIDENTIAL:
        if (permit.units or 0) < settings.permits_min_units:
            return "units_below_min"
    elif permit.building not in KEEP_BUILDINGS:
        return f"building:{permit.building.value}"
    if permit.latitude is not None and permit.longitude is not None:
        if distance_km(permit.latitude, permit.longitude) > settings.permits_max_km:
            return "too_far"
    return None
