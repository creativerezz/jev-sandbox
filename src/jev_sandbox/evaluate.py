"""Run Jev over labeled AJ Studios contact-form inquiries and sweep decision thresholds.

Run with `uv run jev-eval [path/to/contact-analysis.csv]`. Only the message and event
fields are sent to TypeSafe; names, emails and phone numbers stay local. Raw answers
are cached in `data/results.jsonl` (gitignored) so threshold sweeps rerun offline.
"""

import asyncio
import csv
import json
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

from jev_sandbox import load_env

DEFAULT_CSV = Path.home() / "AJStudios/Journal/ajstudios-contact-analysis.csv"
CACHE = Path(__file__).resolve().parents[2] / "data/results.jsonl"

# Human-assigned category -> the intent option Jev should pick.
LABEL_TO_INTENT = {
    "Sales inquiry": "new_client",
    "Existing-client service": "existing_client",
    "Vendor solicitation": "vendor_pitch",
    "Likely test/internal": "test_or_junk",
    "Other business/contact": "other",
    "Unclear": "other",
}
EVENT_FIELDS = ["Event date", "Wedding Date(s)", "Date of event", "Venue/Location", "Venue / Location",
                "What type of event are you planning?", "Services"]

QUESTIONS = {
    "sales": Noul(
        instructions=(
            "This message was submitted through the contact form of AJ Studios, a wedding and event "
            "photography/videography business. Is it a genuine prospective client asking about booking, "
            "pricing, packages, or availability for an event?"
        ),
    ),
    "intent": Choice(
        instructions="What kind of contact-form submission is this for a wedding photo/video studio?",
        criteria={
            "new_client": "A prospective client asking about pricing, packages, availability, or booking coverage",
            "existing_client": "A past or current client following up on delivery, payment, or their own booking",
            "vendor_pitch": "A business selling services to the studio (cleaning, marketing, SEO, leads, software)",
            "test_or_junk": "A test submission, keyboard mashing, or content with no real request",
            "other": "Anything else: networking, partnerships, personal contact, or too vague to tell",
        },
    ),
}


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def state_for(row: dict) -> dict:
    event = {k: row[k].strip() for k in EVENT_FIELDS if row.get(k, "").strip()}
    return {"message": row["Message"].strip(), "event_details": event}


async def fetch(rows: list[dict]) -> list[dict]:
    cached = {}
    if CACHE.exists():
        cached = {r["row"]: r for r in map(json.loads, CACHE.read_text().splitlines())}
    todo = [r for r in rows if r["Source row"] not in cached]
    if todo:
        load_env()
        sem = asyncio.Semaphore(8)
        async with AsyncTypeSafeClient() as client:

            async def one(row: dict) -> dict:
                async with sem:
                    resp = await client.system_one(state=state_for(row), questions=QUESTIONS)
                intent = resp.choices["intent"]
                return {
                    "row": row["Source row"],
                    "label": row["Analysis category"],
                    "sales": resp.nouls["sales"].noul,
                    "intent": intent.choice,
                    "intent_conf": intent.confidence,
                    "intent_probs": intent.probabilities,
                    "tokens": resp.usage.input_tokens,
                }

            fresh = await asyncio.gather(*(one(r) for r in todo))
        CACHE.parent.mkdir(exist_ok=True)
        with CACHE.open("a") as f:
            for r in fresh:
                f.write(json.dumps(r) + "\n")
                cached[r["row"]] = r
    return [cached[r["Source row"]] for r in rows]


def prf(results: list[dict], threshold: float) -> tuple[float, float, float, int, int]:
    tp = sum(r["sales"] >= threshold and r["label"] == "Sales inquiry" for r in results)
    fp = sum(r["sales"] >= threshold and r["label"] != "Sales inquiry" for r in results)
    fn = sum(r["sales"] < threshold and r["label"] == "Sales inquiry" for r in results)
    p = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * rec / (p + rec) if p + rec else 0.0
    return p, rec, f1, fp, fn


def report(rows: list[dict], results: list[dict]) -> None:
    n = len(results)
    tokens = sum(r["tokens"] or 0 for r in results)
    print(f"{n} labeled inquiries, {tokens:,} input tokens (~${tokens * 0.042 / 1e6:.4f})\n")

    print("Sales Noul threshold sweep (positive = 'Sales inquiry')")
    print("  thresh  precision  recall    F1   false+  missed")
    best = max((prf(results, t / 100)[2], -abs(t - 50), t) for t in range(5, 100, 5))[2] / 100
    for t in sorted({0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, best}):
        p, rec, f1, fp, fn = prf(results, t)
        tag = ("  <- current" if t == 0.7 else "") + ("  <- best F1" if t == best else "")
        print(f"  {t:5.2f}   {p:8.3f}  {rec:6.3f}  {f1:6.3f}  {fp:5d}  {fn:6d}{tag}")

    print("\nIntent Choice: accuracy vs. how many get auto-handled at each confidence floor")
    print("  min_conf  auto-handled  accuracy(auto)  sent to review  errors caught")
    for c in [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]:
        auto = [r for r in results if r["intent_conf"] >= c]
        review = [r for r in results if r["intent_conf"] < c]
        acc = sum(r["intent"] == LABEL_TO_INTENT[r["label"]] for r in auto) / len(auto) if auto else 0
        caught = sum(r["intent"] != LABEL_TO_INTENT[r["label"]] for r in review)
        tag = "  <- current" if c == 0.6 else ""
        print(f"  {c:7.2f}  {len(auto):5d} ({len(auto) / n:4.0%})  {acc:12.3f}  {len(review):12d}  {caught:12d}{tag}")

    by_row = {r["Source row"]: r for r in rows}
    wrong = [r for r in results if r["intent"] != LABEL_TO_INTENT[r["label"]] or
             (r["sales"] >= 0.5) != (r["label"] == "Sales inquiry")]
    print(f"\nDisagreements with labels ({len(wrong)}):")
    for r in sorted(wrong, key=lambda r: r["intent_conf"]):
        msg = by_row[r["row"]]["Message"].strip().replace("\n", " ")[:110]
        print(f"  row {r['row']:>3}  label={r['label']:<24} jev={r['intent']:<15} conf={r['intent_conf']:.2f} "
              f"p(sales)={r['sales']:.2f}\n           {msg}")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    rows = [r for r in load_rows(path) if r["Message"].strip()]
    results = asyncio.run(fetch(rows))
    report(rows, results)
