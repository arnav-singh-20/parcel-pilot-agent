import os
import json
import uuid
from .tools import SessionContext, TOOL_SCHEMAS, READ_ONLY_TOOLS, ACTION_TOOLS, dispatch, AccessDenied
from .prompts import build_system_prompt

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TOOL_ITERATIONS = 6

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        _client = genai.Client(api_key=api_key)
    return _client


def _mock_llm_turn(messages, ctx: SessionContext):
    """Offline fallback so the app is runnable/demoable without an API key.
    Not real reasoning - just enough to exercise the tool pipeline end to end."""
    last_user = ""
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], str):
            last_user = m["content"]
            break
    text = (
        "[MOCK MODE - no GEMINI_API_KEY set, so responses are canned rather than reasoned.] "
        f"I received your message: \"{last_user[:200]}\". Set GEMINI_API_KEY in the backend .env "
        "to enable real agent reasoning and tool use."
    )
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


# ---------------------------------------------------------------------------
# Format adapters: the rest of the app (agent loop below, main.py, the
# frontend) all speak the Gemini-style content-block shape that tools.py's
# TOOL_SCHEMAS were originally written in. Rather than rewrite every layer,
# these adapters translate to/from Gemini's request/response shape only at
# the model-call boundary, so run_turn/resume_after_action and the stored
# message history don't need to know which model vendor is behind them.
# ---------------------------------------------------------------------------
_JSON_SCHEMA_TYPE_MAP = {
    "object": "OBJECT",
    "string": "STRING",
    "array": "ARRAY",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
}


def _convert_schema(schema: dict) -> dict:
    """Converts a JSON-Schema-style tool parameter spec into the upper-cased
    OpenAPI-style schema Gemini's function-calling expects."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            out["type"] = _JSON_SCHEMA_TYPE_MAP.get(value, value.upper())
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _convert_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out["items"] = _convert_schema(value)
        else:
            out[key] = value
    return out


def _to_gemini_tools():
    declarations = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": _convert_schema(tool["input_schema"]),
        }
        for tool in TOOL_SCHEMAS
    ]
    return [{"function_declarations": declarations}]


def _messages_to_gemini_contents(messages: list):
    """Converts the app's internal message list into Gemini `contents`,
    tracking tool_use id -> name so tool_result blocks (which only carry the
    id) can be turned into named functionResponse parts."""
    id_to_name = {}
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        content = m["content"]
        parts = []
        if isinstance(content, str):
            parts.append({"text": content})
        else:
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block["text"]})
                elif btype == "tool_use":
                    id_to_name[block["id"]] = block["name"]
                    parts.append({"function_call": {"name": block["name"], "args": block.get("input", {})}})
                elif btype == "tool_result":
                    name = id_to_name.get(block["tool_use_id"], "unknown_tool")
                    raw = block.get("content", "{}")
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else raw
                    except (TypeError, ValueError):
                        payload = {"result": raw}
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                    parts.append({"function_response": {"name": name, "response": payload}})
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _gemini_response_to_content_blocks(response):
    """Converts a Gemini response into the app's internal content-block shape."""
    blocks = []
    candidate = response.candidates[0] if response.candidates else None
    if not candidate or not candidate.content or not candidate.content.parts:
        return blocks
    for part in candidate.content.parts:
        if getattr(part, "text", None):
            blocks.append({"type": "text", "text": part.text})
        elif getattr(part, "function_call", None):
            fc = part.function_call
            blocks.append({
                "type": "tool_use",
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "name": fc.name,
                "input": dict(fc.args) if fc.args else {},
            })
    return blocks


def _stop_reason(response):
    candidate = response.candidates[0] if response.candidates else None
    finish_reason = getattr(candidate, "finish_reason", None) if candidate else None
    return str(finish_reason) if finish_reason else "end_turn"


def _call_model(messages, ctx: SessionContext):
    client = _get_client()
    if client is None:
        return _mock_llm_turn(messages, ctx)
    from google.genai import types
    resp = client.models.generate_content(
        model=MODEL,
        contents=_messages_to_gemini_contents(messages),
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(ctx),
            tools=_to_gemini_tools(),
            max_output_tokens=1500,
        ),
    )
    return {"content": _gemini_response_to_content_blocks(resp), "stop_reason": _stop_reason(resp)}


def run_turn(messages: list, ctx: SessionContext):
    """Runs the agent loop until it either produces a final text answer or
    stages a state-changing action that needs user confirmation.

    Returns a dict:
      {
        "messages": <updated full message list to persist client-side>,
        "trace": [ {tool, input, output}, ... ]   # for UI transparency
        "pending_action": {tool_use_id, input} | None,
        "done": bool
      }
    """
    trace = []
    working_messages = list(messages)

    for _ in range(MAX_TOOL_ITERATIONS):
        result = _call_model(working_messages, ctx)
        content = result["content"]
        working_messages.append({"role": "assistant", "content": content})

        tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
        if not tool_use_blocks:
            return {"messages": working_messages, "trace": trace, "pending_action": None, "done": True}

        action_block = next((b for b in tool_use_blocks if b["name"] in ACTION_TOOLS), None)
        readonly_blocks = [b for b in tool_use_blocks if b["name"] in READ_ONLY_TOOLS]

        tool_result_blocks = []
        for block in readonly_blocks:
            try:
                output = dispatch(ctx, block["name"], block["input"])
            except AccessDenied as e:
                output = {"error": str(e)}
            trace.append({"tool": block["name"], "input": block["input"], "output": output})
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(output),
            })

        if action_block:
            return {
                "messages": working_messages,
                "trace": trace,
                "pending_action": {
                    "tool_use_id": action_block["id"],
                    "input": action_block["input"],
                    "pre_executed_tool_results": tool_result_blocks,
                },
                "done": False,
            }

        working_messages.append({"role": "user", "content": tool_result_blocks})

    return {
        "messages": working_messages,
        "trace": trace,
        "pending_action": None,
        "done": True,
        "note": "Stopped after reaching the maximum tool-call depth for a single turn.",
    }


def resume_after_action(messages: list, ctx: SessionContext, tool_use_id: str,
                         pre_executed_tool_results: list, action_result: dict):
    """Called after the user confirms or declines a staged action."""
    tool_result_blocks = list(pre_executed_tool_results)
    tool_result_blocks.append({
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(action_result),
    })
    working_messages = list(messages)
    working_messages.append({"role": "user", "content": tool_result_blocks})
    return run_turn(working_messages, ctx)
