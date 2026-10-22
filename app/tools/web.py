"""Fetch public web pages for lead research and consent checks.

The model picks the URLs, so this module is the safety boundary:
- http(s) only; hosts that resolve to private, loopback or other internal addresses are refused
  (checked again on every redirect);
- social networks are refused (CLAUDE.md §7: never scrape Facebook or LinkedIn);
- the address actually connected to is checked too (a host can change its DNS answer between
  our check and the connection: "DNS rebinding");
- timeouts (per read and per page), a size cap and an identifying User-Agent; no cookies, no
  JavaScript.

Pages are returned as plain text plus the emails and links found on them. Page text is untrusted
data (CLAUDE.md §2 rule 5): callers hand it to a model only inside a clearly marked data block.
"""

import asyncio
import codecs
import hashlib
import html
import ipaddress
import logging
import os
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
TIMEOUT = httpx.Timeout(15.0, connect=10.0)  # per connect / read
PAGE_DEADLINE = 30.0  # whole page, redirects included: a server can't drip-feed for hours
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

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

# Search engines and their page indexes: an email seen there was not published there, so such a
# page is never accepted as consent evidence (app/guards/consent.py). Live run 2026-10-22: the
# model cited an exa.ai index page as the source of an address. Matched with subdomains, except
# the hosts in NOT_SEARCH_ENGINE, where small companies host their own websites.
SEARCH_ENGINE_DOMAINS = frozenset(
    {
        "exa.ai",
        "google.com",
        "google.ca",
        "bing.com",
        "duckduckgo.com",
        "search.yahoo.com",
        "search.brave.com",
        "perplexity.ai",
        "yandex.com",
        "ecosia.org",
    }
)
NOT_SEARCH_ENGINE = frozenset({"sites.google.com"})
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_\-]+)""", re.IGNORECASE)

EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
# Common ways sites hide addresses from bots: "info [at] site [dot] com", "info(at)site.com".
_AT_RE = re.compile(r"\s*[\[\(\{]\s*(?:at|arobase)\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{]\s*(?:dot|point)\s*[\]\)\}]\s*", re.IGNORECASE)
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "footer"}


class PageError(Exception):
    """The page could not be fetched safely (blocked, internal address, too big, HTTP error...).

    `retryable`: a passing problem (network error, timeout, 5xx, 408/429) worth trying another day.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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
    """Lowercase host without a leading 'www.'. A bare domain ("acme.ca/contact") counts as a URL."""
    url = url.strip()
    if "://" not in url:
        url = "//" + url.lstrip("/")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(host: str, domain: str) -> bool:
    """True if `host` is `domain` or one of its subdomains."""
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_blocked(url: str) -> bool:
    host = host_of(url)
    return any(same_site(host, d) for d in BLOCKED_DOMAINS)


def is_search_engine(url: str) -> bool:
    host = host_of(url)
    if any(same_site(host, h) for h in NOT_SEARCH_ENGINE):
        return False
    return any(same_site(host, d) for d in SEARCH_ENGINE_DOMAINS)


def deobfuscate(text: str) -> str:
    """'info [at] site [dot] ca' → 'info@site.ca' (common ways sites hide addresses from bots)."""
    return _DOT_RE.sub(".", _AT_RE.sub("@", text))


def find_emails(text: str) -> list[str]:
    """Emails in text, including '[at]'/'(dot)' obfuscations. Lowercase, in order, no duplicates."""
    deobfuscated = deobfuscate(text)
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
        deadline: float = PAGE_DEADLINE,
        check_peer: bool | None = None,
    ) -> None:
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self.resolve = resolve
        self.max_bytes = max_bytes
        self.deadline = deadline
        # Behind an HTTP proxy we connect to the proxy, which resolves names itself: nothing to
        # check here. The production server uses no proxy.
        self.check_peer = (
            (not any(os.environ.get(k) for k in PROXY_ENV)) if check_peer is None else check_peer
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def fetch(self, url: str) -> Page:
        try:
            async with asyncio.timeout(self.deadline):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise PageError(f"page took more than {self.deadline:.0f} s", retryable=True) from exc

    async def _fetch(self, url: str) -> Page:
        current = url.strip()
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_url(current)
            try:
                async with self.http.stream(
                    "GET", current, follow_redirects=False, headers={"User-Agent": USER_AGENT}
                ) as resp:
                    self._check_peer(resp)
                    if resp.is_redirect and (location := resp.headers.get("location")):
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        code = resp.status_code
                        raise PageError(f"HTTP {code}", retryable=code >= 500 or code in (408, 429))
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not any(t in ctype for t in ("text/html", "text/plain", "xhtml")):
                        raise PageError(f"not a web page ({ctype.split(';')[0]})")
                    body = await self._read_capped(resp)
                    encoding = _charset(resp, body)
            except httpx.HTTPError as exc:
                raise PageError(f"network error: {type(exc).__name__}", retryable=True) from exc
            raw = body.decode(encoding, errors="replace")
            return _parse(url, current, resp.status_code, raw, plain="text/plain" in ctype)
        raise PageError("too many redirects")

    def _check_peer(self, resp: httpx.Response) -> None:
        """Refuse the response if the connection went to an internal address (DNS rebinding)."""
        if not self.check_peer:
            return
        stream = resp.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if not peer:
            return  # test transports have no socket
        try:
            address = ipaddress.ip_address(str(peer[0]).split("%")[0])
        except ValueError as exc:
            raise PageError("unknown peer address") from exc
        if not address.is_global:
            raise PageError("internal or reserved address refused")

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
                address = href[7:].split("?")[0]
                self.mailtos.append(address)
                # Kept in the text too, where it sits on the page: the consent evidence excerpt
                # and the "named next to the email" check read the text.
                self.parts.append(f" ({address}) ")
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


def _charset(resp: httpx.Response, body: bytes) -> str:
    """Charset from the HTTP header, else from <meta charset> (older Québec sites use latin-1), else UTF-8."""
    candidates = [resp.charset_encoding]
    if match := _META_CHARSET_RE.search(body[:4096]):
        candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        if name:
            try:
                return codecs.lookup(name).name
            except LookupError:
                continue
    return "utf-8"


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
