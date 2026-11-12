"""Fake LiteLLM transport for the tests.

It decides what to answer from the prompt and builds the response with
LiteLLM's own mocks (mock_response / mock_tool_calls), so Poda AI's real
LiteLLM handling (usage, cost, tool calls) is exercised without any network.

Verdict rules, from the candidate's path and symbol: "keep" is kept, "junit"
is unsure, "OldHelper" is `needs_context` in snippet mode (so it escalates to
the agent, which removes it), everything else is removed with high confidence.
FAKE_LLM_PROJECT: root of the analysed project (discovery reads a file from it).
FAKE_LLM_COUNTER: file where every call appends the task name.
FAKE_LLM_MODE=prose_first: the first snippet answer has no JSON (repair path).
"""
import json
import os
import re
from pathlib import Path

import litellm

_state = {"prose_done": False}


def _task(messages):
    first = next((m["content"] for m in messages if m.get("role") == "user"), "") or ""
    found = re.search(r"poda-task: ([\w-]+)", first)
    return (found.group(1) if found else "?"), first


def _candidates(prompt):
    block = re.search(r"## Candidates.*?```json\s*(\[.*?\])\s*```", prompt, re.S)
    return json.loads(block.group(1)) if block else []


def _verdicts(prompt, agent):
    verdicts = []
    for cand in _candidates(prompt):
        text = f"{cand.get('path', '')} {cand.get('symbol') or ''}".lower()
        if "keep" in text:
            decision = ("keep", "high", "se usa desde otro sistema")
        elif "junit" in text:
            decision = ("unsure", "low", "puede usarse en pruebas")
        elif "oldhelper" in text and not agent:
            decision = ("needs_context", "low", "hay que buscar referencias por reflexión")
        else:
            decision = ("remove", "high", "sin referencias tras buscar en todo el proyecto")
        verdicts.append({"id": cand["id"], "decision": decision[0], "confidence": decision[1],
                         "reason": decision[2], "evidence": [f"grep \"{cand.get('symbol') or cand['path']}\" → nada"]})
    return {"verdicts": verdicts}


def _discovery():
    rel = "src/com/acme/util/Helper.java"
    helper = Path(os.environ.get("FAKE_LLM_PROJECT", ".")) / rel
    if not helper.exists():
        return {"findings": []}
    lines = helper.read_text(encoding="utf-8").splitlines()
    line = next(i for i, text in enumerate(lines, 1) if "legacy()" in text)
    return {"findings": [
        {"category": "unused_member", "path": rel, "start_line": line, "end_line": line,
         "first_line_text": lines[line - 1].strip(), "last_line_text": lines[line - 1].strip(),
         "symbol": "legacy", "decision": "remove", "confidence": "high",
         "reason": "solo lo usaba código comentado ya eliminado", "evidence": ["grep \"legacy\" → solo comentarios"]},
        {"category": "leftover", "path": rel, "start_line": 1, "end_line": 1,
         "first_line_text": "texto que no coincide", "last_line_text": "texto que no coincide",
         "decision": "remove", "confidence": "high", "reason": "ubicación incorrecta a propósito", "evidence": []},
    ]}


def _context():
    return {"stack_summary": "Aplicación web Java con servlets y JSP.", "frameworks": ["Servlet API", "JSP"],
            "entry_points": [{"path": "WebContent/**/*.jsp", "kind": "page", "why": "accesibles por URL"}],
            "wiring_mechanisms": [{"mechanism": "web.xml", "where": "WebContent/WEB-INF/web.xml",
                                   "implication_for_dead_code": "servlets declarados por FQCN"}],
            "runtime_or_external": [],
            "never_remove": [{"pattern": "WebContent/protected/*", "why": "enlazado desde otro sistema"}],
            "notes": ""}


async def transport(**kwargs):
    messages = kwargs["messages"]
    tools = kwargs.get("tools")
    task, prompt = _task(messages)
    counter = os.environ.get("FAKE_LLM_COUNTER")
    if counter:
        with open(counter, "a", encoding="utf-8") as fh:
            fh.write(task + "\n")
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}], "_skip_mcp_handler": True}
    if tools and kwargs.get("tool_choice") != "none" and not any(m.get("role") == "tool" for m in messages):
        cands = _candidates(prompt)
        term = (cands[0].get("symbol") or Path(cands[0]["path"]).stem) if cands else "web.xml"
        call = {"id": "call_1", "type": "function",
                "function": {"name": "grep", "arguments": json.dumps({"pattern": term, "path": "."})}}
        return await litellm.acompletion(**base, tools=tools, mock_tool_calls=[call])
    if task == "verify-snippet" and os.environ.get("FAKE_LLM_MODE") == "prose_first" and not _state["prose_done"]:
        _state["prose_done"] = True
        return await litellm.acompletion(**base, mock_response="He revisado los candidatos y todo parece correcto.")
    if task == "project-context":
        answer = _context()
    elif task in ("verify-snippet", "verify-agent"):
        answer = _verdicts(prompt, agent=task == "verify-agent")
    elif task == "discover":
        answer = _discovery()
    else:
        answer = {}
    return await litellm.acompletion(**base, mock_response="```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```")
