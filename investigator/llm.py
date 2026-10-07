"""Model boundary. The only module that builds a chat model.

  live_model() - ChatOpenAI on any OpenAI-compatible endpoint (OpenAI, OpenRouter, ...), chosen by env vars.
  Scripted     - plays back recorded AIMessages: replay of saved runs (no key needed) and scripted tests.
                 An Exception in the script is raised to simulate a failure.
Every call record carries "source" (live / replay / scripted) so cached and simulated
responses are never mistaken for new model calls.
"""
import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field


class LLMError(Exception):
    """The model call failed after the SDK's own retries (timeout, rate limit, 5xx, ...)."""


def live_model(model: str | None = None, reasoning_effort: str | None = None) -> ChatOpenAI:
    """From LLM_* env vars; the eval passes other models."""
    missing = [k for k in ("LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
    if missing:
        raise LLMError(f"set {', '.join(missing)} (see .env.example); replaying saved runs needs no key")
    # use_responses_api=False: langchain-openai sends gpt-6 models with tools to the Responses API by
    # default; the eval validated Chat Completions. max_retries: 408/409/429/5xx with backoff.
    return ChatOpenAI(model=model or os.environ["LLM_MODEL"], api_key=os.environ["LLM_API_KEY"],
                      base_url=os.environ.get("LLM_BASE_URL") or None,
                      reasoning_effort=reasoning_effort or os.environ.get("LLM_REASONING_EFFORT") or None,
                      timeout=60, max_retries=2, use_responses_api=False)


def describe(model) -> dict:
    if isinstance(model, Scripted):
        return model.config
    return {"base_url": model.openai_api_base or "https://api.openai.com/v1", "model": model.model_name,
            "reasoning_effort": model.reasoning_effort}


class Scripted(BaseChatModel):
    script: list
    source: str = "scripted"
    config: dict = Field(default_factory=lambda: {"model": "scripted"})
    seen: list = Field(default_factory=list)  # each call's input, for tests; shared by bound copies
    tool_choice: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.model_copy(update={"tool_choice": tool_choice})  # shallow: same script and seen lists

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append({"messages": list(messages), "tool_choice": self.tool_choice})
        if not self.script:
            raise LLMError(f"{self.source}: no recorded response left")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item.model_copy())])  # a script may repeat one message
