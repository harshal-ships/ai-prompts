"""
Timestamped call transcripts and LangFuse observability for dispute evidence.

LangFuse integration follows the official LangGraph + LangChain callback pattern:
  - propagate_attributes() for session_id / user_id / tags (set early, per call)
  - CallbackHandler per LangGraph invoke (nested under call span)
  - @observe() for triage turns with explicit span input/output
  - flush() before process exit and after each call

See .cursor/skills/langfuse/ for the LangFuse agent skill and best practices.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Literal

from langfuse.types import TraceContext

LOGGER = logging.getLogger("healthfirst_triage.observability")

Role = Literal["ASSISTANT", "PATIENT", "SYSTEM"]
CALL_RECORDS_DIR = Path(os.getenv("CALL_RECORDS_DIR", "call_records"))
LANGFUSE_TAGS = ("healthfirst-triage", "voice", "langgraph")


def _normalize_langfuse_env() -> None:
    """Support LANGFUSE_HOST alias; LangFuse SDK expects LANGFUSE_BASE_URL."""
    if not os.getenv("LANGFUSE_BASE_URL") and os.getenv("LANGFUSE_HOST"):
        os.environ["LANGFUSE_BASE_URL"] = os.environ["LANGFUSE_HOST"]


def langfuse_enabled() -> bool:
    _normalize_langfuse_env()
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def init_langfuse() -> bool:
    """Verify LangFuse credentials at startup. Returns True when authenticated."""
    if not langfuse_enabled():
        LOGGER.info("LangFuse not configured (set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)")
        return False
    try:
        from langfuse import get_client

        client = get_client()
        if client.auth_check():
            LOGGER.info("LangFuse authenticated (%s)", os.getenv("LANGFUSE_BASE_URL", "default host"))
            return True
        LOGGER.warning("LangFuse auth_check failed — verify keys and LANGFUSE_BASE_URL")
        return False
    except Exception:
        LOGGER.exception("LangFuse initialization failed")
        return False


def shutdown_langfuse() -> None:
    """Flush and shut down the LangFuse client (call before process exit)."""
    if not langfuse_enabled():
        return
    try:
        from langfuse import get_client

        client = get_client()
        client.flush()
        client.shutdown()
    except Exception:
        LOGGER.exception("LangFuse shutdown failed")


def caller_user_id(caller_number: str | None) -> str:
    """Stable LangFuse user_id from caller number (hashed for privacy in shared dashboards)."""
    if not caller_number:
        return "unknown-caller"
    digest = hashlib.sha256(caller_number.encode()).hexdigest()[:16]
    return f"caller-{digest}"


def format_elapsed(seconds: float) -> str:
    """Format elapsed call time as HH:MM:SS (e.g. 00:04:25)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TranscriptTurn:
    timestamp: str
    role: Role
    text: str
    source: str
    recorded_at: str


