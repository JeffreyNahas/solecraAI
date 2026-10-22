"""Leads: normalization, dedupe and status transitions.

Dedupe rule (CLAUDE.md §6): two leads are the same if they share a company email domain,
a normalized phone number, or a normalized address. Free-mail domains (gmail.com, ...) never
count as a match, since unrelated people share them.
"""

import re
import unicodedata
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import normalize_phone as _digits
from app.db.audit import audit
from app.db.models import LEAD_STATUSES, Lead

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "hotmail.ca",
        "outlook.com",
        "live.com",
        "live.ca",
        "msn.com",
        "yahoo.com",
        "yahoo.ca",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "videotron.ca",
        "videotron.qc.ca",
        "bell.net",
        "sympatico.ca",
        "rogers.com",
        "gmx.com",
    }
)

# Street words written different ways (FR/EN) mapped to one form.
_ADDRESS_WORDS = {
    "street": "rue",
    "avenue": "av",
    "ave": "av",
    "boulevard": "boul",
    "blvd": "boul",
    "bd": "boul",
    "road": "ch",
    "chemin": "ch",
    "ouest": "o",
    "west": "o",
    "w": "o",
    "est": "e",
    "east": "e",
    "nord": "n",
    "north": "n",
    "sud": "s",
    "south": "s",
    "saint": "st",
    "sainte": "ste",
    "suite": "",
    "bureau": "",
    "app": "",
    "apt": "",
    "unit": "",
    "local": "",
}
_FILLER_WORDS = frozenset({"de", "du", "des", "la", "le", "l", "d", "qc", "quebec", "canada"})
_POSTAL_RE = re.compile(r"\b([a-z]\d[a-z])\s?(\d[a-z]\d)\b")


def email_domain(email: str | None) -> str | None:
    """Company domain of an email, or None for free-mail and invalid addresses."""
    if not email or "@" not in email:
        return None
    domain = email.strip().lower().rsplit("@", 1)[1]
    if not domain or "." not in domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain


def normalize_phone(phone: str | None) -> str | None:
    """North American numbers as 11 digits starting with 1; others as plain digits."""
    if not phone:
        return None
    digits = _digits(phone)
    if len(digits) == 10:
        digits = "1" + digits
    return digits if len(digits) >= 10 else None


