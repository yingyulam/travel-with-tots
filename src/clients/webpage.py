"""Plain text from one web page, for reading a venue's own opening hours.

A sibling to src/osm.py and src/nominatim.py rather than a component: nothing a
parent touches fetches a page, so there is no admin test page to isolate. Only
the proposal path calls this, and only for a URL `official_site` has already
decided belongs to the venue.

Why fetch at all, when the proposer deliberately never asked the model for
hours. The reasoning was that a search snippet cannot establish them, which is
true, and it was generalised to "hours are unfindable", which is not: Maplewood
Farm publishes four plain lines on its homepage, and the proposer had already
found that homepage and stored the link without reading it.

Deliberately small. No JavaScript, no crawling, no link following: one GET of
one page a human could open, which is the same request their browser makes when
they click the citation the review page already shows. Paced and identified, in
the same spirit as nominatim.py, because a venue's own site is somebody's small
server rather than an API.
"""

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "travel-with-tots/1.0 (reading published opening hours)"
REQUEST_TIMEOUT_SECONDS = 10

# One second between fetches. A batch reads at most `batch_size` pages, each on
# a different host, so this is politeness rather than rate-limit avoidance.
DELAY_SECONDS = 1.0

# What is worth reading. A page bigger than this is a web app rather than an
# opening-hours notice, and handing a model half a megabyte of markup costs
# tokens to find nothing.
MAX_BYTES = 500_000

# What the model is given. Hours live near the top of a page or on an
# hours/visit page, never at the end of a 20,000-word blog, and a cap is what
# stops one enormous page dominating a batch's token budget.
MAX_CHARS = 12_000

# Redirects are followed by hand rather than by requests, so every hop can be
# checked. Three covers the redirects real sites use (http to https, bare domain
# to www, / to /home) and ends a loop quickly.
MAX_REDIRECTS = 3

# Tags whose *contents* are not prose. Dropped whole, or a stylesheet's
# "font-size:14px" arrives as text and reads like data.
_DROP_CONTENT = re.compile(
    r"<(script|style|noscript|svg|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")

# The entities that actually turn up in opening hours: a dash between times and
# a non-breaking space between a number and "am". Enough to stop "10:00&#8211;
# 4:00" reading as one token; not an HTML entity library.
_ENTITIES = {
    "&nbsp;": " ", "&#160;": " ", "&amp;": "&", "&#38;": "&",
    "&ndash;": "-", "&#8211;": "-", "&mdash;": "-", "&#8212;": "-",
    "&#45;": "-", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
}


class PageError(Exception):
    """Raised when the page cannot be fetched or holds no readable text."""


def to_text(html: str) -> str:
    """Readable text from HTML, with the markup and scripts removed.

    Regex rather than a parser, and that is a real trade: a malformed tag can
    swallow a few words. It is acceptable here because nothing downstream
    trusts this text on its own. Every time the model reads out of it is
    checked back against the times this same text contains, so a mangled page
    loses hours rather than inventing them, and adds no dependency to a project
    that vendors its one frontend library.
    """
    text = _DROP_CONTENT.sub(" ", html)
    # Before tag stripping: a <br> or </p> is where a line ends, and without
    # this "Mon-Thu10:00am-4:00pmFri-Sun" arrives as one run of characters.
    text = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", text,
                  flags=re.IGNORECASE)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return text.strip()


def require_public_address(url: str) -> None:
    """Raise unless every address `url`'s host resolves to is on the internet.

    This is what stops a fetch reaching inward. The URL is not ours: it comes
    from `official_site`, which picks it out of web search results, so anyone
    who can rank a page has a say in it. Without this, a proposal batch could be
    steered into fetching `http://169.254.169.254/` -- the cloud metadata
    service, which hands out instance credentials on most hosts -- or a database
    on localhost. The page body is then shown to the reviewer and written to
    data/venue_candidates.csv, which is tracked in git, so a read of an internal
    service would be exfiltrated to both.

    Every resolved address is checked, not just the first: a name that answers
    with one public and one private address must not get through on the strength
    of the public one. `is_global` is False for private, loopback, link-local,
    multicast and reserved ranges, in IPv4 and IPv6 alike.

    What this does not stop is DNS rebinding, where the name resolves publicly
    here and privately when requests connects a moment later. Closing that means
    connecting to the address already checked and setting Host by hand, which is
    more machinery than an admin-triggered, once-per-second batch warrants.
    """
    host = urlparse(url).hostname
    if not host:
        raise PageError(f"no host in {url!r}")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        raise PageError(f"could not resolve {host}: {e}") from None
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise PageError(
                f"refusing to fetch {host}: {address} is not a public address")


def _get_checking_every_hop(url: str, timeout):
    """GET `url`, following redirects one at a time so each can be checked.

    By hand rather than `allow_redirects=True`, because that is how an address
    check gets walked past: a site that is public at the first URL redirects to
    169.254.169.254, and requests follows it without asking again. Venues' sites
    really do redirect (http to https, / to /home), so this follows rather than
    refuses.
    """
    for _ in range(MAX_REDIRECTS + 1):
        require_public_address(url)
        response = requests.get(
            url, timeout=timeout, headers={"User-Agent": USER_AGENT},
            allow_redirects=False)
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        url = urljoin(url, response.headers.get("Location", ""))
        if not url.startswith(("http://", "https://")):
            raise PageError(f"refusing a redirect to {url!r}")
    raise PageError(f"more than {MAX_REDIRECTS} redirects")


def fetch_text(url: str, timeout=REQUEST_TIMEOUT_SECONDS) -> str:
    """The readable text of one page, capped at MAX_CHARS.

    Raises PageError for anything that is not a page of text we could read:
    a transport failure, a non-200, a non-HTML content type, a body over
    MAX_BYTES, markup that reduces to nothing, or a host that is not on the
    public internet.
    """
    if not (url or "").startswith(("http://", "https://")):
        raise PageError(f"not a web address: {url!r}")
    try:
        response = _get_checking_every_hop(url, timeout)
    except requests.exceptions.RequestException as e:
        raise PageError(f"{type(e).__name__}: {e}") from None
    finally:
        # In a finally, so a failed fetch still paces the next one.
        time.sleep(DELAY_SECONDS)

    if response.status_code != 200:
        raise PageError(f"HTTP {response.status_code}")
    kind = response.headers.get("Content-Type", "")
    if "html" not in kind and "text/plain" not in kind:
        raise PageError(f"not a readable page: {kind or 'no content type'}")
    if len(response.content) > MAX_BYTES:
        raise PageError(f"page is {len(response.content)} bytes, over the cap")

    text = to_text(response.text)
    if not text:
        raise PageError("no readable text on the page")
    return text[:MAX_CHARS]
