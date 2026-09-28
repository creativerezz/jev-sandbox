# jev-sandbox

Experiments with [TypeSafe](https://docs.typesafe.ai)'s **Jev**, a hosted "System One" model that
returns typed judgments with probabilities (yes/no, pick-one, graded scores) instead of generated
text. The repo contains three parts:

| Part | What it does | Where |
| --- | --- | --- |
| Inquiry sandbox | Classifies AJ Studios contact-form inquiries and sweeps decision thresholds | `src/jev_sandbox/` |
| Claude Code Bash guard | PreToolUse hook that scores every Bash command before it runs | `hooks/jev_guard.py` |
| Hermes skill suggestion | Plugin that suggests at most one relevant skill per turn | `hermes-plugin/jev-skills/` |

Jev runs on TypeSafe's servers (`jev-1.13.0`), not locally. Each part needs a `TYPESAFE_API_KEY` from
[console.typesafe.ai](https://console.typesafe.ai).

## Setup

```sh
uv sync
cp .env.example .env   # then add TYPESAFE_API_KEY=...
```

The scripts read the key from the environment, then `.env` in this folder, then `~/.env.local`.

## Inquiry sandbox

```sh
uv run jev-sandbox   # four sample inquiries: sales probability, intent, urgency
uv run jev-eval      # labeled inquiries from ~/AJStudios/Journal/ajstudios-contact-analysis.csv
```

`jev-eval` sends only the message and event fields. Names, emails and phone numbers stay local.
Raw answers are cached in `data/results.jsonl`, so rerunning with new cutoffs makes no API calls.

On 106 labeled inquiries (about 61.6k input tokens, roughly $0.003), the "is this a sales lead"
probability scored best around a 0.5 cutoff: precision 0.99, recall 0.99, F1 0.987. At 0.7 it missed
17 real leads. The labels came from an earlier automated analysis, not from hand review.

## Claude Code: Bash safety check

A `PreToolUse` hook sends each Bash command to Jev before it runs. Jev returns a 0–3 risk score plus
five yes/no checks: read-only, changes something remote, destroys data, exposes a secret, and stops
processes.

- **Clearly risky:** forces a confirmation prompt (`"ask"`).
- **Clearly read-only:** approves without a prompt (`"allow"`).
- **Everything else, errors, or timeouts:** no decision, so normal permission rules apply. It never
  falls back to auto-approving.

Each call adds about 150–250 ms. The script uses only the Python standard library, so it runs under
the system `python3`.

### Install

Add this to `~/.claude/settings.json`, merging with any existing `hooks`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 /path/to/jev-sandbox/hooks/jev_guard.py",
            "timeout": 8,
            "statusMessage": "Jev guard"
          }
        ]
      }
    ]
  }
}
```

- `JEV_GUARD=observe` logs decisions without acting on them.
- `JEV_GUARD=off` skips the check entirely.
- The hook can also be reviewed or removed through `/hooks`.

### Accuracy

Tested on 105 hand-labeled commands: 90 real ones from past Claude Code sessions and 15 made-up
dangerous ones.

| Result | Count |
| --- | --- |
| Risky commands caught | 31 of 31 |
| Wrong auto-approvals | 0 |
| Read-only commands auto-approved | 37 of 49 |
| Unnecessary prompts | 6 |

The cutoffs were tuned on that same set, so expect somewhat worse results on new commands. To
re-run the evaluation after changing questions or cutoffs:

```sh
python3 hooks/jev_guard.py --eval data/guard-labeled.json
```

The labeled set lives in `data/`, which is gitignored because it contains real commands and paths.

**Not verified:** whether `"ask"` shows a prompt in bypass-permissions mode. The Claude Code docs
say it forces a prompt in auto mode but say nothing about bypass mode.

### Privacy

Commands and working directories are sent to TypeSafe. Strings that look like API keys, and values
assigned to variables named like `TOKEN`, `KEY`, `SECRET` or `PASSWORD`, are redacted first. Every
decision is logged locally to `data/guard-log.jsonl`.

## Hermes: skill suggestion

A Hermes plugin that follows TypeSafe's
[skill-suggestion recipe](https://docs.typesafe.ai/cookbooks/skill_suggestion) in two requests:

1. Rank every enabled skill and ask whether the request needs a skill at all. A single pick-one
   question allows at most 255 options, so larger skill lists are split into chunks within the
   same request.
2. Re-check the top candidates against each skill's full description and the opening of its
   `SKILL.md`. The chosen skill must pass its own "does it fit?" check.

If a skill passes, one hint line is appended to the user's message for that turn, pointing the agent
at `skill_view(name='...')`. The agent keeps its full skill index and its own judgement. If TypeSafe
fails or times out, nothing is suggested.

### Install

```sh
cp -r hermes-plugin/jev-skills ~/.hermes/plugins/
hermes config set TYPESAFE_API_KEY <key>   # stored in ~/.hermes/.env
hermes plugins enable jev-skills
```

The plugin calls TypeSafe's HTTP API with `httpx`, which Hermes already ships, so nothing needs
installing into Hermes's environment.

### Accuracy

| Test | Result |
| --- | --- |
| Requests with one clearly right skill (262 installed) | 16 of 19 correct |
| Requests no skill should match | Quiet on 8 of 10 |
| Correct skill first in the initial ranking | 19 of 19 |
| Real Hermes turn: "find arXiv papers" | Suggested `arxiv` in 809 ms; the agent loaded and used it |

Several misses were defensible neighbours, such as `omh-media-input` instead of `whisper`, since
both handle transcription.

Once warm it costs about 0.4 s and roughly $0.0006 per turn. Suggestions are logged to
`suggestions.jsonl` in the plugin folder. Use that log to decide whether the cutoffs, currently
TypeSafe's default of 0.30, need changing.

## Layout

```
hooks/jev_guard.py            Claude Code PreToolUse hook (stdlib only; --eval mode)
hermes-plugin/jev-skills/     Hermes plugin source (the live copy is in ~/.hermes/plugins/)
src/jev_sandbox/__init__.py   jev-sandbox demo
src/jev_sandbox/evaluate.py   jev-eval threshold sweep
data/                         gitignored: caches, labeled sets, logs
```
