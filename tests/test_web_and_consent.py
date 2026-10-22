import asyncio
import http.server
import threading

import httpx
import pytest

from app.guards.consent import has_no_solicitation_notice, names_business, verify_consent
from app.tools.web import PageError, PageFetcher, find_emails, host_of, is_search_engine

PUBLIC_IP = "151.101.1.1"


def fetcher(pages: dict[str, httpx.Response | str], ips: dict[str, str] | None = None, **kw) -> PageFetcher:
    """Fake web: URL → response (str = 200 HTML). Hosts resolve to a public IP unless overridden."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(str(request.url))
        if page is None:
            return httpx.Response(404)
        if isinstance(page, str):
            return httpx.Response(200, html=page)
        return page

    async def resolve(host: str) -> list[str]:
        return [(ips or {}).get(host, PUBLIC_IP)]

    return PageFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve=resolve, **kw)


CONTACT_PAGE = """<html><head><title>Nous joindre | Groupe Habitat Nord</title>
<script>var x = "hidden@tracker.com";</script></head>
<body><nav><a href="/projets">Projets</a> <a href="https://facebook.com/habitatnord">Facebook</a></nav>
<h1>Groupe Habitat Nord inc.</h1><p>Développement résidentiel à Montréal.</p>
<p>Courriel : <a href="mailto:info@habitatnord.ca?subject=Bonjour">info@habitatnord.ca</a></p>
<p>Ventes : ventes [at] habitatnord [dot] ca</p></body></html>"""


async def test_fetch_extracts_text_emails_and_links():
    page = await fetcher({"https://habitatnord.ca/contact": CONTACT_PAGE}).fetch(
        "https://habitatnord.ca/contact"
    )
    assert page.title == "Nous joindre | Groupe Habitat Nord"
    assert page.emails == ["info@habitatnord.ca", "ventes@habitatnord.ca"]
    assert "hidden@tracker.com" not in page.text  # script content is not page text
    assert ("https://habitatnord.ca/projets", "Projets") in page.links
    assert len(page.sha256) == 64


@pytest.mark.parametrize(
    ("url", "ips", "error"),
    [
        ("ftp://site.ca/x", None, "only http"),
        ("http://127.0.0.1/admin", None, "internal"),
        ("http://169.254.169.254/latest/meta-data", None, "internal"),
        ("http://intranet.local/", {"intranet.local": "10.0.0.5"}, "internal"),
        ("http://site.ca/", {"site.ca": "::1"}, "internal"),
        ("https://www.facebook.com/someone", None, "social"),
        ("https://ca.linkedin.com/company/x", None, "social"),
    ],
)
async def test_fetch_refuses_unsafe_targets(url, ips, error):
    with pytest.raises(PageError, match=error):
        await fetcher({url: "<p>x</p>"}, ips).fetch(url)


async def test_redirect_to_internal_address_refused():
    pages = {"https://short.link/a": httpx.Response(302, headers={"location": "http://10.1.2.3/secret"})}
    with pytest.raises(PageError, match="internal"):
        await fetcher(pages).fetch("https://short.link/a")


async def test_redirects_followed_and_limits_enforced():
    pages = {
        "https://a.ca/": httpx.Response(301, headers={"location": "/fr/"}),
        "https://a.ca/fr/": "<p>Bonjour</p>",
        "https://big.ca/": "<p>" + "x" * 5000 + "</p>",
        "https://pdf.ca/": httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
        "https://gone.ca/": httpx.Response(410),
    }
    f = fetcher(pages, max_bytes=1000)
    page = await f.fetch("https://a.ca/")
    assert page.final_url == "https://a.ca/fr/" and page.text == "Bonjour"
    for url, error in [
        ("https://big.ca/", "too large"),
        ("https://pdf.ca/", "not a web page"),
        ("https://gone.ca/", "HTTP 410"),
    ]:
        with pytest.raises(PageError, match=error):
            await f.fetch(url)


def test_find_emails_variants():
    text = "a.b@x.ca, C@X.CA. info (at) y (dot) com; logo@2x.png"
    assert find_emails(text) == ["a.b@x.ca", "c@x.ca", "info@y.com"]


def test_name_and_notice_helpers():
    assert names_business("Bienvenue chez Habitat Nord", "Groupe Habitat Nord inc.")
    assert not names_business("Bienvenue chez Condos Sud", "Groupe Habitat Nord inc.")
    assert has_no_solicitation_notice("Merci de noter : aucune SOLLICITATION commerciale.")
    assert has_no_solicitation_notice("Please, no solicitations.")
    assert not has_no_solicitation_notice("Contactez-nous pour une soumission.")


async def check(
    pages,
    email="info@habitatnord.ca",
    url="https://habitatnord.ca/contact",
    website="https://habitatnord.ca",
    name="Groupe Habitat Nord",
):
    return await verify_consent(
        fetcher(pages), email=email, source_url=url, business_name=name, website=website
    )


async def test_consent_passes_on_company_site_with_evidence():
    result = await check({"https://habitatnord.ca/contact": CONTACT_PAGE})
    assert result.ok and result.reason is None
    ev = result.evidence
    assert ev["source_type"] == "company_site" and ev["final_url"] == "https://habitatnord.ca/contact"
    assert "info@habitatnord.ca" in ev["excerpt"] and len(ev["page_sha256"]) == 64 and ev["fetched_at"]


async def test_consent_passes_on_directory_naming_the_business():
    directory = "<h2>Groupe Habitat Nord</h2><p>Promoteur. Courriel: info@habitatnord.ca</p>"
    result = await check(
        {"https://annuaire.example.ca/membres/42": directory}, url="https://annuaire.example.ca/membres/42"
    )
    assert result.ok and result.evidence["source_type"] == "directory"


@pytest.mark.parametrize(
    ("pages", "kwargs", "reason"),
    [
        ({}, {}, "could not be checked"),
        ({"https://habitatnord.ca/contact": "<p>Groupe Habitat Nord</p>"}, {}, "not published"),
        (
            {
                "https://habitatnord.ca/contact": CONTACT_PAGE.replace(
                    "</body>", "<p>Pas de sollicitation SVP</p></body>"
                )
            },
            {},
            "no solicitation",
        ),
        (
            {"https://habitatnord.ca/contact": "<h1>Groupe Habitat Nord</h1><p>contact@autre-firme.com</p>"},
            {"email": "contact@autre-firme.com"},
            "domain differs",
        ),
        (
            {"https://annuaire.example.ca/x": "<p>Condos Sud: info@habitatnord.ca</p>"},
            {"url": "https://annuaire.example.ca/x"},
            "not named next to the email",
        ),
        ({}, {"url": "https://www.facebook.com/habitatnord"}, "social network"),
        ({}, {"url": "https://exa.ai/library/organization/w8f1dmq72x5"}, "search engine"),
        ({}, {"email": "not-an-email"}, "invalid email"),
        # Review #45: the model picks `website`, so "own site" must still name the business.
        # A: the model linked the project to the wrong firm and cites that firm's own contact page.
        (
            {"https://other-firm.com/contact": "<h1>Other Firm</h1><p>contact@other-firm.com</p>"},
            {
                "email": "contact@other-firm.com",
                "url": "https://other-firm.com/contact",
                "website": "https://other-firm.com",
            },
            "does not name",
        ),
        # B: a free-mail address on any page, with `website` set to that page's host.
        (
            {"https://random.example/p": "<p>Write to cheap.leads@gmail.com</p>"},
            {
                "email": "cheap.leads@gmail.com",
                "url": "https://random.example/p",
                "website": "https://random.example",
            },
            "does not name",
        ),
        # A directory listing many firms: the business is named, but far from the email used.
        (
            {
                "https://annuaire.example.ca/liste": "<h2>Groupe Habitat Nord</h2><p>Promoteur.</p>"
                + "<p>Autres membres du répertoire, entreprises et services de la région.</p>" * 8
                + "<h2>Rénos Express</h2><p>renos.express@gmail.com</p>"
            },
            {"email": "renos.express@gmail.com", "url": "https://annuaire.example.ca/liste", "website": None},
            "not named next to the email",
        ),
    ],
)
async def test_consent_failures(pages, kwargs, reason):
    result = await check(pages, **kwargs)
    assert not result.ok and reason in result.reason


async def test_consent_free_mail_allowed_only_when_published():
    page = "<h1>Rénovations Tremblay</h1><p>Écrivez-nous : renotremblay@gmail.com</p>"
    result = await check(
        {"https://annuaire.example.ca/t": page},
        email="renotremblay@gmail.com",
        url="https://annuaire.example.ca/t",
        website=None,
        name="Rénovations Tremblay",
    )
    assert result.ok and result.evidence["free_mail"] is True


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (httpx.Response(503), True),
        (httpx.Response(429), True),
        (httpx.Response(403), False),
        (httpx.Response(404), False),
    ],
)
async def test_consent_fetch_errors_flag_passing_problems(response, retryable):
    result = await check({"https://habitatnord.ca/contact": response})
    assert not result.ok and "could not be checked" in result.reason and result.retryable is retryable


async def test_network_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("reset")

    f = PageFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve=_public)
    with pytest.raises(PageError) as exc:
        await f.fetch("https://habitatnord.ca/")
    assert exc.value.retryable


async def _public(host: str) -> list[str]:
    return [PUBLIC_IP]


async def test_fetch_has_a_whole_page_deadline():
    """A server that drip-feeds bytes can't hold the job: the whole page has a time limit."""

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b"<p>x</p>"
                await asyncio.sleep(0.05)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Drip())

    f = PageFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve=_public, deadline=0.3)
    with pytest.raises(PageError, match="took more than") as exc:
        await f.fetch("https://slow.example/")
    assert exc.value.retryable


