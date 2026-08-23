"""Tool implementations and access control.

CRITICAL DESIGN POINT: access control is enforced here, in code, using the
server-held session context (`ctx`). The model never supplies or can
override `ctx` - it only supplies tool arguments like `account_id`, which
are checked against `ctx` before any data is returned. A customer session
cannot read another account's data no matter what the model (or a user
prompt-injecting the model) asks for.
"""
from dataclasses import dataclass, field
from typing import Literal
from . import store, calculations
from .retrieval import search as doc_search

Mode = Literal["customer", "internal"]


@dataclass
class SessionContext:
    mode: Mode
    account_id: str | None = None          # required when mode == "customer"
    staff_name: str | None = None          # required when mode == "internal"
    staff_role: str | None = None          # "agent" | "manager", when mode == "internal"


class AccessDenied(Exception):
    pass


def _check_account_access(ctx: SessionContext, account_id: str | None):
    """Returns the account_id the caller is actually allowed to see, or raises."""
    if ctx.mode == "customer":
        if account_id and account_id != ctx.account_id:
            raise AccessDenied(
                f"Access denied: this session is scoped to account {ctx.account_id}. "
                f"Customers cannot view data for other accounts."
            )
        return ctx.account_id
    # internal: any account is viewable by any authenticated staff member
    return account_id


# ---------------------------------------------------------------------------
# Tool 1: document search
# ---------------------------------------------------------------------------
def tool_search_documents(ctx: SessionContext, query: str, doc_types: list[str] | None = None):
    account_id = ctx.account_id if ctx.mode == "customer" else None
    results = doc_search(query, account_id=account_id, doc_types=doc_types)
    return {"results": results}


# ---------------------------------------------------------------------------
# Tool 2-4: structured lookups
# ---------------------------------------------------------------------------
def tool_lookup_account(ctx: SessionContext, account_id: str):
    scoped_id = _check_account_access(ctx, account_id)
    acct = store.get_account(scoped_id)
    if not acct:
        return {"error": f"Account {scoped_id} not found."}
    # Never expose other accounts' identifiers even indirectly
    return acct


def tool_lookup_orders(ctx: SessionContext, account_id: str | None = None, order_id: str | None = None, status: str | None = None):
    if order_id:
        order = store.get_order(order_id)
        if not order:
            return {"error": f"Order {order_id} not found."}
        _check_account_access(ctx, order["account_id"])  # raises if not permitted
        return {"orders": [order]}
    scoped_id = _check_account_access(ctx, account_id)
    if ctx.mode == "customer" and not scoped_id:
        scoped_id = ctx.account_id
    orders = store.list_orders(account_id=scoped_id, status=status)
    return {"orders": orders}


def tool_lookup_tickets(ctx: SessionContext, account_id: str | None = None, ticket_id: str | None = None, status: str | None = None):
    if ticket_id:
        ticket = store.get_ticket(ticket_id)
        if not ticket:
            return {"error": f"Ticket {ticket_id} not found."}
        _check_account_access(ctx, ticket["account_id"])
        return {"tickets": [ticket]}
    scoped_id = _check_account_access(ctx, account_id)
    if ctx.mode == "customer" and not scoped_id:
        scoped_id = ctx.account_id
    tickets = store.list_tickets(account_id=scoped_id, status=status)
    return {"tickets": tickets}


# ---------------------------------------------------------------------------
# Tool 5: calculations
# ---------------------------------------------------------------------------
def tool_calculate(ctx: SessionContext, calc_type: str, order_id: str | None = None, account_id: str | None = None, severity: str | None = None):
    if calc_type == "cancellation_fee":
        if not order_id:
            return {"error": "order_id is required for cancellation_fee"}
        order = store.get_order(order_id)
        if order:
            _check_account_access(ctx, order["account_id"])
        return calculations.calculate_cancellation_fee(order_id)
    if calc_type == "service_credit":
        if not order_id:
            return {"error": "order_id is required for service_credit"}
        order = store.get_order(order_id)
        if order:
            _check_account_access(ctx, order["account_id"])
        return calculations.calculate_service_credit(order_id)
    if calc_type == "sla_target":
        if not account_id or not severity:
            return {"error": "account_id and severity are required for sla_target"}
        _check_account_access(ctx, account_id)
        return calculations.calculate_sla_target(account_id, severity)
    return {"error": f"Unknown calc_type '{calc_type}'. Use cancellation_fee, service_credit, or sla_target."}


# ---------------------------------------------------------------------------
# Tool 6: state-changing action (mocked, gated by confirmation in app.py)
# ---------------------------------------------------------------------------
_ACTION_LOG = []  # in-memory mock "database" of created actions


