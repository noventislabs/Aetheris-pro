"""HMAC-SHA256 request signing. The only module that reads a secret.

**This module deliberately imports no logger.** Every other approach to "the
secret must never be logged" relies on remembering; this one relies on there
being nothing here to log with. The same reasoning bans returning the secret,
embedding it in an exception, and putting it in a repr.

The signature covers the encoded query string exactly as it is sent. Binance
recomputes it over the bytes it receives, so building the string once and
signing *that* string -- rather than re-encoding a dictionary a second time --
is what keeps the two in agreement. A parameter re-ordered between signing and
sending is the classic source of ``-1022 Signature for this request is not
valid``, and it is avoided here by construction rather than by care.

RSA and Ed25519 keys are out of scope for phase 8b by decision. HMAC only.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlencode

from pydantic import SecretStr

__all__ = [
    "API_KEY_HEADER",
    "SignedRequest",
    "build_signed_query",
    "utc_timestamp_ms",
]

#: Binance carries the key in a header, never in the signed payload.
API_KEY_HEADER: Final = "X-MBX-APIKEY"


class SignedRequest:
    """A signed query string and the header that must accompany it.

    Holds the finished artefacts, never the secret. ``__repr__`` is overridden
    so an accidental interpolation into a log line or an exception cannot leak
    the key either -- the key is a credential even though the signature is not.
    """

    __slots__ = ("_api_key", "query")

    def __init__(self, query: str, api_key: SecretStr) -> None:
        self.query = query
        self._api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self._api_key.get_secret_value()}

    def __repr__(self) -> str:  # pragma: no cover - defensive, asserted in tests
        return "SignedRequest(query=<signed>, api_key=<redacted>)"

    __str__ = __repr__


def utc_timestamp_ms(*, offset_ms: int = 0) -> int:
    """Milliseconds since the epoch, with a correction for clock drift.

    ``offset_ms`` carries the difference between our clock and the venue's,
    measured from its own time endpoint. Binance rejects a request whose
    timestamp is ahead of server time, so a machine running slightly fast
    fails every signed call until the offset is applied -- a failure that looks
    like bad credentials and is not.
    """
    return int(time.time() * 1000) + offset_ms


def build_signed_query(
    params: Mapping[str, str | int | float],
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    recv_window_ms: int,
    offset_ms: int = 0,
) -> SignedRequest:
    """Encode, sign, and return the exact string to send.

    ``timestamp`` and ``recvWindow`` are added here rather than by callers, so
    no call site can forget them and no call site needs to know they exist.
    """
    payload: dict[str, str] = {str(k): str(v) for k, v in params.items() if v is not None}
    payload["recvWindow"] = str(recv_window_ms)
    payload["timestamp"] = str(utc_timestamp_ms(offset_ms=offset_ms))

    encoded = urlencode(payload)
    signature = hmac.new(
        api_secret.get_secret_value().encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return SignedRequest(f"{encoded}&signature={signature}", api_key)