async def test_fetch_refuses_internal_peer_after_connect(monkeypatch):
    """DNS rebinding: the name passed the check, but the connection lands on an internal address."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>metadata</p>")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        f = PageFetcher(httpx.AsyncClient(trust_env=False), check_peer=True)

        async def pretend_public(url: str) -> None:  # the DNS answer we checked was public...
            return None

        monkeypatch.setattr(f, "_check_url", pretend_public)
        with pytest.raises(PageError, match="internal"):  # ...but the socket went to 127.0.0.1
            await f.fetch(f"http://127.0.0.1:{server.server_port}/")
        await f.aclose()
    finally:
        server.shutdown()


async def test_meta_charset_is_used_when_the_header_has_none():
    body = '<meta charset="iso-8859-1"><h1>Rénovations Côté</h1>'.encode("latin-1")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    f = PageFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve=_public)
    page = await f.fetch("https://reno.example/")
    assert "Rénovations Côté" in page.text


def test_search_engines_but_not_google_sites():
    for url in (
        "https://www.google.com/maps/place/x",
        "https://news.google.com/articles/x",
        "https://exa.ai/library/organization/x",
        "https://api.exa.ai/x",
        "https://search.brave.com/search?q=x",
    ):
        assert is_search_engine(url), url
    assert not is_search_engine("https://sites.google.com/view/renos-tremblay")


def test_host_of_accepts_bare_domains():
    assert host_of("acme.ca") == host_of("www.acme.ca/contact") == host_of("https://acme.ca") == "acme.ca"


# Second review of #45: the name check must not be satisfied by a shared trade word.
@pytest.mark.parametrize(
    ("text", "name", "expected"),
    [
        ("Rénovations Express renos.express@gmail.com", "Rénovations Tremblay", False),
        ("Beta Properties — contact", "Acme Properties", False),
        ("Groupe HD — Nous joindre", "Groupe HD", True),
        ("Groupe ABC", "Groupe HD", False),
        ("S3 Technologies — contact", "S3 Technologies Inc.", True),
        ("Gestion de patrimoine ASF", "Gestion de patrimoine ASF", True),
        ("Construction Pomerleau inc.", "Pomerleau", True),
        ("Rénovations Générales", "Rénovations Générales Inc.", True),  # only generic words: full name
        ("Alpha Beta Gamma Delta", "Alpha Beta Gamma Epsilon", True),  # 3 of 4: majority
        ("Alpha Beta Zeta Eta", "Alpha Beta Gamma Epsilon", False),  # 2 of 4: not a majority
    ],
)
def test_names_business(text, name, expected):
    assert names_business(text, name) is expected


async def test_consent_directory_address_without_business_name_is_flagged():
    """Personal addresses are allowed on directories (Jeff, 2026-10-22), but a listing page can't
    tell neighbouring firms apart, so the owners confirm these from the excerpt."""
    page = (
        "<h2>Rénovations Tremblay</h2><p>Rénovations résidentielles.</p>"
        "<h2>Rénovations Express</h2><p>renos.express@gmail.com</p>"
    )
    result = await check(
        {"https://annuaire.example.ca/t": page},
        email="renos.express@gmail.com",
        url="https://annuaire.example.ca/t",
        website=None,
        name="Rénovations Tremblay",
    )
    assert result.ok and result.evidence["needs_owner_check"] is True
    assert "Rénovations Express" in result.evidence["excerpt"]  # what the owner will see

    named = await check(
        {"https://annuaire.example.ca/r": "<h2>Rénovations Tremblay</h2><p>renotremblay@gmail.com</p>"},
        email="renotremblay@gmail.com",
        url="https://annuaire.example.ca/r",
        website=None,
        name="Rénovations Tremblay",
    )
    assert named.ok and "needs_owner_check" not in named.evidence


async def test_consent_wrong_firm_own_site_with_same_trade_word_fails():
    result = await check(
        {"https://beta.ca/contact": "<h1>Beta Properties</h1><p>info@beta.ca</p>"},
        email="info@beta.ca",
        url="https://beta.ca/contact",
        website="https://beta.ca",
        name="Acme Properties",
    )
    assert not result.ok and "does not name" in result.reason


async def test_consent_bare_domain_website_still_checks_the_domain():
    page = "<p>Notre client Acme Properties.</p><p>Écrivez à acme.properties@contractor.com</p>"
    result = await check(
        {"https://contractor.com/projets": page},
        email="acme.properties@contractor.com",
        url="https://contractor.com/projets",
        website="acme.ca",
        name="Acme Properties",
    )
    assert not result.ok and "domain differs" in result.reason


async def test_consent_mailto_only_and_obfuscated_addresses_have_evidence():
    directory = '<h2>Rénovations Tremblay</h2><p><a href="mailto:renotremblay@gmail.com">Écrire</a></p>'
    result = await check(
        {"https://annuaire.example.ca/t": directory},
        email="renotremblay@gmail.com",
        url="https://annuaire.example.ca/t",
        website=None,
        name="Rénovations Tremblay",
    )
    assert result.ok and "renotremblay@gmail.com" in result.evidence["excerpt"]

    own_site = "<h1>Groupe Habitat Nord</h1><p>Ventes : ventes [at] habitatnord [dot] ca</p>"
    result = await check({"https://habitatnord.ca/contact": own_site}, email="ventes@habitatnord.ca")
    assert result.ok and "ventes@habitatnord.ca" in result.evidence["excerpt"]


async def test_consent_refuses_redirect_to_search_engine():
    pages = {
        "https://short.example/x": httpx.Response(
            302, headers={"location": "https://www.google.com/search?q=x"}
        ),
        "https://www.google.com/search?q=x": "<p>Groupe Habitat Nord info@habitatnord.ca</p>",
    }
    result = await check(pages, url="https://short.example/x", website=None)
    assert not result.ok and "search engine" in result.reason
