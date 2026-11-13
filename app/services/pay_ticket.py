"""Payment ticket of the Mini App: a signed, short-lived promise «this user may start a payment for this pack».

The wallet buttons (SberPay, SBP, T-Pay) must open the bank's link inside the tap handler: a link opened after an
``await`` has lost the user gesture and MAX / the browser silently ignore it. So the client asks for the ticket in advance
(when the buyer reaches the choice of the way to pay) and a tap only opens ``pay-go?t=<ticket>&m=<way>`` synchronously; the
payment itself is created by that page, from the ticket. The ticket creates nothing: no row, no push.

Format: ``gp1.<base64url(json)>.<hmac-sha256 hex>``, the key is derived from ``API_SECRET_TOKEN`` (as ``mini_access``).
The payload holds only what the server checks again against the price list when the payment is made.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from app.core.config import settings

PREFIX = "gp1"
TTL_SECONDS = 10 * 60
# The status of the payments of a ticket is read much later than the ticket may start a payment (the buyer pays in the bank app).
STATUS_LEEWAY_SECONDS = 60 * 60
WAYS = {"sberpay": "sberbank", "sbp": "sbp", "tpay": "tinkoff_bank", "page": "redirect"}


@dataclass(frozen=True)
class Ticket:
    nonce: str
    user_id: int
    platform: str  # "max" | "telegram": the Mini App platform of the buyer
    external_id: str
    generations: int
    amount_rubles: int
    context: str
    source: str | None
    expires_at: int

    def operation_key(self, way: str) -> str:
        """Key of the one payment of this ticket for a way to pay (unique per user in ``yookassa_payments``)."""
        return f"tk:{self.nonce}:{way}"

    @property
    def operation_prefix(self) -> str:
        return f"tk:{self.nonce}:"


def _key() -> bytes:
    return hashlib.sha256(f"pay-ticket:{settings.API_SECRET_TOKEN}".encode()).digest()


def _sign(body: str) -> str:
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def issue(
    *, user_id: int, platform: str, external_id: str | int, generations: int, amount_rubles: int, context: str,
    source: str | None, now: float | None = None,
) -> tuple[str, int]:
    """(ticket, expires_at as a unix time)."""
    expires_at = int((now if now is not None else time.time()) + TTL_SECONDS)
    payload = {
        "n": secrets.token_hex(8), "u": int(user_id), "p": platform, "x": str(external_id),
        "g": int(generations), "a": int(amount_rubles), "c": context, "s": source, "e": expires_at,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{PREFIX}.{body}.{_sign(body)}", expires_at


def verify(token: str | None, *, leeway: int = 0, now: float | None = None) -> Ticket | None:
    """The ticket, or None when it is forged, malformed or older than its life (plus ``leeway``)."""
    try:
        prefix, body, signature = (token or "").split(".")
        if prefix != PREFIX or not hmac.compare_digest(_sign(body), signature):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        ticket = Ticket(
            nonce=str(payload["n"]), user_id=int(payload["u"]), platform=str(payload["p"]), external_id=str(payload["x"]),
            generations=int(payload["g"]), amount_rubles=int(payload["a"]), context=str(payload["c"]),
            source=payload.get("s"), expires_at=int(payload["e"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if (now if now is not None else time.time()) > ticket.expires_at + leeway:
        return None
    return ticket
