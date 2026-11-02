"""
LangChain chat model for the Elastic Inference Service (EIS) unified chat_completion API.

Calls  POST {ES_URL}/_inference/chat_completion/{inference_id}/_stream  with the Elasticsearch
API key, parses the OpenAI-style server-sent-event stream (content, tool calls, usage), and
returns a normal AIMessage so it plugs into create_agent / LangGraph unchanged.

Model settings are left at the endpoint's defaults (no temperature, no reasoning overrides)
unless passed explicitly.
"""
import asyncio
import json
import re
import os
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("thinking", "redacted_thinking"):
                    continue
                parts.append(p.get("text", json.dumps(p)))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return json.dumps(content)


def _to_wire(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": _text(m.content)})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": _text(m.content)})
        elif isinstance(m, AIMessage):
            msg: Dict[str, Any] = {"role": "assistant", "content": _text(m.content)}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                    for tc in m.tool_calls
                ]
            out.append(msg)
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": _text(m.content)})
        else:
            out.append({"role": "user", "content": _text(m.content)})
    return out


def _repair_args(raw: str) -> Dict[str, Any]:
    """Best-effort repair of tool-call arguments the model emitted as invalid JSON.

    Seen in practice: an array field written as a bare comma list ("tags": a,b,c}). Quote it into a list;
    return {} when the text still does not parse, so the tool reports its own validation error.
    """
    m = re.search(r'"(\w+)":\s*([^\s\[\]"{}](?:(?!"\w+":)[^{}\[\]])*?)\s*}\s*$', raw)
    if m:
        items = [x.strip() for x in m.group(2).split(",") if x.strip()]
        fixed = raw[:m.start()] + f'"{m.group(1)}": ' + json.dumps(items) + "}"
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
    return {}


class ChatEIS(BaseChatModel):
    es_url: str
    api_key: str
    inference_id: str = ".anthropic-claude-5.5-opus-chat_completion"
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout: float = 900.0
    max_retries: int = 4
    tools_wire: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "elastic-inference-service"

    def bind_tools(self, tools, **kwargs):
        wire = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(update={"tools_wire": wire})

    def _body(self, messages: List[BaseMessage], stop: Optional[List[str]]) -> Dict[str, Any]:
        body: Dict[str, Any] = {"messages": _to_wire(messages)}
        if self.tools_wire:
            body["tools"] = self.tools_wire
        if self.max_completion_tokens:
            body["max_completion_tokens"] = self.max_completion_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if stop:
            body["stop"] = stop
        return body

    @property
    def _url(self) -> str:
        return f"{self.es_url.rstrip('/')}/_inference/chat_completion/{self.inference_id}/_stream"

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"ApiKey {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _assemble(events: List[Dict[str, Any]], inference_id: str) -> ChatResult:
        text, calls, usage, model, finish = [], {}, None, None, None
        for ev in events:
            model = ev.get("model") or model
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(tc["index"], {"id": None, "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    slot["args"] += fn.get("arguments") or ""
                finish = ch.get("finish_reason") or finish
        tool_calls = []
        for idx in sorted(calls):
            c = calls[idx]
            try:
                args = json.loads(c["args"]) if c["args"] else {}
            except json.JSONDecodeError as ex:
                args = _repair_args(c["args"])
                if os.environ.get("EIS_PARSE_LOG"):
                    with open(os.environ["EIS_PARSE_LOG"], "a") as f:
                        f.write(json.dumps({"tool": c["name"], "error": str(ex), "chars": len(c["args"]),
                                            "head": c["args"][:300], "tail": c["args"][-300:]}) + "\n")
            tool_calls.append({"name": c["name"], "args": args, "id": c["id"], "type": "tool_call"})
        u = None
        if usage:
            u = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
                 "total_tokens": usage.get("total_tokens", 0)}
        msg = AIMessage(content="".join(text), tool_calls=tool_calls, usage_metadata=u,
                        response_metadata={"model_name": model or inference_id, "finish_reason": finish})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                         run_manager: Optional[AsyncCallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        body = self._body(messages, stop)
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                events: List[Dict[str, Any]] = []
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream("POST", self._url, headers=self._headers, json=body) as r:
                        if r.status_code in (429, 500, 502, 503, 504, 529):
                            raise httpx.HTTPStatusError(f"retryable {r.status_code}", request=r.request, response=r)
                        if r.status_code >= 400:
                            detail = (await r.aread()).decode("utf-8", "replace")[:600]
                            raise RuntimeError(f"EIS {r.status_code}: {detail}")
                        async for line in r.aiter_lines():
                            line = line.lstrip("﻿").strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            events.append(json.loads(payload))
                for ev in events:
                    if "error" in ev:
                        raise RuntimeError(f"EIS stream error: {json.dumps(ev['error'])[:400]}")
                return self._assemble(events, self.inference_id)
            except (httpx.HTTPError, httpx.StreamError) as e:
                last = e
                await asyncio.sleep(min(2 ** attempt * 2, 30))
        raise RuntimeError(f"EIS request failed after retries: {last}")

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop, **kwargs))
