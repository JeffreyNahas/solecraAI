"""CASL consent check for a researched contact (CLAUDE.md §2 rule 7, §7 pipeline step 4).

Implied consent by "conspicuous publication" needs: the address was published by or for the
business, with no statement that they don't want commercial messages, and our message is relevant
to their business role. The model only proposes a contact; this code re-fetches the cited page
itself and decides. Nothing the model or the page says can skip a check.

A contact passes only if, on a fresh fetch of `source_url`:
  1. the page loads and is not a social network or a search engine's page;
  2. the email is published on it (text or mailto link);
  3. there is no "no solicitation" notice;
  4. the page names the business: anywhere on its own website (which the model claims, so the
     claim is checked too), or next to the email on a directory (a listing page names many firms),
     where the address itself must also carry the business name;
  5. a company-domain email matches the business's website domain.
The caller also checks the suppression list (needs the database).

A fetch that fails for a passing reason (network, timeout, 5xx) is marked `retryable`: the caller
tries again another day instead of closing the lead.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.leads import FREE_MAIL_DOMAINS, email_domain
from app.tools.web import (
    EMAIL_RE,
    PageError,
    PageFetcher,
    deobfuscate,
    host_of,
    is_blocked,
    is_search_engine,
    same_site,
)

# Notices meaning "don't send us commercial messages" (FR/EN), compared without accents.
NO_SOLICITATION = (
    "pas de sollicitation",
    "aucune sollicitation",
    "sollicitation interdite",
    "sans sollicitation",
    "ne pas solliciter",
    "ne sollicitez pas",
    "aucun demarchage",
    "pas de demarchage",
    "ne desire pas recevoir",
    "ne souhaite pas recevoir",
    "no solicitation",
    "no solicitations",
    "do not solicit",
    "no unsolicited",
    "not wish to receive",
    "not want to receive",
)

# Words that don't identify a business on their own: legal forms, articles and trade words
# ("Rénovations Tremblay" and "Rénovations Express" must not match each other).
_NAME_STOPWORDS = frozenset(
    {
        "le",
        "la",
        "de",
        "du",
        "en",
        "au",
        "of",
        "sa",
        "sec",
        "senc",
        "enr",
        "inc",
        "ltee",
        "ltd",
        "llc",
        "corp",
        "co",
        "cie",
        "the",
        "les",
        "des",
        "and",
        "et",
        "groupe",
        "group",
        "construction",
        "constructions",
        "immobilier",
        "immobiliere",
        "developpement",
        "developpements",
        "gestion",
        "entreprises",
        "services",
        "canada",
        "quebec",
        "montreal",
        "renovation",
        "renovations",
        "reno",
        "renos",
        "property",
        "properties",
        "proprietes",
        "development",
        "developments",
        "realty",
        "real",
        "estate",
        "homes",
        "home",
        "habitation",
        "habitations",
        "condos",
        "condo",
        "immeubles",
        "management",
        "holdings",
        "investments",
        "investissements",
        "capital",
        "solutions",
        "contracting",
        "contractor",
        "contractors",
        "entrepreneur",
        "entrepreneurs",
        "entreprise",
        "general",
        "generale",
        "batiment",
        "batiments",
        "design",
        "interieur",
        "interieurs",
        "interior",
        "interiors",
        "architecture",
        "architectes",
        "architects",
    }
)

EXCERPT_CHARS = 200


@dataclass
class ConsentCheck:
    ok: bool
    reason: str | None  # why it failed, in plain words (stored on the lead)
    evidence: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False  # the page couldn't be fetched for a passing reason


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9@.\- ]+", " ", text).split())


def name_tokens(name: str) -> list[str]:
    return [w for w in _plain(name).replace(".", " ").split() if len(w) >= 2 and w not in _NAME_STOPWORDS]


def names_business(page_text: str, business_name: str) -> bool:
    """True if the text mentions the business: every distinctive word when it has one or two
    ("Groupe HD" → "hd"), else a strict majority of them. Names made only of generic words must
    appear in full."""
    tokens = name_tokens(business_name)
    if not tokens:
        return _plain(business_name) in _plain(page_text)
    words = set(_plain(page_text).replace(".", " ").split())
    found = sum(t in words for t in tokens)
    return found == len(tokens) if len(tokens) <= 2 else found * 2 > len(tokens)


def email_mentions_business(email: str, business_name: str) -> bool:
    """True if the address itself carries the business name (a distinctive word of it, or the whole
    name when it has none): renotremblay@gmail.com fits "Rénovations Tremblay"."""
    compact = re.sub(r"[^a-z0-9]", "", _plain(email))
    tokens = name_tokens(business_name)
    if not tokens:
        return re.sub(r"[^a-z0-9]", "", _plain(business_name)) in compact
    return any(t in compact for t in tokens)


def has_no_solicitation_notice(page_text: str) -> bool:
    plain = _plain(page_text)
    return any(phrase in plain for phrase in NO_SOLICITATION)


def _excerpt(text: str, email: str) -> str:
    text = deobfuscate(text)  # "ventes [at] firm [dot] ca" is the published form of the address
    i = text.lower().find(email)
    if i < 0:
        return ""
    return " ".join(text[max(0, i - EXCERPT_CHARS) : i + len(email) + EXCERPT_CHARS].split())


async def verify_consent(
    fetcher: PageFetcher,
    *,
    email: str,
    source_url: str,
    business_name: str,
    website: str | None,
) -> ConsentCheck:
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return ConsentCheck(False, "invalid email address")
    if is_blocked(source_url):
        return ConsentCheck(False, "source is a social network")
    if is_search_engine(source_url):
        return ConsentCheck(False, "source is a search engine page, not where the email was published")
    try:
        page = await fetcher.fetch(source_url)
    except PageError as exc:
        return ConsentCheck(False, f"source page could not be checked ({exc})", retryable=exc.retryable)

    evidence: dict[str, Any] = {
        "url": source_url,
        "final_url": page.final_url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "http_status": page.status,
        "page_title": page.title[:200],
        "page_sha256": page.sha256,
        "excerpt": _excerpt(page.text, email),
    }

    def fail(reason: str) -> ConsentCheck:
        return ConsentCheck(False, reason, evidence)

    if is_blocked(page.final_url):
        return fail("source redirects to a social network")
    if is_search_engine(page.final_url):
        return fail("source redirects to a search engine page")
    if email not in page.emails:
        return fail("email not published on the source page")
    if has_no_solicitation_notice(page.text):
        return fail("source page says no solicitation")

    page_host = host_of(page.final_url)
    site_host = host_of(website) if website else ""
    on_own_site = bool(site_host) and same_site(page_host, site_host)
    domain = email_domain(email)  # None for free-mail
    if on_own_site:
        # The model chose `website`: the page must still show it is this business's site.
        if not names_business(page.title + "\n" + page.text, business_name):
            return fail("source page does not name the business")
        evidence["source_type"] = "company_site"
    else:
        if not names_business(evidence["excerpt"], business_name):
            return fail("business not named next to the email on the source page")
        # A listing page shows many firms side by side and text position can't tell them apart:
        # the address must carry the business's name (no website domain to compare it with here).
        if not email_mentions_business(email, business_name):
            return fail("email address does not carry the business name (directory)")
        evidence["source_type"] = "directory"
    if domain and site_host and not (same_site(domain, site_host) or same_site(site_host, domain)):
        return fail("email domain differs from the company website")
    evidence["free_mail"] = email.rsplit("@", 1)[-1] in FREE_MAIL_DOMAINS
    return ConsentCheck(True, None, evidence)
