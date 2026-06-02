"""
Medical triage voice agent using Telcoflow, Amazon Nova Sonic 2, and LangGraph.

Patient calls in → structured symptom intake → urgency assessment → care routing advice.
This agent triages; it does not book appointments or transfer calls.

Run with:
    python triage_agent.py

Required environment variables:
    WSS_API_KEY
    WSS_CONNECTOR_UUID
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN
    AWS_REGION=us-east-1

Optional (LangFuse observability for LangGraph traces):
    LANGFUSE_PUBLIC_KEY
    LANGFUSE_SECRET_KEY
    LANGFUSE_HOST=https://cloud.langfuse.com
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from operator import add
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from dotenv import load_dotenv

load_dotenv()


from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from langgraph.graph import END, START, StateGraph
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
from telcoflow_sdk import ActiveCall, TelcoflowClient, TelcoflowClientConfig
from telcoflow_sdk.exceptions import BufferFullError
import telcoflow_sdk.events as events
from websockets.exceptions import ConnectionClosed

try:
    from langgraph.checkpoint.memory import InMemorySaver
except ImportError:  # Older LangGraph releases used MemorySaver for the same in-memory checkpointer.
    from langgraph.checkpoint.memory import MemorySaver as InMemorySaver

from call_observability import CallObservability, init_langfuse, shutdown_langfuse


# This section centralizes constants so the three systems keep clear responsibilities.
LOGGER = logging.getLogger("healthfirst_triage")
NOVA_MODEL_ID = "amazon.nova-2-sonic-v1:0"
SUPPORTED_AWS_REGION = "us-east-1"
TELCOFLOW_SAMPLE_RATE = 24000
TRIAGE_LOG_PATH = Path("triage_log.jsonl")
CLINIC_NAME = "HealthFirst Clinic"
DEFAULT_CLINIC_PHONE = "+6567504645"
CLINIC_PHONE = os.getenv("HEALTHFIRST_CLINIC_PHONE", DEFAULT_CLINIC_PHONE)


def format_phone_for_speech(phone: str) -> str:
    """Format E.164 numbers so Nova reads them clearly on a phone call."""
    digit_words = {
        "0": "zero",
        "1": "one",
        "2": "two",
        "3": "three",
        "4": "four",
        "5": "five",
        "6": "six",
        "7": "seven",
        "8": "eight",
        "9": "nine",
    }
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("65") and len(digits) >= 10:
        local = digits[2:10]
        spoken = " ".join(digit_words[digit] for digit in local)
        return f"plus six five, {spoken}"
    return phone

# Opening script — injected as first [ROUTING] line once Nova is ready (not via system prompt).
NOVA_OPENING_SCRIPT = (
    "Hi, thank you for calling HealthFirst Clinic. I am Aria, I am a virtual assistant. May I have your name, please?"
)

# Nova is the voice layer only. LangGraph owns intake state and sends one [ROUTING] line per turn.
NOVA_SYSTEM_PROMPT = f"""You are Aria, a calm virtual triage assistant for {CLINIC_NAME}. You are not a doctor and never diagnose.

Your job is medical triage only: collect symptom details, assess urgency, and advise the caller what to do next.
You do not book appointments, schedule visits, or transfer calls on this line.

