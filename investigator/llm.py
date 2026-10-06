"""Model boundary. The only module that talks to a model provider.

Two implementations of one method, complete(messages, tools, tool_choice) -> call record:
  LiveLLM     - any OpenAI-compatible endpoint (OpenAI, OpenRouter, ...), chosen by env vars.
  ScriptedLLM - plays back recorded assistant messages: replay of saved runs (no key needed)
                and scripted tests. An Exception in the script is raised to simulate a failure.
Every call record carries "source" (live / replay / scripted) so cached and simulated
responses are never mistaken for new model calls.
"""
import os
import time

import openai


class LLMError(Exception):
    """The model call failed after the SDK's own retries (timeout, rate limit, 5xx, ...)."""


class LiveLLM:
    def __init__(self, model: str, api_key: str, base_url: str | None = None,
                 reasoning_effort: str | None = None, timeout_s: float = 60, max_retries: int = 2):
        # max_retries: the SDK retries 408/409/429/5xx and connection errors with exponential backoff.
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries)
        self.reasoning_effort = reasoning_effort
        self.config = {"base_url": str(self.client.base_url), "model": model, "reasoning_effort": reasoning_effort}

    @classmethod
    def from_env(cls) -> "LiveLLM":
        missing = [k for k in ("LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
        if missing:
            raise LLMError(f"set {', '.join(missing)} (see .env.example); replaying saved runs needs no key")
        return cls(model=os.environ["LLM_MODEL"], api_key=os.environ["LLM_API_KEY"],
                   base_url=os.environ.get("LLM_BASE_URL") or None,
                   reasoning_effort=os.environ.get("LLM_REASONING_EFFORT") or None)

    def complete(self, messages: list, tools: list, tool_choice="auto") -> dict:
        extra = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        started = time.monotonic()
        try:
            # one tool call per turn: each query is chosen after seeing the previous result
            r = self.client.chat.completions.create(model=self.config["model"], messages=messages, tools=tools,
                                                    tool_choice=tool_choice, parallel_tool_calls=False, **extra)
        except openai.OpenAIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        m = r.choices[0].message
        message = {"role": "assistant", "content": m.content}
        if m.tool_calls:
            message["tool_calls"] = [tc.model_dump(include={"id", "type", "function"}) for tc in m.tool_calls]
        return {
            "source": "live",
            "id": r.id,
            "model": r.model,  # the snapshot the provider actually served
            "finish_reason": r.choices[0].finish_reason,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "usage": {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens} if r.usage else None,
            "message": message,
        }


class ScriptedLLM:
    def __init__(self, calls: list, source: str = "replay", config: dict | None = None):
        self.calls = list(calls)
        self.source = source
        self.config = config or {"model": source}
        self.seen = []  # what each call received, for tests

    def complete(self, messages: list, tools: list, tool_choice="auto") -> dict:
        self.seen.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice})
        if not self.calls:
            raise LLMError(f"{self.source}: no recorded response left")
        call = self.calls.pop(0)
        if isinstance(call, Exception):
            raise call
        return {**call, "source": self.source}
