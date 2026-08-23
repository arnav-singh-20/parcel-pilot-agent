"""In-memory data store loaded from the supplied data pack.

This is intentionally a flat JSON-backed store rather than a database:
the assessment data pack is tiny and static. Swapping this for a real
DB/vector-store later only requires changing this module - callers
(tools.py) only see the query functions below.
"""
import json
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent.parent / "data"

with open(DATA_DIR / "documents.json") as f:
    DOCUMENTS = json.load(f)

with open(DATA_DIR / "structured.json") as f:
    STRUCTURED = json.load(f)

SNAPSHOT_TIME = datetime.fromisoformat(STRUCTURED["snapshot_time"])
ACCOUNTS = {a["account_id"]: a for a in STRUCTURED["accounts"]}
ORDERS = {o["order_id"]: o for o in STRUCTURED["orders"]}
TICKETS = {t["ticket_id"]: t for t in STRUCTURED["tickets"]}
DOCS_BY_ID = {d["doc_id"]: d for d in DOCUMENTS}


def get_account(account_id: str):
    return ACCOUNTS.get(account_id)


def list_orders(account_id: str | None = None, status: str | None = None):
    out = list(ORDERS.values())
    if account_id:
        out = [o for o in out if o["account_id"] == account_id]
    if status:
        out = [o for o in out if o["status"].upper() == status.upper()]
    return out


def list_tickets(account_id: str | None = None, status: str | None = None):
    out = list(TICKETS.values())
    if account_id:
        out = [t for t in out if t["account_id"] == account_id]
    if status:
        out = [t for t in out if t["status"].lower() == status.lower()]
    return out


def get_order(order_id: str):
    return ORDERS.get(order_id)


def get_ticket(ticket_id: str):
    return TICKETS.get(ticket_id)


def account_agreement_doc(account_id: str):
    acct = ACCOUNTS.get(account_id)
    if not acct or not acct.get("contract_doc_id"):
        return None
    return DOCS_BY_ID.get(acct["contract_doc_id"])
