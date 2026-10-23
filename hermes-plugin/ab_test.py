"""A/B: does the jev-skills hint change which skill Hermes's model loads first?

Run with Hermes's venv (it imports Hermes's own prompt builder and skill schema):
  ~/.hermes/hermes-agent/venv/bin/python hermes-plugin/ab_test.py

Each request is sent to the model once without the hint and once with it, using Hermes's real
skill index as the system prompt. Tools are offered but never executed; only the first
skill_view call is recorded, so nothing touches your accounts. Results: data/skills-ab.json.
"""

import importlib.util
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

HERMES = Path.home() / ".hermes/hermes-agent"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERMES))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path.home() / ".hermes/.env")
load_dotenv(Path.home() / ".env.local")

from agent.prompt_builder import build_skills_system_prompt  # noqa: E402
from tools.skills_tool import SKILL_VIEW_SCHEMA  # noqa: E402

spec = importlib.util.spec_from_file_location("jev_skills", ROOT / "hermes-plugin/jev-skills/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
plugin.LOG = Path(os.devnull)

MODEL = os.environ.get("AB_MODEL", "openai/gpt-6-astra")
IDENTITY = (
    "You are Hermes, a capable AI assistant with access to tools and a library "
    "of skills. You help the user with coding, research, and everyday tasks.\n\n"
)
SYSTEM = IDENTITY + build_skills_system_prompt()
STUB = lambda name, desc, prop: {  # noqa: E731
    "type": "function",
    "function": {"name": name, "description": desc,
                 "parameters": {"type": "object", "properties": {prop: {"type": "string"}}, "required": [prop]}},
}
TOOLS = [
    {"type": "function", "function": SKILL_VIEW_SCHEMA},
    STUB("terminal", "Run a shell command on the user's machine and return its output.", "command"),
    STUB("read_file", "Read a file from the user's filesystem.", "path"),
    STUB("web_search", "Search the web and return result snippets.", "query"),
]

COVERED = [
    ("Add a reminder to call the florist tomorrow at 10am", "apple-reminders"),
    ("Save this recipe as a new note in my Notes app so it syncs to my phone", "apple-notes"),
    ("Where is my AirPods case right now?", "findmy"),
    ("Turn the living room lights to 30% warm white", "openhue"),
    ("Post this thread to my X account: we just launched AJ Studios booking!", "xurl"),
    ("Create an Excel spreadsheet tracking my wedding bookings with formulas for deposits and balances", "xlsx"),
    ("Deploy this Cloudflare Worker with wrangler and add a KV namespace binding", "wrangler"),
    ("Transcribe interview.m4a to text", "whisper"),
    ("Turn these meeting notes into a 10-slide PowerPoint deck", "powerpoint"),
    ("Review PR #42 on my GitHub repo and leave inline comments", "github-code-review"),
    ("Run an SEO audit on ajstudios.net and tell me why it isn't ranking", "seo-audit"),
    ("Add Cloudflare Turnstile bot protection to my contact form", "turnstile-spin"),
    ("Find recent arXiv papers on speculative decoding", "arxiv"),
    ("Draw an excalidraw diagram of our login flow", "excalidraw"),
    ("How much should I set aside for my quarterly estimated tax payment?", "tax-prep"),
    ("Write a failing test first, then implement the slugify function", "test-driven-development"),
    ("Text Sam on iMessage that I'm running 10 minutes late", "imessage"),
    ("Find me a gif of a cat celebrating", "gif-search"),
    ("Make a p5.js sketch of particles following the mouse", "p5js"),
]
UNCOVERED = [
    "Explain what a monad is", "What's a good name for a golden retriever?",
    "Post this announcement to my Mastodon account.", "Translate 'thank you for choosing us' into Spanish",
    "What's the difference between TCP and UDP?", "Write me a haiku about autumn",
    "Summarize the plot of Hamlet in 3 sentences", "Add these three cards to our Trello backlog.",
    "How many grams are in a pound?", "thanks!",
]


def first_skill(user_content: str) -> dict:
    started = time.monotonic()
    resp = httpx.post(
        "https://ai-gateway.vercel.sh/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"},
        json={"model": MODEL, "max_tokens": 400, "tools": TOOLS,
              "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_content}]},
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    calls = body["choices"][0]["message"].get("tool_calls") or []
    loaded = None
    for call in calls:
        if call["function"]["name"] == "skill_view":
            loaded = json.loads(call["function"]["arguments"] or "{}").get("name")
            break
    return {"loaded": loaded, "first_tool": calls[0]["function"]["name"] if calls else None,
            "ms": round((time.monotonic() - started) * 1000), "usage": body.get("usage", {})}


def run_case(case: tuple) -> dict:
    text, gold = case
    suggestion = plugin.suggest(text)
    hint = None
    if suggestion["skill"]:
        hint = (f"<skill_relevance>\nRelevant to the current request: {suggestion['skill']} "
                f"(load it with skill_view(name='{suggestion['skill']}')). "
                "Ignore this if it does not fit what the user actually asked for.\n</skill_relevance>")
    without = first_skill(text)
    with_jev = first_skill(f"{text}\n\n{hint}") if hint else {**without, "reused": True}
    return {"request": text, "gold": gold, "suggestion": suggestion, "without": without, "with": with_jev}


def main() -> None:
    cases = COVERED + [(t, None) for t in UNCOVERED]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(run_case, cases))
    out = ROOT / f"data/skills-ab-{MODEL.split('/')[-1]}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": MODEL, "skills_prompt_chars": len(SYSTEM), "results": results}, indent=1))

    def ok(r, arm):
        return r[arm]["loaded"] == r["gold"]

    cov = [r for r in results if r["gold"]]
    unc = [r for r in results if not r["gold"]]
    for arm in ("without", "with"):
        right = sum(ok(r, arm) for r in cov)
        needless = sum(r[arm]["loaded"] is not None for r in unc)
        print(f"{arm:>7}: right skill {right}/{len(cov)}   loaded one when none fit {needless}/{len(unc)}")
    for r in results:
        a, b = r["without"]["loaded"], r["with"]["loaded"]
        if a != b:
            print(f"  changed: {r['request'][:50]!r}  gold={r['gold']}  {a} -> {b}")


if __name__ == "__main__":
    main()
