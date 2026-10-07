"""The guard alone, live: each guard prompt on the eval's data questions and follow-ups (must allow) and on
small talk and off-topic requests (must block), 3 trials each. About $0.01. Only the guard runs: a message it
allows goes through the same pipeline whatever the guard version. Every label is saved to evals/guard_results.json.

  uv run --env-file .env python -m evals.guard_eval
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from evals.run_eval import QUESTIONS
from investigator import agent
from investigator.llm import live_model

ALLOW = [q[0] for q in QUESTIONS if len(q) == 4] + [
    "What can you do?", "How do you check the figures?", "Break the refund increase down by customer segment.",
    "thanks! now show October too", "hi, why did refunds go up in September 2026?"]
BLOCK = ["hi", "how r ya", "hello there", "thanks!", "good morning", "how are you doing today?", "lol", "what's up",
         "tell me a joke", "Write a Python function that reverses a list.", "What is the capital of France?"]
PROMPTS = ["prompts/guard-v1.md", "prompts/guard-v2.md"]  # v1 allowed greetings and thanks on purpose


def label(model, prompt: str, message: str) -> str:
    ctx = agent.Ctx(model, None, {}, {"guard": (agent.ROOT / prompt).read_text().strip()})
    return agent.guard({"messages": [HumanMessage(message)], "model_calls": []}, SimpleNamespace(context=ctx))["guard"]["label"]


def main():
    model = live_model()
    jobs = [(m, want) for want, ms in (("allow", ALLOW), ("block", BLOCK)) for m in ms for _ in range(3)]
    saved = {"model": model.model_name, "trials": 3, "results": {}}
    for prompt in PROMPTS:
        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(lambda job, p=prompt: label(model, p, job[0]), jobs))
        wrong = [m for (m, want), g in zip(jobs, got) if (g == "allow") != (want == "allow")]
        print(f"{Path(prompt).name}: false blocks {sum(m in ALLOW for m in wrong)}/{3 * len(ALLOW)}, "
              f"small talk and off-topic let through {sum(m in BLOCK for m in wrong)}/{3 * len(BLOCK)}")
        for m in dict.fromkeys(wrong):
            print(f"  wrong {wrong.count(m)}/3: {m}")
        saved["results"][prompt] = [{"message": m, "want": want, "got": g} for (m, want), g in zip(jobs, got)]
    (Path(__file__).parent / "guard_results.json").write_text(json.dumps(saved, indent=1) + "\n")


if __name__ == "__main__":
    main()
