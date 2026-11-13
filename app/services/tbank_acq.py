"""T-Bank internet acquiring (Т-Касса, API v2): the second card provider next to YooKassa.

The payment is created with ``Init`` and the buyer opens ``PaymentURL`` (T-Bank's own form: cards, SBP,
T-Pay, SberPay — whatever the terminal has enabled). The result comes back as an HTTP notification signed
with the terminal password and, for a lost notification, ``GetState``. A refund is ``Cancel``.

The Mini App can also skip T-Bank's page and ask for the link of one way to pay (``wallet_link``): SBP (``GetQr``),
T-Pay and SberPay (GET links). Those ways must be enabled for the terminal in the cabinet; otherwise T-Bank answers
3001 / 11 / ``Allowed=false`` and ``tbank_methods`` hides the button.

Everything the rest of the backend sees is shaped like the YooKassa service: ``create_payment`` returns
``yookassa_id`` (= T-Bank ``PaymentId``), ``get_payment_status`` returns ``status`` as
``succeeded|canceled|pending``. The table, the settlement and the side effects are the YooKassa ones.

Keys are read from ``settings`` (env only) and never logged. Docs: https://developer.tbank.ru/eacq/
"""

import asyncio
import hashlib
import hmac
import logging
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Statuses of a payment (docs «GetState»): what they mean for us.
PAID_STATUSES = frozenset({"CONFIRMED"})
# A failed attempt that ends the payment. REJECTED can in theory be followed by a retry on the same form;
# the webhook reopens a canceled T-Bank payment when CONFIRMED does arrive (yookassa_repo.reopen_canceled).
FAILED_STATUSES = frozenset({"REJECTED", "CANCELED", "DEADLINE_EXPIRED", "AUTH_FAIL", "REVERSED"})
REFUND_STATUSES = frozenset({"REFUNDED", "PARTIAL_REFUNDED"})

# Order ids of the test terminal start with this; settlement writes such a payment as ``test_owner``, not ``succeeded``
# (a real id is ``gl`` + hex and a hex string never starts with "t").
TEST_ORDER_PREFIX = "gltest"

_DEFAULT_CA_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_ca.pem"

_RETRY_DELAYS = (1.0, 2.0)
_RETRY_BUDGET_S = 20.0


class TBankError(Exception):
    """T-Bank refused a call or did not answer. ``unknown`` — the outcome on its side is not known."""

    def __init__(self, message: str, *, code: str | None = None, unknown: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.unknown = unknown


def is_configured() -> bool:
    return bool(settings.TBANK_TERMINAL_KEY and settings.TBANK_PASSWORD)


# ── signature ───────────────────────────────────────────────────────

def _token_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def make_token(params: dict, password: str) -> str:
    """SHA-256 of the values of the root-level fields plus ``Password``, ordered by key.

    Nested objects/arrays (``DATA``, ``Receipt``, ``Data``) and ``Token`` itself do not take part;
    a ``null`` value is skipped.
    """
    pairs = {
        key: _token_value(value)
        for key, value in params.items()
        if key != "Token" and value is not None and not isinstance(value, (dict, list))
    }
    pairs["Password"] = password
    return hashlib.sha256("".join(pairs[key] for key in sorted(pairs)).encode("utf-8")).hexdigest()


def verify_notification(body: dict, password: str | None = None) -> bool:
    """True when the ``Token`` of a notification is the signature of its other fields."""
    password = settings.TBANK_PASSWORD if password is None else password
    token = body.get("Token")
    if not password or not isinstance(token, str) or not token:
        return False
    return hmac.compare_digest(token.lower(), make_token(body, password))


# ── HTTP ────────────────────────────────────────────────────────────

_ssl_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """``certifi`` plus the Russian Trusted CA bundle: ``securepay.tinkoff.ru`` is signed by the Ministry of Digital
    Development, which ``certifi`` does not know. Only this client uses it; the system store is not touched."""
    global _ssl_context
    if _ssl_context is None:
        context = ssl.create_default_context(cafile=certifi.where())
        context.load_verify_locations(cafile=settings.TBANK_CA_BUNDLE or str(_DEFAULT_CA_BUNDLE))
        _ssl_context = context
    return _ssl_context


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.TBANK_API_BASE.rstrip("/"),
        verify=ssl_context(),
        timeout=httpx.Timeout(20.0, connect=8.0),
        headers={"Content-Type": "application/json"},
        transport=transport,
    )


