"""City of Montréal open data: "Permis de construction, transformation et démolition".

Dataset `permis-construction` on donnees.montreal.ca (CC BY 4.0, updated weekly), CKAN datastore
resource below. 19 boroughs of the City of Montréal only (not Laval, Longueuil or the other
cities on the island).

Fetched with the datastore SQL API (POST). The city's firewall blocks bursts of requests, so the
client is deliberately gentle: few large pages, a pause between pages, long backoff on 403/429.

Field notes (real data, 2026-10):
- `code_type_base_demande`: CO (construction), TR (transformation), CA (certificates: trees,
  pools, snow, signage…), DE (demolition). Only CO and TR can be leads, so only those are fetched.
- Type and building labels differ by borough ("unifamiliale (h1)", "H1 Unifamiliale", "1 Logement",
  "Cottage"…); `nb_logements` is often 0 on transformations. Units are parsed from the labels.
- `nature_travaux` (free text) carries the real signal (e.g. "remplacer 700 fenêtres").
"""

import asyncio
import logging
import re
import unicodedata
from datetime import date
from typing import Any

import httpx

from app.permits.model import BuildingCategory, Permit, WorkCategory

log = logging.getLogger(__name__)

API_SQL = "https://donnees.montreal.ca/api/3/action/datastore_search_sql"
RESOURCE_ID = "5232a72d-235a-48eb-ae20-bb9d501300ad"
FIELDS = (
    "id_permis, date_emission, emplacement, arrondissement, code_type_base_demande, "
    "description_type_demande, description_type_batiment, description_categorie_batiment, "
    "nature_travaux, nb_logements, longitude, latitude"
)
PAGE_SIZE = 1000
USER_AGENT = "SolecraAgents/1.0 (lead research; contact: see github.com/JeffreyNahas/solecraAI)"


class MontrealPermitsError(Exception):
    pass


class MontrealPermitSource:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
        page_pause_s: float = 2.0,
    ) -> None:
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), headers={"User-Agent": USER_AGENT}
        )
        self.sleep = sleep
        self.page_pause_s = page_pause_s

    async def aclose(self) -> None:
        await self.http.aclose()

    async def fetch_since(self, since: date) -> list[dict[str, Any]]:
        """CO and TR permits issued on or after `since`, oldest first."""
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            sql = (
                f'SELECT {FIELDS} FROM "{RESOURCE_ID}" '
                f"WHERE date_emission >= '{since.isoformat()}' "
                "AND code_type_base_demande IN ('CO', 'TR') "
                f"ORDER BY date_emission, id_permis LIMIT {PAGE_SIZE} OFFSET {offset}"
            )
            page = await self._query(sql)
            records.extend(page)
            if len(page) < PAGE_SIZE:
                return records
            offset += PAGE_SIZE
            await self.sleep(self.page_pause_s)

    async def _query(self, sql: str) -> list[dict[str, Any]]:
        last = ""
        # Firewall blocks are temporary: back off for minutes, not seconds.
        delays = [30, 120, 300]
        for attempt in range(len(delays) + 1):
            try:
                resp = await self.http.post(API_SQL, json={"sql": sql})
            except httpx.TransportError as exc:
                last = f"network error: {exc!r}"
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    if not body.get("success"):
                        raise MontrealPermitsError(f"API error: {str(body.get('error'))[:300]}")
                    return body["result"]["records"]
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in (403, 429, 500, 502, 503, 504):
                    break
            if attempt < len(delays):
                log.warning("Montréal permits API: %s, retrying in %ss", last, delays[attempt])
                await self.sleep(delays[attempt])
        raise MontrealPermitsError(f"Montréal permits API failed: {last}")


# --- Mapping a city record to our Permit ------------------------------------------


def _plain(text: str | None) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


_MINOR_LABEL = re.compile(
    r"piscine|spa\b|abattage|enseigne|excavation|numero|numerotage|stationnement|cloture|terrasse|"
    r"sollicitation|renouvellement|abri|dependance|accessoire|pieux"
)
_NEW_LABEL = re.compile(r"construction|nouveau batiment")
_CHANGE_OF_USE = re.compile(r"changement d.usage|changer l.usage|nouvel usage|abriter un usage")
_CONDO = re.compile(r"copropriete|condominium|condo\b")
_FITOUT = re.compile(
    r"amenag\w*\s+(?:le |la |les |l.|des |un |une )?(?:local|locaux|suite|bureau|commerce|espace)"
)


