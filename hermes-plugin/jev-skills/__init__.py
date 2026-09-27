"""jev-skills: suggest at most one skill per turn with TypeSafe Jev.

Follows TypeSafe's skill-suggestion cookbook (https://docs.typesafe.ai/cookbooks/skill_suggestion):
  1. one request ranks the whole roster (a Choice per chunk of <=255 skills) and asks three
     Nouls whether the turn wants an action at all; below GATE_THRESHOLD nothing is suggested
  2. a second request re-reads the shortlist with full descriptions plus the opening of each
     SKILL.md; unless the chosen skill itself "fits" at FITS_THRESHOLD or above, nothing is suggested
The winner is appended to the user turn as a one-line <skill_relevance> hint. The agent keeps
its full skill index and its own judgement. Any error or timeout suggests nothing.
"""

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("plugins.jev-skills")

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
TIMEOUT_S = 6.0
CHUNK = 250  # Choice questions accept at most 255 options
PER_CHUNK = 2  # candidates each chunk contributes to the shortlist
SHORTLIST = 4
MIN_PROB = 0.02  # chunk candidates below this are noise, not contenders
INDEX_CHARS = 220  # description characters per option in the wide ranking
EXCERPT_CHARS = 700  # SKILL.md body characters per candidate in the re-check
GATE_THRESHOLD = 0.30  # cookbook defaults; retune on your own traffic
FITS_THRESHOLD = 0.30
LOG = Path(__file__).parent / "suggestions.jsonl"

GATE_QUESTIONS = {
    "acts_on_user_system": (
        "Is the assistant being asked to act on the user's files, accounts, devices, "
        "or online services, rather than only to explain or advise?"
    ),
    "would_follow_documented_procedure": (
        "Would a careful expert answering this consult a specific documented procedure "
        "or set of commands, rather than answering from general understanding?"
    ),
    "prose_suffices": (
        "Could a knowledgeable generalist fully satisfy this request in prose, with "
        "no tools, no documentation, and no access to the user's files or accounts?"
    ),
}
INVERTED = {"prose_suffices"}

_client = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=TIMEOUT_S)
    return _client


def _ask(state: dict, questions: dict) -> dict:
    resp = _http().post(
        API_URL,
        headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"},
        json={"state": state, "model": MODEL, "questions": questions},
    )
    resp.raise_for_status()
    return resp.json()["answers"]


def _roster() -> list[dict]:
    """Enabled skills as Hermes itself lists them (disabled/platform-filtered, cached 30s)."""
    from tools.skills_tool import _find_all_skills

    return [s for s in _find_all_skills() if s.get("name")]


def _skill_body(name: str) -> str:
    """Opening of a skill's SKILL.md, or '' if it can't be located."""
    try:
        from agent.skill_utils import iter_skill_index_files
        from tools.skills_tool import _parse_frontmatter, _read_skill_text, _skill_search_dirs

        for scan_dir in _skill_search_dirs()[1]:
            for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
                meta, body = _parse_frontmatter(_read_skill_text(skill_md)[:6000])
                if meta.get("name", skill_md.parent.name) == name:
                    return body.strip()[:EXCERPT_CHARS]
    except Exception:
        logger.debug("jev-skills: could not read SKILL.md for %s", name, exc_info=True)
    return ""


def _recent_context(history: list) -> str:
    """The previous assistant message, so short follow-ups ("yes, do that") keep their meaning."""
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            return msg["content"][-600:]
    return ""


def suggest(user_message: str, recent_context: str = "") -> dict:
    """Returns {"skill": name|None, ...diagnostics}. Two TypeSafe requests at most."""
    started = time.monotonic()
    roster = _roster()
    by_name = {s["name"]: s for s in roster}
    state = {"request": user_message, "recent_context": recent_context}

    questions = {}
    chunks = [roster[i : i + CHUNK] for i in range(0, len(roster), CHUNK)]
    for i, chunk in enumerate(chunks):
        questions[f"which::{i}"] = {
            "type": "choice",
            "instructions": "Which of these skills, if any, is the right one to load to help with the user's latest request?",
            "criteria": {s["name"]: (s.get("description") or "")[:INDEX_CHARS] or None for s in chunk},
        }
    for key, text in GATE_QUESTIONS.items():
        questions[f"gate::{key}"] = {"type": "noul", "instructions": text}
    wide = _ask(state, questions)

    gate_values = {k.removeprefix("gate::"): a["noul"] for k, a in wide.items() if k.startswith("gate::")}
    gate = sum((1 - v) if k in INVERTED else v for k, v in gate_values.items()) / len(gate_values)
    candidates = []
    for i in range(len(chunks)):
        ranked = sorted(wide[f"which::{i}"]["probabilities"].items(), key=lambda kv: -kv[1])
        candidates += ranked[:PER_CHUNK]
    ranked = sorted(candidates, key=lambda kv: -kv[1])
    shortlist = [name for name, p in ranked[:SHORTLIST] if p >= MIN_PROB] or [ranked[0][0]]
    result = {"skill": None, "gate": round(gate, 3), "shortlist": shortlist}
    if gate < GATE_THRESHOLD:
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result

    criteria, questions = {}, {}
    for name in shortlist:
        full = by_name[name].get("description") or ""
        body = _skill_body(name)
        criteria[name] = f"{full} — {body}" if body else full or None
        questions[f"fits::{name}"] = {
            "type": "noul",
            "instructions": f"Does the skill '{name}' do the specific thing the user's request asks for? It is described as: {full}",
        }
    questions["which"] = {
        "type": "choice",
        "instructions": "Exactly one of these skills is the right one to load for the user's latest request. Which one? Read what each actually does, not just its name.",
        "criteria": criteria,
    }
    narrow = _ask(state, questions)
    fits = {k.removeprefix("fits::"): round(a["noul"], 3) for k, a in narrow.items() if k.startswith("fits::")}
    result["fits"] = fits
    winner = narrow["which"]["choice"]
    if fits.get(winner, 0) >= FITS_THRESHOLD:  # the pick itself must fit, not just any candidate
        result["skill"] = winner
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


def _already_loaded(name: str, history: list) -> bool:
    for msg in history or []:
        for call in (msg.get("tool_calls") or []) if isinstance(msg, dict) else []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            if fn.get("name") == "skill_view" and f'"{name}"' in str(fn.get("arguments", "")):
                return True
    return False


def _on_pre_llm_call(user_message=None, conversation_history=None, session_id=None, **_):
    if not os.environ.get("TYPESAFE_API_KEY") or not isinstance(user_message, str):
        return None
    text = user_message.strip()
    if not text or text.startswith("/"):
        return None
    try:
        result = suggest(text[:4000], _recent_context(conversation_history))
    except Exception as e:
        logger.warning("jev-skills: suggestion failed: %s", e)
        return None
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "session": session_id, "request": text[:300], **result}) + "\n")
    except OSError:
        pass
    skill = result["skill"]
    if not skill or _already_loaded(skill, conversation_history):
        return None
    return {
        "context": (
            "<skill_relevance>\n"
            f"Relevant to the current request: {skill} (load it with skill_view(name='{skill}')). "
            "Ignore this if it does not fit what the user actually asked for.\n"
            "</skill_relevance>"
        )
    }


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
