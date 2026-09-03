"""lifeops_plugin.py — LifeOps AI Action Planner Executa (Anna App).

This plugin:
  - Speaks JSON-RPC 2.0 over stdio (Executa v2 protocol).
  - Negotiates v2 capability handshake (initialize → capabilities.sampling).
  - Declares host_capabilities: ["llm.sample"] in describe().
  - Implements the `plan` tool: accepts a messy situation description,
    calls Anna's LLM via reverse sampling/createMessage, and returns a
    structured JSON action plan.
  - Implements the `ping` smoke-test method.

Threading model:
  - A single reader thread reads stdin line by line.
  - Each line is either:
      (a) an Agent-initiated request (has a "method" field) → put on agent_q
      (b) a host response to our reverse RPC (has "result"/"error", no "method")
          → delivered to the matching pending Future keyed by "id"
  - Tool invocations run in a ThreadPoolExecutor so multiple calls can
    overlap (relevant when the host relays sampling calls asynchronously).
  - All stdout writes are serialised through a threading.Lock.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# The host speaks UTF-8 over stdio, but a child process on Windows inherits the
# console codepage (cp1252 here).  Encoding an LLM reply containing any
# character outside that codepage -- U+2011, arrows, emoji -- then raises
# UnicodeEncodeError inside the worker thread; the reply frame is never written
# and the host waits out the entire job deadline for a frame that never comes.
# Pin both streams to UTF-8 so the transport matches the protocol.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n")
except (AttributeError, ValueError):  # not a reconfigurable TextIOWrapper
    pass

# from dotenv import load_dotenv
# from openai import OpenAI

import openai

openai.api_key = "sk-12X8LZjp1HxAuvezfeLliyVaHdZSjh3mQawOOrLUN7tsmR4i"
openai.base_url = "https://kiosapi.com/v1/"

# One attempt only.  The per-call `timeout` below is per *attempt*, so the SDK's
# default max_retries=2 would turn a 100 s timeout into a ~300 s worst case and
# blow the UI's wall clock all over again.
openai.max_retries = 0

def _env_base_dir() -> str:
    """Directory to resolve the project ``.env`` against.

    From source this is the repo root, three levels up from this module.  In a
    PyInstaller one-file binary ``__file__`` lives in the temporary _MEIPASS
    extraction directory, so walking up from it finds nothing -- resolve
    against the executable's own directory instead.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Load the .env file from the root of the project
# load_dotenv(os.path.join(_env_base_dir(), ".env"))
# client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), base_url=os.environ.get("OPENAI_BASE_URL"))



# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TOOL_ID = "tool-dev-lifeops"
VERSION = "0.3.0"
PROTOCOL_VERSION_V2 = "2.0"

# Upper bound on a single LLM call.  Must stay the *smallest* timer in the
# chain so that when the provider is slow this plugin fails first and its own
# readable message reaches the UI, rather than the SDK's opaque wall-clock
# error.  The ladder, set in bundle/app.js:
#
#     plugin LLM timeout (100 s) < SDK wall clock (150 s) < job deadline (180 s)
#
# This is a true ceiling only because openai.max_retries is pinned to 0 above.
SAMPLING_TIMEOUT_SECONDS = 100.0

# NOTE: We intentionally use responseFormat={type:"json_object"} rather than
# the strict json_schema variant.  The strict schema mode forces expensive
# constrained-decoding on the LLM which adds 40-80 s of latency through
# the Anna sampling proxy and reliably hits the dev-harness 65 s invocation
# timeout.  The system-prompt already gives the model the full JSON
# structure, so json_object mode produces correct output at normal speed.
# The _plan_from_markdown fallback handles the rare model that ignores it.

