const API = "";

const state = {
  mode: "customer",
  account_id: null,
  staff_name: "Rohit",
  staff_role: "agent",
  rawMessages: [],   // Gemini-format history, source of truth sent to backend
  busy: false,
  tab: "chat",
};

const TOOL_LABELS = {
  search_documents: "DOC SEARCH",
  lookup_account: "ACCOUNT LOOKUP",
  lookup_orders: "ORDER LOOKUP",
  lookup_tickets: "TICKET LOOKUP",
  calculate: "CALCULATE",
};

const el = (id) => document.getElementById(id);

function currentSession() {
  return {
    mode: state.mode,
    account_id: state.mode === "customer" ? state.account_id : null,
    staff_name: state.mode === "internal" ? state.staff_name : null,
    staff_role: state.mode === "internal" ? state.staff_role : null,
  };
}

function renderSessionSummary() {
  const box = el("sessionSummary");
  if (state.mode === "customer") {
    const acct = ACCOUNTS.find((a) => a.account_id === state.account_id);
    box.innerHTML = acct
      ? `Signed in as <b>${acct.account_name}</b><br/>Plan: ${acct.plan} · <span style="font-family:var(--mono)">${acct.account_id}</span>`
      : "Select an account";
  } else {
    box.innerHTML = `Signed in as <b>${state.staff_name || "(unnamed)"}</b><br/>Role: ${state.staff_role} · internal access`;
  }
}

let ACCOUNTS = [];

async function loadAccounts() {
  const res = await fetch(`${API}/api/accounts`);
  const data = await res.json();
  ACCOUNTS = data.accounts;
  const sel = el("accountSelect");
  sel.innerHTML = ACCOUNTS.map(
    (a) => `<option value="${a.account_id}">${a.account_name} (${a.plan})</option>`
  ).join("");
  state.account_id = ACCOUNTS[0]?.account_id || null;
  renderSessionSummary();
}

function setMode(mode) {
  state.mode = mode;
  el("modeCustomerBtn").classList.toggle("active", mode === "customer");
  el("modeInternalBtn").classList.toggle("active", mode === "internal");
  el("customerFields").style.display = mode === "customer" ? "block" : "none";
  el("internalFields").style.display = mode === "internal" ? "block" : "none";
  el("tabDashboard").style.display = mode === "internal" ? "block" : "none";
  el("tabActionLog").style.display = mode === "internal" ? "block" : "none";
  if (mode === "customer" && state.tab !== "chat") setTab("chat");
  renderSessionSummary();
  resetConversation(true);
}

function setTab(tab) {
  state.tab = tab;
  el("tabChat").classList.toggle("active", tab === "chat");
  el("tabDashboard").classList.toggle("active", tab === "dashboard");
  el("tabActionLog").classList.toggle("active", tab === "actionlog");
  el("chatPane").style.display = tab === "chat" ? "flex" : "none";
  el("dashboardPane").style.display = tab === "dashboard" ? "block" : "none";
  el("actionLogPane").style.display = tab === "actionlog" ? "block" : "none";
  if (tab === "dashboard") loadDashboard();
  if (tab === "actionlog") loadActionLog();
}

function resetConversation(silent) {
  state.rawMessages = [];
  el("messages").innerHTML = "";
  if (!silent) addSystemNote("Conversation reset.");
  addSystemNote(
    state.mode === "customer"
      ? "You're chatting as a signed-in ParcelPilot customer. Try: “Can I cancel ORD-1001 without a fee?”"
      : "You're chatting as internal ParcelPilot staff. Try: “Is Northstar’s TKT-501 breaching SLA?”"
  );
}

function addSystemNote(text) {
  const wrap = document.createElement("div");
  wrap.className = "msg system";
  wrap.innerHTML = `<div class="bubble">${text}</div>`;
  el("messages").appendChild(wrap);
  scrollToBottom();
}

function scrollToBottom() {
  const m = el("messages");
  m.scrollTop = m.scrollHeight;
}

function extractText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("\n");
}

function renderUserTurn(text) {
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  wrap.innerHTML = `<div class="bubble"></div>`;
  wrap.querySelector(".bubble").textContent = text;
  el("messages").appendChild(wrap);
  scrollToBottom();
}