async def _call(
    method: str, payload: dict, *, retry_connect: bool = True, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST ``/<method>`` with the signature. Connect failures are retried (the request never left);
    anything after the request was sent raises ``TBankError(unknown=True)``."""
    if not is_configured():
        raise TBankError("Т-Банк не настроен")
    body = {"TerminalKey": settings.TBANK_TERMINAL_KEY, **payload}
    body["Token"] = make_token(body, settings.TBANK_PASSWORD)
    started = time.monotonic()
    for delay in (*_RETRY_DELAYS, None):
        try:
            async with _client(transport) as http:
                response = await http.post(f"/{method}", json=body)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            elapsed = time.monotonic() - started
            if not retry_connect or delay is None or elapsed + delay >= _RETRY_BUDGET_S:
                raise TBankError(f"Т-Банк недоступен ({method})", unknown=False) from exc
            logger.warning("T-Bank %s: connect failed (%.1fs), retrying", method, elapsed)
            await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            raise TBankError(f"Т-Банк не ответил ({method})", unknown=True) from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise TBankError(f"Т-Банк временно недоступен ({method}: {response.status_code})", unknown=True)
    try:
        data = response.json()
    except ValueError as exc:
        raise TBankError(f"Т-Банк вернул не JSON ({method}: {response.status_code})", unknown=True) from exc
    if not isinstance(data, dict):
        raise TBankError(f"Т-Банк вернул неожиданный ответ ({method})", unknown=True)
    if not data.get("Success"):
        code = str(data.get("ErrorCode") or "")
        message = f"{data.get('Message') or ''} {data.get('Details') or ''}".strip()
        raise TBankError(f"Т-Банк отказал ({method}, {code}): {message}"[:300], code=code)
    return data


async def _get(path: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """GET ``/<path>`` (T-Pay and SberPay links). Checked 10.11: the GET methods are not signed (a Bearer header makes no
    difference); a refusal is still HTTP 200 with ``Success=false``. Read only, so connect failures are retried."""
    if not is_configured():
        raise TBankError("Т-Банк не настроен")
    started = time.monotonic()
    for delay in (*_RETRY_DELAYS, None):
        try:
            async with _client(transport) as http:
                response = await http.get(f"/{path}")
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            elapsed = time.monotonic() - started
            if delay is None or elapsed + delay >= _RETRY_BUDGET_S:
                raise TBankError("Т-Банк недоступен (GET)", unknown=False) from exc
            await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            raise TBankError("Т-Банк не ответил (GET)", unknown=True) from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise TBankError(f"Т-Банк временно недоступен (GET: {response.status_code})", unknown=True)
    try:
        data = response.json()
    except ValueError as exc:
        raise TBankError(f"Т-Банк вернул не JSON (GET: {response.status_code})", unknown=True) from exc
    if not isinstance(data, dict):
        raise TBankError("Т-Банк вернул неожиданный ответ (GET)", unknown=True)
    if not data.get("Success"):
        code = str(data.get("ErrorCode") or "")
        raise TBankError(f"Т-Банк отказал (GET, {code}): {data.get('Message') or ''}"[:300], code=code)
    return data


# ── payments ────────────────────────────────────────────────────────

def kopecks(amount_rubles) -> int:
    return int((Decimal(str(amount_rubles)) * 100).to_integral_value())


def new_order_id() -> str:
    """Our id of the operation (≤ 50 chars, unique); the row stores it in ``provider_order_id``.
    The test terminal gets ``gltest…`` so that its payments can never settle as revenue."""
    return f"{TEST_ORDER_PREFIX if settings.TBANK_TEST_MODE else 'gl'}{uuid.uuid4().hex}"


def is_test_order(order_id: str | None) -> bool:
    return bool(order_id) and str(order_id).startswith(TEST_ORDER_PREFIX)


def _receipt(amount_rubles, description: str) -> dict:
    # Only with TBANK_SEND_RECEIPT=1; an e-mail/phone of the buyer would have to be added by the caller.
    return {
        "Taxation": "usn_income",
        "Items": [{
            "Name": description[:128], "Price": kopecks(amount_rubles), "Quantity": 1,
            "Amount": kopecks(amount_rubles), "Tax": "none",
            "PaymentMethod": "full_payment", "PaymentObject": "service",
        }],
    }


async def create_payment(
    amount_rubles: int,
    description: str,
    generations: int,
    *,
    user_id: int,
    telegram_id: int | None = None,
    return_url: str | None = None,
    fail_url: str | None = None,
    order_id: str | None = None,
    extra_metadata: dict | None = None,
    device_data: dict | None = None,
    notify: bool = True,
    due_in_minutes: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """``Init``: one-stage payment, the buyer is sent to ``PaymentURL``.

    ``device_data`` goes into ``DATA`` as it is (``SberPayWeb``, ``TinkoffPayWeb``, ``Device``: real booleans), these
    switch on the wallets for the link methods. ``notify=False`` and ``due_in_minutes`` are for the availability probe
    (no notification, the payment expires by itself).

    Returns ``yookassa_id`` (= PaymentId), ``confirmation_url`` (= PaymentURL), ``status``, ``order_id``.
    """
    order_id = order_id or new_order_id()
    success_url = return_url or f"https://t.me/{settings.BOT_USERNAME}"
    data = {"user_id": str(user_id), "generations": str(generations)}
    if telegram_id is not None:
        data["telegram_id"] = str(telegram_id)
    if extra_metadata:
        data.update({str(key)[:20]: str(value)[:100] for key, value in extra_metadata.items()})
    if device_data:
        data.update(device_data)
    payload = {
        "Amount": kopecks(amount_rubles),
        "OrderId": order_id,
        "Description": description[:140],
        "PayType": "O",
        "Language": "ru",
        "SuccessURL": success_url,
        "FailURL": fail_url or success_url,
        "DATA": data,
    }
    if notify:
        payload["NotificationURL"] = settings.TBANK_NOTIFICATION_URL
    if due_in_minutes:
        payload["RedirectDueDate"] = (datetime.now(timezone.utc) + timedelta(minutes=due_in_minutes)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if settings.TBANK_SEND_RECEIPT:
        payload["Receipt"] = _receipt(amount_rubles, description)
    # Init creates a payment: a lost answer must not be retried blindly (a second Init is a second payment).
    result = await _call("Init", payload, transport=transport)
    payment_id = str(result.get("PaymentId") or "")
    url = result.get("PaymentURL")
    if not payment_id or not url:
        raise TBankError("Т-Банк не вернул PaymentId/PaymentURL", unknown=True)
    # Форма берёт язык телефона, а не ``Language`` из Init: на английском телефоне она английская.
    # Параметр ``language=ru`` в ссылке форма слушается (проверено 10.11 в Chromium с локалью en-US).
    url = _with_russian_form(url)
    logger.info("T-Bank payment created: payment_id=%s order=%s status=%s amount=%s",
                payment_id, order_id, result.get("Status"), amount_rubles)
    return {
        "yookassa_id": payment_id,
        "confirmation_url": url,
        "status": "pending",
        "order_id": order_id,
        "provider": "tbank",
    }


def _with_russian_form(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "language"]
    return urlunsplit(parts._replace(query=urlencode([*query, ("language", "ru")])))


# ── one way to pay: the link of a wallet (the buyer's phone opens the bank app) ──

TPAY_VERSION = "2.0"
# Our names of the ways to pay (they equal the Mini App's ``method`` values of the YooKassa buttons), in the order of
# the buttons (the owner's choice 10.11: SberPay, SBP, T-Pay; the card is the last button).
WALLET_METHODS = ("sberbank", "sbp", "tinkoff_bank")


def wallet_device_data(method: str, mobile: bool) -> dict:
    """``DATA`` of ``Init`` that switches the wallet on (docs «Своя платёжная форма»: ``SberPayWeb``/``TinkoffPayWeb``)."""
    data: dict = {"Device": "Mobile" if mobile else "Desktop"}
    if method == "sberbank":
        data["SberPayWeb"] = True
    elif method == "tinkoff_bank":
        data["TinkoffPayWeb"] = True
    return data


def _https(url) -> str:
    url = str(url or "").strip()
    if not url.lower().startswith("https://"):
        raise TBankError("Т-Банк вернул не https-ссылку", unknown=False)
    return url


async def sbp_link(payment_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """``GetQr`` (DataType=PAYLOAD): ``Data`` is ``https://qr.nspk.ru/…``, on a phone it opens the choice of the bank.
    Checked on the test terminal 10.11 (the production one answered 3001 «Оплата выбранным способом недоступна»)."""
    data = await _call("GetQr", {"PaymentId": str(payment_id), "DataType": "PAYLOAD"}, transport=transport)
    return _https(data.get("Data"))


async def tpay_allowed(*, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """``TinkoffPay/terminals/{key}/status``: the version of T-Pay the terminal can use, or None when it cannot."""
    data = await _get(f"TinkoffPay/terminals/{settings.TBANK_TERMINAL_KEY}/status", transport=transport)
    params = data.get("Params") or {}
    if not params.get("Allowed"):
        return None
    return str(params.get("Version") or TPAY_VERSION)


async def tpay_link(payment_id: str, version: str = TPAY_VERSION, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    data = await _get(f"TinkoffPay/transactions/{payment_id}/versions/{version}/link", transport=transport)
    return _https((data.get("Params") or {}).get("RedirectUrl"))


async def sberpay_link(payment_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """``SberPay/transactions/{id}/link``: ``Params.RedirectUrl``, a deeplink of the Sber app or its https page."""
    data = await _get(f"SberPay/transactions/{payment_id}/link", transport=transport)
    url = str((data.get("Params") or {}).get("RedirectUrl") or "").strip()
    if not url:
        raise TBankError("Т-Банк не вернул ссылку SberPay", unknown=False)
    return url


async def wallet_link(method: str, payment_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The link of the wallet for an ``Init``-ed payment; ``TBankError`` (``code`` 3001/11…) when it is not enabled."""
    if method == "sbp":
        return await sbp_link(payment_id, transport=transport)
    if method == "sberbank":
        return await sberpay_link(payment_id, transport=transport)
    if method == "tinkoff_bank":
        version = await tpay_allowed(transport=transport)
        if version is None:
            raise TBankError("T-Pay недоступен на терминале", code="tpay_not_allowed")
        return await tpay_link(payment_id, version, transport=transport)
    raise ValueError(f"unknown wallet method {method!r}")


def normalize_status(tbank_status: str | None) -> str:
    if tbank_status in PAID_STATUSES:
        return "succeeded"
    if tbank_status in FAILED_STATUSES:
        return "canceled"
    return "pending"


# ``Params[Source]`` of a GetState answer (docs «GetState»: BNPL, cards, Installment, MirPay, qrsbp, SberPay, TinkoffPay,
# qrdigitalruble, AlfaPay; SberPay and cards seen on the production terminal 10.11) -> the type YooKassa would write
# (``yookassa_payments.payment_method_type``), so the ways of both providers can be compared. Mir Pay is a wallet that pays
# with a card of the Mir system, the way YooKassa lists apple_pay/google_pay apart from bank_card, hence its own type.
# Anything else becomes ``tbank_<source lower>`` (tbank_alfapay, tbank_bnpl, tbank_installment, tbank_qrdigitalruble).
SOURCE_METHOD_TYPES = {
    "sberpay": "sberbank",
    "qrsbp": "sbp",
    "tinkoffpay": "tinkoff_bank",
    "cards": "bank_card",
    "mirpay": "mir_pay",
}


def method_type_of_source(source: str | None) -> str | None:
    """Our payment-method type for a T-Bank ``Source``; None when the answer has no ``Source`` (an unpaid payment)."""
    key = str(source or "").strip().lower()
    if not key:
        return None
    return SOURCE_METHOD_TYPES.get(key) or f"tbank_{key}"[:64]


def state_payment_method(data: dict) -> dict | None:
    """``{"type": …}`` from the ``Params`` of a GetState answer (a list of ``{"Key": …, "Value": …}``), or None.

    The card columns stay empty: the answer has no card, only the notification does (``notification_card``)."""
    params = data.get("Params")
    if not isinstance(params, list):
        return None
    source = next(
        (item.get("Value") for item in params if isinstance(item, dict) and item.get("Key") == "Source"), None,
    )
    method_type = method_type_of_source(source)
    if method_type is None:
        return None
    return {"type": method_type, "card_first6": None, "card_last4": None, "card_type": None, "card_country": None}


def remote_view(data: dict) -> dict:
    """A GetState answer or a notification reduced to the YooKassa-shaped dict the call sites compare."""
    status = data.get("Status")
    try:
        amount_value = Decimal(int(data.get("Amount"))) / 100
    except (TypeError, ValueError):
        amount_value = None
    return {
        "yookassa_id": str(data.get("PaymentId") or ""),
        "order_id": data.get("OrderId"),
        "status": normalize_status(status),
        "tbank_status": status,
        "paid": status in PAID_STATUSES,
        "amount_value": amount_value,
        "currency": "RUB",
        "metadata": {},
        "payment_method": state_payment_method(data),
    }


async def get_payment_status(
    payment_id: str, *, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """``GetState`` (read only, so it is retried freely)."""
    data = await _call("GetState", {"PaymentId": str(payment_id)}, transport=transport)
    view = remote_view(data)
    view["yookassa_id"] = view["yookassa_id"] or str(payment_id)
    return view


def matches_local_payment(remote: dict, local: dict) -> bool:
    """Provider data against the immutable local intent: same payment, order, amount, and it is paid."""
    amount = remote.get("amount_value")
    try:
        local_amount = Decimal(str(local["amount_rubles"]))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        remote.get("yookassa_id") == local["yookassa_id"]
        and bool(local.get("provider_order_id"))
        and remote.get("order_id") == local.get("provider_order_id")
        and bool(remote.get("paid"))
        and amount is not None
        and Decimal(str(amount)) == local_amount
    )


async def cancel_payment(
    payment_id: str, amount_rubles: Decimal | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """``Cancel``: a refund of a confirmed payment, full without ``amount_rubles`` and partial with it.

    Returns ``status``, ``original_amount`` and ``new_amount`` (rubles; ``new_amount`` is what stays paid).
    A lost answer is ``TBankError(unknown=True)``: ``GetState`` and the cabinet tell whether it went through.
    """
    payload: dict = {"PaymentId": str(payment_id)}
    if amount_rubles is not None:
        payload["Amount"] = kopecks(amount_rubles)
    data = await _call("Cancel", payload, retry_connect=True, transport=transport)

    def _rub(key):
        try:
            return Decimal(int(data.get(key))) / 100
        except (TypeError, ValueError):
            return None

    return {
        "status": data.get("Status"),
        "original_amount": _rub("OriginalAmount"),
        "new_amount": _rub("NewAmount"),
        "raw": {key: data.get(key) for key in ("Status", "PaymentId", "OrderId", "OriginalAmount", "NewAmount")},
    }


def notification_card(body: dict) -> dict | None:
    """The payer's card from a notification (``Pan`` = ``430000******0777``) for the anti-abuse columns."""
    pan = str(body.get("Pan") or "")
    if len(pan) < 10 or "*" not in pan:
        return None
    first6, last4 = pan[:6], pan[-4:]
    if not (first6.isdigit() and last4.isdigit()):
        return None
    # No type: the card mask does not tell a card from a wallet, the type comes from GetState (``state_payment_method``).
    return {"type": None, "card_first6": first6, "card_last4": last4, "card_type": None, "card_country": None}
