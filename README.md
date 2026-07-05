# ContinueCare.ai

## The Problem

Healthcare conversations lose context.

Patients repeat the same symptoms at every visit. Doctors rarely see how conditions develop over weeks or months. Chat history and simple vector search store text, but they do not understand relationships — that a headache worsens on poor-sleep nights, that ibuprofen was started for that headache, or that stress and mood shifted at the same time.

There is no evolving memory that connects events, surfaces trends, and gives clinicians an evidence-backed picture before the appointment.

## What ContinueCare.ai Does

ContinueCare.ai is a hospital-grade continuity-of-care memory system. Patients log symptoms, medications, mood, and observations through a conversational companion. Doctors receive pre-visit briefs grounded in stored memory — not generic LLM summaries.

Memory improves over time: entities are linked in a knowledge graph, patterns emerge, and forgotten information truly disappears from retrieval.

---

## How We Leverage Cognee

[Cognee](https://www.cognee.ai/) is the memory engine behind everything. We use it for the full memory lifecycle — not as a chat wrapper around an LLM.

| Cognee capability | What it solves | How we use it |
|-------------------|----------------|---------------|
| **`remember()` + custom `graph_model`** | Plain text notes don't capture structure | Every patient message runs through `remember()` with a healthcare `DataPoint` ontology (`HealthRecord`, `Symptom`, `Medication`, `MoodEntry`, `Observation`). Gemini extracts entities and edges — e.g. `Medication.treats → Symptom` — into a per-patient knowledge graph (`patient_{uuid}` dataset). |
| **`recall()` with graph completion** | Chatbots forget and hallucinate without grounding | The patient companion calls `recall()` with a clinical system prompt. Cognee auto-routes to graph-backed retrieval so answers reference prior symptoms, meds, and mood — not just the latest message. |
| **`recall()` multi-query decomposition** | One broad question misses nuance | Doctor briefs run five focused sub-queries (symptoms, medications, mood, observations, correlations). Each hits the graph separately; results are synthesized with citations from stored memory. |
| **`improve()`** | Memory stays flat unless enriched | Patients trigger `improve()` to bridge session data into permanent memory, discover new entity relationships, and build a global context index for richer retrieval. |
| **`forget()`** | Deletion must be real, not cosmetic | Patients can clear their dataset. After `forget()`, `recall()` no longer surfaces removed data — proving memory actually changed. |
| **Knowledge graph + vectors together** | Vector search alone misses structure | Cognee stores both graph relationships and embeddings. We visualize the graph in Memory Explorer and use `SearchType.CHUNKS` + `get_schema_inventory()` to show what is remembered and how entities connect. |
| **Session memory** | Short-term context within a conversation | Session-scoped `remember()` keeps recent chat available for fast retrieval while permanent graph memory builds in the background. |

**Why Cognee instead of chat history or RAG alone?**

- Chat history is linear — it cannot traverse `symptom → medication → mood` relationships.
- Vector search finds similar text — it does not model that sleep dropped to 5 hours *before* headaches worsened.
- Cognee builds a **structured, evolving knowledge graph** per patient, then retrieves with graph-aware completion — which is exactly what continuity of care requires.

---

## Architecture

```
┌─────────────────────┐       ┌────────────────────────────────┐
│   React Frontend    │──────▶│       FastAPI Backend          │
│                     │       │                                │
│ • Patient Companion │       │  ┌──────────────────────────┐  │
│ • Doctor Brief      │       │  │   Cognee Memory Engine   │  │
│ • Memory Explorer   │       │  │                          │  │
│ • Knowledge Graph   │       │  │  remember() → KG build   │  │
│                     │       │  │  recall()   → retrieval   │  │
└─────────────────────┘       │  │  improve()  → enrichment  │  │
                              │  │  forget()   → deletion    │  │
                              │  └──────────┬───────────────┘  │
                              │             │                  │
                              │  ┌──────────▼───────────────┐  │
                              │  │  Healthcare Ontology      │  │
                              │  │  (DataPoint subclasses)   │  │
                              │  │                          │  │
                              │  │  HealthRecord            │  │
                              │  │  ├── Symptom             │  │
                              │  │  ├── Medication ─treats─▶│  │
                              │  │  ├── MoodEntry           │  │
                              │  │  └── Observation         │  │
                              │  └──────────────────────────┘  │
                              └────────────────────────────────┘
```

### Cognee Features Used

| Feature | How It's Used |
|---------|---------------|
| `remember()` with `graph_model` | Extracts structured health entities (symptoms, medications, mood, observations) from patient messages into the knowledge graph |
| `recall()` with `GRAPH_COMPLETION` | Generates contextual companion responses grounded in stored memory |
| `recall()` multi-query | Decomposes doctor brief generation into focused sub-queries for comprehensive summaries |
| `improve()` | Enriches the knowledge graph with new relationships and builds global context index |
| `forget()` | Removes patient data from graph and vector stores, proving true deletion |
| Custom `DataPoint` ontology | Healthcare-specific schema constraining LLM extraction to domain entities |
| Session memory | Stores conversation context for short-term retrieval |
| `SearchType.CHUNKS` | Raw chunk retrieval for knowledge graph visualization |
| `get_schema_inventory()` | Memory inventory showing entity types and counts |

### Healthcare Ontology

The custom ontology (defined as `DataPoint` subclasses) constrains how Cognee's LLM
extraction builds the knowledge graph:

- **HealthRecord** — root extraction node, connects to all entity types
- **Symptom** — name, severity, body location, duration, frequency
- **Medication** — name, dosage, frequency, purpose; `treats` → Symptom (edge)
- **MoodEntry** — emotional state, intensity, triggers; `associated_symptoms` → Symptom
- **Observation** — measurable findings with category (vital sign, lab result, etc.)

---

## Hospital Access Model

ContinueCare.ai is built as a hospital product with role-based access:

| Role | How to access | Can access |
|------|-------------|------------|
| **Patient** | Self-register on the login screen | Own health companion, own memory only |
| **Doctor** | Staff login with `@continuecare.com` email (hospital-provisioned) | All registered patients, briefs, memory graphs |

### Hospital staff (login only — cannot register)

| Doctor | Specialization | Email | Default password |
|--------|----------------|-------|------------------|
| Dr. John Multispecialist | Multispecialist | john@continuecare.com | `continuecare` |
| Dr. Sarah Chen | Cardiology | sarah.chen@continuecare.com | `continuecare` |
| Dr. Michael Patel | Neurology | michael.patel@continuecare.com | `continuecare` |
| Dr. Emily Rivera | Pediatrics | emily.rivera@continuecare.com | `continuecare` |
| Dr. David Okonkwo | Internal Medicine | david.okonkwo@continuecare.com | `continuecare` |

Override the staff password with `HOSPITAL_DOCTOR_PASSWORD` in `backend/.env`.

### How it works

1. **Patient registers** at the login screen → logs symptoms, medications, mood via the companion
2. Data is stored in Cognee under that patient's unique ID (`patient_{uuid}`)
3. **Doctor logs in** with a hospital `@continuecare.com` account → sees a **Patients** list
4. Doctor selects a patient → generates a **Doctor Brief** or views their **Memory Explorer**
5. Patients cannot see other patients' data; only provisioned hospital staff can access doctor views

Auth uses session tokens (`Authorization: Bearer <token>`). User accounts are stored in `backend/data/users.json`.

### Demo flow

```bash
# Terminal 1 — register as patient in browser, log a few symptoms
# Terminal 2 — register as doctor, open Patients tab, select that patient, Generate Brief
```

---

## Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- AWS account with Bedrock model access (Nova Lite + Titan Embeddings)
- AWS credentials configured in `backend/.env` (see `.env.example`)

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure AWS Bedrock credentials
cp .env.example .env
# Edit .env — set AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN
```

### Frontend

```bash
cd frontend
npm install
```

---

## Running

Start both servers (in separate terminals):

```bash
# Terminal 1: Backend
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Terminal 2: Frontend
cd frontend
npm run dev
```

Open http://localhost:5173

---

## Demo Script

**Full presenter script (7 min, two tabs, copy-paste lines):** see [DEMO_SCRIPT.md](./DEMO_SCRIPT.md)

Quick version below:

## Demo Walkthrough

### 1. Patient Companion — Building Memory

Record several health entries to build the knowledge graph:

> "I've been having headaches for the past 3 days, mostly in the morning. The pain is moderate, around the temples."

> "I started taking ibuprofen 400mg twice daily for the headaches."

> "My mood has been low lately. I've been feeling stressed about work deadlines and not sleeping well."

> "I noticed my blood pressure was 140/90 when I checked at the pharmacy yesterday."

> "The headaches seem worse on days when I sleep less than 6 hours."

Each message extracts entities into the knowledge graph. The companion references
past entries when responding.

### 2. Ask About History

> "What symptoms have I reported?"

> "Is there any pattern between my sleep and headaches?"

> "What medications am I currently taking?"

The system recalls from the knowledge graph, demonstrating semantic retrieval
beyond simple keyword matching.

### 3. Doctor Brief — Evidence-Based Summary

Switch to the Doctor Brief tab and generate a summary. The brief includes:
- Symptom progression with timeline
- Medication history with effectiveness
- Mood trends and correlations
- Citations linking each finding to stored memory

### 4. Memory Explorer — Graph Visualization

View the knowledge graph to see how entities are connected:
- Symptom nodes (red)
- Medication nodes (blue)
- Mood nodes (purple)
- Observation nodes (green)
- HealthRecord nodes (amber)

Use "Improve Memory" to trigger Cognee's enrichment pipeline.

### 5. Forgetting — Proving Deletion

Click "Forget All" in the Memory Explorer. Then return to the companion
and ask about previous symptoms — the system will no longer remember them.
This proves that `cognee.forget()` truly removes data from the graph and
vector stores, not just hiding it.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/patient/message` | Send a patient message, extract entities, recall response |
| POST | `/api/doctor/brief` | Generate a pre-visit clinical summary |
| POST | `/api/memory/improve` | Trigger knowledge graph enrichment |
| POST | `/api/memory/forget` | Delete memory (all or by dataset) |
| GET | `/api/memory/graph` | Retrieve graph nodes and edges for visualization |
| GET | `/api/memory/inventory` | Get entity type counts and samples |
| GET | `/api/health` | Health check |

---

## Technology Choices

| Technology | Justification |
|------------|---------------|
| **Cognee v1.2** | Core memory engine — knowledge graph, semantic retrieval, memory lifecycle |
| **FastAPI** | Async Python framework matching Cognee's async API |
| **React + TypeScript** | Type-safe component architecture for complex UI |
| **Tailwind CSS v4** | Utility-first styling for rapid, consistent UI development |
| **react-force-graph-2d** | Interactive knowledge graph visualization |
| **LanceDB + Ladybug** (via Cognee) | Embedded vector + graph storage — zero-config local demo |
