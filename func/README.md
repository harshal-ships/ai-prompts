# HealthFirst Triage Voice Agent

A phone-based **medical triage** assistant named **Aria** for HealthFirst Clinic.

Patient calls in → Aria asks structured symptom questions → LangGraph assesses urgency → Aria advises the caller what to do next. This is a **triage agent**, not a booking or transfer system.

This project demonstrates how **LangGraph** integrates into a real-time voice stack with **observability** (LangFuse) and **timestamped call transcripts** — creating a source of truth for handling call disputes when paired with call recordings.

## Why LangGraph + observability?

| Concern | How this project addresses it |
|--------|-------------------------------|
| **Who said what?** | Timestamped transcript (`00:04:25` elapsed) for every patient and assistant turn |
| **Why was this route chosen?** | LangGraph triage timeline records urgency, routing, and symptoms at each turn |
| **Can we audit the AI decision?** | LangFuse traces every LangGraph invocation (nodes: collect → assess → route) per call |
| **Dispute evidence** | `call_records/{call_id}/` bundles transcript + triage timeline + manifest |

When a caller disputes what was said or what advice they received, reviewers can cross-reference:
1. **Telcoflow call recording** (if enabled on the connector)
2. **Local transcript** with timestamps
3. **LangFuse session** grouped by `call_id` showing graph execution

## What happens on a call

1. **Caller rings in** through Telcoflow (the phone layer).
2. **Aria talks** using Amazon Nova Sonic 2 on AWS Bedrock (native speech in and out).
3. **After each patient turn**, the transcript goes to a **LangGraph** workflow that:
   - Collects symptom details (name, age, main symptom, duration, severity, and related info)
   - Assesses urgency: **LOW**, **MEDIUM**, or **HIGH**
   - Chooses a triage route and spoken advice
4. **Observability layer** records every turn and LangGraph decision; LangFuse traces the graph when configured.

## Triage outcomes by urgency

| Urgency | What Aria advises |
|--------|-------------------|
| **LOW** | Not an emergency. Call **+6567504645** during business hours to schedule a **routine** clinic visit. |
| **MEDIUM** | Needs **same-day clinic review**. Call **+6567504645** today and mention this triage call. |
| **HIGH** | **Call 995 immediately** (Singapore emergency services). |

Every call starts with a safety line: *"I am a virtual assistant and not a doctor. If you are in immediate danger, please call 995 now."*

## Architecture

```
Phone call → Telcoflow → Nova Sonic 2 (voice) → audio back to caller
                              ↕
                   Transcript → LangGraph (triage logic)
                              ↕
              LangFuse traces + call_records/ evidence bundle
```

- **Telcoflow** — handles the phone call and audio stream
- **Nova Sonic 2** — conducts the conversation and speaks
- **LangGraph** — symptom intake, urgency scoring, and care routing advice
- **LangFuse** — observability for LangGraph (optional, recommended for demos)
- **call_records/** — timestamped dispute-evidence bundles per call

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate

pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ -r requirements.txt

python triage_agent.py
```

Create a `.env` file in this folder:

```env
WSS_API_KEY=your_telcoflow_key
WSS_CONNECTOR_UUID=your_connector_uuid

AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
AWS_REGION=us-east-1

# Optional — LangFuse observability (https://langfuse.com)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Optional:

```env
HEALTHFIRST_CLINIC_PHONE=+6567504645
CALL_RECORDS_DIR=call_records
```

Nova Sonic 2 only runs in **`us-east-1`**. Refresh AWS SSO credentials when the session token expires.

## Call evidence bundle

After each call, artifacts are written to `call_records/{call_id}/`:

| File | Purpose |
|------|---------|
| `manifest.json` | Index: call metadata, LangFuse session ID, artifact paths |
| `transcript.txt` | Human-readable transcript with `[HH:MM:SS]` timestamps |
| `transcript.json` | Structured turns (PATIENT / ASSISTANT / SYSTEM) |
| `triage_timeline.json` | LangGraph decision at each patient turn |

Example transcript line:

```
[00:04:25] PATIENT: I have had a fever for two days
[00:04:26] SYSTEM: LangGraph triage: urgency=LOW route=continue_collection action=Continue collecting triage details
[00:04:28] ASSISTANT: Thank you. On a scale of 1 to 10, how severe is it?
```

## LangFuse observability

This project includes the [Langfuse agent skill](https://github.com/langfuse/skills) at `.cursor/skills/langfuse/` for Cursor-assisted LangFuse work.

Each call uses **`call_id` as the LangFuse `session_id`**. Tracing follows LangFuse best practices:

| Practice | Implementation |
|----------|----------------|
| Session grouping | `propagate_attributes(session_id=call_id)` for the full call |
| User attribution | Hashed `user_id` from caller number |
| Nested spans | Call span → `@observe` triage turn → LangGraph `CallbackHandler` nodes |
| Explicit I/O | Span input = patient transcript; output = routing decision (not raw args) |
| Tags | `healthfirst-triage`, `voice`, `langgraph` |
| Flush | After each call and on process shutdown |

Every patient turn triggers a LangGraph trace showing:

- `collect_symptoms` — structured extraction from transcript
- `assess_urgency` — rule-based urgency classification
- `route_decision` — routing action and spoken instruction

**LangFuse UI:** Sessions → search by `call_id` → open nested triage-turn traces.

**Local evidence:** `call_records/{call_id}/manifest.json` lists `langfuse_trace_ids` for cross-reference with recordings.

## Logs

Each triage result is appended to **`triage_log.jsonl`** (one JSON object per line) with call ID, symptoms, urgency, and routing decision.

## What this is not

- Not a replacement for a doctor or nurse
- Not an appointment booking system — it advises callers to contact the clinic
- Not a call transfer system — no nurse handoff on this line
- Not using Google ADK or Gemini for the voice layer
