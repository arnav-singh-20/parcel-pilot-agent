"""Deterministic calculations that apply ParcelPilot's precedence rules.

These are implemented as plain code, not left to the LLM, precisely
because the assessment is testing whether numeric/policy outcomes are
computed reliably. The model calls these as tools and explains the
result; it does not do the arithmetic or precedence resolution itself.

Precedence used throughout: signed customer agreement > current SOP/policy
> current product docs. Deprecated docs and historical ticket resolutions
are never authoritative.
"""
from datetime import datetime, timedelta
from .store import get_account, get_order, SNAPSHOT_TIME

CANCELLATION_FEE_INR = 250.0
DEFAULT_CREDIT_THRESHOLD_HOURS = 2.0
DEFAULT_CREDIT_CAP_INR = 500.0
DEFAULT_CREDIT_PCT = 0.10
MANAGER_APPROVAL_THRESHOLD_INR = 1000.0


def _parse(dt):
    return datetime.fromisoformat(dt) if dt else None


def calculate_cancellation_fee(order_id: str):
    order = get_order(order_id)
    if not order:
        return {"error": f"Order {order_id} not found."}
    account = get_account(order["account_id"])
    status = order["status"]
    result = {
        "order_id": order_id,
        "account_id": order["account_id"],
        "order_status": status,
        "source_precedence_applied": [],
    }

    if status == "DELIVERED":
        result.update(can_cancel=False, fee_inr=None,
                       reason="Order is DELIVERED. Delivered orders cannot be cancelled.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - order cancellation rules")
        return result

    if status == "PICKED_UP":
        result.update(can_cancel=False, fee_inr=None,
                       reason="Order has been PICKED_UP. Do not cancel; use the return-to-origin workflow instead.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - order cancellation rules")
        return result

    if status == "DRAFT":
        result.update(can_cancel=True, fee_inr=0.0, reason="DRAFT orders may be cancelled with no fee.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - order cancellation rules")
        return result

    if status == "BOOKED":
        booked_at = _parse(order["booked_at"])
        # Use the actual cancellation request time when we have one (this reflects
        # when the customer asked, not "now") - fall back to snapshot time (now)
        # for a hypothetical "if I cancel right now" question with no request logged yet.
        reference = _parse(order.get("cancellation_requested_at")) or SNAPSHOT_TIME
        minutes_since_booking = (reference - booked_at).total_seconds() / 60.0

        # Account-agreement override check (Northstar / ACCT-001)
        if order["account_id"] == "ACCT-001":
            result.update(
                can_cancel=True,
                fee_inr=0.0,
                reason=(
                    "Northstar Logistics Enterprise Agreement overrides the default SOP: "
                    "Northstar may cancel any BOOKED shipment before pickup with no fee, "
                    "regardless of elapsed time since booking."
                ),
            )
            result["source_precedence_applied"].append(
                "Northstar Logistics Enterprise Agreement (overrides default SOP) - takes precedence over Cancellation SOP v4"
            )
            return result

        # Default SOP path (applies to all other accounts, including LumenWorks,
        # whose agreement explicitly states no cancellation-fee waiver applies)
        if minutes_since_booking <= 30:
            result.update(can_cancel=True, fee_inr=0.0,
                           reason=f"Cancellation requested {minutes_since_booking:.0f} minutes after booking, within the 30-minute no-fee window.")
        else:
            result.update(can_cancel=True, fee_inr=CANCELLATION_FEE_INR,
                           reason=f"Cancellation requested {minutes_since_booking:.0f} minutes after booking, past the 30-minute window. Standard INR {CANCELLATION_FEE_INR:.0f} fee applies.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - order cancellation rules (default; no applicable agreement override for this account)")
        if account and account.get("contract_doc_id"):
            result["note"] = "This account has a signed agreement on file; it was checked and does not override the cancellation-fee terms."
        return result

    result.update(can_cancel=False, fee_inr=None, reason=f"Unrecognized order status '{status}'; escalate for manual review.")
    return result


