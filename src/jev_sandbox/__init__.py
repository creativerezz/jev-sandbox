"""Jev sandbox: run a few typed questions over sample inbound leads.

Run with `uv run jev-sandbox`. Needs TYPESAFE_API_KEY in `.env` (this folder)
or `~/.env.local`.
"""

import os
from pathlib import Path

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

ENV_FILES = [Path(__file__).resolve().parents[2] / ".env", Path.home() / ".env.local"]

LEADS = [
    "Hi! We're getting married June 14 next year in Detroit and would love a quote for photo + video, about 150 guests.",
    "Your last invoice charged my card twice, can someone refund the duplicate?",
    "We're a 40-person agency and want to partner on wedding video packages for 3 of our clients next spring.",
    "BUY CHEAP FOLLOWERS NOW!!! 10k Instagram followers for $5, click here",
]

QUESTIONS = {
    "sales": Noul(instructions="Should this message be routed to the sales team as a new business opportunity?"),
    "intent": Choice(
        instructions="What does the sender primarily want?",
        criteria={
            "quote": "Pricing or booking for a new shoot",
            "support": "Help with an existing booking, delivery, or billing",
            "partnership": "A business collaboration or referral arrangement",
            "spam": "Unsolicited promotion or irrelevant content",
        },
    ),
    "urgency": Score(
        instructions="How time-sensitive is a reply to this message?",
        criteria=["No timeline or no reply needed", "Timeline is months away", "Needs a reply within days", "Needs a reply today"],
    ),
}

# Starting thresholds only; tune them on labeled examples before trusting them.
SALES_THRESHOLD = 0.7
MIN_CONFIDENCE = 0.6


def load_env() -> None:
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_env()
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise SystemExit("TYPESAFE_API_KEY not set. Add it to .env or ~/.env.local (key from https://console.typesafe.ai).")

    with TypeSafeClient() as client:
        for lead in LEADS:
            r = client.system_one(state={"message": lead}, questions=QUESTIONS)
            sales = r.nouls["sales"].noul
            intent = r.choices["intent"]
            urgency = r.scores["urgency"]

            if intent.confidence < MIN_CONFIDENCE:
                action = "review (unsure)"
            elif sales >= SALES_THRESHOLD:
                action = "route to sales"
            else:
                action = f"handle as {intent.choice}"

            print(f"\n{lead[:80]}")
            print(f"  sales    p(yes)={sales:.2f}")
            print(f"  intent   {intent.choice} (confidence {intent.confidence:.2f})")
            print(f"  urgency  {urgency.score:.2f} / 3")
            print(f"  -> {action}   [{r.model}, {r.usage.input_tokens} input tokens]")
