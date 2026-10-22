"""Mapping of real City of Montréal permit records/labels (seen in the dataset, 2026-10)."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.permits.filters import reject_reason
from app.permits.model import BuildingCategory as B
from app.permits.model import WorkCategory as W
from app.permits.montreal import (
    MontrealPermitsError,
    MontrealPermitSource,
    building_category,
    to_permit,
    units_from_label,
    work_category,
)

RECORDS = json.loads((Path(__file__).parent / "fixtures" / "montreal_permits.json").read_text())


@pytest.mark.parametrize(
    ("label", "units"),
    [
        ("1 Logement", 1),
        ("unifamiliale (1 log)", 1),
        ("H1 Unifamiliale", 1),
        ("Cottage", 1),
        ("Bungalow", 1),
        ("unifamiliale en rangée", 1),
        ("Jumelée", 1),
        ("Familiale", 1),
        ("2 Logements", 2),
        ("Duplex", 2),
        ("H2 Bifamiliale", 2),
        ("bi et trifamiliale (h2)", 2),
        ("Multiplex", 2),
        ("3 Logements", 3),
        ("Triplex", 3),
        ("H3 Multiplex (3logs)", 3),
        ("4 Logements", 4),
        ("H3 Multiplex (4logs)", 4),
        ("4 à 8 logements", 4),
        ("Multi - 4 à 8 log. / 2 à 3 ét.", 4),
        ("5 à 11 Logements", 5),
        ("Multifamiliale (7 à 10 log.)", 7),
        ("8 à 12 Logements", 8),
        ("Multi - > 8 log. / max. 4 ét.", 9),
        ("Multifamiliale (11 log & plus)", 11),
        ("12 Logements +", 12),
        ("H4 Multifamiliale (12et+logs)", 12),
        ("12 à 36 logements", 12),
        ("36 logements et plus", 36),
        ("Condominium", 4),
        ("quadruplex (h3)", 4),
        ("Multifamiliale", 4),
        ("multifamiliale (h4)", 4),
    ],
)
def test_units_from_real_labels(label, units):
    assert units_from_label(label) == units


@pytest.mark.parametrize(
    ("type_label", "cat_label", "expected"),
    [
        ("Résidentiel", "1 Logement", (B.SINGLE_FAMILY, 1)),
        ("Résidentiel", "12 Logements +", (B.MULTI_RESIDENTIAL, 12)),
        ("HABITATION", "H1 Unifamiliale", (B.SINGLE_FAMILY, 1)),
        ("Habitation", "Condominium", (B.MULTI_RESIDENTIAL, 4)),
        ("Résidentiel", "Dépendance", (B.UNKNOWN, None)),
        ("Résidentiel", "Maison de chambre", (B.UNKNOWN, None)),
        ("Commercial", "Commercial", (B.COMMERCIAL, None)),
        ("Commercial", "Mixte", (B.COMMERCIAL, None)),
        ("Commerce", "Mixte (commerce et résidence)", (B.COMMERCIAL, None)),
        ("commercial", "restauration", (B.COMMERCIAL, None)),
        ("COMMERCE DE DÉTAIL", "C1 Léger", (B.COMMERCIAL, None)),
        ("Commercial", "Edifice à bureau", (B.OFFICE, None)),
        ("Gouvernemental", "Maison d'enseignement", (B.INSTITUTIONAL, None)),
        ("Gouvernemental", "Centre hospitalier", (B.INSTITUTIONAL, None)),
        ("Équi.Colle. et Institutionnels", "Équi.Colle. et Institutionnels", (B.INSTITUTIONAL, None)),
        ("Industriel", "Bâtiment industriel", (B.INDUSTRIAL, None)),
        ("INDUSTRIE", "I2 Commerce de gros", (B.INDUSTRIAL, None)),
        ("Véhicule", "Véhicule", (B.UNKNOWN, None)),
    ],
)
def test_building_category_from_real_labels(type_label, cat_label, expected):
    assert building_category(type_label, cat_label, "0") == expected


def test_nb_logements_overrides_when_larger():
    assert building_category("Résidentiel", "Multiplex", "6") == (B.MULTI_RESIDENTIAL, 6)


@pytest.mark.parametrize(
    ("code", "label", "description", "expected"),
    [
        ("DE", "Démolition", "", W.DEMOLITION),
        ("CA", "Abattage d'arbre", "", W.MINOR),
        ("CO", "Piscine résidentielle", "", W.MINOR),
        ("CO", "Construction dépendance", "", W.MINOR),
        ("CO", "Excavation", "", W.MINOR),
        (
            "CO",
            "Construction",
            "Bâtiment de 7 étages, contigu, 2 commerces, 82 logements",
            W.NEW_CONSTRUCTION,
        ),
        ("CO", "Construction bât.principal", "", W.NEW_CONSTRUCTION),
        (
            "TR",
            "Transformation",
            "RÉAMÉNAGER L'INTÉRIEUR ... POUR Y ABRITER UN USAGE DE BUREAU",
            W.CHANGE_OF_USE,
        ),
        (
            "TR",
            "Transformation",
            "Aménager le local pour l'exploitant X, conformément aux plans",
            W.COMMERCIAL_FITOUT,
        ),
        ("TR", "Transformation", "Conversion de l'immeuble en copropriété divise", W.CONDO_CONVERSION),
        ("TR", "Transformation", "Changement d\u2019usage du rez-de-chaussée", W.CHANGE_OF_USE),
        ("TR", "Transformation", "Aménager l\u2019espace pour un bureau", W.COMMERCIAL_FITOUT),
        (
            "TR",
            "Travaux extérieurs",
            "Aux 4 murs, remplacer 700 fenêtres dans les ouvertures existantes",
            W.MAJOR_RENOVATION,
        ),
        (
            "TR",
            "Agrandissement",
            "Agrandissement de 3 étages et sous-sol afin d'ajouter 8 logements",
            W.MAJOR_RENOVATION,
        ),
        ("CO", "Agrandissement", "AGRANDISSEMENT SUR RDC", W.MAJOR_RENOVATION),
    ],
)
def test_work_category(code, label, description, expected):
    assert work_category(code, label, description) == expected


def test_real_records_map_and_filter():
    s = Settings(_env_file=None)
    permits = [to_permit(r) for r in RECORDS]
    single, four, five = permits
    assert (single.permit_id, single.issued_on, single.building, single.units) == (
        "3001987762",
        date(2026, 10, 12),
        B.SINGLE_FAMILY,
        1,
    )
    assert single.address == "100 rue Exemple"  # whitespace normalised
    assert reject_reason(single, s) == "building:single_family"
    assert (four.building, four.units, reject_reason(four, s)) == (B.MULTI_RESIDENTIAL, 4, None)
    assert (five.work, five.units, five.building_label) == (
        W.MAJOR_RENOVATION,
        5,
        "Résidentiel / 5 à 11 Logements",
    )
    assert five.latitude == pytest.approx(45.5557, abs=1e-3) and reject_reason(five, s) is None


async def test_fetch_pages_and_backs_off_on_firewall_blocks():
    sqls, sleeps = [], []
    responses = iter(
        [
            httpx.Response(403, text="<title>403</title>"),
            httpx.Response(
                200,
                json={"success": True, "result": {"records": [{"id_permis": str(i)} for i in range(1000)]}},
            ),
            httpx.Response(200, json={"success": True, "result": {"records": [{"id_permis": "x"}]}}),
        ]
    )

    def handler(request):
        sqls.append(json.loads(request.content)["sql"])
        return next(responses)

    async def fake_sleep(s):
        sleeps.append(s)

    source = MontrealPermitSource(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=fake_sleep
    )
    records = await source.fetch_since(date(2026, 9, 22))
    assert len(records) == 1001
    assert sleeps == [30, 2.0]  # firewall backoff, then the pause between pages
    assert "date_emission >= '2026-09-22'" in sqls[0] and "IN ('CO', 'TR')" in sqls[0]
    assert "OFFSET 0" in sqls[1] and "OFFSET 1000" in sqls[2]


async def test_persistent_block_raises():
    async def no_sleep(_):
        return None

    source = MontrealPermitSource(
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))), sleep=no_sleep
    )
    with pytest.raises(MontrealPermitsError, match="HTTP 403"):
        await source.fetch_since(date(2026, 10, 1))
