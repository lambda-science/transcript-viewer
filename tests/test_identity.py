"""Who is asking, and what they are allowed to do about it.

Both failures here are quiet ones. Trusting a header nobody was configured to
expect turns a sign-in into a formality, and treating an empty admin list as
"everybody" hands the first visitor to a shared instance the server's
credentials. Each has a test that fails loudly if it comes back.
"""

from __future__ import annotations

import pytest

from transcript_viewer import deploy, identity

SHARED = deploy.Deployment(
    identity_header="x-amzn-oidc-identity",
    admins=("ann@example.com",),
)
LOCAL = deploy.Deployment()


def test_nobody_is_signed_in_on_a_laptop():
    assert identity.who({"x-amzn-oidc-identity": "ann@example.com"}, LOCAL) is None


def test_an_identity_arrives_in_the_header_the_deployment_names():
    assert identity.who({"x-amzn-oidc-identity": "ann@example.com"}, SHARED) == (
        "ann@example.com"
    )


def test_a_header_nobody_asked_for_is_not_an_identity():
    """The security property of the whole module. A header this was not told to
    expect is one anybody can send, so sniffing for a plausible-looking one
    would make the sign-in decorative."""
    plausible = {
        "x-forwarded-user": "attacker@example.com",
        "x-auth-request-email": "attacker@example.com",
        "remote-user": "attacker@example.com",
    }
    assert identity.who(plausible, SHARED) is None


def test_a_proxy_chain_leaves_the_last_word_to_the_nearest_proxy():
    """Appending is what a chain does; the one nearest us is the one that
    authenticated the request."""
    assert (
        identity.who(
            {"x-amzn-oidc-identity": "spoofed@example.com, ann@example.com"}, SHARED
        )
        == "ann@example.com"
    )


@pytest.mark.parametrize("value", ["", "   ", None, 7, []])
def test_an_empty_or_nonsense_header_is_nobody(value):
    assert identity.who({"x-amzn-oidc-identity": value}, SHARED) is None


def test_the_person_at_the_laptop_may_do_anything():
    """There is only one of them, and it is their machine."""
    assert identity.may_administer(None, LOCAL) is True


def test_a_signed_in_stranger_may_not_reconfigure_the_server():
    assert identity.may_administer("bo@example.com", SHARED) is False
    assert identity.may_administer("ann@example.com", SHARED) is True


def test_nobody_administers_a_shared_instance_that_named_no_admins():
    """Empty means nobody, not everybody. The failure that matters is a shared
    instance where the first visitor can rewrite the server's credentials."""
    unnamed = deploy.Deployment(identity_header="x-amzn-oidc-identity")

    assert identity.may_administer(None, unnamed) is False
    assert identity.may_administer("anyone@example.com", unnamed) is False