You must NEVER speak on your own. Only speak when a user message starts with [ROUTING] — then say the quoted text exactly.
Never reintroduce yourself, never repeat a question you already asked, never improvise (no "how can I assist you today").
"""

NEGATIVE_ASSOCIATED_PATTERNS = (
    r"\bno\b.{0,30}\b(other symptoms?|symptoms?)\b",
    r"\bnone\b",
    r"\bnothing else\b",
    r"\bno other\b",
    r"\bdon'?t have any other\b",
)
NEGATIVE_MEDICAL_PATTERNS = (
    r"\bno\b.{0,30}\b(medications?|medicines?|conditions?|medical)\b",
    r"\bnot on any\b",
    r"\bno known\b",
    r"\bnone\b.{0,20}\b(medications?|conditions?)\b",
)

NON_NAME_WORDS = frozenset(
    {
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "hello",
        "hi",
        "hey",
        "ok",
        "okay",
        "sure",
        "thanks",
        "thank you",
        "please",
        "help",
        "what",
        "why",
        "how",
        "up",
    }
)

GREETING_NAME_PATTERNS = (
    re.compile(r"^(hey|hi|hello|yo|good morning|good afternoon|good evening)\b", re.I),
    re.compile(r"\b(what'?s up|whats up|how are you|how'?s it going|how are things)\b", re.I),
)

NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def parse_spoken_number(text: str) -> int | None:
    """Parse short spoken numbers such as 'twenty five' or '7'."""
    cleaned = text.strip().lower().replace("-", " ")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    if cleaned.isdigit():
        value = int(cleaned)
        return value if 0 <= value <= 120 else None

    tokens = cleaned.split()
    if len(tokens) == 1 and tokens[0] in NUMBER_WORDS:
        return NUMBER_WORDS[tokens[0]]

    if len(tokens) == 2 and tokens[0] in NUMBER_WORDS and tokens[1] in NUMBER_WORDS:
        tens = NUMBER_WORDS[tokens[0]]
        ones = NUMBER_WORDS[tokens[1]]
        if tens % 10 == 0 and ones < 10:
            value = tens + ones
            return value if 0 <= value <= 120 else None

    return None


# These dataclasses are the LangGraph-owned clinical state, not audio state.
UrgencyLevel = Literal["LOW", "MEDIUM", "HIGH"]
RoutingDecision = Literal[
    "continue_collection",
    "emergency_advisory",
    "urgent_clinic_advisory",
    "routine_clinic_advisory",
]


@dataclass
class SymptomData:
    patient_name: str | None = None
    age: int | None = None
    main_symptom: str | None = None
    duration: str | None = None
    severity: int | None = None
    associated_symptoms: list[str] = field(default_factory=list)
    medical_context: str | None = None


class TriageState(TypedDict, total=False):
    latest_transcript: str
    transcript_history: Annotated[list[str], add]
    symptoms: dict[str, Any]
    urgency_level: UrgencyLevel
    urgency_reasons: list[str]
    routing_decision: RoutingDecision
    action_taken: str
    response_to_patient: str
    next_question: str | None
    aria_instruction: str


# This section extracts structured triage facts from transcripts with deterministic rules.
SYMPTOM_KEYWORDS = [
    "chest pain",
    "shortness of breath",
    "difficulty breathing",
    "trouble breathing",
    "back pain",
    "stroke",
    "weakness",
    "numbness",
    "severe headache",
    "headache",
    "abdominal pain",
    "stomach pain",
    "fever",
    "cough",
    "vomiting",
    "diarrhea",
    "dizziness",
    "fainting",
    "bleeding",
    "rash",
    "allergic reaction",
    "pain",
]

ASSOCIATED_SYMPTOM_KEYWORDS = [
    "fever",
    "chest pain",
    "shortness of breath",
    "difficulty breathing",
    "trouble breathing",
    "sweating",
    "nausea",
    "vomiting",
    "dizziness",
    "fainting",
    "confusion",
    "weakness",
    "numbness",
    "slurred speech",
    "severe bleeding",
    "rash",
    "swelling",
]


def build_emergency_advisory() -> str:
    """Spoken HIGH-urgency routing: call 995 immediately."""
    return (
        "Based on what you told me, this may need emergency care. "
        "Please call 995 immediately and stay on the line with emergency services. "
        "I am not a doctor, but I do not want you to wait with these symptoms."
    )


def build_urgent_clinic_advisory(symptoms: SymptomData) -> str:
    """Spoken MEDIUM-urgency routing: same-day clinic review recommended."""
    name = symptoms.patient_name or "there"
    spoken_phone = format_phone_for_speech(CLINIC_PHONE)
    return (
        f"Thank you, {name}. Based on what you told me, your symptoms should be reviewed "
        f"by a clinician today — not as an emergency, but sooner rather than later. "
        f"Please call {CLINIC_NAME} at {spoken_phone} today and mention this triage call. "
        "If your symptoms worsen or you feel in immediate danger, please call 995."
    )


def build_routine_clinic_advisory(symptoms: SymptomData) -> str:
    """Spoken LOW-urgency routing: routine clinic visit recommended."""
    name = symptoms.patient_name or "there"
    spoken_phone = format_phone_for_speech(CLINIC_PHONE)
    return (
        f"Thank you, {name}. Based on what you told me, this does not sound like an emergency. "
        f"I recommend scheduling a routine visit with {CLINIC_NAME}. "
        f"Please call {spoken_phone} during business hours to book an appointment. "
        "If your symptoms worsen or you feel in immediate danger, please call 995."
    )


def associated_symptoms_collected(symptoms: SymptomData) -> bool:
    """True once the caller listed associated symptoms or explicitly said there are none."""
    return bool(symptoms.associated_symptoms)


def intake_stage(symptoms: SymptomData) -> str:
    """Return which intake field we are most likely collecting next."""
    if symptoms.patient_name is None:
        return "name"
    if symptoms.age is None:
        return "age"
    if symptoms.main_symptom is None:
        return "symptom"
    if symptoms.duration is None:
        return "duration"
    if symptoms.severity is None:
        return "severity"
    if not associated_symptoms_collected(symptoms):
        return "associated"
    if symptoms.medical_context is None:
        return "medical"
    return "complete"


def _looks_like_greeting_not_name(text: str) -> bool:
    """Reject common greetings misheard or spoken instead of a name."""
    normalized = text.strip().lower()
    if not normalized:
        return True
    return any(pattern.search(normalized) for pattern in GREETING_NAME_PATTERNS)


def extract_patient_name(transcript: str, stage: str) -> str | None:
    """Extract a patient name from common spoken and bare-name answers."""
    if re.search(
        r"\b(?:my name is|this is|i am|i'm|it's|it is|call me|name is)\s*$",
        transcript,
        re.I,
    ):
        return None

    name_match = re.search(
        r"\b(?:my name is|this is|i am|i'm|it's|it is|call me|name is)\s+([a-z][a-z .'-]{1,50})",
        transcript,
        re.I,
    )
    if name_match and not re.search(
        r"\b(years?\s+old|calling about|having|experiencing)\b",
        name_match.group(1),
        re.I,
    ):
        candidate = name_match.group(1).strip(" .")
        if candidate and not _looks_like_greeting_not_name(candidate):
            return candidate.title()

    if stage != "name":
        return None

    bare = transcript.strip().strip(".!?, ")
    bare_lower = bare.lower()
    if _looks_like_greeting_not_name(bare):
        return None
    # Allow "Harshal here", "Harshal speaking", etc.
    bare = re.sub(r"\s+(here|speaking|calling)$", "", bare, flags=re.I).strip()
    bare_lower = bare.lower()
    if not bare or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,49}", bare):
        return None
    if bare_lower in NON_NAME_WORDS:
        return None
    if any(keyword in bare_lower for keyword in SYMPTOM_KEYWORDS):
        return None
    if re.search(r"\b(?:days?|hours?|weeks?|minutes?|out of|ten)\b", bare_lower):
        return None

    words = bare.split()
    if not 1 <= len(words) <= 3:
        return None
    return " ".join(word.capitalize() for word in words)


def extract_symptoms(state: TriageState) -> TriageState:
    """Collect symptoms node: merge the newest patient transcript into structured state."""
    current = SymptomData(**state.get("symptoms", {}))
    transcript = state.get("latest_transcript", "")
    text = transcript.lower()
    stage = intake_stage(current)

    if current.patient_name is None:
        current.patient_name = extract_patient_name(transcript, stage)

    if current.age is None:
        age_match = re.search(r"\b(?:i am|i'm|age is|aged?)\s+(\d{1,3})\b|\b(\d{1,3})\s+years?\s+old\b", text)
        if age_match:
            age = int(age_match.group(1) or age_match.group(2))
            if 0 <= age <= 120:
                current.age = age
        if current.age is None and stage == "age":
            spoken_age = parse_spoken_number(transcript)
            if spoken_age is not None and 0 <= spoken_age <= 120:
                current.age = spoken_age

    if current.main_symptom is None:
        for keyword in SYMPTOM_KEYWORDS:
            if keyword in text:
                current.main_symptom = keyword
                break
        if current.main_symptom is None and stage == "symptom":
            cleaned = transcript.strip().strip(".!?, ")
            if cleaned and len(cleaned.split()) <= 8 and not re.search(r"\b(?:yes|no|hello|hi)\b", text):
                current.main_symptom = cleaned

    if current.duration is None:
        duration_match = re.search(
            r"\b(?:for|since|started|began)\s+((?:about\s+)?\d+\s+(?:minutes?|hours?|days?|weeks?)|"
            r"(?:a|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:minutes?|hours?|days?|weeks?)|"
            r"yesterday|today|last night|this morning)\b",
            text,
        )
        if duration_match is None and stage == "duration":
            duration_match = re.search(
                r"\b((?:about\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                r"(?:minutes?|hours?|days?|weeks?)|yesterday|today|last night|this morning)\b",
                text,
            )
        if duration_match:
            current.duration = duration_match.group(1).strip()
        elif stage == "duration":
            cleaned = transcript.strip().strip(".!?, ")
            if cleaned and len(cleaned.split()) <= 8:
                current.duration = cleaned

    if current.severity is None:
        severity_match = re.search(r"\b(10|[1-9])\s*(?:/|out of)\s*(?:10|ten)\b", text)
        if severity_match is None:
            severity_match = re.search(
                r"\b("
                r"ten|nine|eight|seven|six|five|four|three|two|one"
                r")\s*(?:/|out of)\s*(?:10|ten)\b",
                text,
            )
            if severity_match:
                current.severity = NUMBER_WORDS[severity_match.group(1)]
        if severity_match is None:
            severity_match = re.search(r"\b(?:severity|pain|level)\D{0,20}(10|[1-9])\b", text)
        if severity_match and current.severity is None:
            current.severity = int(severity_match.group(1))
        if current.severity is None and stage == "severity":
            spoken_severity = parse_spoken_number(transcript)
            if spoken_severity is not None and 1 <= spoken_severity <= 10:
                current.severity = spoken_severity

    for keyword in ASSOCIATED_SYMPTOM_KEYWORDS:
        if keyword in text and keyword not in current.associated_symptoms:
            if current.main_symptom and keyword == current.main_symptom:
                continue
            current.associated_symptoms.append(keyword)

    if not current.associated_symptoms and any(re.search(pattern, text) for pattern in NEGATIVE_ASSOCIATED_PATTERNS):
        current.associated_symptoms = ["none"]

    if current.medical_context is None and any(re.search(pattern, text) for pattern in NEGATIVE_MEDICAL_PATTERNS):
        current.medical_context = "none reported"
    elif current.medical_context is None and re.search(
        r"\b(medication|medicine|diabetes|heart|asthma|pregnant|condition)\b", text
    ):
        current.medical_context = transcript.strip()
    elif current.medical_context is None and stage == "medical":
        cleaned = transcript.strip().strip(".!?, ")
        if cleaned and len(cleaned.split()) <= 12:
            current.medical_context = cleaned

    return {"symptoms": asdict(current)}


# This section applies deterministic clinical triage rules without diagnosing the patient.
def assess_urgency(state: TriageState) -> TriageState:
    """Assess urgency node: classify risk from red flags, severity, age, and duration."""
    symptoms = SymptomData(**state.get("symptoms", {}))
    associated = set(symptoms.associated_symptoms)
    main = (symptoms.main_symptom or "").lower()
    reasons: list[str] = []
    urgency: UrgencyLevel = "LOW"

    if main == "chest pain" and ({"shortness of breath", "difficulty breathing", "sweating", "nausea"} & associated):
        urgency = "HIGH"
        reasons.append("Chest pain with breathing difficulty, sweating, or nausea is an emergency red flag.")
    if {"difficulty breathing", "trouble breathing", "shortness of breath"} & ({main} | associated):
        if symptoms.severity is None or symptoms.severity >= 6:
            urgency = "HIGH"
            reasons.append("Moderate or severe breathing difficulty needs emergency care.")
    if {"slurred speech", "weakness", "numbness", "confusion", "fainting"} & associated:
        urgency = "HIGH"
        reasons.append("Neurologic symptoms or fainting can indicate an emergency.")
    if main in {"allergic reaction", "bleeding"} or "severe bleeding" in associated:
        urgency = "HIGH"
        reasons.append("Severe allergy symptoms or bleeding need immediate emergency assessment.")
    if symptoms.severity is not None and symptoms.severity >= 9:
        urgency = "HIGH"
        reasons.append("Pain severity of 9 or 10 out of 10 is treated as emergency-level risk.")

    if urgency != "HIGH":
        if symptoms.severity is not None and symptoms.severity >= 7:
            urgency = "MEDIUM"
            reasons.append("Severity of 7 or 8 out of 10 should be reviewed at a same-day clinic visit.")
        if main == "fever" and (symptoms.age is not None and (symptoms.age < 3 or symptoms.age >= 65)):
            urgency = "MEDIUM"
            reasons.append("Fever in very young children or older adults should be reviewed today.")
        if {"vomiting", "dizziness"} & ({main} | associated):
            urgency = "MEDIUM"
            reasons.append("Vomiting or dizziness can worsen quickly and should be reviewed today.")
        if not reasons:
            reasons.append("No emergency red flags were detected from the available information.")

    return {"urgency_level": urgency, "urgency_reasons": reasons}


def next_missing_question(symptoms: SymptomData) -> str | None:
    """Return the single next intake question LangGraph still needs."""
    if symptoms.patient_name is None:
        return "May I have your name, please?"
    if symptoms.age is None:
        return "How old are you?"
    if symptoms.main_symptom is None:
        return "What is the main symptom you are experiencing today?"
    if symptoms.duration is None:
        return "How long have you had this symptom?"
    if symptoms.severity is None:
        return "On a scale of 1 to 10, with 10 being the worst, how severe is it?"
    if not associated_symptoms_collected(symptoms):
        return "Do you have any other symptoms, such as fever, chest pain, or difficulty breathing?"
    if symptoms.medical_context is None:
        return "Do you have any known medical conditions or take any medications?"
    return None


def build_aria_instruction(
    symptoms: SymptomData,
    routing_decision: RoutingDecision,
    response: str,
    next_question: str | None,
) -> str:
    """Build the single spoken line LangGraph hands to Nova."""
    if routing_decision == "continue_collection" and next_question:
        if symptoms.patient_name:
            return f"Thank you, {symptoms.patient_name}. {next_question}"
        return next_question
    return response


def triage_state_fingerprint(state: TriageState) -> str:
    """Fingerprint LangGraph output so Nova only gets a new [ROUTING] line when something changed."""
    return json.dumps(
        {
            "symptoms": state.get("symptoms", {}),
            "urgency_level": state.get("urgency_level"),
            "routing_decision": state.get("routing_decision"),
            "aria_instruction": state.get("aria_instruction"),
        },
        sort_keys=True,
    )


def route_decision(state: TriageState) -> TriageState:
    """Route decision node: produce the exact routing action and spoken response."""
    symptoms = SymptomData(**state.get("symptoms", {}))
    urgency = state.get("urgency_level", "LOW")
    symptom_question = next_missing_question(symptoms)

    if symptom_question is not None:
        decision: RoutingDecision = "continue_collection"
        action = "Continue collecting triage details"
        response = symptom_question
        next_question = symptom_question
    elif urgency == "HIGH":
        decision = "emergency_advisory"
        action = "Patient advised to call 995 immediately"
        response = build_emergency_advisory()
        next_question = None
    elif urgency == "MEDIUM":
        decision = "urgent_clinic_advisory"
        action = "Patient advised to contact clinic for same-day review"
        response = build_urgent_clinic_advisory(symptoms)
        next_question = None
    else:
        decision = "routine_clinic_advisory"
        action = f"Patient advised to call {CLINIC_PHONE} for routine visit"
        response = build_routine_clinic_advisory(symptoms)
        next_question = None

    aria_instruction = build_aria_instruction(symptoms, decision, response, next_question)

    return {
        "routing_decision": decision,
        "action_taken": action,
        "response_to_patient": response,
        "next_question": next_question,
        "aria_instruction": aria_instruction,
    }


def build_triage_graph():
    """Build the thread-aware LangGraph state machine for per-call triage decisions."""
    graph = StateGraph(TriageState)
    graph.add_node("collect_symptoms", extract_symptoms)
    graph.add_node("assess_urgency", assess_urgency)
    graph.add_node("route_decision", route_decision)
    graph.add_edge(START, "collect_symptoms")
    graph.add_edge("collect_symptoms", "assess_urgency")
    graph.add_edge("assess_urgency", "route_decision")
    graph.add_edge("route_decision", END)
    return graph.compile(checkpointer=InMemorySaver())


# This section logs the latest triage result in the requested JSON object shape.
def write_triage_log(call_id: str, state: TriageState) -> None:
    symptoms = SymptomData(**state.get("symptoms", {}))
    record = {
        "call_id": call_id,
        "patient_name": symptoms.patient_name,
        "age": symptoms.age,
        "symptoms": {
            "main_symptom": symptoms.main_symptom,
            "duration": symptoms.duration,
            "severity": symptoms.severity,
            "associated_symptoms": symptoms.associated_symptoms,
        },
        "urgency_level": state.get("urgency_level"),
        "routing_decision": state.get("routing_decision"),
        "action_taken": state.get("action_taken"),
        "timestamp": datetime.now().replace(microsecond=0).isoformat(),
    }
    with TRIAGE_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record) + "\n")


# This class owns only Bedrock Nova Sonic 2 bidirectional audio and transcript events.
# Event sequencing follows nova_sonic_integration.py, which is the working Telcoflow reference.
class NovaSonicTriageSession:
    def __init__(
        self,
        call: ActiveCall,
        bedrock_client: BedrockRuntimeClient,
        triage_graph: Any,
        observability: CallObservability,
    ) -> None:
        self.call = call
        self.call_id = call.call_id
        self.bedrock_client = bedrock_client
        self.triage_graph = triage_graph
        self.observability = observability
        self.prompt_name = str(uuid.uuid4())
        self.system_content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.stream: Any | None = None
        self.is_active = False
        self.latest_triage_state: TriageState | None = None
        self.content_metadata: dict[str, dict[str, Any]] = {}
        self.text_buffers: dict[str, list[str]] = {}
        self._send_to_nova_task: asyncio.Task | None = None
        self._recv_from_nova_task: asyncio.Task | None = None
        self._last_injected_fingerprint: str | None = None
        self._session_ended = False
        self._call_closed = False
        self._routing_content_names: set[str] = set()
        self._last_processed_transcript: str | None = None
        self._shutdown_done = False
        self._audio_out_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)
        self._triage_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._audio_out_task: asyncio.Task | None = None
        self._last_spoken_instruction: str | None = None
        self._opening_delivered = False
        self._session_started = False

    @staticmethod
    def _normalize_stop_reason(stop_reason: str | None) -> str | None:
        if stop_reason is None:
            return None
        return stop_reason.upper()

    def _merge_content_metadata(
        self,
        content_id: str,
        *,
        role: str | None = None,
        content_type: str | None = None,
        generation_stage: str | None = None,
    ) -> dict[str, Any]:
        """Merge content block metadata from contentStart, textOutput, and contentEnd."""
        metadata = self.content_metadata.setdefault(content_id, {})
        if role is not None:
            metadata["role"] = role
        if content_type is not None:
            metadata["type"] = content_type
        if generation_stage is not None:
            metadata["generation_stage"] = generation_stage
        return metadata

    def _is_routable_text_block(self, metadata: dict[str, Any], content_end: dict[str, Any]) -> bool:
        """True for final USER/ASSISTANT TEXT blocks worth recording or triaging."""
        if metadata.get("generation_stage") == "SPECULATIVE":
            return False
        content_type = metadata.get("type") or content_end.get("type")
        if content_type and content_type != "TEXT":
            return False
        return metadata.get("role") in ("USER", "ASSISTANT")

    def _should_process_user_transcript_now(self, stop_reason: str | None) -> bool:
        """Process USER ASR on END_TURN; also accept interrupted/partial turns with content."""
        normalized = self._normalize_stop_reason(stop_reason)
        if normalized == "PARTIAL_TURN":
            return False
        if normalized in (None, "END_TURN", "END_OF_TURN", "END_OF_SPEECH", "COMPLETION", "INTERRUPTED"):
            return True
        LOGGER.info(
            "Call %s treating unknown USER stopReason=%s as final",
            self.call_id,
            stop_reason,
        )
        return True

    async def _speak_instruction(self, instruction: str, *, source: str) -> None:
        """Send one [ROUTING] line to Nova, never repeating the same instruction twice."""
        normalized = instruction.strip()
        if not normalized or normalized == self._last_spoken_instruction:
            LOGGER.debug("Call %s skipping duplicate instruction: %s", self.call_id, normalized[:80])
            return
        self._last_spoken_instruction = normalized
        self.observability.transcript.append_turn("ASSISTANT", normalized, source)
        await self._inject_routing_instruction(normalized)

    def _spawn_background(self, coro: Any) -> asyncio.Task:
        """Run work off the Nova recv loop so audioOutput is never blocked."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                LOGGER.exception(
                    "Background task failed for call %s",
                    self.call_id,
                    exc_info=exc,
                )

        task.add_done_callback(_on_done)
        return task

    async def _process_user_transcript(
        self,
        content_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Run LangGraph triage without blocking Nova audio playback."""
        transcript = "".join(self.text_buffers.pop(content_id, [])).strip()
        self.content_metadata.pop(content_id, None)
        if not transcript or transcript.startswith("[ROUTING]"):
            return
        async with self._triage_lock:
            await self._run_langgraph_triage(transcript)

    async def _process_text_block(
        self,
        content_id: str,
        metadata: dict[str, Any],
    ) -> None:
        transcript = "".join(self.text_buffers.pop(content_id, [])).strip()
        self.content_metadata.pop(content_id, None)
        if not transcript or transcript.startswith("[ROUTING]"):
            return

        role = metadata.get("role")
        if role == "USER":
            async with self._triage_lock:
                await self._run_langgraph_triage(transcript)
        elif role == "ASSISTANT":
            self.observability.transcript.append_turn("ASSISTANT", transcript, "nova_tts")

    async def _flush_pending_user_transcripts(self) -> None:
        """Process any buffered USER ASR still waiting for END_TURN (e.g. call ended early)."""
        for content_id in list(self.text_buffers.keys()):
            metadata = self.content_metadata.get(content_id, {})
            if metadata.get("role") != "USER":
                continue
            if metadata.get("generation_stage") == "SPECULATIVE":
                continue
            content_type = metadata.get("type")
            if content_type and content_type != "TEXT":
                continue
            await self._process_text_block(content_id, metadata)

    async def send_event(self, event_json: str) -> None:
        """Send one JSON event string into Nova Sonic's bidirectional stream."""
        if self.stream is None:
            raise RuntimeError("Nova Sonic stream has not been started.")
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )
        await self.stream.input_stream.send(event)

    async def start_session(self) -> None:
        """Start Nova Sonic using the same event order as nova_sonic_integration.py."""
        self.stream = await self.bedrock_client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=NOVA_MODEL_ID)
        )
        self.is_active = True

        session_start = """
        {
          "event": {
            "sessionStart": {
              "inferenceConfiguration": {
                "maxTokens": 1024,
                "topP": 0.9,
                "temperature": 0.7
              }
            }
          }
        }
        """
        await self.send_event(session_start)

        prompt_start = f"""
        {{
          "event": {{
            "promptStart": {{
              "promptName": "{self.prompt_name}",
              "textOutputConfiguration": {{
                "mediaType": "text/plain"
              }},
              "audioOutputConfiguration": {{
                "mediaType": "audio/lpcm",
                "sampleRateHertz": {TELCOFLOW_SAMPLE_RATE},
                "sampleSizeBits": 16,
                "channelCount": 1,
                "voiceId": "tiffany",
                "encoding": "base64",
                "audioType": "SPEECH"
              }}
            }}
          }}
        }}
        """
        await self.send_event(prompt_start)

        text_content_start = f"""
        {{
            "event": {{
                "contentStart": {{
                    "promptName": "{self.prompt_name}",
                    "contentName": "{self.system_content_name}",
                    "type": "TEXT",
                    "interactive": false,
                    "role": "SYSTEM",
                    "textInputConfiguration": {{
                        "mediaType": "text/plain"
                    }}
                }}
            }}
        }}
        """
        await self.send_event(text_content_start)

        text_input = json.dumps(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
                        "content": NOVA_SYSTEM_PROMPT,
                    }
                }
            }
        )
        await self.send_event(text_input)

        text_content_end = f"""
        {{
            "event": {{
                "contentEnd": {{
                    "promptName": "{self.prompt_name}",
                    "contentName": "{self.system_content_name}"
                }}
            }}
        }}
        """
        await self.send_event(text_content_end)

        audio_content_start = f"""
        {{
            "event": {{
                "contentStart": {{
                    "promptName": "{self.prompt_name}",
                    "contentName": "{self.audio_content_name}",
                    "type": "AUDIO",
                    "interactive": true,
                    "role": "USER",
                    "audioInputConfiguration": {{
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": {TELCOFLOW_SAMPLE_RATE},
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64"
                    }}
                }}
            }}
        }}
        """
        await self.send_event(audio_content_start)
        self._session_started = True

        self._audio_out_task = asyncio.create_task(self._drain_audio_to_caller())
        self._recv_from_nova_task = asyncio.create_task(self.process_nova_events_to_telcoflow())

    async def deliver_opening(self) -> None:
        """Speak the opening greeting immediately after the call is answered."""
        if self._opening_delivered:
            return
        await self._speak_instruction(NOVA_OPENING_SCRIPT, source="opening_script")
        self._opening_delivered = True
        self._last_injected_fingerprint = triage_state_fingerprint(
            {
                "symptoms": {},
                "urgency_level": "LOW",
                "routing_decision": "continue_collection",
                "aria_instruction": "May I have your name, please?",
            }
        )

    async def stream_telcoflow_audio_to_nova(self) -> None:
        """Telcoflow phone audio enters here and is passed directly to Nova Sonic at 24 kHz."""
        try:
            async for audio_chunk in self.call.audio_stream():
                if not self.is_active:
                    break
                if not audio_chunk:
                    continue
                blob = base64.b64encode(audio_chunk)
                audio_event = json.dumps(
                    {
                        "event": {
                            "audioInput": {
                                "promptName": self.prompt_name,
                                "contentName": self.audio_content_name,
                                "content": blob.decode("utf-8"),
                            }
                        }
                    }
                )
                await self.send_event(audio_event)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            if self.is_active:
                LOGGER.exception("Error streaming Telcoflow audio to Nova for call %s", self.call_id)
            raise

    async def process_nova_events_to_telcoflow(self) -> None:
        """Nova audio returns here; user transcripts are routed through LangGraph."""
        try:
            while self.is_active:
                if not self.stream:
                    await asyncio.sleep(0.1)
                    continue

                output = await self.stream.await_output()
                result = await output[1].receive()
                if not result.value or not result.value.bytes_:
                    continue

                event_payload = json.loads(result.value.bytes_.decode("utf-8"))
                await self._handle_nova_event(event_payload)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as error:
            message = str(error)
            if "ExpiredTokenException" in message:
                LOGGER.error(
                    "AWS session token expired for call %s. Refresh AWS SSO credentials in .env and restart.",
                    self.call_id,
                )
            elif "UnrecognizedClientException" in message:
                LOGGER.error(
                    "Invalid AWS credentials for call %s. Check AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, "
                    "and AWS_SESSION_TOKEN in .env.",
                    self.call_id,
                )
            elif "ValidationException" in message:
                LOGGER.error(
                    "Nova Sonic rejected an input event for call %s: %s",
                    self.call_id,
                    message,
                )
            elif "InvalidStateError" in message or "CANCELLED" in message:
                LOGGER.debug("Nova stream closed during shutdown for call %s", self.call_id)
            elif self.is_active:
                LOGGER.exception("Nova Sonic event processing failed for call %s", self.call_id)
        finally:
            self.is_active = False

    async def _drain_audio_to_caller(self) -> None:
        """Feed Telcoflow from a queue so Nova recv never waits on send_audio."""
        while True:
            try:
                audio = await asyncio.wait_for(self._audio_out_queue.get(), timeout=0.25)
            except TimeoutError:
                if not self.is_active and self._audio_out_queue.empty():
                    break
                continue

            if audio is None:
                break

            for attempt, delay in enumerate((0.0, 0.025, 0.05, 0.1)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await self.call.send_audio(audio)
                    break
                except BufferFullError:
                    if attempt == 3:
                        LOGGER.warning(
                            "Call %s Telcoflow buffer full; dropped %d byte audio chunk",
                            self.call_id,
                            len(audio),
                        )
                except Exception as error:
                    if self.is_active:
                        LOGGER.warning(
                            "Call %s failed sending audio to Telcoflow: %s",
                            self.call_id,
                            error,
                        )
                    break

    def _enqueue_audio_output(self, audio: bytes) -> None:
        """Non-blocking handoff from Nova recv loop to the Telcoflow audio worker."""
        try:
            self._audio_out_queue.put_nowait(audio)
        except asyncio.QueueFull:
            LOGGER.warning(
                "Call %s audio output queue full; dropping %d byte chunk",
                self.call_id,
                len(audio),
            )

    async def _handle_nova_event(self, payload: dict[str, Any]) -> None:
        """Dispatch Nova output events by type while keeping audio and triage separated."""
        event = payload.get("event", {})
        if "contentStart" in event:
            self._handle_content_start(event["contentStart"])
        elif "textOutput" in event:
            text_content = event["textOutput"].get("content", "")
            if '{ "interrupted" : true }' in text_content:
                LOGGER.debug("Call %s Nova barge-in signal (keeping queued audio)", self.call_id)
            self._handle_text_output(event["textOutput"])
        elif "audioOutput" in event:
            self._enqueue_audio_output(base64.b64decode(event["audioOutput"]["content"]))
        elif "contentEnd" in event:
            await self._handle_content_end(event["contentEnd"])

    def _handle_content_start(self, content_start: dict[str, Any]) -> None:
        """Track content block metadata so text chunks can be classified by role."""
        content_id = content_start.get("contentId") or content_start.get("contentName")
        if content_id is None:
            return
        additional_fields = {}
        if content_start.get("additionalModelFields"):
            try:
                additional_fields = json.loads(content_start["additionalModelFields"])
            except json.JSONDecodeError:
                additional_fields = {}
        self.content_metadata[content_id] = {
            "role": content_start.get("role"),
            "type": content_start.get("type"),
            "generation_stage": additional_fields.get("generationStage"),
        }
        if content_start.get("type") == "TEXT" or content_start.get("role") in ("USER", "ASSISTANT"):
            self.text_buffers.setdefault(content_id, [])

    def _handle_text_output(self, text_output: dict[str, Any]) -> None:
        """Buffer text chunks; enrich metadata from textOutput when Nova omits contentStart fields."""
        content_id = text_output.get("contentId") or text_output.get("contentName")
        if content_id is None:
            return
        self._merge_content_metadata(
            content_id,
            role=text_output.get("role"),
        )
        self.text_buffers.setdefault(content_id, []).append(text_output.get("content", ""))

    async def _handle_content_end(self, content_end: dict[str, Any]) -> None:
        """Close content blocks and run LangGraph on patient turns."""
        stop_reason = content_end.get("stopReason")
        if self._normalize_stop_reason(stop_reason) == "INTERRUPTED":
            LOGGER.debug("Call %s assistant turn interrupted by Nova", self.call_id)

        content_id = content_end.get("contentId") or content_end.get("contentName")
        if not content_id:
            return
        if content_id in self._routing_content_names:
            self._routing_content_names.discard(content_id)
            self.text_buffers.pop(content_id, None)
            self.content_metadata.pop(content_id, None)
            return

        metadata = self._merge_content_metadata(
            content_id,
            role=content_end.get("role"),
            content_type=content_end.get("type"),
        )
        if not self._is_routable_text_block(metadata, content_end):
            return

        role = metadata.get("role")
        if role == "USER":
            if not self._should_process_user_transcript_now(stop_reason):
                LOGGER.debug(
                    "Call %s buffering USER transcript (stopReason=%s)",
                    self.call_id,
                    stop_reason,
                )
                return
        elif role == "ASSISTANT":
            if self._normalize_stop_reason(stop_reason) not in (None, "END_TURN", "END_OF_TURN", "COMPLETION"):
                return
            await self._process_text_block(content_id, metadata)
            return

        self._spawn_background(self._process_user_transcript(content_id, dict(metadata)))

    async def _run_langgraph_triage(self, transcript: str) -> None:
        """Invoke LangGraph with call.call_id as the isolated thread ID."""
        if transcript == self._last_processed_transcript:
            LOGGER.debug("Call %s duplicate transcript skipped: %s", self.call_id, transcript)
            return

        self._last_processed_transcript = transcript
        LOGGER.info("Call %s patient said: %s", self.call_id, transcript)
        self.observability.transcript.append_turn("PATIENT", transcript, "nova_asr")
        state, trace_id = await asyncio.to_thread(
            self.observability.run_triage,
            self.triage_graph,
            transcript,
            self.call_id,
        )
        self.latest_triage_state = state
        write_triage_log(self.call_id, state)
        self.observability.transcript.append_triage_decision(state, transcript, trace_id)

        fingerprint = triage_state_fingerprint(state)
        if fingerprint == self._last_injected_fingerprint:
            LOGGER.debug("Call %s triage unchanged; skipping Nova routing injection", self.call_id)
            return

        self._last_injected_fingerprint = fingerprint
        LOGGER.info(
            "Call %s triage=%s route=%s",
            self.call_id,
            state.get("urgency_level"),
            state.get("routing_decision"),
        )

        instruction = state.get("aria_instruction", "")
        if self._opening_delivered and instruction == "May I have your name, please?":
            LOGGER.debug("Call %s opening already asked for name; skipping duplicate prompt", self.call_id)
            return
        if instruction:
            await self._speak_instruction(instruction, source="langgraph_routing")

    async def _inject_routing_instruction(self, instruction: str) -> None:
        """Hand LangGraph's single spoken line to Nova."""
        guidance_content_name = str(uuid.uuid4())
        routing_message = f"[ROUTING] Say exactly: {json.dumps(instruction)}"

        content_start = json.dumps(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": guidance_content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "USER",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        text_input = json.dumps(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": guidance_content_name,
                        "content": routing_message,
                    }
                }
            }
        )
        content_end = f"""
        {{
            "event": {{
                "contentEnd": {{
                    "promptName": "{self.prompt_name}",
                    "contentName": "{guidance_content_name}"
                }}
            }}
        }}
        """
        await self.send_event(content_start)
        await self.send_event(text_input)
        await self.send_event(content_end)
        self._routing_content_names.add(guidance_content_name)

    async def end_session(self) -> None:
        """Close Nova Sonic in the same order as nova_sonic_integration.py."""
        if self._session_ended or not self.stream:
            return

        self._session_ended = True
        self.is_active = False

        try:
            audio_content_end = f"""
            {{
                "event": {{
                    "contentEnd": {{
                        "promptName": "{self.prompt_name}",
                        "contentName": "{self.audio_content_name}"
                    }}
                }}
            }}
            """
            await self.send_event(audio_content_end)

            prompt_end = f"""
            {{
                "event": {{
                    "promptEnd": {{
                        "promptName": "{self.prompt_name}"
                    }}
                }}
            }}
            """
            await self.send_event(prompt_end)

            session_end = """
            {
                "event": {
                    "sessionEnd": {}
                }
            }
            """
            await self.send_event(session_end)
        except Exception:
            pass
        finally:
            if self.stream:
                try:
                    await self.stream.input_stream.close()
                except Exception:
                    pass
                self.stream = None

    async def close_call(self) -> None:
        """Close the Telcoflow call once."""
        if self._call_closed:
            return
        self._call_closed = True
        try:
            await self.call.close()
        except Exception as error:
            LOGGER.debug("Call %s already closed: %s", self.call_id, error)

    async def shutdown(self) -> None:
        """Flush pending transcripts and close Nova once per call."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.is_active = False

        if self._send_to_nova_task and not self._send_to_nova_task.done():
            self._send_to_nova_task.cancel()
        if self._recv_from_nova_task and not self._recv_from_nova_task.done():
            self._recv_from_nova_task.cancel()

        tasks = [task for task in [self._send_to_nova_task, self._recv_from_nova_task] if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._audio_out_task and not self._audio_out_task.done():
            try:
                self._audio_out_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            await asyncio.gather(self._audio_out_task, return_exceptions=True)

        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

        try:
            await self._flush_pending_user_transcripts()
        except Exception:
            LOGGER.exception("Failed flushing pending transcripts for call %s", self.call_id)

        try:
            await self.end_session()
        except Exception:
            pass

    async def _on_terminated(self) -> None:
        """Stop Nova tasks cleanly when Telcoflow ends the call."""
        if self._shutdown_done:
            return

        LOGGER.info("Call terminated: %s", self.call_id)
        await self.shutdown()

    async def run(self) -> None:
        """Bridge Telcoflow audio to Nova Sonic using the reference integration lifecycle."""
        self.call.register_event_handler(events.CALL_TERMINATED, self._on_terminated)
        await self.start_session()
        await self.call.answer()
        LOGGER.info("Call %s answered; delivering opening", self.call_id)
        await self.deliver_opening()

        self._send_to_nova_task = asyncio.create_task(self.stream_telcoflow_audio_to_nova())

        try:
            await asyncio.gather(self._send_to_nova_task, self._recv_from_nova_task)
        finally:
            if self._audio_out_task and not self._audio_out_task.done():
                try:
                    self._audio_out_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                await asyncio.gather(self._audio_out_task, return_exceptions=True)


def create_bedrock_client() -> BedrockRuntimeClient:
    """Create a shared Bedrock client using the same config as nova_sonic_integration.py."""
    region = get_aws_region() or SUPPORTED_AWS_REGION
    aws_config = Config(
        endpoint_uri=f"https://bedrock-runtime.{region}.amazonaws.com",
        region=region,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    return BedrockRuntimeClient(config=aws_config)


# This section owns only Telcoflow call lifecycle and connects it to the Nova session.
async def handle_call(
    call: ActiveCall,
    bedrock_client: BedrockRuntimeClient,
    triage_graph: Any,
) -> None:
    LOGGER.info("Incoming call %s from %s", call.call_id, call.caller_number)
    observability = CallObservability(call.call_id, call.caller_number)

    @call.on(events.CALL_ERROR)
    def on_call_error(data: dict[str, Any]) -> None:
        LOGGER.error("Call error for %s: %s", call.call_id, data)

    session = NovaSonicTriageSession(call, bedrock_client, triage_graph, observability)
    try:
        with observability.call_trace():
            try:
                await session.run()
            finally:
                # Await background LangGraph tasks before the call span closes so
                # triage_decisions and final_routing reflect the full call.
                try:
                    await session.shutdown()
                except Exception:
                    LOGGER.exception("Session shutdown failed for call %s", call.call_id)
    except asyncio.CancelledError:
        LOGGER.info("Call handler cancelled for %s", call.call_id)
    except Exception:
        LOGGER.exception("Error in triage call %s", call.call_id)
        raise
    finally:
        await session.close_call()
        try:
            path = observability.finalize_once()
            if path:
                LOGGER.info("Call evidence saved for %s at %s", call.call_id, path)
        except Exception:
            LOGGER.exception("Failed to finalize call evidence for %s", call.call_id)


# This section validates configuration before opening any network streams.
def get_aws_region() -> str | None:
    """Read AWS region from .env-compatible names and normalize it for the SDK."""
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or SUPPORTED_AWS_REGION
    os.environ["AWS_REGION"] = region
    return region


def require_environment() -> None:
    required = [
        "WSS_API_KEY",
        "WSS_CONNECTOR_UUID",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ]
    missing = [name for name in required if not os.getenv(name)]
    aws_region = get_aws_region()
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    if aws_region != SUPPORTED_AWS_REGION:
        raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION must be us-east-1 for Amazon Nova Sonic 2.")


# This is the runnable entrypoint: Telcoflow handles calls, Nova handles audio, LangGraph handles triage.
async def main() -> None:
    require_environment()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_langfuse()
    triage_graph = build_triage_graph()
    bedrock_client = create_bedrock_client()
    config = TelcoflowClientConfig.sandbox(
        api_key=os.environ["WSS_API_KEY"],
        connector_uuid=os.environ["WSS_CONNECTOR_UUID"],
        buffer_size=1024 * 1024,
        sample_rate=TELCOFLOW_SAMPLE_RATE,
    )

    try:
        async with TelcoflowClient(config) as client:
            LOGGER.info("Connected to Telcoflow. Waiting for HealthFirst Clinic calls.")

            @client.on(events.INCOMING_CALL)
            async def on_incoming_call(call: ActiveCall) -> None:
                await handle_call(call, bedrock_client, triage_graph)

            await client.run_forever()
    finally:
        shutdown_langfuse()


if __name__ == "__main__":
    asyncio.run(main())
