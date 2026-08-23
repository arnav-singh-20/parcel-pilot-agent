"""Rule-based signal detection for the internal Ops Dashboard (Problem 1).

Deliberately deterministic/explainable rather than LLM-generated: for an
internal triage view, an ops lead needs to trust *why* something is
flagged, and rule-based signals are cheap to compute over a small ticket
volume and easy to extend. See PRODUCT.md for how this would evolve
(clustering/embeddings) at higher ticket volume.
"""
from collections import defaultdict
from datetime import timedelta
from .store import STRUCTURED, SNAPSHOT_TIME, ACCOUNTS
from .calculations import calculate_sla_target, _parse

SLA_HOURS = {
    ("ACCT-001", "P1"): 0.25, ("ACCT-001", "P2"): 1, ("ACCT-001", "P3"): 8,
    ("ACCT-002", "P1"): 2, ("ACCT-002", "P2"): 4, ("ACCT-002", "P3"): 48,
}
DEFAULT_SLA_HOURS = {
    "Enterprise": {"P1": 0.5, "P2": 2, "P3": 24},
    "Growth": {"P1": 2, "P2": 4, "P3": 48},
    "Standard": {"P1": 4, "P2": 24, "P3": 48},
}

# Very light heuristic severity inference from subject/description, purely for
# the dashboard's SLA-risk signal (a real system would have a severity field
# set at ticket creation - see PRODUCT.md).
def _infer_severity(ticket):
    text = (ticket["subject"] + " " + ticket["description"]).lower()
    if any(k in text for k in ["all shipment", "every user", "http 500", "security", "api key", "exposure", "outage"]):
        return "P1"
    if any(k in text for k in ["bulk upload", "fails", "still shows booked", "webhook"]):
        return "P2"
    return "P3"


def _sla_hours(account_id, severity):
    if (account_id, severity) in SLA_HOURS:
        return SLA_HOURS[(account_id, severity)]
    plan = ACCOUNTS[account_id]["plan"]
    return DEFAULT_SLA_HOURS[plan][severity]


def build_dashboard():
    tickets = [t for t in STRUCTURED["tickets"] if t["status"] == "open"]
    orders = STRUCTURED["orders"]

    signals = []

    # 1. SLA risk - open tickets approaching/exceeding inferred first-response SLA
    sla_risk = []
    for t in tickets:
        sev = _infer_severity(t)
        hours_target = _sla_hours(t["account_id"], sev)
        created = _parse(t["created_at"])
        age_hours = (SNAPSHOT_TIME - created).total_seconds() / 3600.0
        pct = age_hours / hours_target if hours_target else 0
        if pct >= 0.7:
            sla_risk.append({
                "ticket_id": t["ticket_id"], "account_id": t["account_id"],
                "account_name": ACCOUNTS[t["account_id"]]["account_name"],
                "subject": t["subject"], "inferred_severity": sev,
                "age_hours": round(age_hours, 2), "sla_target_hours": hours_target,
                "pct_of_sla_used": round(pct * 100, 0),
                "status": "BREACHED" if pct >= 1.0 else "AT RISK",
            })
    sla_risk.sort(key=lambda r: -r["pct_of_sla_used"])
    if sla_risk:
        signals.append({
            "type": "sla_risk",
            "title": "Tickets approaching or exceeding first-response SLA",
            "severity": "high" if any(r["status"] == "BREACHED" for r in sla_risk) else "medium",
            "items": sla_risk,
        })

    # 2. Recurring product issue - multiple open tickets referencing the same theme
    theme_map = {
        "bulk_upload": ["bulk upload", "csv"],
        "pickup_status_lag": ["still shows booked", "webhook", "pickup"],
    }
    theme_hits = defaultdict(list)
    for t in tickets:
        text = (t["subject"] + " " + t["description"]).lower()
        for theme, keywords in theme_map.items():
            if any(k in text for k in keywords):
                theme_hits[theme].append(t)
    recurring = []
    for theme, hits in theme_hits.items():
        if len(hits) >= 2 or (len(hits) >= 1 and theme == "pickup_status_lag"):
            accounts_affected = sorted({h["account_id"] for h in hits})
            recurring.append({
                "theme": theme,
                "known_issue": "KI-208 (Bulk Upload)" if theme == "bulk_upload" else "KI-211 (SwiftShip pickup webhook delay)",
                "ticket_count": len(hits),
                "accounts_affected": accounts_affected,
                "ticket_ids": [h["ticket_id"] for h in hits],
            })
    if recurring:
        signals.append({
            "type": "recurring_product_issue",
            "title": "Multiple open tickets matching a known product issue",
            "severity": "medium",
            "items": recurring,
        })

    # 3. Multi-customer impact - same underlying issue hitting >1 account concurrently
    multi_customer = [r for r in recurring if len(r["accounts_affected"]) > 1]
    if multi_customer:
        signals.append({
            "type": "multi_customer_impact",
            "title": "Issues affecting multiple customers at the same time",
            "severity": "high",
            "items": multi_customer,
        })

    # 4. Unusual order pattern - carrier-fault delays not yet resolved
    stuck_orders = []
    for o in orders:
        if o["status"] == "BOOKED" and o.get("carrier_fault") and not o.get("pickup_actual_at"):
            window_end = _parse(o["pickup_window_end"])
            delay_hours = (SNAPSHOT_TIME - window_end).total_seconds() / 3600.0
            if delay_hours > 0:
                stuck_orders.append({
                    "order_id": o["order_id"], "account_id": o["account_id"],
                    "account_name": ACCOUNTS[o["account_id"]]["account_name"],
                    "carrier": o["carrier"], "delay_hours": round(delay_hours, 2),
                })
    if stuck_orders:
        signals.append({
            "type": "unresolved_carrier_fault",
            "title": "Carrier-at-fault shipments still not picked up",
            "severity": "medium",
            "items": stuck_orders,
        })

    # 5. High-severity security-flavoured ticket surfaced regardless of SLA math
    security_tickets = [t for t in tickets if any(k in t["description"].lower() for k in ["api key", "credential", "security", "exposure"])]
    if security_tickets:
        signals.append({
            "type": "security_flag",
            "title": "Tickets mentioning potential security/credential exposure",
            "severity": "high",
            "items": [{"ticket_id": t["ticket_id"], "account_id": t["account_id"],
                       "account_name": ACCOUNTS[t["account_id"]]["account_name"],
                       "subject": t["subject"]} for t in security_tickets],
        })

    severity_order = {"high": 0, "medium": 1, "low": 2}
    signals.sort(key=lambda s: severity_order.get(s["severity"], 3))
    return {"generated_at": SNAPSHOT_TIME.isoformat(), "signals": signals}
