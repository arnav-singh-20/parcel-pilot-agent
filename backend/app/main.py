import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from . import agent, tools, store, insights

app = FastAPI(title="ParcelPilot Support Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionModel(BaseModel):
    mode: str                      # "customer" | "internal"
    account_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None  # "agent" | "manager"


class ChatRequest(BaseModel):
    session: SessionModel
    messages: list


class ConfirmRequest(BaseModel):
    session: SessionModel
    messages: list
    tool_use_id: str
    pre_executed_tool_results: list
    action_input: dict
    approved: bool


def _ctx_from_session(s: SessionModel) -> tools.SessionContext:
    if s.mode == "customer" and not s.account_id:
        raise HTTPException(400, "customer session requires account_id")
    if s.mode == "internal" and not s.staff_name:
        raise HTTPException(400, "internal session requires staff_name")
    return tools.SessionContext(
        mode=s.mode, account_id=s.account_id,
        staff_name=s.staff_name, staff_role=s.staff_role or "agent",
    )


@app.get("/api/accounts")
def list_accounts():
    """Used by the frontend to populate the mock-login account picker."""
    return {"accounts": [
        {"account_id": a["account_id"], "account_name": a["account_name"], "plan": a["plan"]}
        for a in store.STRUCTURED["accounts"]
    ]}


@app.post("/api/chat")
def chat(req: ChatRequest):
    ctx = _ctx_from_session(req.session)
    try:
        result = agent.run_turn(req.messages, ctx)
    except Exception as e:
        raise HTTPException(500, f"Agent error: {e}")
    return result


@app.post("/api/confirm_action")
def confirm_action(req: ConfirmRequest):
    ctx = _ctx_from_session(req.session)
    if req.approved:
        try:
            action_result = tools.tool_create_action(ctx, **req.action_input)
        except tools.AccessDenied as e:
            action_result = {"error": str(e)}
    else:
        action_result = {"status": "declined", "note": "The user declined to confirm this action. It was not created."}

    result = agent.resume_after_action(
        req.messages, ctx, req.tool_use_id, req.pre_executed_tool_results, action_result,
    )
    result["action_result"] = action_result
    return result


@app.get("/api/action_log")
def action_log(mode: str, staff_name: str | None = None):
    if mode != "internal":
        raise HTTPException(403, "Action log is internal-only.")
    return {"actions": tools.get_action_log()}


@app.get("/api/dashboard")
def dashboard(mode: str):
    if mode != "internal":
        raise HTTPException(403, "Dashboard is internal-only.")
    return insights.build_dashboard()


# --- static frontend -------------------------------------------------------
FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
