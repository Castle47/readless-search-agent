"""Keep question, user constraints and prior model prose in different roles."""

import json
import re
from ..contracts import UnderstandingDecision
from ..errors import Stopped
from .request_urls import request_urls


def history_messages(context):
    if not context.strip():
        return []
    try:
        data = json.loads(context)
        if isinstance(data, dict) and data.get("format") == "readless-conversation-v1":
            rows = data["messages"]
            if all(
                isinstance(m, dict)
                and m.get("role") in {"user", "assistant", "source_leads"}
                and isinstance(m.get("content"), str)
                for m in rows
            ):
                return [{"role": m["role"], "content": m["content"]} for m in rows]
    except (ValueError, KeyError, TypeError):
        pass
    # Backward compatibility with actual requests from the previous web UI.
    pattern = re.compile(r"(?m)^(用户：|上次(?:实验)?回答（[^\n]*?）：|上次来源线索：)")
    markers = list(pattern.finditer(context))
    if not markers:
        return [{"role": "context", "content": context}]
    rows = []
    if markers[0].start():
        rows.append({"role": "context", "content": context[: markers[0].start()]})
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(context)
        role = "user" if m[1] == "用户：" else "source_leads" if m[1] == "上次来源线索：" else "assistant"
        rows.append({"role": role, "content": context[m.end() : end].strip()})
    return rows


def context_urls(context):
    from ..adapters.fetch import canonical_url
    from ..errors import ToolFailure

    urls = set()
    for message in history_messages(context):
        # Generated prose and its bibliography are not user-selected sources.
        # They still reach understanding as explicitly labelled history.
        if message["role"] != "user":
            continue
        for url in request_urls(message["content"]):
            try:
                urls.add(canonical_url(url.rstrip(".,，。;；:：!?！？'\"）)]}>")))
            except (ToolFailure, ValueError):
                pass
    return urls


def understanding_payload(request, llm, summary=None):
    payload = request.model_dump(mode="json")
    payload.pop("context", None)
    payload["history"] = history_messages(request.context)
    if summary:
        payload["conversation_summary"] = summary
        payload["history"] = [
            m
            for i, m in enumerate(payload["history"])
            if m["role"] == "user" or i >= len(payload["history"]) - 2
        ]
    omitted = []
    while True:
        try:
            llm.build("understand", payload, UnderstandingDecision)
            return payload, omitted
        except Stopped as error:
            if error.code != "input_budget_exceeded":
                raise
            index = next(
                (
                    i
                    for i, m in enumerate(payload["history"])
                    if m["role"] in {"assistant", "source_leads"} and m["content"]
                ),
                None,
            )
            if index is None:
                raise  # Never truncate the current question or user constraints.
            omitted.append(index)
            payload["history"][index] = {
                **payload["history"][index],
                "content": "",
                "omitted_for_input_capacity": True,
            }