def normalize_address(address: str | None) -> str | None:
    """Comparable form of an address: lowercase, no accents or punctuation, FR/EN street words unified,
    postal code joined, filler words dropped, words sorted."""
    if not address:
        return None
    text = unicodedata.normalize("NFKD", address).encode("ascii", "ignore").decode().lower()
    text = _POSTAL_RE.sub(lambda m: m.group(1) + m.group(2), text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = {_ADDRESS_WORDS.get(w, w) for w in text.split()} - _FILLER_WORDS - {""}
    # Sorted words: French puts the street type first ("rue Ontario"), English last ("Ontario St").
    return " ".join(sorted(words)) or None


class LeadIn(BaseModel):
    """Data needed to create a lead. Every source (permits, tips, places) produces this."""

    source: str
    source_ref: str | None = None
    business_name: str | None = None
    contact_name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None
    language: str = "fr"
    relevance_note: str | None = None
    source_details: dict[str, Any] | None = None


async def find_duplicate(
    session: AsyncSession, *, email: str | None, phone: str | None, address: str | None
) -> Lead | None:
    conditions = []
    if domain := email_domain(email):
        conditions.append(Lead.email_domain == domain)
    if phone_n := normalize_phone(phone):
        conditions.append(Lead.phone_normalized == phone_n)
    if address_n := normalize_address(address):
        conditions.append(Lead.address_normalized == address_n)
    if not conditions:
        return None
    result = await session.execute(select(Lead).where(or_(*conditions)).order_by(Lead.id).limit(1))
    return result.scalar_one_or_none()


async def insert_lead(session: AsyncSession, data: LeadIn, actor: str = "system") -> tuple[Lead, bool]:
    """Insert a lead unless it already exists. Returns (lead, created).

    Existing = same (source, source_ref), or a dedupe match on domain/phone/address.
    """
    if data.source_ref:
        existing = (
            await session.execute(
                select(Lead).where(Lead.source == data.source, Lead.source_ref == data.source_ref)
            )
        ).scalar_one_or_none()
        if existing:
            return existing, False
    duplicate = await find_duplicate(session, email=data.email, phone=data.phone, address=data.address)
    if duplicate:
        return duplicate, False

    values: dict[str, Any] = data.model_dump()
    values.update(
        email=data.email.strip().lower() if data.email else None,
        email_domain=email_domain(data.email),
        phone_normalized=normalize_phone(data.phone),
        address_normalized=normalize_address(data.address),
        status="new",
        version=1,
    )
    # ON CONFLICT covers two jobs inserting the same source_ref at the same time.
    result = await session.execute(
        insert(Lead)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_leads_source_ref")
        .returning(Lead.id)
    )
    lead_id = result.scalar_one_or_none()
    if lead_id is None:
        existing = (
            await session.execute(
                select(Lead).where(Lead.source == data.source, Lead.source_ref == data.source_ref)
            )
        ).scalar_one()
        return existing, False
    audit(session, actor, "lead_created", "lead", lead_id, source=data.source)
    lead = await session.get(Lead, lead_id)
    assert lead is not None
    return lead, True


# --- Status transitions --------------------------------------------------------

EXIT_STATUSES = frozenset({"disqualified", "no_valid_contact", "do_not_contact"})
TERMINAL_STATUSES = frozenset({"won", "lost"}) | EXIT_STATUSES

_FORWARD: dict[str, set[str]] = {
    "new": {"researched"},
    "researched": {"qualified"},
    "qualified": {"drafted"},
    "drafted": {"approved", "qualified"},  # qualified = draft rejected, redraft later
    "approved": {"sent", "drafted"},  # drafted = owner wants changes before sending
    "sent": {"follow_up_1", "replied"},
    "follow_up_1": {"follow_up_2", "replied"},
    "follow_up_2": {"replied", "lost"},
    "replied": {"site_visit_booked", "won", "lost"},
    "site_visit_booked": {"won", "lost", "replied"},
    "won": set(),
    "lost": {"replied"},  # they came back
}


def allowed_transitions(current: str) -> set[str]:
    allowed = set(_FORWARD.get(current, set()))
    if current not in TERMINAL_STATUSES:
        allowed |= {"disqualified", "no_valid_contact"}
    allowed.add("do_not_contact")  # an opt-out always applies, whatever the stage
    allowed.discard(current)
    return allowed


class TransitionError(Exception):
    pass


async def transition(
    session: AsyncSession,
    lead_id: int,
    new_status: str,
    *,
    expected_version: int,
    actor: str,
    reason: str | None = None,
) -> Lead:
    """Move a lead to `new_status` if allowed and nobody changed it since `expected_version`."""
    if new_status not in LEAD_STATUSES:
        raise TransitionError(f"unknown status {new_status!r}")
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise TransitionError(f"lead {lead_id} not found")
    if new_status not in allowed_transitions(lead.status):
        raise TransitionError(f"lead {lead_id}: {lead.status} -> {new_status} not allowed")

    result = await session.execute(
        update(Lead)
        .where(Lead.id == lead_id, Lead.version == expected_version, Lead.status == lead.status)
        .values(status=new_status, version=Lead.version + 1)
        .returning(Lead.version)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        raise TransitionError(f"lead {lead_id} was changed by someone else (version {expected_version})")
    audit(session, actor, "lead_status", "lead", lead_id, frm=lead.status, to=new_status, reason=reason)
    await session.refresh(lead)
    return lead


async def reopen(
    session: AsyncSession, lead_id: int, *, expected_version: int, actor: str, reason: str
) -> None:
    """`disqualified` → `new`, so the lead is scored again (e.g. a new permit at the same address).

    Deliberately outside `transition()`: exits are final for everything else. Callers decide when
    a disqualification may be revisited (the permits pipeline: only one made by the score)."""
    result = await session.execute(
        update(Lead)
        .where(Lead.id == lead_id, Lead.version == expected_version, Lead.status == "disqualified")
        .values(status="new", version=Lead.version + 1)
        .returning(Lead.version)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        raise TransitionError(f"lead {lead_id} is not disqualified at version {expected_version}")
    audit(session, actor, "lead_status", "lead", lead_id, frm="disqualified", to="new", reason=reason)
