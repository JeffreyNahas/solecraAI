"""Build the final outreach email from a draft (CLAUDE.md §2 rules 7 and 8).

The AI (or an owner) only writes the subject and body. Code adds, in the lead's language:
- the greeting ("Bonjour <contact name>," only when research found the person's name);
- the signature (sender, company, phone, website);
- the CASL footer: sender on behalf of Solecra, mailing address, phone, email, website, why the
  recipient gets this email, and the unsubscribe link;
- the List-Unsubscribe headers (one-click, RFC 8058).
So identification and unsubscribe never depend on the model.
"""

import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import Settings


class IdentityIncomplete(Exception):
    """A company identity field needed by CASL is empty in the settings."""


@dataclass(frozen=True)
class Identity:
    sender_name: str
    company_name: str
    address: str
    phone: str
    email: str
    website: str
    public_base_url: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "Identity":
        identity = cls(
            sender_name=settings.company_sender_name.strip(),
            company_name=settings.company_name.strip(),
            address=settings.company_address.strip(),
            phone=settings.company_phone.strip(),
            email=settings.company_email.strip(),
            website=settings.company_website.strip().rstrip("/"),
            public_base_url=settings.public_base_url.strip().rstrip("/"),
        )
        env_names = {
            "sender_name": "COMPANY_SENDER_NAME",
            "company_name": "COMPANY_NAME",
            "address": "COMPANY_ADDRESS",
            "phone": "COMPANY_PHONE",
            "email": "COMPANY_EMAIL",
            "website": "COMPANY_WEBSITE",
            "public_base_url": "PUBLIC_BASE_URL",
        }
        missing = [env_names[name] for name, value in identity.__dict__.items() if not value]
        if missing:
            raise IdentityIncomplete("set in .env before drafting outreach emails: " + ", ".join(missing))
        return identity

    @property
    def website_host(self) -> str:
        host = (
            urlsplit(self.website if "://" in self.website else f"//{self.website}").hostname or ""
        ).lower()
        return host[4:] if host.startswith("www.") else host

    def required_in_footer(self) -> list[str]:
        return [self.sender_name, self.company_name, self.address, self.phone, self.email, self.website]


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    headers: dict[str, str]
    unsubscribe_url: str

    @property
    def sha256(self) -> str:
        return content_hash(self.subject, self.text)


def content_hash(subject: str, text: str) -> str:
    return hashlib.sha256(f"{subject}\n\n{text}".encode()).hexdigest()


def new_unsubscribe_token() -> str:
    return secrets.token_urlsafe(16)  # 128 bits, no personal data


def _source_domain(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_TEXT = {
    "fr": {
        "hello": "Bonjour {name},",
        "hello_anon": "Bonjour,",
        "on_behalf": "{sender}, au nom de {company}",
        "why": (
            "Vous recevez ce courriel parce que votre adresse est publiée sur {source} "
            "pour votre rôle de {role}."
        ),
        "why_no_role": "Vous recevez ce courriel parce que votre adresse est publiée sur {source}.",
        "unsubscribe": "Pour ne plus recevoir de courriels de notre part : {url}",
    },
    "en": {
        "hello": "Hello {name},",
        "hello_anon": "Hello,",
        "on_behalf": "{sender}, on behalf of {company}",
        "why": (
            "You are receiving this email because your address is published on {source} "
            "for your role as {role}."
        ),
        "why_no_role": "You are receiving this email because your address is published on {source}.",
        "unsubscribe": "To stop receiving emails from us: {url}",
    },
}


def render(
    *,
    subject: str,
    body: str,
    language: str,
    contact_name: str | None,
    consent_source_url: str | None,
    consent_role: str | None,
    identity: Identity,
    unsubscribe_token: str,
) -> RenderedEmail:
    t = _TEXT["en" if language == "en" else "fr"]
    url = f"{identity.public_base_url}/u/{unsubscribe_token}"
    greeting = (
        t["hello"].format(name=contact_name.strip())
        if contact_name and contact_name.strip()
        else t["hello_anon"]
    )
    source = _source_domain(consent_source_url) or identity.website_host
    why = (
        t["why"].format(source=source, role=consent_role.strip())
        if consent_role and consent_role.strip()
        else t["why_no_role"].format(source=source)
    )
    signature = "\n".join(
        [identity.sender_name, identity.company_name, f"{identity.phone} · {identity.website}"]
    )
    footer = "\n".join(
        [
            "—",
            t["on_behalf"].format(sender=identity.sender_name, company=identity.company_name),
            identity.address,
            f"{identity.phone} · {identity.email} · {identity.website}",
            why,
            t["unsubscribe"].format(url=url),
        ]
    )
    text = f"{greeting}\n\n{body.strip()}\n\n{signature}\n\n{footer}\n"
    headers = {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return RenderedEmail(subject=subject.strip(), text=text, headers=headers, unsubscribe_url=url)