# Plugin describe() manifest
MANIFEST = {
    "name": TOOL_ID,
    "version": VERSION,
    "display_name": "LifeOps Planner",
    "description": "Turns a messy real-world situation into a structured, prioritised action plan using the host LLM.",
    "author": "LifeOps",
    # v2 reverse-RPC capabilities this plugin will use
    "host_capabilities": ["llm.sample"],
    "tools": [
        {
            "name": "plan",
            "description": (
                "Analyse a messy real-world situation and return a structured "
                "action plan with prioritised tasks, next action, resources, "
                "and risks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "situation": {
                        "type": "string",
                        "description": "The messy situation the user wants help with.",
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Situation category: personal, work, finance, "
                            "health, relationships, or other."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context or constraints.",
                    },
                },
                "required": ["situation", "category"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ping",
            "description": "Smoke-test method — returns pong.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# I/O helpers
# ─────────────────────────────────────────────────────────────────────────────

_stdout_lock = threading.Lock()


def _write(msg: dict) -> None:
    """Write one JSON-RPC frame to stdout. Thread-safe."""
    payload = json.dumps(msg, ensure_ascii=False)
    with _stdout_lock:
        try:
            sys.stdout.write(payload + "\n")
        except UnicodeEncodeError:
            # Defence in depth behind the UTF-8 reconfigure above: if the stream
            # still cannot represent the payload, escape it.  \uXXXX output is
            # valid JSON and stays on one line, so the frame is never lost.
            sys.stdout.write(json.dumps(msg, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def _ok(req_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# ─────────────────────────────────────────────────────────────────────────────
# Reverse-RPC dispatcher (shared stdin reader)
# ─────────────────────────────────────────────────────────────────────────────

# Incoming agent requests land here.
agent_q: queue.Queue = queue.Queue()

# Pending reverse-RPC calls: request_id → queue.Queue[dict]
_pending: dict[str, queue.Queue] = {}
_pending_lock = threading.Lock()


def _reader() -> None:
    """Long-running thread: reads stdin and dispatches frames."""
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue  # ignore malformed lines

        if "method" in frame:
            # Agent → Plugin (request or notification)
            agent_q.put(frame)
        else:
            # Plugin → Host response (answer to our reverse RPC)
            rid = frame.get("id")
            if rid is not None:
                with _pending_lock:
                    q = _pending.pop(rid, None)
                if q is not None:
                    q.put(frame)


def _call_host(method: str, params: dict, timeout: float = 90.0) -> dict:
    """Issue a reverse JSON-RPC call to the host and block until the response."""
    rid = str(uuid.uuid4())
    response_q: queue.Queue = queue.Queue()
    with _pending_lock:
        _pending[rid] = response_q
    _write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

    try:
        resp = response_q.get(timeout=timeout)
    except queue.Empty:
        # A late host response must not leave a stale waiter in the registry.
        with _pending_lock:
            _pending.pop(rid, None)
        raise RuntimeError(
            f"Anna LLM sampling did not respond within {timeout:.0f}s. Please try again."
        ) from None

    if "error" in resp:
        raise RuntimeError(
            f"Host error {resp['error'].get('code')}: {resp['error'].get('message')}"
        )
    return resp["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Sampling
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are LifeOps, an expert life-operations coach and strategic planner.

Your job is to take a messy real-world situation and transform it into a clear, structured, actionable plan.

Guidelines:
- Be practical and concrete — no vague advice.
- Prioritise ruthlessly: what MUST happen first?
- Identify the single most important next action the person can do right now.
- Surface hidden risks or blockers the person might not have considered.
- Adapt tone to the category (work = professional, personal = warm, health = careful, finance = precise).
- Keep task titles short and action-oriented (start with a verb).

CRITICAL INSTRUCTION:
You MUST return ONLY valid JSON.
Do NOT include markdown formatting, backticks, or introductory prose.
The response must start with "{" and end with "}".

Use EXACTLY these keys and no others.  The UI reads these names literally, so a
renamed or omitted field renders as blank:

{
  "summary": "2-3 sentence plain-language summary of the situation",
  "urgency": "high" | "medium" | "low",
  "tasks": [
    {
      "id": 1,
      "title": "Short verb-first action",
      "why": "One sentence on why this matters",
      "by_when": "Human-readable timeframe, e.g. Today / This week",
      "priority": "critical" | "high" | "medium" | "low"
    }
  ],
  "next_action": "The single most important thing to do right now",
  "resources_needed": ["plain strings, not objects"],
  "risks": ["plain strings, not objects"]
}

Rules:
- "tasks" must have 3-6 entries, "id" numbered from 1 in priority order.
- "urgency" and "priority" must be one of the listed values, lowercase.
- "resources_needed" and "risks" are arrays of plain strings — never objects.
- Use only the keys above; do not add "category", "description", "due",
  "dependencies", "mitigation", or any other field.
"""


def _do_sample(invoke_id: str, situation: str, category: str, context: str = "") -> dict:
    """Call sampling/createMessage and return parsed plan dict."""
    user_content = f"Category: {category}\n\nSituation:\n{situation}"
    if context:
        user_content += f"\n\nAdditional context:\n{context}"

    try:
        response = openai.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            max_tokens=1200,
            temperature=0.2,
            response_format={"type": "json_object"},
            timeout=SAMPLING_TIMEOUT_SECONDS,
        )
        text = response.choices[0].message.content or ""
    except Exception as exc:
        raise RuntimeError(f"OpenAI API call failed: {exc}") from exc

    # Robustly extract JSON block in case the LLM ignored the formatting instructions
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Some user-selected Anna models honour the planning prompt but ignore
        # JSON mode and return a conventional Markdown plan.  Preserve that
        # useful reverse-sampling result instead of turning it into an error.
        markdown_plan = _plan_from_markdown(text)
        if markdown_plan is not None:
            return markdown_plan
        raise RuntimeError(f"LLM returned non-JSON output. Please try again.\n\nRaw: {text[:400]}") from exc


def _plan_from_markdown(text: str) -> dict | None:
    """Adapt a conventional Markdown action plan to the tool's JSON contract.

    JSON remains the normal response format.  This narrow fallback exists for
    hosts whose selected model downgrades structured output despite the
    ``onUnsupported: json_object`` request.
    """
    lines = [line.strip() for line in text.splitlines()]
    headings = [
        (index, re.sub(r"^(?:step\s*\d+\s*[:.-]?\s*)", "", match.group(1), flags=re.I).strip())
        for index, line in enumerate(lines)
        if (match := re.match(r"^#{2,6}\s+(.+)$", line))
    ]
    task_headings = [
        (index, title)
        for index, title in headings
        if title and not re.search(r"\b(resources?|risks?|blockers?|next action)\b", title, re.I)
    ]
    if not task_headings:
        return None

    first_heading_index = headings[0][0] if headings else len(lines)
    summary_lines = [line for line in lines[:first_heading_index] if line and not line.startswith("#")]
    summary = " ".join(summary_lines).strip() or "A structured action plan was generated."
    urgency = "high" if re.search(r"\b(asap|urgent|deadline|today|tomorrow|overdue)\b", text, re.I) else "medium"
    tasks = []
    for task_id, (start, title) in enumerate(task_headings, start=1):
        next_start = next((index for index, _ in task_headings if index > start), len(lines))
        detail = " ".join(
            line.lstrip("-* ").strip()
            for line in lines[start + 1 : next_start]
            if line and not line.startswith("#")
        )
        timeframe_match = re.search(r"\b(today|tomorrow|tonight|this week|this evening|asap)\b", detail, re.I)
        tasks.append(
            {
                "id": task_id,
                "title": title,
                "why": detail or "This is a concrete step in the generated plan.",
                "by_when": timeframe_match.group(0).title() if timeframe_match else "This week",
                "priority": "critical" if task_id == 1 and urgency == "high" else ("high" if task_id == 1 else "medium"),
            }
        )

    return {
        "summary": summary,
        "urgency": urgency,
        "tasks": tasks,
        "next_action": tasks[0]["title"],
        "resources_needed": [],
        "risks": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_plan(args: dict, context: dict) -> dict:
    situation = (args.get("situation") or "").strip()
    category = (args.get("category") or "other").strip()
    extra = (args.get("context") or "").strip()

    if not situation:
        return {"success": False, "error": "situation cannot be empty"}

    invoke_id = context.get("invoke_id", "dev")
    try:
        plan = _do_sample(invoke_id, situation, category, extra)
        return {"success": True, "data": {"plan": plan}}
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}


def _handle_ping(_args: dict, _context: dict) -> dict:
    return {"success": True, "data": {"pong": True, "version": VERSION}}


_TOOL_HANDLERS = {
    "plan": _handle_plan,
    "ping": _handle_ping,
}


# ─────────────────────────────────────────────────────────────────────────────
# Request handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_initialize(req: dict) -> None:
    # v2 handshake — advertise sampling capability
    _ok(req.get("id"), {
        "protocolVersion": PROTOCOL_VERSION_V2,
        "serverInfo": {"name": TOOL_ID, "version": VERSION},
        "capabilities": {"sampling": {}},
    })


def _handle_describe(req: dict) -> None:
    _ok(req.get("id"), MANIFEST)


def _handle_health(req: dict) -> None:
    _ok(req.get("id"), {"status": "ready", "message": "", "details": {}})


def _handle_invoke(req: dict) -> None:
    params = req.get("params", {})
    tool_name = params.get("tool")
    args = params.get("arguments", {})
    ctx = params.get("context") or {}

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        result = {"success": False, "error": f"unknown tool: {tool_name}"}
    else:
        try:
            result = handler(args, ctx)
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": str(exc)}

    try:
        _ok(req.get("id"), result)
    except Exception as exc:  # noqa: BLE001
        # This runs on a pool thread, where a raised exception is swallowed into
        # the Future.  Every invoke must be answered with *something* or the host
        # blocks until the job deadline, so degrade to an error frame.
        _err(req.get("id"), -32603, f"failed to write tool result: {exc}")


def _handle_shutdown(_req: dict) -> None:
    # Graceful shutdown — stop reading
    sys.exit(0)


_REQUEST_HANDLERS = {
    "initialize": _handle_initialize,
    "describe": _handle_describe,
    "health": _handle_health,
    "invoke": _handle_invoke,
    "shutdown": _handle_shutdown,
}


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Start the reader thread FIRST so we can receive both agent requests
    # and host responses to our reverse RPCs on the same stdin channel.
    reader_thread = threading.Thread(target=_reader, daemon=True, name="stdin-reader")
    reader_thread.start()

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="invoke") as pool:
        while True:
            try:
                req = agent_q.get(timeout=1.0)
            except queue.Empty:
                if not reader_thread.is_alive():
                    break
                continue

            method = req.get("method")
            handler = _REQUEST_HANDLERS.get(method)

            if handler is None:
                _err(req.get("id"), -32601, f"Method not found: {method}")
                continue

            if method == "invoke":
                # Run invocations in the thread pool so sampling responses
                # (which arrive on the reader thread) can be processed
                # concurrently while the invoke is still in-flight.
                pool.submit(_handle_invoke, req)
            else:
                handler(req)


if __name__ == "__main__":
    main()