def work_category(code: str | None, label: str | None, description: str | None) -> WorkCategory:
    code, lab, desc = (code or "").upper(), _plain(label), _plain(description)
    if code == "DE":
        return WorkCategory.DEMOLITION
    if code == "CA" or _MINOR_LABEL.search(lab):
        return WorkCategory.MINOR
    if _NEW_LABEL.search(lab) and code == "CO":
        return WorkCategory.NEW_CONSTRUCTION
    if _CHANGE_OF_USE.search(desc):
        return WorkCategory.CHANGE_OF_USE
    if _CONDO.search(desc):
        return WorkCategory.CONDO_CONVERSION
    if _FITOUT.search(desc):
        return WorkCategory.COMMERCIAL_FITOUT
    if code in ("CO", "TR"):
        # Transformations, renovations, extensions: the scoring step judges how major they are.
        return WorkCategory.MAJOR_RENOVATION
    return WorkCategory.UNKNOWN


_UNITS_RANGE = re.compile(r"(\d+)\s*(?:a\s*\d+\s*)?(?:et\s*)?(?:log|\+)")
_UNITS_MORE_THAN = re.compile(r">\s*(\d+)\s*log")


def units_from_label(label: str) -> int | None:
    """Lower bound of the number of dwellings described by a building label."""
    t = _plain(label)
    if m := _UNITS_MORE_THAN.search(t):
        return int(m.group(1)) + 1
    if m := _UNITS_RANGE.search(t):
        return int(m.group(1))
    if re.search(r"multifamil|condominium|quadrupl|\bh4\b", t):
        return 4
    if re.search(r"bifamil|duplex|bi et tri|\bh2\b|multiplex", t):  # before "tri": lower bound
        return 2
    if re.search(r"trifamil|triplex", t):
        return 3
    if re.search(r"unifamil|cottage|bungalow|jumel|en rangee|\bh1\b|^familiale|\bfamiliale", t):
        return 1
    return None


def building_category(
    type_label: str | None, category_label: str | None, nb_logements: Any
) -> tuple[BuildingCategory, int | None]:
    t = _plain(f"{type_label or ''} / {category_label or ''}")
    if "bureau" in t:
        return BuildingCategory.OFFICE, None
    if re.search(r"gouvernement|institution|enseignement|hospital|culte|public|equi\.?\s*coll|ecole", t):
        return BuildingCategory.INSTITUTIONAL, None
    if "industri" in t:
        return BuildingCategory.INDUSTRIAL, None
    if re.search(r"commerc|mixte|service|restauration|detail|\bc1\b|\bs1\b", t):
        return BuildingCategory.COMMERCIAL, None
    if re.search(r"residentiel|habitation", t):
        if "dependance" in t or "maison de chambre" in t:
            return BuildingCategory.UNKNOWN, None
        units = units_from_label(category_label or "")
        try:
            listed = int(float(nb_logements or 0))
        except ValueError:
            listed = 0
        if listed > (units or 0):
            units = listed
        if units is None:
            return BuildingCategory.UNKNOWN, None
        return (BuildingCategory.SINGLE_FAMILY if units <= 1 else BuildingCategory.MULTI_RESIDENTIAL), units
    return BuildingCategory.UNKNOWN, None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def to_permit(record: dict[str, Any]) -> Permit:
    building, units = building_category(
        record.get("description_type_batiment"),
        record.get("description_categorie_batiment"),
        record.get("nb_logements"),
    )
    issued = record.get("date_emission")
    return Permit(
        permit_id=str(record["id_permis"]),
        issued_on=date.fromisoformat(issued[:10]) if issued else None,
        address=" ".join((record.get("emplacement") or "").split()) or None,
        borough=record.get("arrondissement"),
        work=work_category(
            record.get("code_type_base_demande"),
            record.get("description_type_demande"),
            record.get("nature_travaux"),
        ),
        building=building,
        units=units,
        description=record.get("nature_travaux"),
        work_label=record.get("description_type_demande"),
        building_label=" / ".join(
            x
            for x in (record.get("description_type_batiment"), record.get("description_categorie_batiment"))
            if x
        )
        or None,
        latitude=_float(record.get("latitude")),
        longitude=_float(record.get("longitude")),
    )
