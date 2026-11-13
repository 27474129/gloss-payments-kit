"""Which ways to pay the T-Bank terminal can really give a link for (SBP, SberPay, T-Pay), for the Mini App's buttons.

T-Bank has no «what is enabled» call for SBP and SberPay: a terminal where they are off still lists the banks and
accepts ``Init``, and only the link call refuses (``GetQr`` → 3001, SberPay/T-Pay → 11, T-Pay status ``Allowed=false``;
seen on the production terminal 10.11, before the cabinet setting «способы через API» was switched on). So the answer is
learned by trying once: a probe makes a 10 ₽ ``Init`` (no notification, expires in minutes), asks every link and cancels.
The result lives ``PROBE_TTL`` (open ways) / ``FAILED_TTL`` (closed ways) in memory of the process; a refusal at a real
checkout closes the way for ``FAILED_TTL`` at once (``mark_unavailable``), so a button that stopped working disappears
without waiting for the probe.
"""

import asyncio
import logging
import time
import uuid

from app.core.config import settings
from app.services import tbank_acq

logger = logging.getLogger(__name__)

# A probe is a 10 ₽ Init that stays in the cabinet's operations, so it is rare: an open way is trusted for an hour (a refusal
# at a real checkout closes it at once), a closed one is looked at again in 15 minutes (the cabinet setting may have changed).
PROBE_TTL = 60 * 60.0
PROBE_ERROR_TTL = 60.0
FAILED_TTL = 15 * 60.0
PROBE_WAIT = 8.0
PROBE_AMOUNT_RUBLES = 10  # the minimum of SBP
PROBE_DUE_MINUTES = 5

# method -> (checked at, can give a link)
_state: dict[str, tuple[float, bool]] = {}
_probe_task: asyncio.Task | None = None
_probe_error_at = 0.0


def reset() -> None:
    """For tests."""
    global _probe_task, _probe_error_at
    _state.clear()
    _probe_task = None
    _probe_error_at = 0.0


def mark_unavailable(method: str) -> None:
    _state[method] = (time.monotonic(), False)


def _forced() -> list[str]:
    asked = {v.strip() for v in settings.TBANK_WALLETS_ASSUME.split(",")}
    return [m for m in tbank_acq.WALLET_METHODS if m in asked]


async def _try(method: str, payment_id: str) -> bool:
    try:
        await tbank_acq.wallet_link(method, payment_id)
        return True
    except tbank_acq.TBankError as exc:
        logger.info("T-Bank probe: %s is off (%s)", method, exc.code or exc)
        return False


async def _probe() -> None:
    global _probe_error_at
    try:
        payment = await tbank_acq.create_payment(
            PROBE_AMOUNT_RUBLES, "Проверка способов оплаты", 0, user_id=0,
            order_id=f"gl-probe-{uuid.uuid4().hex[:16]}",
            device_data={"SberPayWeb": True, "TinkoffPayWeb": True, "Device": "Mobile"},
            notify=False, due_in_minutes=PROBE_DUE_MINUTES,
        )
    except tbank_acq.TBankError:
        logger.warning("T-Bank probe: Init failed", exc_info=True)
        _probe_error_at = time.monotonic()
        return
    payment_id = payment["yookassa_id"]
    now = time.monotonic()
    # One after another, the QR last: a made QR may close the payment to the other ways (it also cannot be canceled).
    for method in ("tinkoff_bank", "sberbank", "sbp"):
        _state[method] = (now, await _try(method, payment_id))
    try:
        await tbank_acq.cancel_payment(payment_id)
    except tbank_acq.TBankError:
        # After a QR was made the payment cannot be canceled; it expires by itself (RedirectDueDate).
        logger.info("T-Bank probe: %s left to expire", payment_id)
    logger.info("T-Bank probe: %s", {m: _state[m][1] for m in tbank_acq.WALLET_METHODS})


def _fresh() -> bool:
    now = time.monotonic()
    return all(
        m in _state and now - _state[m][0] < (PROBE_TTL if _state[m][1] else FAILED_TTL)
        for m in tbank_acq.WALLET_METHODS
    )


def _known() -> list[str]:
    now = time.monotonic()
    return [
        m for m in tbank_acq.WALLET_METHODS
        if m in _state and _state[m][1] and now - _state[m][0] < PROBE_TTL
    ]


async def available_wallets() -> list[str]:
    """Ways to pay with a link, in the order of the buttons (``tbank_acq.WALLET_METHODS``).
    Waits for a running probe up to ``PROBE_WAIT`` s; without an answer there are no wallets (the card still works)."""
    global _probe_task
    forced = _forced()
    if forced:
        return forced
    if not tbank_acq.is_configured():
        return []
    if not _fresh() and time.monotonic() - _probe_error_at > PROBE_ERROR_TTL:
        if _probe_task is None or _probe_task.done():
            _probe_task = asyncio.create_task(_probe())
        try:
            await asyncio.wait_for(asyncio.shield(_probe_task), PROBE_WAIT)
        except (asyncio.TimeoutError, Exception):  # a probe never breaks the sheet
            logger.warning("T-Bank probe did not finish", exc_info=True)
    return _known()
