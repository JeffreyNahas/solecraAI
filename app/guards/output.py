"""Output guard for outreach emails (CLAUDE.md §2 rules 3, 4, 5, 7, 8).

Runs on every draft text, AI-written or typed by an owner: after drafting, after each edit, and
again on the final email just before it is queued. Code only, no model. A blocked text never
reaches a prospect; there is no override.

`check_body` — the part the AI or an owner writes (subject + body):
  numbers only from the lead's own facts or the owners' approved list; no prices or costs, no
  durations, no measurements, no supplier/brand names; no links except Solecra's website; no
  email addresses or phone numbers (code adds the contact block); no placeholders or markup; the
  lead's language (Bill 96); sane lengths; no greeting/signature (code adds them).
`check_rendered` — the final email: identity, footer and unsubscribe link present.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

MAX_SUBJECT = 90
MAX_BODY = 1500
MIN_BODY = 40

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_COST_RE = re.compile(
    r"\$|€|%|\b(cad|prix|cout|couts|tarif|tarifs|rabais|escompte|escomptes|marge|marges|"
    r"fournisseur|fournisseurs|grossiste|price|prices|pricing|cost|costs|"
    r"rate|rates|discount|discounts|margin|margins|supplier|suppliers|wholesale|cheap|cheapest)\b"
)
_DURATION_RE = re.compile(
    r"\b(jour|jours|semaine|semaines|heure|heures|mois|ans|annee|annees|delai|delais|"
    r"day|days|week|weeks|hour|hours|month|months|year|years|lead\s+time|turnaround)\b"
)
_MEASURE_RE = re.compile(
    r"\b(po|pouce|pouces|pied|pieds|pi2|cm|mm|m2|metre|metres|inch|inches|ft|feet|foot|sq\s*ft)\b"
    r"|\d\s*[x×]\s*\d|²|\d\s*\"|\d\s*'"
)
_URL_RE = re.compile(
    r"https?://[^\s)>\]]+|\bwww\.[^\s)>\]]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|ca|net|org|io|co|info|biz)\b"
)
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\[|\]|\{|\}|<|>|\bxxx\b|lorem|ipsum|\btodo\b|\btbd\b", re.IGNORECASE)
_GREETING_RE = re.compile(
    r"^\s*(bonjour|bonsoir|salut|cher|chère|madame|monsieur|hello|hi|dear)\b", re.IGNORECASE
)
_SIGNOFF_RE = re.compile(
    r"(cordialement|salutations|bien à vous|au plaisir|best regards|regards|sincerely)\s*,?\s*\S*\s*$",
    re.IGNORECASE,
)

_FR_WORDS = frozenset(
    "le la les des du de un une et est nous vous votre vos pour avec sur dans notre nos qui que pas "
    "projet votre au aux ce cette".split()
)
_EN_WORDS = frozenset(
    "the and you your we our for with is are to of this that in on project would like".split()
)


@dataclass
class GuardResult:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "violations": self.violations}


@dataclass(frozen=True)
class GuardContext:
    """What a draft for this lead may contain."""

    language: str  # fr | en
    allowed_numbers: frozenset[str]  # digits from the lead's facts + the owners' approved list
    blocked_terms: tuple[str, ...]  # supplier / brand names
    company_website_host: str  # the only link host allowed


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _digits(token: str) -> str:
    return re.sub(r"\D", "", token)


def numbers_in(texts: Iterable[str]) -> frozenset[str]:
    """Digit strings found in trusted facts (e.g. "4866" from an address, "184" units)."""
    found: set[str] = set()
    for text in texts:
        for match in _NUMBER_RE.findall(text or ""):
            found.add(_digits(match))
    return frozenset(found)


def detect_language(text: str) -> str | None:
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    fr = sum(w in _FR_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if fr == en:
        return None
    return "fr" if fr > en else "en"


def _host(url: str) -> str:
    url = url.lower()
    url = re.sub(r"^https?://", "", url)
    host = url.split("/")[0].split("?")[0].strip(".,;:")
    return host[4:] if host.startswith("www.") else host


def check_body(subject: str, body: str, ctx: GuardContext) -> GuardResult:
    subject, body = (subject or "").strip(), (body or "").strip()
    text = f"{subject}\n{body}"
    plain = _plain(text)
    violations: list[str] = []

    if not subject:
        violations.append("subject is empty")
    if len(subject) > MAX_SUBJECT:
        violations.append(f"subject is too long ({len(subject)} > {MAX_SUBJECT} characters)")
    if len(body) < MIN_BODY:
        violations.append("body is too short")
    if len(body) > MAX_BODY:
        violations.append(f"body is too long ({len(body)} > {MAX_BODY} characters)")

    bad_numbers = sorted({m for m in _NUMBER_RE.findall(text) if _digits(m) not in ctx.allowed_numbers})
    if bad_numbers:
        violations.append(f"numbers not from the lead's facts or the approved list: {', '.join(bad_numbers)}")
    if m := _COST_RE.search(plain):
        violations.append(f"price/cost term: {m.group(0)!r}")
    if m := _DURATION_RE.search(plain):
        violations.append(f"duration/delay term: {m.group(0)!r}")
    if m := _MEASURE_RE.search(plain):
        violations.append(f"measurement: {m.group(0)!r}")
    for term in ctx.blocked_terms:
        if re.search(rf"\b{re.escape(_plain(term))}\b", plain):
            violations.append(f"blocked name: {term!r}")

    for email in _EMAIL_RE.findall(text):
        violations.append(f"email address in the text: {email} (contact details are added automatically)")
    text_without_emails = _EMAIL_RE.sub(" ", text)
    for url in _URL_RE.findall(text_without_emails.lower()):
        if _host(url) != ctx.company_website_host:
            violations.append(f"link to another site: {url}")
    if m := _PLACEHOLDER_RE.search(text):
        violations.append(f"placeholder or markup: {m.group(0)!r}")

    if _GREETING_RE.search(body):
        violations.append("body starts with a greeting (it is added automatically)")
    if _SIGNOFF_RE.search(body):
        violations.append("body ends with a sign-off (the signature is added automatically)")

    language = detect_language(body)
    if language != ctx.language:
        expected = "French" if ctx.language == "fr" else "English"
        violations.append(
            f"the email must be in {expected} (Bill 96: French unless the contact uses English)"
        )

    return GuardResult(not violations, violations)


def check_rendered(
    text: str, headers: dict[str, str], required: Iterable[str], unsubscribe_url: str
) -> GuardResult:
    """The final email carries every identity field, the unsubscribe link and its headers."""
    violations = [f"missing in footer: {value!r}" for value in required if not value or value not in text]
    if not unsubscribe_url or unsubscribe_url not in text:
        violations.append("unsubscribe link missing")
    if headers.get("List-Unsubscribe") != f"<{unsubscribe_url}>":
        violations.append("List-Unsubscribe header missing")
    if headers.get("List-Unsubscribe-Post") != "List-Unsubscribe=One-Click":
        violations.append("List-Unsubscribe-Post header missing")
    return GuardResult(not violations, violations)
