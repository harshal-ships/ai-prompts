# ContinueCare.ai — Demo Script (~7 minutes)

Use **two browser windows**: Tab A = Patient, Tab B = Doctor (incognito is fine).

**Before you start**
```bash
# Terminal 1
cd backend && source venv/bin/activate && uvicorn app.main:app --reload --port 8000

# Terminal 2
cd frontend && npm run dev
```
Open http://localhost:5173

> **Note:** The first patient message takes 15–30 seconds while Cognee builds the knowledge graph.

---

## Act 1 — The Problem (30 sec, talk track)

**Say:**
> "Patients repeat the same story every visit. Doctors don't see trends over weeks. Chat history is linear — it can't connect headaches, sleep, meds, and mood. ContinueCare.ai uses Cognee to build an evolving knowledge graph per patient."

---

## Act 2 — Patient builds memory (3 min)

**Tab A → Register → Patient**

| Field | Value |
|-------|-------|
| Name | Alex Morgan |
| Email | alex.morgan@gmail.com |
| Password | anything you choose |

**Say:** "Patients self-register. Each gets their own isolated Cognee dataset."

---

### Message 1 — Symptom
**Type:**
```
I've been having headaches for the past 3 days, mostly in the morning. The pain is moderate, around my temples.
```

**Say while waiting:** "This calls `cognee.remember()` with our healthcare ontology — Gemini extracts a Symptom node into the graph."

**Point out:** Entity tags on the response (if shown). Companion acknowledges the headache.

---

### Message 2 — Medication
**Type:**
```
I started taking ibuprofen 400mg twice daily for the headaches.
```

**Say:** "Cognee links Medication → treats → Symptom. Not just text storage — a relationship in the graph."

---

### Message 3 — Mood
**Type:**
```
I've been feeling stressed about work deadlines and not sleeping well, maybe 5 hours a night.
```

---

### Message 4 — Observation
**Type:**
```
I noticed my blood pressure was 140/90 when I checked at the pharmacy yesterday.
```

---

### Message 5 — Pattern (optional)
**Type:**
```
The headaches seem worse on days when I sleep less than 6 hours.
```

---

### Ask about history
**Type:**
```
Is there a connection between my sleep and headaches?
```

**Say:** "This is `cognee.recall()` — graph-backed retrieval, not keyword search. Click the response to show **Supporting Memories** in the sidebar."

**Point out:** Citations / memories used panel.

---

### Memory Explorer (patient view)
**Tab A → My Memory**

**Say:** "Patients can inspect what's remembered — nodes for symptoms, medications, mood, observations."

Click **Improve Memory** (optional):
**Say:** "`cognee.improve()` enriches the graph and discovers new relationships."

---

## Act 3 — Doctor pre-visit brief (2 min)

**Tab B → Staff Login**

| Field | Value |
|-------|-------|
| Email | john@continuecare.com |
| Password | continuecare |

**Say:** "Doctors can't self-register — only hospital staff with @continuecare.com emails on the roster."

**Tab B → Patients → select Alex Morgan**

**Tab B → Doctor Brief → Generate Brief**

**Say while waiting:** "Five focused `recall()` sub-queries hit the graph — symptoms, meds, mood, observations, correlations — then synthesize a brief with citations."

**Point out:**
- Symptom progression
- Medication history
- Mood trends
- **Citations & Evidence** section — every claim tied to stored memory

**Tab B → Memory Explorer** (read-only for doctors):
**Say:** "Doctors see the same knowledge graph — relationships visible, not a black-box summary."

---

## Act 4 — Forgetting proves real deletion (1 min)

**Tab A → My Memory → Clear Memory → confirm**

**Tab A → My Health Companion**

**Type:**
```
What symptoms have I reported?
```

**Say:** "`cognee.forget()` removed the dataset. The companion no longer recalls headaches or ibuprofen — memory actually changed, not hidden."

---

## Act 5 — Cognee recap (30 sec)

**Say:**
> "We used the full Cognee lifecycle:
> - **remember** — structured extraction into a healthcare graph
> - **recall** — graph-grounded companion + doctor brief
> - **improve** — relationship enrichment
> - **forget** — true deletion
>
> That's continuity of care — memory that evolves, connects, and can be trusted."

---

## Quick reference — copy/paste messages

```
I've been having headaches for the past 3 days, mostly in the morning. The pain is moderate, around my temples.

I started taking ibuprofen 400mg twice daily for the headaches.

I've been feeling stressed about work deadlines and not sleeping well, maybe 5 hours a night.

I noticed my blood pressure was 140/90 when I checked at the pharmacy yesterday.

The headaches seem worse on days when I sleep less than 6 hours.

Is there a connection between my sleep and headaches?

What symptoms have I reported?
```

## Login credentials

| Role | Email | Password |
|------|-------|----------|
| Patient | alex.morgan@gmail.com | (your choice at register) |
| Doctor | john@continuecare.com | continuecare |

Other staff: sarah.chen@, michael.patel@, emily.rivera@, david.okonkwo@ — all `@continuecare.com`, password `continuecare`.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 401 after server restart | Log in again (sessions are in-memory) |
| First message slow | Normal — Cognee ingesting + cognifying |
| Empty doctor brief | Patient must send messages first |
| 500 on message | Check `backend/.env` has valid Gemini keys |