@dataclass
class CallTranscriptRecorder:
    """Append-only, timestamped transcript for a single phone call."""

    call_id: str
    caller_number: str | None
    started_at: datetime = field(default_factory=_utc_now)
    turns: list[TranscriptTurn] = field(default_factory=list)
    triage_timeline: list[dict[str, Any]] = field(default_factory=list)
    langfuse_trace_ids: list[str] = field(default_factory=list)
    ended_at: datetime | None = None

    def elapsed_seconds(self) -> float:
        end = self.ended_at or _utc_now()
        return max(0.0, (end - self.started_at).total_seconds())

    def append_turn(self, role: Role, text: str, source: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self.turns.append(
            TranscriptTurn(
                timestamp=format_elapsed(self.elapsed_seconds()),
                role=role,
                text=cleaned,
                source=source,
                recorded_at=_utc_now().replace(microsecond=0).isoformat(),
            )
        )

    def append_triage_decision(
        self,
        state: dict[str, Any],
        patient_transcript: str,
        langfuse_trace_id: str | None = None,
    ) -> None:
        """Record a LangGraph triage snapshot aligned to the patient turn that triggered it."""
        symptoms = state.get("symptoms") or {}
        entry = {
            "timestamp": format_elapsed(self.elapsed_seconds()),
            "recorded_at": _utc_now().replace(microsecond=0).isoformat(),
            "patient_transcript": patient_transcript,
            "urgency_level": state.get("urgency_level"),
            "routing_decision": state.get("routing_decision"),
            "action_taken": state.get("action_taken"),
            "aria_instruction": state.get("aria_instruction"),
            "symptoms": symptoms,
            "langfuse_trace_id": langfuse_trace_id,
        }
        self.triage_timeline.append(entry)
        if langfuse_trace_id and langfuse_trace_id not in self.langfuse_trace_ids:
            self.langfuse_trace_ids.append(langfuse_trace_id)

        summary = (
            f"LangGraph triage: urgency={state.get('urgency_level')} "
            f"route={state.get('routing_decision')} action={state.get('action_taken')}"
        )
        self.append_turn("SYSTEM", summary, "langgraph")

    def render_transcript_txt(self) -> str:
        lines = [
            f"Call ID: {self.call_id}",
            f"Caller: {self.caller_number or 'unknown'}",
            f"Started: {self.started_at.replace(microsecond=0).isoformat()}",
            f"Duration: {format_elapsed(self.elapsed_seconds())}",
            "",
            "--- Transcript (timestamp = elapsed from call start) ---",
            "",
        ]
        for turn in self.turns:
            lines.append(f"[{turn.timestamp}] {turn.role}: {turn.text}")
        return "\n".join(lines) + "\n"

    def finalize(self, langfuse_host: str | None = None) -> Path:
        """Write the dispute-evidence bundle to disk."""
        self.ended_at = _utc_now()
        out_dir = CALL_RECORDS_DIR / self.call_id
        out_dir.mkdir(parents=True, exist_ok=True)

        transcript_json = {
            "call_id": self.call_id,
            "caller_number": self.caller_number,
            "started_at": self.started_at.replace(microsecond=0).isoformat(),
            "ended_at": self.ended_at.replace(microsecond=0).isoformat(),
            "duration": format_elapsed(self.elapsed_seconds()),
            "duration_seconds": int(self.elapsed_seconds()),
            "turns": [
                {
                    "timestamp": turn.timestamp,
                    "role": turn.role,
                    "text": turn.text,
                    "source": turn.source,
                    "recorded_at": turn.recorded_at,
                }
                for turn in self.turns
            ],
        }

        (out_dir / "transcript.json").write_text(
            json.dumps(transcript_json, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out_dir / "transcript.txt").write_text(self.render_transcript_txt(), encoding="utf-8")
        (out_dir / "triage_timeline.json").write_text(
            json.dumps(self.triage_timeline, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        base_url = langfuse_host or os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        manifest = {
            "call_id": self.call_id,
            "caller_number": self.caller_number,
            "started_at": self.started_at.replace(microsecond=0).isoformat(),
            "ended_at": self.ended_at.replace(microsecond=0).isoformat(),
            "duration": format_elapsed(self.elapsed_seconds()),
            "langfuse_session_id": self.call_id,
            "langfuse_trace_ids": self.langfuse_trace_ids,
            "purpose": (
                "Dispute evidence bundle: timestamped call transcript plus "
                "LangGraph triage decision timeline. Pair with Telcoflow call "
                "recordings when available."
            ),
            "artifacts": {
                "transcript_json": "transcript.json",
                "transcript_txt": "transcript.txt",
                "triage_timeline_json": "triage_timeline.json",
            },
            "final_triage": self.triage_timeline[-1] if self.triage_timeline else None,
            "langfuse": {
                "enabled": langfuse_enabled(),
                "session_id": self.call_id,
                "host": base_url,
                "sessions_url_hint": f"{base_url.rstrip('/')}/project/{{project_id}}/sessions/{self.call_id}",
            },
        }
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        LOGGER.info(
            "Call evidence bundle written: %s (%d turns, %s, %d langfuse traces)",
            out_dir,
            len(self.turns),
            format_elapsed(self.elapsed_seconds()),
            len(self.langfuse_trace_ids),
        )
        return out_dir


def _triage_span_output(state: dict[str, Any]) -> dict[str, Any]:
    """Trace output: routing decision only — not full patient record."""
    symptoms = state.get("symptoms") or {}
    return {
        "urgency_level": state.get("urgency_level"),
        "routing_decision": state.get("routing_decision"),
        "action_taken": state.get("action_taken"),
        "aria_instruction": state.get("aria_instruction"),
        "symptoms_summary": {
            "patient_name": symptoms.get("patient_name"),
            "age": symptoms.get("age"),
            "main_symptom": symptoms.get("main_symptom"),
            "severity": symptoms.get("severity"),
        },
    }


def run_triage_turn(
    graph: Any,
    *,
    transcript: str,
    thread_id: str,
    call_id: str,
    caller_number: str | None,
    parent_trace_context: TraceContext | None = None,
) -> tuple[dict[str, Any], str | None]:
    """
    Invoke LangGraph with LangFuse tracing (CallbackHandler + propagate_attributes).

    When parent_trace_context is set, the triage turn nests under the active call span
    (required when invoking from asyncio.to_thread, which drops OTEL context).

    Returns (state, langfuse_trace_id).
    """
    from langfuse import get_client, observe, propagate_attributes
    from langfuse.langchain import CallbackHandler

    @observe(name="langgraph-triage-turn")
    def _invoke() -> tuple[dict[str, Any], str | None]:
        client = get_client()
        client.update_current_span(input={"patient_transcript": transcript})

        with propagate_attributes(
            session_id=call_id,
            user_id=caller_user_id(caller_number),
            tags=list(LANGFUSE_TAGS),
            metadata={"call_id": call_id, "feature": "voice-triage"},
        ):
            handler = CallbackHandler()
            config: dict[str, Any] = {
                "configurable": {"thread_id": thread_id},
                "callbacks": [handler],
                "run_name": "langgraph-triage-turn",
                "metadata": {
                    "langfuse_session_id": call_id,
                    "langfuse_user_id": caller_user_id(caller_number),
                    "langfuse_tags": list(LANGFUSE_TAGS),
                },
            }
            state = graph.invoke(
                {"latest_transcript": transcript, "transcript_history": [transcript]},
                config,
            )
            client.update_current_span(output=_triage_span_output(state))
            return state, handler.last_trace_id

    invoke_kwargs: dict[str, str] = {}
    if parent_trace_context:
        invoke_kwargs["langfuse_trace_id"] = parent_trace_context["trace_id"]
        parent_span_id = parent_trace_context.get("parent_span_id")
        if parent_span_id:
            invoke_kwargs["langfuse_parent_observation_id"] = parent_span_id

    return _invoke(**invoke_kwargs)


class CallObservability:
    """Coordinates timestamped transcripts and LangFuse tracing for one phone call."""

    def __init__(self, call_id: str, caller_number: str | None) -> None:
        self.call_id = call_id
        self.caller_number = caller_number
        self.transcript = CallTranscriptRecorder(call_id=call_id, caller_number=caller_number)
        self._langfuse_client: Any | None = None
        self._call_span: Any | None = None
        self._finalized = False

        if langfuse_enabled():
            try:
                from langfuse import get_client

                self._langfuse_client = get_client()
            except Exception:
                LOGGER.exception("Failed to initialize LangFuse client for call %s", call_id)
                self._langfuse_client = None

    @contextmanager
    def call_trace(self) -> Generator[CallObservability, None, None]:
        """
        Open a call-level LangFuse span with session/user/tags propagated to all child traces.

        Enter this at call answer; exit at hangup (before flush).
        """
        if self._langfuse_client is None:
            yield self
            return

        from langfuse import propagate_attributes

        with propagate_attributes(
            session_id=self.call_id,
            user_id=caller_user_id(self.caller_number),
            tags=list(LANGFUSE_TAGS),
            trace_name=f"healthfirst-call-{self.call_id}",
            metadata={"call_id": self.call_id, "clinic": CLINIC_NAME_SLUG},
        ):
            with self._langfuse_client.start_as_current_observation(
                as_type="span",
                name="healthfirst-voice-call",
                input={"call_id": self.call_id, "caller_number": self.caller_number},
            ) as span:
                self._call_span = span
                try:
                    yield self
                finally:
                    span.update(
                        output={
                            "turn_count": len(self.transcript.turns),
                            "triage_decisions": len(self.transcript.triage_timeline),
                            "final_routing": (
                                self.transcript.triage_timeline[-1].get("routing_decision")
                                if self.transcript.triage_timeline
                                else None
                            ),
                        }
                    )
                    self._call_span = None

    def langfuse_trace_context(self) -> TraceContext | None:
        """Return call-span trace context for nesting child spans from worker threads."""
        if self._call_span is None:
            return None
        return {
            "trace_id": self._call_span.trace_id,
            "parent_span_id": self._call_span.id,
        }

    def run_triage(
        self,
        graph: Any,
        transcript: str,
        thread_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Run LangGraph for one patient turn with LangFuse nested tracing."""
        if not langfuse_enabled():
            state = graph.invoke(
                {"latest_transcript": transcript, "transcript_history": [transcript]},
                {"configurable": {"thread_id": thread_id}},
            )
            return state, None
        return run_triage_turn(
            graph,
            transcript=transcript,
            thread_id=thread_id,
            call_id=self.call_id,
            caller_number=self.caller_number,
            parent_trace_context=self.langfuse_trace_context(),
        )

    def flush(self) -> None:
        if self._langfuse_client is not None:
            try:
                self._langfuse_client.flush()
            except Exception:
                LOGGER.exception("LangFuse flush failed for call %s", self.call_id)

    def finalize_once(self) -> Path | None:
        """Write the evidence bundle once per call (safe to call from multiple shutdown paths)."""
        if self._finalized:
            return None
        self._finalized = True
        return self.finalize()

    def finalize(self) -> Path:
        self.flush()
        host = os.getenv("LANGFUSE_BASE_URL") if langfuse_enabled() else None
        return self.transcript.finalize(langfuse_host=host)


CLINIC_NAME_SLUG = "healthfirst-clinic"