function renderToolTrace(trace) {
  if (!trace || !trace.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "tool-trail";
  trace.forEach((t, i) => {
    const isError = t.output && t.output.error;
    const chip = document.createElement("div");
    chip.className = "tool-chip" + (isError ? " error" : "");
    const argSummary = Object.entries(t.input || {})
      .map(([k, v]) => `${k}=${v}`)
      .join(" · ");
    chip.innerHTML = `<span class="stamp">${TOOL_LABELS[t.tool] || t.tool}</span><span class="args">${argSummary}</span>`;
    const detail = document.createElement("div");
    detail.className = "tool-detail";
    detail.style.display = "none";
    detail.textContent = JSON.stringify(t.output, null, 2);
    chip.addEventListener("click", () => {
      detail.style.display = detail.style.display === "none" ? "block" : "none";
    });
    wrap.appendChild(chip);
    wrap.appendChild(detail);
  });
  return wrap;
}

function actionLabel(actionType) {
  return {
    create_escalation: "Create escalation",
    update_ticket: "Update ticket",
    create_followup_task: "Create follow-up task",
  }[actionType] || actionType;
}

function renderPendingAction(pending) {
  const card = document.createElement("div");
  card.className = "action-card";
  const input = pending.input;
  const metaParts = [];
  if (input.account_id) metaParts.push(`account: ${input.account_id}`);
  if (input.order_id) metaParts.push(`order: ${input.order_id}`);
  if (input.ticket_id) metaParts.push(`ticket: ${input.ticket_id}`);
  if (input.severity) metaParts.push(`severity: ${input.severity}`);
  if (input.amount_inr) metaParts.push(`amount: INR ${input.amount_inr}`);

  card.innerHTML = `
    <div class="label">Proposed action · requires your confirmation</div>
    <div class="summary"><b>${actionLabel(input.action_type)}</b> — ${input.summary}</div>
    <div class="meta">${metaParts.join(" · ")}</div>
    <div class="action-buttons">
      <button class="btn btn-confirm">Confirm</button>
      <button class="btn btn-decline">Decline</button>
    </div>
  `;
  const [confirmBtn, declineBtn] = card.querySelectorAll("button");
  confirmBtn.addEventListener("click", () => resolveAction(pending, true, card));
  declineBtn.addEventListener("click", () => resolveAction(pending, false, card));
  return card;
}

function renderAssistantTurn({ text, trace, pending_action }) {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const traceEl = renderToolTrace(trace);
  if (traceEl) wrap.appendChild(traceEl);

  if (text) {
    bubble.textContent = text;
    wrap.appendChild(bubble);
  }

  if (pending_action) {
    wrap.appendChild(renderPendingAction(pending_action));
  }

  el("messages").appendChild(wrap);
  scrollToBottom();
  return wrap;
}

async function resolveAction(pending, approved, cardEl) {
  cardEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  cardEl.querySelector(".label").textContent = approved
    ? "Confirmed — executing…"
    : "Declined";

  const res = await fetch(`${API}/api/confirm_action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session: currentSession(),
      messages: state.rawMessages,
      tool_use_id: pending.tool_use_id,
      pre_executed_tool_results: pending.pre_executed_tool_results,
      action_input: pending.input,
      approved,
    }),
  });
  const data = await res.json();
  state.rawMessages = data.messages;

  if (data.action_result) {
    cardEl.querySelector(".label").textContent = approved
      ? data.action_result.error
        ? "Could not complete"
        : `Confirmed — ${data.action_result.record ? data.action_result.record.action_id : "done"}`
      : "Declined by user";
    if (data.action_result.error) {
      const err = document.createElement("div");
      err.className = "meta";
      err.style.color = "var(--danger)";
      err.textContent = data.action_result.error;
      cardEl.appendChild(err);
    }
  }

  const lastAssistant = [...data.messages].reverse().find((m) => m.role === "assistant");
  const text = lastAssistant ? extractText(lastAssistant.content) : "";
  if (text || (data.trace && data.trace.length) || data.pending_action) {
    renderAssistantTurn({ text, trace: data.trace, pending_action: data.pending_action });
  }
}

async function sendMessage() {
  const input = el("chatInput");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  renderUserTurn(text);
  state.rawMessages.push({ role: "user", content: text });

  state.busy = true;
  el("sendBtn").disabled = true;
  el("typingIndicator").style.display = "block";

  try {
    const res = await fetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: currentSession(), messages: state.rawMessages }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      addSystemNote(`Error: ${err.detail || res.statusText}`);
      return;
    }
    const data = await res.json();
    state.rawMessages = data.messages;
    const lastAssistant = [...data.messages].reverse().find((m) => m.role === "assistant");
    const responseText = lastAssistant ? extractText(lastAssistant.content) : "";
    renderAssistantTurn({ text: responseText, trace: data.trace, pending_action: data.pending_action });
  } catch (e) {
    addSystemNote(`Network error: ${e.message}`);
  } finally {
    state.busy = false;
    el("sendBtn").disabled = false;
    el("typingIndicator").style.display = "none";
  }
}

function severityBadge(sev) {
  return `<span class="badge ${sev}">${sev}</span>`;
}

async function loadDashboard() {
  const pane = el("dashboardPane");
  pane.innerHTML = `<div class="empty-state">Loading signals…</div>`;
  const res = await fetch(`${API}/api/dashboard?mode=internal`);
  const data = await res.json();
  if (!data.signals.length) {
    pane.innerHTML = `<h2>Ops dashboard</h2><div class="empty-state">No active signals as of the dataset snapshot.</div>`;
    return;
  }
  pane.innerHTML =
    `<h2>Ops dashboard</h2><div class="empty-state" style="margin-bottom:16px;">Snapshot: ${data.generated_at}</div>` +
    data.signals
      .map((s) => {
        const items = s.items
          .map((it) => {
            const parts = Object.entries(it)
              .map(([k, v]) => `${k}: <b>${Array.isArray(v) ? v.join(", ") : v}</b>`)
              .join(" &nbsp;·&nbsp; ");
            return `<div class="sig-item">${parts}</div>`;
          })
          .join("");
        return `
        <div class="signal-card ${s.severity}">
          <div class="sig-title">${severityBadge(s.severity)} ${s.title}</div>
          ${items}
        </div>`;
      })
      .join("");
}

async function loadActionLog() {
  const pane = el("actionLogPane");
  pane.innerHTML = `<div class="empty-state">Loading…</div>`;
  const res = await fetch(`${API}/api/action_log?mode=internal`);
  const data = await res.json();
  if (!data.actions.length) {
    pane.innerHTML = `<h2>Action log</h2><div class="empty-state">No actions created yet in this session.</div>`;
    return;
  }
  pane.innerHTML =
    `<h2>Action log</h2>` +
    data.actions
      .map(
        (a) => `<div class="signal-card medium"><div class="sig-title">${a.action_id} · ${actionLabel(a.action_type)}</div>
      <div class="sig-item">${Object.entries(a)
        .filter(([k]) => k !== "action_id" && k !== "action_type")
        .map(([k, v]) => `${k}: <b>${v}</b>`)
        .join(" &nbsp;·&nbsp; ")}</div></div>`
      )
      .join("");
}

// --- wiring ---
el("modeCustomerBtn").addEventListener("click", () => setMode("customer"));
el("modeInternalBtn").addEventListener("click", () => setMode("internal"));
el("accountSelect").addEventListener("change", (e) => {
  state.account_id = e.target.value;
  renderSessionSummary();
  resetConversation(true);
});
el("staffNameInput").addEventListener("input", (e) => {
  state.staff_name = e.target.value;
  renderSessionSummary();
});
el("staffRoleSelect").addEventListener("change", (e) => {
  state.staff_role = e.target.value;
  renderSessionSummary();
  resetConversation(true);
});
el("tabChat").addEventListener("click", () => setTab("chat"));
el("tabDashboard").addEventListener("click", () => setTab("dashboard"));
el("tabActionLog").addEventListener("click", () => setTab("actionlog"));
el("resetBtn").addEventListener("click", () => resetConversation());
el("sendBtn").addEventListener("click", sendMessage);
el("chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendMessage();
});

loadAccounts().then(() => resetConversation(true));
