"""Ways to pay for the buttons of the chat bots (TG, MAX): SberPay · SBP · T-Pay · card for a T-Bank payment.

The bot still creates its payment at once (it polls the status by ``yookassa_id``); only the place the buttons lead
to changes. The create call hands the bot a list of links (``pay_buttons``) and, for an old bot that knows nothing of
the list, puts the page with the choice (``pay-choose``) into ``confirmation_url``. A link carries a signed key
``k`` (payment, buyer, platform): the page behind it finds the payment already made and asks the bank for the link of
the chosen way (``pay-bot``). The key creates nothing and unlocks only that one payment of that one buyer.

Format of the key: ``gb1.<base64url(json)>.<hmac-sha256 hex>``, derived from ``API_SECRET_TOKEN`` with its own label
(a pay ticket of the Mini App is not a key and the other way round). A button in a chat lives long, so does the key.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import quote

from app.core.config import settings
from app.services import tbank_methods

PREFIX = "gb1"
TTL_SECONDS = 48 * 60 * 60
PLATFORMS = ("tg", "max")

# The buttons of the bots in their order: (way in the link, T-Bank wallet, text). The card is always the last one.
WALLET_BUTTONS = (
    ("sberpay", "sberbank", "SberPay"),
    ("sbp", "sbp", "СБП"),
    ("tpay", "tinkoff_bank", "T-Pay"),
)
CARD_BUTTON = ("card", None, "Картой")
WAYS = {way: wallet for way, wallet, _ in (*WALLET_BUTTONS, CARD_BUTTON)}

# ``DATA`` of ``Init`` for a payment of a bot: the flags that make SberPay and T-Pay give a link (without ``SberPayWeb`` the
# SberPay link answers 500; checked on the production terminal 10.11, with the flags all three links work on one payment,
# in any order and repeatedly, the QR of SBP closes nothing and the payment can still be canceled).
BOT_DEVICE_DATA = {"SberPayWeb": True, "TinkoffPayWeb": True, "Device": "Mobile"}


@dataclass(frozen=True)
class BotLink:
    payment_id: str
    user_id: int
    platform: str  # "tg" | "max"
    expires_at: int


def _key() -> bytes:
    return hashlib.sha256(f"pay-bot:{settings.API_SECRET_TOKEN}".encode()).digest()


def _sign(body: str) -> str:
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def issue(*, payment_id: str, user_id: int, platform: str, now: float | None = None) -> str:
    expires_at = int((now if now is not None else time.time()) + TTL_SECONDS)
    payload = {"y": str(payment_id), "u": int(user_id), "p": platform, "e": expires_at}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{PREFIX}.{body}.{_sign(body)}"


def verify(token: str | None, *, now: float | None = None) -> BotLink | None:
    """The key's content, or None when it is forged, malformed or expired."""
    try:
        prefix, body, signature = (token or "").split(".")
        if prefix != PREFIX or not hmac.compare_digest(_sign(body), signature):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        link = BotLink(
            payment_id=str(payload["y"]), user_id=int(payload["u"]), platform=str(payload["p"]), expires_at=int(payload["e"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if link.platform not in PLATFORMS or (now if now is not None else time.time()) > link.expires_at:
        return None
    return link


def base_url() -> str:
    """``…/api/v1/miniapp``: the folder of the public pages (where ``pay-open`` lives too)."""
    return settings.TBANK_RETURN_URL.rsplit("/", 1)[0]


def button_url(key: str, way: str, platform: str) -> str:
    return f"{base_url()}/pay-bot?k={quote(key, safe='')}&m={way}&p={platform}"


def choose_url(key: str, platform: str) -> str:
    return f"{base_url()}/pay-choose?k={quote(key, safe='')}&p={platform}"


async def open_wallet_ways() -> list[str]:
    """Our names of the wallets (``sberpay`` | ``sbp`` | ``tpay``) the terminal can give a link for, in the order of the buttons."""
    open_wallets = set(await tbank_methods.available_wallets())
    return [way for way, wallet, _ in WALLET_BUTTONS if wallet in open_wallets]


async def fields_for_bot(*, provider: str | None, payment_id: str, user_id: int, platform: str) -> dict:
    """What the create call of a bot adds to its answer for a T-Bank payment: ``pay_buttons`` and ``confirmation_url``
    (the page with the choice). ``{}`` — the bot gets the answer as before (YooKassa, the buttons are off, or no wallet
    is open: then the card is the only way and the bank's own form is the single «Оплатить»)."""
    if provider != "tbank" or not settings.TBANK_BUTTONS or not settings.TBANK_BOT_BUTTONS:
        return {}
    ways = await open_wallet_ways()
    if not ways:
        return {}
    key = issue(payment_id=payment_id, user_id=user_id, platform=platform)
    texts = {way: text for way, _, text in (*WALLET_BUTTONS, CARD_BUTTON)}
    return {
        "confirmation_url": choose_url(key, platform),
        "pay_buttons": [
            {"method": way, "text": texts[way], "url": button_url(key, way, platform)}
            for way in (*ways, CARD_BUTTON[0])
        ],
    }
