# Architecture Note

## Agent design

A single Gemini agent (`backend/app/agent.py`) serves both the customer-facing and
internal contexts. Rather than two separate agents, one agent has its **system prompt
and tool-layer permissions parameterized by session context** (`SessionContext`:
mode, account_id, staff_name/role). This avoids duplicating the source-precedence
reasoning in two places and matches how the underlying support knowledge really is
context-independent - what changes between a customer and a staff member is *what
they're allowed to see and do*, not *what the truth is*.

The loop (`run_turn`) is a standard tool-use loop against the Gemini API:
call the model → if it asks for read-only tools, execute them and feed results back →
repeat (capped at 6 iterations) → return once the model produces a final text answer,
**or pause immediately if the model calls the state-changing action tool**, so a human
can approve before anything is written.

One simplification: the prompt instructs the model to call tools one at a time. Gemini
can call multiple tools in parallel, and the code path handles that generically
(executes all read-only tool_use blocks it sees, holds any action tool_use), but the
UI's "click a tool chip to see what happened, in order" affordance reads best with one
tool per turn, and it makes the confirmation-pause logic much easier to reason about
correctly. Given the task sizes here, the loss of latency from serializing tool calls
is negligible.

## Tool design

Six tools across the three required categories:

| Category | Tools |
|---|---|
| Document search | `search_documents` |
| Structured lookup / calculation | `lookup_account`, `lookup_orders`, `lookup_tickets`, `calculate` |
| State-changing action | `create_action` (create_escalation / update_ticket / create_followup_task) |

`calculate` is deliberately a single tool with a `calc_type` enum
(`cancellation_fee` / `service_credit` / `sla_target`) rather than a bare "do math"
tool exposed to the model - the actual precedence logic (agreement overrides SOP,
carrier-fault checks, manager-approval thresholds) lives in
`backend/app/calculations.py` as plain, testable Python, not in the model's
reasoning. The model's job is to pick the right calculation and explain the result
in context; the arithmetic and precedence resolution are code, so they are
consistent and auditable no matter what the model does. This was tested directly
against every order in the dataset (see the worked cases below).

`create_action` is intentionally one tool with an `action_type` field instead of
three separate tools, since all three share the same shape (who/what/why) and the
same confirmation gate; splitting them would add tool-schema surface without adding
either safety or clarity.

## Document and structured-data handling

- **Documents** (`data/documents.json`): the six PDFs are pre-chunked into short,
  labeled sections with explicit metadata - `status` (CURRENT/DEPRECATED),
  `effective_date`, `superseded_by`/`supersedes`, and `account_id` for
  customer-specific agreements. `retrieval.py` does lexical (keyword + phrase-overlap)
  search over these chunks rather than embeddings: with six short documents,
  semantic search would add a dependency and non-determinism without a real
  recall benefit, and keyword search is trivial to unit test and to explain
  in a demo. A deprecated document is still returned if it's the best keyword
  match (never hidden), but every result surfaces its status, and the system
  prompt and a `warning` field on deprecated results both instruct the model
  never to treat it as current.
- **Structured data** (`data/structured.json`): accounts/orders/tickets, loaded
  once into memory (`store.py`). This is a JSON snapshot standing in for a real
  operational DB; the module boundary (`store.py` exposing `get_account`,
  `list_orders`, etc.) is what would actually change if this became a real
  Postgres-backed service - nothing above that layer would need to change.
- The dataset's snapshot time (`README` sheet, 2026-08-16 11:00 IST) is loaded as
  `SNAPSHOT_TIME` and used as "now" everywhere time math happens
  (`calculations.py`, `insights.py`), rather than the real wall clock - the whole
  scenario is a fixed point in time.

## Source reliability and conflict handling

This is treated as a first-class design constraint, not an afterthought:

1. **Precedence is enforced by code, not just prompted.** `calculations.py` checks
   for an account-specific override *before* falling back to the default SOP/policy
   for every calculation - e.g. `calculate_cancellation_fee` special-cases
   `ACCT-001` (Northstar: no fee, ever, pre-pickup) before applying the default
   30-minute/INR 250 rule, and `calculate_service_credit` special-cases `ACCT-002`
   (LumenWorks: fixed INR 300 at >4h) before applying the default
   2h/min(500, 10%) rule. Every result includes `source_precedence_applied` so the
   reasoning is inspectable, not just asserted.
