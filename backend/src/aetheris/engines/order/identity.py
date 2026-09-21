"""Deterministic order identity.

The identity a venue echoes back is what recovery queries by, so it has one
hard requirement: **it must be derivable again after a restart.** A generated
id -- a UUID, a counter, anything drawn from the process -- is lost with the
process, and an order whose identity cannot be reconstructed is an order that
cannot be asked about. That is the difference between a recoverable crash and
an unrecoverable one.

So the id is *derived*, not generated: the same intent produces the same id on
every machine, in every process, forever.

**Venue constraints are real and narrow.** USDT-M perpetual venues cap the
client-order-id field at 36 characters over a restricted alphabet. The phase 7
paper key -- ``auto-SYMBOL-15m-1789992000000-BUY`` -- is 33 characters for
``ETHUSDT`` and overflows for longer symbols, which would be discovered at the
worst possible moment: the first real submission of an unusual instrument.

The concrete limit is asserted against the real venue by the adapter in phase
8c, where venue facts belong. This layer only has to stay inside it.

The fix is a bounded encoding. The venue gets a short prefix plus a digest; the
readable form is kept in our own record as ``intent_key``, where nothing
truncates it. A reader loses nothing and the venue field cannot overflow.

There are two identities here and conflating them is the mistake this module
exists to prevent. ``client_order_id`` is the **venue** identity and must be
derived, for the reason above. ``order_id`` is our own local handle, and it has
the opposite requirement: it must be unique against every order that has *ever*
existed, including the ones this process has never seen. See ``new_order_id``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Final

from aetheris.domain.enums import OrderSide

__all__ = [
    "VENUE_ID_MAX_LENGTH",
    "build_intent_key",
    "client_order_id",
    "is_valid_venue_id",
    "new_order_id",
]


def new_order_id() -> str:
    """A local order handle that no other order will ever share.

    **This is the one identity that must not be derived.** ``client_order_id``
    is derived because recovery has to reconstruct it after a restart; this one
    is a primary key, and the requirement is the mirror image -- it must not
    collide with an order written by a previous process, a previous boot, or a
    worker running right now.

    A per-process counter satisfied that only while the store died with the
    process. Against a durable store it resets to zero on every boot and the
    first order after a restart collides with the first order of the run
    before, which surfaces as a unique-constraint violation on the very
    operation a restart is supposed to make safe.

    A random UUID needs no coordination, no round trip, and no shared state, so
    it holds across restarts and across concurrent workers alike. The ``order-``
    prefix is kept so the value is still recognisable in a log.
    """
    return f"order-{uuid.uuid4().hex}"


#: The venue's client-order-id limit. Stated as a constant rather than a magic
#: number because the whole encoding exists to respect it, and because phase 8c
#: asserts it against the venue's own published filters.
VENUE_ID_MAX_LENGTH: Final = 36

#: The venue's permitted alphabet. Anything outside it is rejected by the venue
#: rather than truncated, so the generated form must stay inside it by
#: construction -- a hex digest and a fixed prefix always do.
#:
#: Kept as a pattern string rather than a pre-built regex object. The
#: architecture test bans the dynamic-execution builtins package-wide, and the
#: regex module's builder shares a name with one of them. A blunt ban that
#: occasionally costs a microsecond is worth more than an exemption someone
#: could later widen.
_VENUE_ID_PATTERN: Final = r"^[.A-Za-z0-9:/_-]{1,36}$"

_PREFIX: Final = "aeth"
#: 10 bytes -> 20 hex characters -> 25 with the prefix. Well inside the limit,
#: and 80 bits is far more collision headroom than one account's order history
#: could ever need.
_DIGEST_BYTES: Final = 10


def build_intent_key(
    *,
    symbol: str,
    side: OrderSide,
    purpose: str,
    bucket: str,
) -> str:
    """The readable, canonical description an identity is derived from.

    ``bucket`` is whatever makes this intent distinct and *stable*: a candle's
    close time for an autonomous entry, a caller-supplied key for a manual one.
    It must not be a wall-clock reading -- re-deriving the identity during
    recovery would then produce a different id, which defeats the entire
    mechanism.
    """
    return f"{purpose}:{symbol.upper()}:{side.value}:{bucket}"


def client_order_id(*, account_id: str, intent_key: str) -> str:
    """Derive the venue identity. Same inputs, same id, always.

    Deliberately not reversible: the venue does not need to read our intent out
    of an order id, and a readable id that overflows the venue's limit is worse
    than an opaque one that does not. The readable form lives on the record.
    """
    canonical = f"{account_id}|{intent_key}"
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()
    candidate = f"{_PREFIX}-{digest}"
    if not is_valid_venue_id(candidate):  # pragma: no cover - guarded by construction
        raise ValueError(f"derived id {candidate!r} is not venue-acceptable")
    return candidate


def is_valid_venue_id(value: str) -> bool:
    """Whether a venue would accept this identifier as-is."""
    return bool(re.fullmatch(_VENUE_ID_PATTERN, value))
