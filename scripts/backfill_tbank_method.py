"""
Fill ``yookassa_payments.payment_method_type`` of the T-Bank payments that were paid before the type was recorded.

A T-Bank notification does not say how the buyer paid (its ``Pan`` is a card mask or a phone); only ``GetState`` has
``Params[Source]`` (SberPay, qrsbp, TinkoffPay, cards …). Payments with ``provider = 'tbank'`` and the status
succeeded / refunded / test_owner and an empty type get one ``GetState`` each, and the type is written the way the
backend writes it for new payments (``tbank_acq.method_type_of_source``: sberbank / sbp / tinkoff_bank / bank_card /
mir_pay / tbank_<source>). A payment whose answer has no ``Source`` is left empty. The card columns are not touched.

Dry run by default (prints what would be written and a summary by type):
    python scripts/backfill_tbank_method.py
Write the values:
    python scripts/backfill_tbank_method.py --apply

--dsn defaults to DATABASE_URL, the terminal comes from TBANK_TERMINAL_KEY / TBANK_PASSWORD of the backend env.
--recheck-cards also looks at rows already typed ``bank_card`` (the first version wrote it from a card-like ``Pan``) and
rewrites the type when ``Source`` says it was a wallet; a row that is a card in ``Source`` stays as it is.
Only the type is ever written, only where the column still holds the value the script read, so a second run (or a
notification that filled the type meanwhile) is harmless. Read-only toward T-Bank.
"""
import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from app.core.config import settings
from app.services import tbank_acq

STATUSES = ("succeeded", "refunded", "test_owner")
PAUSE_SECONDS = 0.25  # GetState is read only, but the terminal has a rate limit: no bursts


async def pick_rows(conn: asyncpg.Connection, recheck_cards: bool, limit: int | None) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, yookassa_id, provider_order_id, status, amount_rubles, payment_method_type
          FROM yookassa_payments
         WHERE provider = 'tbank' AND status = ANY($1::text[])
           AND (payment_method_type IS NULL OR ($2 AND payment_method_type = 'bank_card'))
         ORDER BY id
         LIMIT $3
        """,
        list(STATUSES), recheck_cards, limit,
    )


async def run(dsn: str, apply: bool, recheck_cards: bool, limit: int | None) -> int:
    if not tbank_acq.is_configured():
        print("TBANK_TERMINAL_KEY / TBANK_PASSWORD are empty: nothing to ask the bank with", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn)
    try:
        rows = await pick_rows(conn, recheck_cards, limit)
        print(f"{len(rows)} T-Bank payment(s) to look at ({'APPLY' if apply else 'dry run'})")
        summary: Counter[str] = Counter()
        written = 0
        for row in rows:
            try:
                remote = await tbank_acq.get_payment_status(row["yookassa_id"])
            except tbank_acq.TBankError as exc:
                summary["error"] += 1
                print(f"  id={row['id']} payment={row['yookassa_id']}: GetState failed: {exc}")
                continue
            finally:
                await asyncio.sleep(PAUSE_SECONDS)
            if remote.get("order_id") != row["provider_order_id"]:
                summary["order mismatch"] += 1
                print(f"  id={row['id']} payment={row['yookassa_id']}: OrderId differs from ours, skipped")
                continue
            method = remote.get("payment_method")
            if not method:
                summary["no Source"] += 1
                print(f"  id={row['id']} payment={row['yookassa_id']} ({remote.get('tbank_status')}): no Source, left empty")
                continue
            new_type = method["type"]
            if new_type == row["payment_method_type"]:
                summary[f"{new_type} (already)"] += 1
                continue
            summary[new_type] += 1
            print(f"  id={row['id']} payment={row['yookassa_id']} {row['amount_rubles']} ₽: "
                  f"{row['payment_method_type']} -> {new_type}")
            if apply:
                result = await conn.execute(
                    "UPDATE yookassa_payments SET payment_method_type = $2 "
                    "WHERE id = $1 AND payment_method_type IS NOT DISTINCT FROM $3",
                    row["id"], new_type, row["payment_method_type"],
                )
                written += int(result.rsplit(" ", 1)[-1])
        print("summary:", dict(summary))
        print(f"written: {written}" if apply else "nothing written (add --apply)")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=settings.DATABASE_URL)
    parser.add_argument("--apply", action="store_true", help="write the types (default: dry run)")
    parser.add_argument("--recheck-cards", action="store_true", help="also re-read rows typed bank_card")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.dsn, args.apply, args.recheck_cards, args.limit))


if __name__ == "__main__":
    sys.exit(main())