2. **Deprecated documents are flagged, not hidden.** Hiding them would make the
   system look like it doesn't know they exist, which is worse for trust than
   surfacing them with a clear warning.
3. **Historical ticket resolutions are marked as context only.** Two closed
   tickets in the dataset contain resolutions that were wrong even at the time
   (TKT-450 told a Northstar customer a INR 250 fee applied - Northstar's
   agreement waives it entirely; TKT-451 told a LumenWorks customer the row
   limit was 3,000 - the actual documented limit is 5,000, with an
   *intermittent bug* around 3,000 rows). The system prompt explicitly forbids
   citing `historical_resolution` as policy justification, and the tool
   description on `lookup_tickets` repeats the same warning at the point where
   the model would see the data.
4. **Missing data blocks confident answers, by design.** `calculate_service_credit`
   returns `eligible: None` (not `False`) and a "do not promise a credit" message
   when carrier-fault is unrecorded, matching the SOP's own instruction not to
   guess.
5. **Approval thresholds are enforced in the tool, not just mentioned.**
   `tool_create_action` rejects (rather than just warns about) a >INR 1,000 credit
   action unless the session's `staff_role` is `manager`.

### Worked examples checked against the source pack

| Question | Result |
|---|---|
| Can Northstar cancel ORD-1001 without a fee? | Yes, no fee - agreement overrides the default 30-min/INR 250 rule regardless of elapsed time. |
| LumenWorks pickup 3h late, carrier at fault - service credit? | No - LumenWorks' agreement requires **>4h**, overriding the SOP's default 2h threshold. |
| Beacon (ACCT-003) cancels ORD-3001, requested 15 min after booking | No fee - within the default 30-minute window; no agreement to override it (Beacon has none on file). |
| ORD-2002 (LumenWorks, carrier fault, still not picked up, 4.5h late) | Eligible, fixed INR 300 (LumenWorks override applies, and it's still evaluated live against the snapshot time since pickup hasn't happened). |

## Access control (enforced in the tool layer)

`tools.py`'s `_check_account_access` is called on every read/lookup/calculate/action
call and compares the *requested* `account_id` against the session's actual
`account_id` (for customers). It raises `AccessDenied` rather than filtering silently,
so a denied request is visible in the tool trace, not swallowed. This runs
independent of the system prompt - even if a user convinced the model to *try* to
look up another account (prompt injection, social engineering, etc.), the function
call itself would fail before any data left the server. Internal sessions can view
any account (for legitimate investigation) but are still restricted on the action
side: customers can only ever request an escalation, never directly update tickets
or create internal follow-up tasks; credits above INR 1,000 require a manager role.

## Major trade-offs

- **Lexical search over embeddings** - right call at this document-pack size;
  would need to change at scale (see PRODUCT.md).
- **JSON files over a real database** - fine for a fixed assessment snapshot;
  the `store.py` boundary is where a real DB integration would slot in.
- **One agent, parameterized by session** over two separate agents - less
  duplication, at the cost of a slightly more complex prompt with two addenda.
- **Serialized tool calls** (one at a time) over exploiting Gemini's native
  parallel tool calling - simpler confirmation-pause logic and UI, at a small
  latency cost.
- **Stateless backend, client-held conversation history** - the frontend resends
  the full Gemini-format message list on every request; no server-side session
  store for chat state (only the mocked action log is server-held). This matches
  how the Gemini API itself is stateless, keeps the backend simple to
  reason about and horizontally scalable, at the cost of the client needing to
  carry (and trust) its own history faithfully.
- **Mocked auth** - account/role selection is a plain dropdown, not real
  authentication. Called out explicitly as out of scope; a real deployment would
  put a verified JWT/session behind `SessionContext` instead of a client-chosen
  value the server accepts as-is. The *access-control enforcement* pattern (checks
  live in the tool layer against a trusted context object) would not need to
  change - what would change is that `SessionContext` is entirely truthworthy input.
