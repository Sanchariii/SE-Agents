"""Microsoft Graph lookups and Entra ID id-token verification for SSO login.

Login trusts two independent proofs: the id token must verify against the tenant's
JWKS with our application as its audience, and the access token must be accepted by
Graph. Both are then required to describe the *same* principal, which stops an id
token from one user being paired with an access token from another.
"""

import asyncio
from functools import lru_cache

import httpx
import jwt

from app.config.constants import EntraConstants, GraphConstants, HttpConstants
from app.config.exceptions import UnauthorizedError, UpstreamError
from app.config.settings import get_settings
from app.utilities import tls_util as tls
from app.utilities.logger_util import get_logger

log = get_logger(__name__)

# Pooled Graph client, created lazily inside the running event loop.
_CLIENT: httpx.AsyncClient | None = None


@lru_cache
def _jwks_client(tenant_id: str):
    """Cached JWKS client for a tenant.

    Entra rotates signing keys rarely, so the keys are fetched once per process and
    refreshed by the client's own lifespan instead of on every login.

    Args:
        tenant_id: Entra tenant (directory) id.

    Returns:
        A ``jwt.PyJWKClient`` bound to that tenant's discovery endpoint.
    """
    from jwt import PyJWKClient
    url = EntraConstants.JWKS_URL_TEMPLATE.format(tenant_id=tenant_id)
    log.info("building Entra JWKS client for tenant '%s'", tenant_id)
    return PyJWKClient(url, cache_keys=True,
                       lifespan=EntraConstants.JWKS_CACHE_LIFESPAN_SECONDS)


def verify_id_token(id_token: str) -> dict:
    """Validate an Entra id token and return its claims.

    Verification is skipped (with a warning) when the tenant/client ids are not
    configured, because without them there is no audience to check against; the
    Graph call then remains the only proof of the token's validity.

    Args:
        id_token: The raw JWT issued to the front-end application.

    Returns:
        The verified claims, or the unverified ones when verification is disabled.

    Raises:
        UnauthorizedError: The token is expired, malformed or not issued to us.
    """
    settings = get_settings()
    if not settings.id_token_verification_enabled:
        log.warning("id token accepted without signature verification "
                    "(ENTRA_TENANT_ID / ENTRA_CLIENT_ID not configured)")
        try:
            # Unverified: used only to bind the login to the Graph identity below.
            return jwt.decode(id_token, options={"verify_signature": False,
                                                 "verify_aud": False})
        except Exception as exc:                    # noqa: BLE001
            log.warning("id token could not be parsed: %s", exc)
            raise UnauthorizedError("invalid identity token") from exc

    tenant = settings.entra_tenant_id
    issuers = [t.format(tenant_id=tenant) for t in EntraConstants.ISSUER_TEMPLATES]
    try:
        key = _jwks_client(tenant).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token, key,
            algorithms=list(EntraConstants.ID_TOKEN_ALGORITHMS),
            audience=settings.entra_client_id,
            leeway=EntraConstants.CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("id token rejected: %s", exc)
        raise UnauthorizedError("invalid identity token") from exc

    # PyJWT can only check one issuer string, and Entra emits two shapes (v1/v2).
    if claims.get("iss") not in issuers:
        log.warning("id token issuer '%s' is not this tenant", claims.get("iss"))
        raise UnauthorizedError("invalid identity token")

    log.info("id token verified for oid '%s'", claims.get("oid"))
    return claims


def _client() -> httpx.AsyncClient:
    """Return the shared Graph HTTP client, creating it on first use.

    One pooled client is reused for the life of the process: a fresh client per call
    would pay a full TCP + TLS handshake every time, which on the login path (two
    concurrent Graph calls) is the single largest avoidable cost.

    Returns:
        The process-wide ``httpx.AsyncClient`` bound to the Graph base URL.
    """
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        log.info("creating shared Microsoft Graph HTTP client")
        _CLIENT = httpx.AsyncClient(
            base_url=GraphConstants.BASE_URL,
            timeout=GraphConstants.TIMEOUT_SECONDS,
            verify=tls.verify_setting(),
            limits=httpx.Limits(
                max_connections=HttpConstants.MAX_CONNECTIONS,
                max_keepalive_connections=HttpConstants.MAX_KEEPALIVE_CONNECTIONS))
    return _CLIENT