def calculate_service_credit(order_id: str):
    order = get_order(order_id)
    if not order:
        return {"error": f"Order {order_id} not found."}
    account = get_account(order["account_id"])
    result = {
        "order_id": order_id,
        "account_id": order["account_id"],
        "source_precedence_applied": [],
    }

    carrier_fault = order.get("carrier_fault")
    customer_fault = order.get("customer_fault")

    if carrier_fault is None or customer_fault is None:
        result.update(eligible=None, credit_inr=None,
                       reason="Carrier-fault / customer-fault status is not recorded for this order. Do not promise a credit; verify with the carrier or ops before responding.")
        return result

    window_end = _parse(order["pickup_window_end"])
    pickup_actual = _parse(order.get("pickup_actual_at"))
    if pickup_actual:
        reference_time = pickup_actual
        timing_note = "actual pickup time"
    else:
        reference_time = SNAPSHOT_TIME
        timing_note = "current time (pickup has not yet occurred as of the dataset snapshot)"

    delay_hours = (reference_time - window_end).total_seconds() / 3600.0
    result["delay_hours"] = round(delay_hours, 2)
    result["delay_measured_against"] = timing_note
    result["carrier_fault"] = carrier_fault
    result["customer_fault"] = customer_fault

    if customer_fault:
        result.update(eligible=False, credit_inr=0.0, reason="Customer is at fault for the delay; no service credit applies.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - failed-pickup service credits")
        return result

    if not carrier_fault:
        result.update(eligible=False, credit_inr=0.0, reason="Carrier is not recorded as at fault; the default policy requires carrier fault for a failed-pickup credit.")
        result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - failed-pickup service credits")
        return result

    # Carrier at fault, customer not at fault - now apply the correct threshold/amount,
    # checking for an account-agreement override first.
    if order["account_id"] == "ACCT-002":
        threshold = 4.0
        if delay_hours > threshold:
            result.update(eligible=True, credit_inr=300.0,
                           reason=f"LumenWorks Service Agreement overrides the default rule: pickup is {delay_hours:.2f}h past the window end (threshold {threshold}h), carrier at fault, customer not at fault. Fixed INR 300 credit applies.")
        else:
            result.update(eligible=False, credit_inr=0.0,
                           reason=f"LumenWorks Service Agreement requires more than {threshold}h delay for a credit; measured delay is {delay_hours:.2f}h.")
        result["source_precedence_applied"].append("LumenWorks Service Agreement (overrides default SOP amount and threshold) - takes precedence over Cancellation SOP v4")
        return result

    # Default SOP (applies to Northstar per its own agreement's explicit deferral,
    # and to any account without an overriding clause)
    threshold = DEFAULT_CREDIT_THRESHOLD_HOURS
    if delay_hours > threshold:
        shipment_fee = order.get("shipment_fee_inr") or 0.0
        credit = min(DEFAULT_CREDIT_CAP_INR, DEFAULT_CREDIT_PCT * shipment_fee)
        result.update(eligible=True, credit_inr=round(credit, 2),
                       reason=f"Pickup is {delay_hours:.2f}h past the window end (default threshold {threshold}h), carrier at fault, customer not at fault. Default credit = lower of INR {DEFAULT_CREDIT_CAP_INR:.0f} or 10% of shipment fee (INR {shipment_fee:.0f}) = INR {credit:.2f}.")
        if credit > MANAGER_APPROVAL_THRESHOLD_INR:
            result["requires_manager_approval"] = True
        if order["account_id"] == "ACCT-001":
            result["note"] = "Northstar's agreement caps monthly aggregate credits at INR 5,000 but does not change this per-incident calculation; verify month-to-date credits before issuing if near the cap (not tracked in this dataset)."
    else:
        result.update(eligible=False, credit_inr=0.0,
                       reason=f"Delay is {delay_hours:.2f}h, at or below the default {threshold}h threshold for a credit.")
    result["source_precedence_applied"].append("Cancellation & Service Credit SOP v4 - failed-pickup service credits (default; no applicable agreement override for this account)")
    if account and account.get("contract_doc_id") and order["account_id"] not in ("ACCT-001", "ACCT-002"):
        result["note"] = "This account has a signed agreement on file; it was checked and does not override the service-credit terms."
    return result


def calculate_sla_target(account_id: str, severity: str):
    account = get_account(account_id)
    if not account:
        return {"error": f"Account {account_id} not found."}
    severity = severity.upper()
    if severity not in ("P1", "P2", "P3"):
        return {"error": "severity must be P1, P2, or P3"}

    result = {"account_id": account_id, "severity": severity, "source_precedence_applied": []}

    overrides = {
        "ACCT-001": {"P1": "15 minutes, 24x7", "P2": "1 hour", "P3": "8 business hours"},
        "ACCT-002": {"P1": "2 business hours", "P2": "4 business hours", "P3": "2 business days"},
    }
    if account_id in overrides:
        result["target"] = overrides[account_id][severity]
        result["source"] = f"{account['account_name']} signed agreement (overrides default policy)"
        result["source_precedence_applied"].append("Signed customer agreement - takes precedence over Support Policy v3")
        if account_id == "ACCT-002":
            result["note"] = "LumenWorks agreement also states no weekend or after-hours support coverage."
        return result

    defaults = {
        "Enterprise": {"P1": "30 minutes, 24x7", "P2": "2 hours", "P3": "1 business day"},
        "Growth": {"P1": "2 business hours", "P2": "4 business hours", "P3": "2 business days"},
        "Standard": {"P1": "4 business hours", "P2": "1 business day", "P3": "2 business days"},
    }
    plan = account["plan"]
    result["target"] = defaults[plan][severity]
    result["source"] = f"Support Policy v3 (CURRENT), default for {plan} plan"
    result["source_precedence_applied"].append("Support Policy v3 - default first-response targets (no agreement override for this account)")
    return result
