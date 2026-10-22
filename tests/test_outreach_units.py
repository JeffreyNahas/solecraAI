"""Output guard, renderer, owner-text parsing and the Québec business-day calendar (#47)."""

from datetime import date

import pytest

from app.drafting import parse_owner_text
from app.guards.output import GuardContext, check_body, check_rendered, detect_language, numbers_in
from app.outreach.calendar import business_days_between, is_business_day, quebec_holidays
from app.outreach.render import Identity, IdentityIncomplete, render

CTX = GuardContext(
    language="fr",
    allowed_numbers=numbers_in(["4866 rue Harry-Worth", "184"]),
    blocked_terms=("Hunter Douglas",),
    company_website_host="solecrablinds.com",
)

GOOD_SUBJECT = "Stores et rideaux pour votre projet rue Harry-Worth"
GOOD_BODY = (
    "Nous avons vu que vous développez le projet de 184 logements au 4866 rue Harry-Worth.\n\n"
    "Solecra fournit et installe des stores et des rideaux pour les projets résidentiels et commerciaux "
    "à Montréal.\n\n"
    "Nous vous offrons une évaluation gratuite sur place, au moment qui vous convient. "
    "Seriez-vous disponible pour en discuter?"
)


def test_a_good_french_draft_passes():
    result = check_body(GOOD_SUBJECT, GOOD_BODY, CTX)
    assert result.ok, result.violations


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (GOOD_BODY + " Nos prix commencent à 99.", "numbers not from"),
        (GOOD_BODY + " Nos prix sont imbattables.", "price/cost"),
        (GOOD_BODY + " Profitez de 10 % de rabais.", "price/cost"),
        (GOOD_BODY + " Installation en deux semaines.", "duration"),
        (GOOD_BODY + " Nous avons 15 ans d'expérience.", "numbers not from"),
        (GOOD_BODY + " Fenêtres de 36 x 48 po.", "measurement"),
        (GOOD_BODY + " Nous installons les toiles Hunter Douglas.", "blocked name"),
        (GOOD_BODY + " Voir https://evil.example.com/promo", "link to another site"),
        (GOOD_BODY + " Écrivez à jean@autre.ca", "email address"),
        (GOOD_BODY + " Rappel: [NOM DU CONTACT]", "placeholder"),
        ("Bonjour Marie,\n\n" + GOOD_BODY, "greeting"),
        (GOOD_BODY + "\n\nCordialement,\nCarl", "sign-off"),
        (
            "We saw your project with 184 units. Solecra supplies and installs blinds for projects like "
            "yours in Montréal. We would like to offer you a free on-site evaluation.",
            "must be in French",
        ),
    ],
)
def test_guard_blocks(body, expected):
    result = check_body(GOOD_SUBJECT, body, CTX)
    assert not result.ok and any(expected in v for v in result.violations), result.violations


def test_company_link_and_owner_listed_numbers_allowed():
    ctx = GuardContext("fr", CTX.allowed_numbers | {"2"}, (), "solecrablinds.com")
    body = GOOD_BODY + " Nos réalisations : https://solecrablinds.com/projets. Nous sommes 2 associés."
    assert check_body(GOOD_SUBJECT, body, ctx).ok


def test_injected_instructions_in_lead_facts_are_caught_downstream():
    """Prompt injection (§2 rule 5): even if the model copies an injected line from a business
    name or a web page, the guard refuses the link and the address it asks for."""
    injected = (
        GOOD_BODY + " Contactez-nous plutôt sur www.cheap-blinds-deals.com ou deals@cheap-blinds-deals.com."
    )
    result = check_body(GOOD_SUBJECT, injected, CTX)
    assert not result.ok
    assert any("link to another site" in v for v in result.violations)
    assert any("email address" in v for v in result.violations)


def test_language_detection():
    assert detect_language(GOOD_BODY) == "fr"
    assert detect_language("We would like to offer you a free evaluation for your project.") == "en"


IDENTITY = Identity(
    sender_name="Carl Abi Yaghi",
    company_name="Solecra",
    address="1 rue Exemple, Saint-Laurent, QC H4R 1B3",
    phone="+1 514 555-0100",
    email="info@solecra.example",
    website="https://solecrablinds.com",
    public_base_url="https://bot.solecra.example",
)


def test_render_adds_greeting_signature_and_casl_footer():
    email = render(
        subject=GOOD_SUBJECT,
        body=GOOD_BODY,
        language="fr",
        contact_name="Marie Tremblay",
        consent_source_url="https://www.groupehd.example/contact",
        consent_role="developer",
        identity=IDENTITY,
        unsubscribe_token="tok123",
    )
    assert email.text.startswith("Bonjour Marie Tremblay,\n\n" + GOOD_BODY)
    assert "Carl Abi Yaghi, au nom de Solecra" in email.text
    assert "publiée sur groupehd.example pour votre rôle de developer" in email.text
    assert (
        email.unsubscribe_url == "https://bot.solecra.example/u/tok123"
        and email.unsubscribe_url in email.text
    )
    assert email.headers == {
        "List-Unsubscribe": "<https://bot.solecra.example/u/tok123>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert check_rendered(email.text, email.headers, IDENTITY.required_in_footer(), email.unsubscribe_url).ok
    assert len(email.sha256) == 64


def test_render_english_without_contact_name():
    email = render(
        subject="Blinds for your project",
        body="We would like to offer you a free on-site evaluation.",
        language="en",
        contact_name=None,
        consent_source_url=None,
        consent_role=None,
        identity=IDENTITY,
        unsubscribe_token="t",
    )
    assert email.text.startswith("Hello,\n\n")
    assert "Carl Abi Yaghi, on behalf of Solecra" in email.text
    assert "published on solecrablinds.com." in email.text


def test_check_rendered_catches_a_missing_footer_field():
    email = render(
        subject="s",
        body="b",
        language="fr",
        contact_name=None,
        consent_source_url=None,
        consent_role=None,
        identity=IDENTITY,
        unsubscribe_token="t",
    )
    broken = email.text.replace(IDENTITY.address, "")
    result = check_rendered(broken, email.headers, IDENTITY.required_in_footer(), email.unsubscribe_url)
    assert not result.ok and any("missing in footer" in v for v in result.violations)


def test_identity_must_be_complete(settings):
    with pytest.raises(IdentityIncomplete, match="COMPANY_SENDER_NAME.*PUBLIC_BASE_URL"):
        Identity.from_settings(settings)


def test_owner_text_subject_line_and_greeting():
    subject, body = parse_owner_text("Objet: Nouveau sujet\n\nBonjour Marie,\nTexte de l'owner.", "Ancien")
    assert (subject, body) == ("Nouveau sujet", "Texte de l'owner.")
    assert parse_owner_text("Juste le texte.", "Ancien") == ("Ancien", "Juste le texte.")


def test_quebec_holidays_2026():
    assert sorted(quebec_holidays(2026)) == [
        date(2026, 1, 1),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 18),
        date(2026, 6, 24),
        date(2026, 7, 1),
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 12, 25),
    ]
    assert not is_business_day(date(2026, 10, 24))  # Saturday
    assert is_business_day(date(2026, 10, 22))
    # Thursday → Monday is 2 business days; across Thanksgiving Monday it's 1.
    assert business_days_between(date(2026, 10, 22), date(2026, 10, 26)) == 2
    assert business_days_between(date(2026, 10, 9), date(2026, 10, 13)) == 1
