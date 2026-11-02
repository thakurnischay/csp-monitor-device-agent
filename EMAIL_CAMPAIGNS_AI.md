# AI drafting assistant for Email Campaigns

An optional helper on the **New / Edit campaign** form. It proposes a subject and message for a festival; a person reviews,
edits and launches as always. It never sends anything and never chooses recipients.

## What it measures (the "AI quality" page, Email Campaigns -> AI quality)

Every draft is stored as one immutable record: the prompt, the context chunks given (with relevance scores and why each was
included or left out), the model's answer, token usage, latency and request id. Every later human action is appended to it:
saved (with edits), regenerated, dry run, test email, launched, each with the **Levenshtein distance** between the AI text and the
person's text.

| Measure | Definition | Target |
|---|---|---|
| Implicit acceptance | launched AI drafts identical to the AI text / launched AI drafts | > 75% |
| Correction delta | mean Levenshtein distance and edit ratio, AI text vs launched text | towards 0 |
| Context efficiency | mean input tokens per draft; tokens per launched campaign | falling |
| Retrieval precision | optional context the model says it used / optional context given (**model-reported**) | > 85% |
| Human interventions | (edits saved + regenerations) / launched | falling |
| Task completion | usable drafts / all attempts (errors, refusals, invalid output count as failures) | > 95% |

Figures show "Not enough data" until 5 AI-drafted campaigns have launched.

## Privacy

Only non-personal text leaves the server: fixed rules, device descriptions, style notes, the occasion, and the optional brief
after scrubbing (emails, phone numbers, long numbers, links and any known operator name/code/email/mobile are replaced). If the
assembled prompt still looks personal, nothing is sent. A reply containing personal data is discarded. Names and emails are
filled in later by our own code from the `{name}`-style placeholders. Outbound HTTPS to the Anthropic API is required.

## Setup (administrator)

Set in the server environment (never in the repository): `ANTHROPIC_API_KEY`, optionally `CAMPAIGN_AI_ENABLED=0` to switch
off, `CAMPAIGN_AI_MODEL` (default `claude-opus-5-5`), `CAMPAIGN_AI_CONTEXT_BUDGET`. `anthropic` is a new dependency; rebuilding the
Docker image installs it. Telemetry writes happen in a background thread and can never block a page.

## Using NVIDIA Nemotron instead of Anthropic

Set `CAMPAIGN_AI_PROVIDER=nvidia`, `NVIDIA_API_KEY=nvapi-...` and `CAMPAIGN_AI_MODEL=<exact model name from build.nvidia.com>` in the server `.env`,
then `docker compose up -d`. Needs outbound HTTPS to integrate.api.nvidia.com. Same privacy rules, telemetry and quality page; each draft
records its provider. Nemotron replies are cleaned of `<think>` blocks and code fences; a reply that is not valid JSON, or uses unknown
placeholders, is recorded as `invalid_output` (shown on the quality page as a lower completion rate). Cost is not estimated for NVIDIA.

## Golden test set

`server/campaigns/ai/golden.json`: 56 fixed cases (14 occasions x 4 device sets) with programmatic graders (valid placeholders, no
personal data, no high-risk wording, length, mentions the occasion, names the missing devices, greets by name).

```
python tools/run_ai_eval.py                     free, deterministic stand-in (tests the harness)
python tools/run_ai_eval.py --live --yes-spend  56 real model calls; costs money
```

## Not built yet

True token-level confidence highlighting (the API provides none; the page shows keyword review flags instead), the side-by-side
prompt playground, guardrail revocation, and LangSmith/Phoenix export.
