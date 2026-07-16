# Ditch the IVR: How to Connect Real Phone Numbers to Nova Sonic 2 via WSS

*A case study built with Amazon Bedrock AgentCore, AgentDuet SDK, and Amazon Nova Sonic 2. Presented as part of the AgentDuet × AWS partnership.*

Calling a bank or insurer usually goes the same way: hold music, a menu that doesn't have your option, then repeating your policy number to three different people before someone can actually help. In finance and insurance, this is one of the fastest ways to lose a customer: when people call about a loan or a claim, they want an answer, not an interrogation by a robot. It's not a technology problem anymore; the AI to hold that conversation has existed for a while. It's a *plumbing* problem. Nobody wants to spend a quarter wiring SIP trunks, jitter buffers, and audio codecs together just to let a caller talk to a model.

This post shows you how to skip that plumbing entirely. We'll connect a real, ringable phone number straight into Amazon Nova Sonic 2 over a persistent WebSocket (WSS) connection, and then prove it works by building **FinAssist**, a phone agent for a fictional lender called SecureFinance that takes loan applications and insurance claims end to end, live, on a call.

No app to download. No IVR tree. The caller talks, the AI listens, a set of deterministic business rules decides the outcome, and the caller hears a real answer before they've even hung up.

That shift from "leave a message, we'll call you back" to "resolved on the first call" is where the actual business case lives:

- **Zero wait times**: handle thousands of concurrent calls instantly, without scaling up headcount
- **Lower cost per call**: routine intake work moves off human agents entirely
- **Higher conversion**: loan applications and claims get completed on the first touchpoint, not lost to a callback that never happens

## Why this is harder than it sounds

Real-time speech models like Nova Sonic 2 expect a clean, continuous, bidirectional audio stream: caller audio going in, generated speech coming back out, both while the caller can interrupt at any point. Getting a phone call into that shape usually means dealing with:

- Answering the call and negotiating a live audio session
- Buffering and re-sampling audio between telephony codecs and the model's expected sample rate
- Detecting when the caller barges in mid-sentence and clearing the AI's own audio buffer instantly, so it doesn't talk over them
- Keeping the whole thing alive over a persistent WSS connection without dropped packets or multi-second dead air

This is normally where teams lose months. **AgentDuet handles this layer out of the box.** It answers the inbound call, opens a low-latency WSS session, streams caller audio in at 16 kHz and Nova Sonic 2's generated speech back out at 24 kHz, and gives you a clean, pull-based audio interface so your agent code never has to touch telecom internals directly. AgentDuet is carrier-grade and ISO 27001, 27017, and 27018 certified, the kind of infrastructure a regulated team can actually put a real customer's voice through.

Amazon Bedrock AgentCore is the other half: it hosts the agent process itself, keeps the phone listener running continuously, and gives you HTTP endpoints for testing and deployment without you having to manage servers.

![WSS audio bridge between caller, AgentDuet, and Nova Sonic 2](images/wss-audio-bridge-flow.png)
*Caller audio streams in over a persistent WSS connection at 16 kHz; Nova Sonic 2's generated speech streams back at 24 kHz. Barge-in clears the buffer instantly so the agent never talks over the caller.*

## Architecture Overview

Here's how a single call actually flows through the system:

**Caller → AgentDuet SDK (answers the call, streams audio both ways) → Amazon Nova Sonic 2 (speech-to-speech, guided by business rules) → back to caller**

Two things happen alongside that live audio path, on every call:

- **Call transcripts → Amazon S3**: final USER/ASSISTANT turns only, written as structured JSON after each call, for audit and review
- **AgentCore Observability → Amazon CloudWatch**: OpenTelemetry traces and metrics, always on, so you can see latency, errors, and step-by-step session behavior

| Component | Role |
|---|---|
| **AgentDuet SDK** | Answers the inbound call and streams phone audio in and out over WSS |
| **Amazon Nova Sonic 2** | Converts speech to understanding and generates spoken replies, in one continuous voice session |
| **Amazon Bedrock AgentCore** | Hosts the agent process, keeps the phone listener running, exposes HTTP endpoints for testing and deployment |
| **Call transcripts (S3)** | Persists final USER/ASSISTANT text turns after each call for audit and review |
| **AgentCore Observability** | Emits OpenTelemetry traces and metrics to CloudWatch GenAI Observability for latency, errors, and session debugging |

