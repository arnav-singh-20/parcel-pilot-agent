# Product Note

## Additional client problem chosen: Proactive Issue Detection

I built the **Ops dashboard** (internal mode → "Ops dashboard" tab,
`backend/app/insights.py`) over Problem 2 (Trust & Reliability), because trust and
reliability are already the backbone of the required chatbot itself - source
precedence, deprecation flags, and "don't guess" behavior are core to every answer,
not an add-on. Proactive detection, on the other hand, is a genuinely new
capability: today ParcelPilot's ops team only finds a pattern once someone happens
to ask about it.

The dashboard currently surfaces five rule-based signal types over the open
tickets/orders: **SLA risk** (age vs. inferred severity's target, using the same
per-account override logic as the chatbot), **recurring product issues** (matched
against the known-issues doc, e.g. KI-211's webhook delay explaining a "still shows
BOOKED" ticket), **multi-customer impact** (a recurring theme hitting more than one
account), **unresolved carrier-fault delays**, and a **security-flag** pass that
surfaces anything mentioning credential/API-key exposure regardless of SLA math,
because that category of issue shouldn't wait on a severity heuristic.

These are deliberately rule-based and explainable rather than LLM-summarized: for
an internal triage view feeding real decisions, "why is this flagged" needs to be
inspectable at a glance, and rules are cheap and testable at the ticket volumes a
20-person team actually generates in a day.

## What I'd build next, in priority order

1. **Real severity + plan/account fields on ticket creation**, replacing the
   keyword-based `_infer_severity` heuristic in `insights.py`. This is the single
   biggest fidelity gap in the current build - the dashboard's SLA-risk signal is
   only as good as its severity guess. High priority because every other signal
   downstream (SLA math, escalation defaults) inherits this.
2. **A real vector/hybrid search index once the document pack grows beyond a
   handful of files.** Lexical search is fine for six documents; it will not scale
   to a real ParcelPilot's actual documentation and hundreds of customer contracts.
   I'd add embeddings + a reranker, keep the status/effective-date metadata
   filtering exactly as-is (that logic doesn't change), and add a "this agreement
   has expired" check using `term_end`, which the current dataset doesn't need
   (all agreements are ACTIVE) but a real one will.
3. **Real authentication** behind `SessionContext`, replacing the mock dropdown -
   this is a hard prerequisite for a customer-facing launch, not a nice-to-have.
4. **Ticket-resolution feedback loop**: when the model gives an answer that
   contradicts a `historical_resolution`, log that explicitly so ops can see where
   past guidance was wrong and correct the underlying ticket record - right now the
   model is told to distrust historical resolutions, but nothing closes the loop to
   fix the bad historical data itself.
5. **Monthly aggregate credit tracking** for accounts like Northstar's INR 5,000
   cap - `calculate_service_credit` currently notes this as unverified rather than
   checking it, because the dataset has no running total to check against.
6. **Streaming responses** in the UI (currently a single blocking response per
   turn) - not correctness-critical, but noticeably better UX once real model
   latency is in play instead of the mock stub.

## What I intentionally left out

- **Real authentication/authorization** (see above) - mocked per the assessment's
  explicit allowance.
- **A real database** - JSON snapshot only; fine for a fixed dataset, not for a
  live system with writes.
- **Semantic/embedding search** - lexical search is adequate and more auditable at
  this document-pack size; would revisit at scale (see roadmap above).
- **Multi-language support, voice/channel integrations (email/chat widget), and
  a full ticketing-system integration** for the action tool - `create_action` is
  mocked in-memory rather than wired to a real ticketing API, since none was
  supplied and building a fake one wouldn't demonstrate anything beyond what the
  mock already does.
- **Monthly credit-cap enforcement** (see roadmap #5) and **business-hours-aware
  SLA countdowns** (the SOP's "business hours/days" targets are currently compared
  against raw elapsed hours, not a business-calendar - a reasonable simplification
  given the dataset's short timeframe, but wrong for a real deployment).

## One metric I'd use to judge usefulness

**Percentage of chatbot answers that required no human correction or escalation
reversal**, tracked separately for customer-facing and internal use. Concretely:
of all conversations where the bot gave a direct (non-escalated) answer, what
fraction were later confirmed correct by a human reviewer or by the customer not
re-raising the same question? This is the metric that most directly tracks the
thing the whole design optimizes for - not "did it answer," but "was the answer
actually trustworthy" - and it would catch regressions in the precedence logic
(e.g. a future document update silently breaking an override rule) faster than a
generic satisfaction score would.
