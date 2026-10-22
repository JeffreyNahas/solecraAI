"""Fetch public web pages for lead research and consent checks.

The model picks the URLs, so this module is the safety boundary:
- http(s) only; hosts that resolve to private, loopback or other internal addresses are refused
  (checked again on every redirect);
- social networks are refused (CLAUDE.md §7: never scrape Facebook or LinkedIn);
- timeouts, a size cap and an identifying User-Agent; no cookies, no JavaScript.

Pages are returned as plain text plus the emails and links found on them. Page text is untrusted
data (CLAUDE.md §2 rule 5): callers hand it to a model only inside a clearly marked data block.
"""

import asyncio
import hashlib
import html
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "SolecraAgents/1.0 (business contact research; github.com/JeffreyNahas/solecraAI)"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# Never fetched: social networks (no scraping, CLAUDE.md §7).
BLOCKED_DOMAINS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "linkedin.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "threads.net",
    }
)

EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
# Common ways sites hide addresses from bots: "info [at] site [dot] com", "info(at)site.com".
_AT_RE = re.compile(r"\s*[\[\(\{]\s*(?:at|arobase)\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{]\s*(?:dot|point)\s*[\]\)\}]\s*", re.IGNORECASE)
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "footer"}


class PageError(Exception):
    """The page could not be fetched safely (blocked, internal address, too big, HTTP error...)."""


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    title: str
    text: str
    emails: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, link text)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def host_of(url: str) -> str:
    """Lowercase host without a leading 'www.'."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(host: str, domain: str) -> bool:
    """True if `host` is `domain` or one of its subdomains."""
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_blocked(url: str) -> bool:
    host = host_of(url)
    return any(same_site(host, d) for d in BLOCKED_DOMAINS)


def find_emails(text: str) -> list[str]:
    """Emails in text, including '[at]'/'(dot)' obfuscations. Lowercase, in order, no duplicates."""
    deobfuscated = _DOT_RE.sub(".", _AT_RE.sub("@", text))
    seen: dict[str, None] = {}
    for match in EMAIL_RE.findall(deobfuscated):
        email = match.lower().strip(".")
        if email.rsplit(".", 1)[-1] not in {"png", "jpg", "jpeg", "gif", "svg", "webp"}:
            seen.setdefault(email, None)
    return list(seen)


Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve_host(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class PageFetcher:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        resolve: Resolver = resolve_host,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self.resolve = resolve
        self.max_bytes = max_bytes

    async def aclose(self) -> None:
        await self.http.aclose()

    async def fetch(self, url: str) -> Page:
        current = url.strip()
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_url(current)
            try:
                async with self.http.stream(
                    "GET", current, follow_redirects=False, headers={"User-Agent": USER_AGENT}
                ) as resp:
                    if resp.is_redirect and (location := resp.headers.get("location")):
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise PageError(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not any(t in ctype for t in ("text/html", "text/plain", "xhtml")):
                        raise PageError(f"not a web page ({ctype.split(';')[0]})")
                    body = await self._read_capped(resp)
                    encoding = resp.encoding or "utf-8"
            except httpx.HTTPError as exc:
                raise PageError(f"network error: {type(exc).__name__}") from exc
            raw = body.decode(encoding, errors="replace")
            return _parse(url, current, resp.status_code, raw, plain="text/plain" in ctype)
        raise PageError("too many redirects")

    async def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PageError("only http(s) URLs are allowed")
        if is_blocked(url):
            raise PageError("social networks are not fetched")
        host = parts.hostname
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(a.split("%")[0]) for a in await self.resolve(host)]
            except OSError as exc:
                raise PageError(f"cannot resolve {host}") from exc
        if not addresses or any(not a.is_global for a in addresses):
            raise PageError("internal or reserved address refused")

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise PageError("page too large")
            chunks.append(chunk)
        return b"".join(chunks)


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title = ""
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0])
            elif href and not href.startswith(("#", "javascript:", "tel:")):
                self._href = urljoin(self.base_url, href)
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._link_text).split())[:80]))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.parts.append(data)
        if self._href:
            self._link_text.append(data)


def _parse(url: str, final_url: str, status: int, raw: str, plain: bool = False) -> Page:
    if plain:
        text = raw
        title, links, mailtos = "", [], []
    else:
        parser = _TextExtractor(final_url)
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
        title, links, mailtos = parser.title, parser.links, parser.mailtos
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape(text))
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    emails = find_emails(text + "\n" + "\n".join(html.unescape(m) for m in mailtos))
    unique_links = list(dict.fromkeys(links))
    return Page(url, final_url, status, " ".join(title.split()), text, emails, unique_links)