def tool_create_action(ctx: SessionContext, action_type: str, account_id: str, summary: str,
                        order_id: str | None = None, ticket_id: str | None = None,
                        severity: str | None = None, amount_inr: float | None = None):
    """Executes the mocked side effect. Only ever called by app.py AFTER the
    user has explicitly confirmed - never directly from the model's tool call."""
    if action_type not in ("create_escalation", "update_ticket", "create_followup_task"):
        return {"error": f"Unknown action_type '{action_type}'."}

    scoped_id = _check_account_access(ctx, account_id)

    if ctx.mode == "customer" and action_type != "create_escalation":
        raise AccessDenied("Customers may only request an escalation, not directly update tickets or create internal follow-up tasks.")

    if amount_inr and amount_inr > calculations.MANAGER_APPROVAL_THRESHOLD_INR:
        if ctx.mode != "internal" or ctx.staff_role != "manager":
            return {
                "error": (
                    f"This action involves an amount (INR {amount_inr:.0f}) above the "
                    f"INR {calculations.MANAGER_APPROVAL_THRESHOLD_INR:.0f} manager-approval threshold. "
                    "A manager must approve before this can be created."
                )
            }

    record = {
        "action_id": f"ACT-{len(_ACTION_LOG) + 1:04d}",
        "action_type": action_type,
        "account_id": scoped_id,
        "order_id": order_id,
        "ticket_id": ticket_id,
        "severity": severity,
        "amount_inr": amount_inr,
        "summary": summary,
        "created_by": ctx.staff_name or f"customer:{ctx.account_id}",
        "status": "created",
    }
    _ACTION_LOG.append(record)
    return {"status": "success", "record": record}


def get_action_log():
    return list(_ACTION_LOG)


# ---------------------------------------------------------------------------
# Tool schemas (internal format; converted to Gemini function-calling format
# by the adapters in agent.py)
# ---------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "search_documents",
        "description": (
            "Search ParcelPilot's policies, SOPs, product documentation, and customer agreements. "
            "Returns matching passages tagged with status (CURRENT/DEPRECATED), effective date, and "
            "which account (if any) an agreement applies to. Always check status/effective date before "
            "relying on a result - deprecated documents are returned only for transparency, never as "
            "current guidance. Use this before asserting any policy, SLA, or contract term."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query, e.g. 'cancellation fee after 30 minutes' or 'bulk upload row limit'."},
                "doc_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["policy", "sop", "product_docs", "customer_agreement"]},
                    "description": "Optional filter to restrict the search to specific document types."
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_account",
        "description": "Look up a ParcelPilot account's plan, status, CSM, and notes by account_id.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string", "description": "e.g. ACCT-001"}},
            "required": ["account_id"],
        },
    },
    {
        "name": "lookup_orders",
        "description": "Look up shipment orders by order_id, or list orders for an account, optionally filtered by status (DRAFT/BOOKED/PICKED_UP/DELIVERED).",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "account_id": {"type": "string"},
                "status": {"type": "string", "enum": ["DRAFT", "BOOKED", "PICKED_UP", "DELIVERED"]},
            },
        },
    },
    {
        "name": "lookup_tickets",
        "description": "Look up support tickets by ticket_id, or list tickets for an account, optionally filtered by status (open/closed). Historical ticket 'historical_resolution' fields are past agent notes only - they may be wrong and are never authoritative over current policy/SOP/agreement documents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "account_id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "closed"]},
            },
        },
    },
    {
        "name": "calculate",
        "description": (
            "Run a deterministic ParcelPilot business calculation - use this instead of computing "
            "fees, credits, or SLA targets yourself. calc_type='cancellation_fee' (needs order_id), "
            "'service_credit' (needs order_id), or 'sla_target' (needs account_id and severity: P1/P2/P3). "
            "The result already applies the correct precedence between the customer's signed agreement "
            "and the default policy/SOP, and flags when manager approval or human verification is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "calc_type": {"type": "string", "enum": ["cancellation_fee", "service_credit", "sla_target"]},
                "order_id": {"type": "string"},
                "account_id": {"type": "string"},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
            },
            "required": ["calc_type"],
        },
    },
    {
        "name": "create_action",
        "description": (
            "Prepare a state-changing action: create_escalation, update_ticket, or create_followup_task. "
            "IMPORTANT: calling this tool only STAGES the action for the user to review - it will never "
            "take effect until the user explicitly confirms in the interface. After calling this, tell the "
            "user what you are proposing to do and why, and wait for their confirmation before assuming it happened."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": ["create_escalation", "update_ticket", "create_followup_task"]},
                "account_id": {"type": "string"},
                "summary": {"type": "string", "description": "Human-readable summary of what this action does and why."},
                "order_id": {"type": "string"},
                "ticket_id": {"type": "string"},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                "amount_inr": {"type": "number", "description": "If this action involves issuing a service credit amount."},
            },
            "required": ["action_type", "account_id", "summary"],
        },
    },
]

READ_ONLY_TOOLS = {"search_documents", "lookup_account", "lookup_orders", "lookup_tickets", "calculate"}
ACTION_TOOLS = {"create_action"}


def dispatch(ctx: SessionContext, tool_name: str, tool_input: dict):
    if tool_name == "search_documents":
        return tool_search_documents(ctx, **tool_input)
    if tool_name == "lookup_account":
        return tool_lookup_account(ctx, **tool_input)
    if tool_name == "lookup_orders":
        return tool_lookup_orders(ctx, **tool_input)
    if tool_name == "lookup_tickets":
        return tool_lookup_tickets(ctx, **tool_input)
    if tool_name == "calculate":
        return tool_calculate(ctx, **tool_input)
    raise ValueError(f"Unknown or non-dispatchable tool: {tool_name}")
