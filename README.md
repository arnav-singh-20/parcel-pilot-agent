# ParcelPilot Support Agent

An AI support/ops chatbot for ParcelPilot, built for the CalQuity AI Engineer assessment.
Supports both a **customer-facing** mode (scoped to one account) and an **internal
staff** mode (cross-account investigation + an ops dashboard), backed by a single
tool-using agent.

## What's here

```
backend/
  app/
    main.py          FastAPI app (REST endpoints, static file serving)
    agent.py          Agent loop: calls Gemini with tools, pauses on state-changing actions
    tools.py           Tool schemas + dispatch + ACCESS CONTROL (enforced here, not in the prompt)
    calculations.py   Deterministic cancellation-fee / service-credit / SLA-target logic
    retrieval.py       Lexical document search over the policy/SOP/contract pack
    insights.py        Proactive issue-detection signals for the internal dashboard
    prompts.py         System prompt (source-precedence rules), scoped per session
    store.py           Loads the two JSON datasets below
  data/
    documents.json      Chunked policies/SOPs/product docs/customer agreements, with
                         status (CURRENT/DEPRECATED) and effective dates
    structured.json     Accounts, orders, tickets (from the supplied workbook)
  requirements.txt
  .env.example
frontend/
  index.html / app.js / styles.css   Plain HTML/JS chat UI (no build step)
README.md
ARCHITECTURE.md
PRODUCT.md
```

## Run it locally

Requires Python 3.11+.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
cp .env.example .env
# edit .env and set GEMINI_API_KEY=AIza...
uvicorn app.main:app --reload --port 8000
```

Open **https://parcel-pilot-agent-2.onrender.com/**.

Without a `GEMINI_API_KEY` set, the app still runs in a "mock mode" (canned
text responses, no tool calls) purely so the server/UI can be smoke-tested; real
agent reasoning and tool use requires a valid key.

## Using the app

- **Sidebar → Customer**: pick an account from the dropdown to simulate a logged-in
  customer. The chatbot can only ever see and act on that account's data - try asking
  about a different account's order or ticket and it will be refused, in the tool
  layer, regardless of what you ask the model to do.
- **Sidebar → Internal**: enter a staff name and role (agent/manager). Unlocks the
  **Ops dashboard** (proactive issue detection) and **Action log** tabs, and lets the
  agent look up any account and stage escalations/ticket updates/follow-up tasks.
- Any state-changing action the agent proposes appears as a card with **Confirm** /
  **Decline** buttons - nothing is written to the (mocked) action log until you click
  Confirm.
- Click a tool chip above an assistant reply to expand exactly what was queried and
  what came back.

## Suggested things to try

- *Customer, Northstar (ACCT-001):* "Can I cancel ORD-1001 without a fee? Explain why."
- *Customer, LumenWorks (ACCT-002):* "A pickup was 3 hours late and the carrier was at
  fault - do I get a credit?" (LumenWorks' contract needs >4h, so the answer is no -
  a good test of contract-overrides-default reasoning.)
- *Customer, LumenWorks:* try asking about ACCT-001's orders - should be refused.
- *Internal:* "Is TKT-501 breaching its SLA?" then "Escalate it" (P1 security-adjacent
  outage) - watch it require confirmation before the escalation is logged.
- *Internal:* open the **Ops dashboard** tab to see SLA-risk, recurring-issue, and
  security-flag signals computed over the current open tickets.

## AI tool usage

Built with Gemini (Google) as a pair-programming/code-generation assistant for
scaffolding the FastAPI backend, the agent loop, and the frontend; the domain rules
in `calculations.py` and the precedence design were worked through and verified
against the source documents by hand-testing each order/ticket in the dataset
(see the worked examples in ARCHITECTURE.md).
