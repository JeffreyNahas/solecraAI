"""A building permit in our own terms, independent of how the city's dataset names things.

The city client (`app/permits/montreal.py`) maps raw records into `Permit`; filters and scoring
only ever see this model, so a change in the city's format touches one mapping function.
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel


class WorkCategory(StrEnum):
    NEW_CONSTRUCTION = "new_construction"
    MAJOR_RENOVATION = "major_renovation"
    CHANGE_OF_USE = "change_of_use"
    COMMERCIAL_FITOUT = "commercial_fitout"
    CONDO_CONVERSION = "condo_conversion"
    DEMOLITION = "demolition"
    MINOR = "minor"  # pools, fences, signage, small repairs, anything else
    UNKNOWN = "unknown"


class BuildingCategory(StrEnum):
    SINGLE_FAMILY = "single_family"
    MULTI_RESIDENTIAL = "multi_residential"
    COMMERCIAL = "commercial"
    OFFICE = "office"
    INSTITUTIONAL = "institutional"
    INDUSTRIAL = "industrial"
    UNKNOWN = "unknown"


class Permit(BaseModel):
    permit_id: str
    issued_on: date | None = None
    address: str | None = None
    borough: str | None = None
    work: WorkCategory = WorkCategory.UNKNOWN
    building: BuildingCategory = BuildingCategory.UNKNOWN
    units: int | None = None
    description: str | None = None  # the city's free-text description of the work
    work_label: str | None = None  # the city's own type label, kept for context (e.g. "Transformation")
    building_label: str | None = None  # e.g. "Résidentiel / 5 à 11 Logements"
    latitude: float | None = None
    longitude: float | None = None
    estimated_cost: float | None = None
