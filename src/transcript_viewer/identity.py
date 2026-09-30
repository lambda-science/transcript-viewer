"""Who is asking, when anyone is.

On a laptop nobody is signed in and there is one person, which is what `None`
means everywhere below. Shared, an identity arrives in a request header put
there by whatever stands in front — a load balancer that completed an OIDC
sign-in, a mesh that authenticated the connection.

The header is trusted only when the deployment names it. That is the whole
security property of this module: a header nobody was configured to expect is
a header anybody can send, so sniffing for a plausible-looking one would turn
the sign-in into a formality. Nothing here validates a token, because nothing
here can — the thing in front is what did that, and the reason it must be
impossible to reach this server around it.
"""

from __future__ import annotations

from typing import Any

from . import deploy


def who(headers: Any, where: deploy.Deployment | None = None) -> str | None:
    """The person making this request, or None when nobody is signed in.

    `headers` is anything with a case-insensitive `.get`, which is what both
    `BaseHTTPRequestHandler.headers` and a plain dict of lowercased keys give.
    """
    site = where or deploy.current()
    if not site.identity_header:
        return None
    raw = headers.get(site.identity_header)
    if not isinstance(raw, str):
        return None
    # Load balancers vary: some send the address, some a whole principal. Take
    # the last comma-separated part, which is what a proxy chain appends, and
    # keep it short enough to be a key rather than a payload.
    name = raw.split(",")[-1].strip()[:320]
    return name or None


def may_administer(person: str | None, where: deploy.Deployment | None = None) -> bool:
    """Whether this person may reach the routes that reconfigure the server.

    Two questions in one, and the answers differ by deployment. Alone at a
    laptop the only person here is the operator, so everything is theirs. Once
    anyone can sign in, only a named admin qualifies — and an empty list means
    nobody rather than everybody, because the failure that matters is a shared
    instance where the first visitor can rewrite the server's credentials.
    """
    site = where or deploy.current()
    if not site.shared:
        return True
    return person is not None and person in site.admins