Traces tell you *how the runtime behaved*. The transcript tells you *what was said*. Together, that's the difference between a demo and something a compliance team will actually sign off on.

## What You'll Need

- Node.js 20+ and the `@aws/agentcore` CLI
- Python 3.12+
- An AWS account with Bedrock access
- An AgentDuet API key and phone connector ID. **[Get a free developer key and instantly provision a test phone number at agentduet.com/aws-builder](https://www.agentduet.com/aws-builder?utm_source=aws_blog&utm_medium=referral&utm_campaign=finassist_post)**. It takes about two minutes and you'll have a real number to call before you finish reading this post.

## Building FinAssist

**Want to try it yourself right away?** The full source is on GitHub: **[github.com/harshal-ships/finassist-ai-agent](https://github.com/harshal-ships/finassist-ai-agent)**.

### 1. Give the agent a personality and a scope

The system prompt is where FinAssist's voice, opening line, and boundaries live, all in one file, fully decoupled from the audio bridge:

```python
SYSTEM_PROMPT = f"""
You are FinAssist, a professional AI phone agent for SecureFinance.
LOAN: Ask amount, purpose, annual income (one at a time).
CLAIM: Ask policy number, incident date, description (one at a time).
OPENING: "Hello! I'm FinAssist from SecureFinance. Are you looking
to apply for a loan, or file an insurance claim today?"
STYLE: Warm, concise, one question per turn. Never ask for SSN, passwords, or OTPs.
"""
```

Notice what's *not* in there: no request for a Social Security number, password, or one-time code, ever. That constraint is written into the prompt itself, not left to the model's judgment.

### 2. Keep the decisions deterministic

This is the part that makes FinAssist deployable rather than just a voice demo. The AI's job is to hold a natural conversation and collect information. It never decides the outcome; a small, testable Python function does:

```python
def evaluate_loan(*, amount, annual_income, purpose="") -> LoanDecision:
    if annual_income > 50_000 and amount < 20_000:
        return LoanDecision(status=PRE_APPROVED, action="Send digital contract")
    if annual_income < 30_000 or amount > 50_000:
        return LoanDecision(status=NEEDS_REVIEW, action="Schedule agent call")
    return LoanDecision(status=STANDARD_PROCESSING, action="Upload documents")
```

Every loan lands in one of three places: **Pre-Approved**, **Needs Review**, or **Standard Processing**, and claims route the same way based on urgency and policy tier. You can call and test this function directly over HTTP before a real phone ever rings, which matters a lot when a risk team asks "how do you know it won't hallucinate an approval?"

### 3. Bridge the call to Nova Sonic 2

This is the core of the WSS integration. For each inbound call, AgentDuet answers it, opens a Nova Sonic session, and streams audio in both directions concurrently: caller to AI, AI back to caller:

```python
async def bridge_call_to_nova(call: Call) -> None:
    nova = NovaSonicSession(build_call_system_prompt(), ...)
    await call.answer()
    await nova.prepare()

    async def stream_to_nova():
        async for chunk in call.caller.audio_stream():
            await nova.send_audio(chunk)

    async def receive_from_nova():
        async for data in nova.receive():
            if "audioOutput" in data.get("event", {}):
                audio = base64.b64decode(data["event"]["audioOutput"]["content"])
                await call.send_audio(audio)

    await asyncio.gather(stream_to_nova(), receive_from_nova())
```

Three details that matter more than they look:

- **Answer before you connect to Nova.** Otherwise the caller sits through dead ringing while your session spins up.
- **Let the caller interrupt.** Don't mute the mic while the AI is talking. Clearing the AI's audio buffer the instant the caller speaks is what makes this feel like a conversation instead of a script. AgentDuet exposes this as a single call: `await call.clear_send_audio_buffer()`.
- **Match sample rates.** Nova Sonic 2 expects 16 kHz coming in and generates 24 kHz going out; get this wrong and you'll spend a week debugging "garbled audio" that's actually just a resampling mismatch.

### 4. Capture the transcript

Every completed call leaves a structured, auditable record: the *final* USER and ASSISTANT turns only, not the model's speculative in-progress guesses:

```python
class TranscriptCollector:
    """Assemble turns from contentStart → textOutput → contentEnd.
    Persist FINAL assistant text only, skip SPECULATIVE previews."""
```

The transcript lands in S3 as `finassist/transcripts/YYYY/MM/DD/<call_id>_<timestamp>.json`, with the call ID, timing, every turn, and which Nova model and region served the call. If you leave the S3 bucket unset, upload is simply skipped, and the call still completes normally, which is handy for local demos.

Why this matters commercially: compliance teams don't accept "the model said something reasonable" as an answer. They want the actual words, stored outside the model, with a clear retention path. That's table stakes for finance and insurance, not a nice-to-have.

### 5. Turn on observability

Once deployed via the AgentCore CLI, your agent is instrumented with OpenTelemetry by default, and telemetry flows into Amazon CloudWatch GenAI Observability. Enable CloudWatch Transaction Search once per account/region, place a test call, and you can watch session count, latency, and step-level traces show up under Agents → Sessions → Traces.

![Mockup of a CloudWatch GenAI Observability session panel next to an S3 call transcript](images/observability-transcript-mock.png)
*Illustrative mockup, not an actual product screenshot. Swap in a real capture from your own CloudWatch GenAI Observability console once you have a live account.*

### 6. Run it, then deploy it

Locally:

```
cd finassist
agentcore dev
```

Test the business logic without a phone call:

```
agentcore dev '{"action": "evaluate_loan", "amount": 15000, "annual_income": 60000, "purpose": "home repair"}'
```

Then just call your AgentDuet number and you'll hear: *"Hello! I'm FinAssist from SecureFinance…"*

When you're ready for the cloud:

```
agentcore deploy
agentcore invoke '{"action": "status"}'
```

## Scaling to Production

Everything above gets you a working agent on one phone number. Running that at the volume a real call center needs (thousands of concurrent calls, SIP trunking that doesn't fall over, latency SLAs someone can actually be held to) is a different problem, and it's the one AgentDuet is built to solve as an AWS Partner. If you're past the prototype stage and thinking about a production rollout, **[talk to the AgentDuet architecture team](https://www.agentduet.com/contact?utm_source=aws_blog&utm_medium=referral&utm_campaign=finassist_post)** about deploying FinAssist-style agents at scale.

## Beyond the phone call

Voice is the front door, but it doesn't have to be the only one. FinAssist here is voice-only, but AgentDuet's session model isn't limited to phone calls; the same platform also connects agents to **WhatsApp** and **RCS/SMS**. Nothing shown in this post uses that today, but it's a natural next step: a caller who hangs up mid-application could get a WhatsApp follow-up to finish uploading documents, or a claim update could land as a text instead of a callback. Same agent, same business logic, different channel. Worth keeping in mind once the voice flow above is working end to end.

## Production Checklist

FinAssist is a reference implementation, but it's built the way a real financial agent has to behave in production:

- **No sensitive data collected**: never asks for a Social Security number, password, or one-time code
- **Narrow, stated scope**: handles loan and claim intake only, and says so when a request falls outside that
- **Deterministic outcomes**: every decision comes from tested code, not the model improvising in the moment
- **Tuned latency**: early builds had 5–7 second gaps between turns; the current bridge responds at a natural conversational pace
- **Auditable by default**: structured JSON transcripts to S3, OpenTelemetry traces to CloudWatch

## Next Steps

The pattern here generalizes well past loans and insurance: a phone number wired to Nova Sonic 2 over WSS, a system prompt, and a set of deterministic rules is enough to turn Bedrock AgentCore and AgentDuet into a real intake agent for almost any workflow: appointment scheduling, order status, claims, KYC, you name it.

To build your own version: **[create a free AgentDuet account](https://www.agentduet.com/aws-builder?utm_source=aws_blog&utm_medium=referral&utm_campaign=finassist_post)** to get a telephony credential and a test phone number, then clone the **[FinAssist repo from GitHub](https://github.com/harshal-ships/finassist-ai-agent)**, run `agentcore dev`, and call your new agent to hear it work.

---

*AgentDuet is an AWS Partner building low-latency voice infrastructure for generative AI, connecting real-world phone numbers, WhatsApp, and SMS to models like Amazon Nova Sonic 2 without the telecom plumbing. Learn more at [agentduet.com](https://www.agentduet.com?utm_source=aws_blog&utm_medium=referral&utm_campaign=finassist_post).*