async def close_client() -> None:
    """Close the shared Graph client during application shutdown."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
        log.info("shared Microsoft Graph HTTP client closed")
    _CLIENT = None


def _auth_headers(access_token: str) -> dict[str, str]:
    """Bearer headers for Graph.

    Args:
        access_token: The Graph access token supplied by the front end.

    Returns:
        Request headers carrying the bearer credential.
    """
    return {"Authorization": f"Bearer {access_token}"}


def _raise_for_graph(response: httpx.Response, what: str) -> None:
    """Translate a Graph error response into a domain error.

    Args:
        response: The Graph response to inspect.
        what: Short description of the call, used for logging only.

    Raises:
        UnauthorizedError: Graph rejected the access token (401/403).
        UpstreamError: Graph failed for any other reason.
    """
    if response.is_success:
        return
    # 401/403 means the caller's token is bad — that is a client error, not an outage.
    if response.status_code in (401, 403):
        log.warning("graph %s rejected the access token (%s)", what,
                    response.status_code)
        raise UnauthorizedError("identity provider rejected the access token")
    log.error("graph %s failed with %s: %s", what, response.status_code,
              response.text[:500])
    raise UpstreamError("identity provider is unavailable")


async def fetch_me(access_token: str) -> dict:
    """Read the signed-in user's directory profile.

    Args:
        access_token: Graph access token from the front end.

    Returns:
        The Graph ``/me`` payload (id, displayName, mail, userPrincipalName, ...).

    Raises:
        UnauthorizedError: The access token is not accepted by Graph.
        UpstreamError: Graph is unreachable or failing.
    """
    log.info("calling graph %s", GraphConstants.ME_PATH)
    try:
        response = await _client().get(GraphConstants.ME_PATH,
                                       headers=_auth_headers(access_token),
                                       params={"$select": GraphConstants.ME_SELECT})
    except httpx.HTTPError as exc:
        log.error("graph /me transport failure: %s", exc, exc_info=True)
        raise UpstreamError("identity provider is unavailable") from exc

    _raise_for_graph(response, "/me")
    profile = response.json()
    log.info("graph profile resolved for oid '%s'", profile.get("id"))
    return profile


async def check_member_groups(access_token: str, group_ids: list[str]) -> list[str]:
    """Return which of ``group_ids`` the caller belongs to.

    Graph caps ``checkMemberGroups`` at 20 ids per request, so larger sets are split
    and the batches are issued concurrently.

    Args:
        access_token: Graph access token from the front end.
        group_ids: Directory group ids to test membership against.

    Returns:
        The subset of ``group_ids`` the user is a member of (possibly empty).

    Raises:
        UnauthorizedError: The access token is not accepted by Graph.
        UpstreamError: Graph is unreachable or failing.
    """
    if not group_ids:
        log.warning("no group ids configured; skipping membership check")
        return []

    size = GraphConstants.MAX_GROUP_IDS_PER_CALL
    batches = [group_ids[i:i + size] for i in range(0, len(group_ids), size)]
    log.info("calling graph %s for %s group(s) in %s batch(es)",
             GraphConstants.CHECK_MEMBER_GROUPS_PATH, len(group_ids), len(batches))

    async def _one(client: httpx.AsyncClient, batch: list[str]) -> list[str]:
        response = await client.post(GraphConstants.CHECK_MEMBER_GROUPS_PATH,
                                     headers=_auth_headers(access_token),
                                     json={"groupIds": batch})
        _raise_for_graph(response, "/me/checkMemberGroups")
        return list(response.json().get("value") or [])

    try:
        client = _client()
        results = await asyncio.gather(*(_one(client, b) for b in batches))
    except httpx.HTTPError as exc:
        log.error("graph checkMemberGroups transport failure: %s", exc, exc_info=True)
        raise UpstreamError("identity provider is unavailable") from exc

    assigned = [gid for batch in results for gid in batch]
    log.info("user is a member of %s of the %s mapped group(s)",
             len(assigned), len(group_ids))
    return assigned
