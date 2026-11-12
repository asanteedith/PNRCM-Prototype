import json, uuid, csv, io, datetime, time, os
from flask import Flask, render_template, request, jsonify, Response, g
import db
from db import get_db, now, agent_execution_start, agent_execution_complete, audit, add_exception, add_transition, add_timeline
from agents.claims_vetting import vet_claim, run_standards_rules
from agents.documentation import review_documentation, resolve_documentation_review
from agents.billing import generate_bill, validate_bill
from agents import leakage as leakage_mod
from agents.eligibility import check_eligibility, check_encounter_eligibility, create_eligibility_task
from agents.prior_auth import check_authorizations, create_authorization, create_auth_task
from agents.denial import detect_denials, list_denials, resolve_denial, denial_patterns
from agents import appeals as appeals_mod
from agents import followup as followup_mod
from agents import collections as collections_mod
from agents import fraud as fraud_mod
from agents import analytics as analytics_mod
from agents import orchestrator as orch
from agents import evidence as evidence_mod
from agents import reconciliation as reconciliation_mod
from agents import revenue_integrity as revenue_integrity_mod
import security
import revenue_enhancements as rev_enh

app = Flask(__name__)

AGENTS = [
    ("Patient Access", "Registration and encounter setup"),
    ("Encounter Coordinator", "Tracks encounter progression"),
    ("Documentation Review", "Flags missing or inconsistent records"),
    ("Charge Capture", "Identifies delivered but unbilled services"),
    ("Pricing & Tariff", "Checks configured price rules"),
    ("Billing", "Compiles bill for human review"),
    ("Payment Coordinator", "Routes self-pay payment workflow"),
    ("Payment Allocation", "Matches payments to bills"),
    ("Claims Preparation", "Prepares simulated insurance claim"),
    ("Claims Validation", "Checks claim completeness"),
    ("Denial Management", "Tracks rejected or unpaid items"),
    ("Reconciliation", "Matches expected and received revenue"),
    ("Revenue Intelligence", "Summarizes leakage and balances"),
    ("Orchestrator", "Routes tasks and preserves approval gates"),
]
STAGES = ["Registration", "Encounter", "Documentation", "Service delivery", "Charge capture", "Billing", "Payment / claims", "Reconciliation"]

db.init_db()
# Initialize persistent agent states for all registered agents
c = get_db()
orch._init_agent_states_db(c)
c.commit(); c.close()

def event(c, rid, agent, msg, status="done", payload=None):
    c.execute("INSERT INTO events(run_id, ts, agent, event, status, payload) VALUES(?,?,?,?,?,?)", (rid, now(), agent, msg, status, json.dumps(payload or {})))

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return jsonify(ok=True, service="PNRCM demo API", database="SQLite")

@app.get("/api/agents")
def agents():
    return jsonify([{"id": i, "name": a, "description": d} for i, (a, d) in enumerate(AGENTS)])

@app.get("/api/runs")
def runs():
    c = get_db()
    rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 25").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.get("/api/runs/<rid>")
def run_detail(rid):
    c = get_db()
    run = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not run:
        c.close()
        return jsonify(error="Run not found"), 404
    ev = c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (rid,)).fetchall()
    rev = c.execute("SELECT * FROM reviews WHERE run_id=? ORDER BY created_at", (rid,)).fetchall()
    c.close()
    return jsonify(run=dict(run), events=[dict(x) for x in ev], reviews=[dict(x) for x in rev])

@app.post("/api/workflows/run")
def execute():
    data = request.get_json(force=True)
    scenario = data.get("scenario", "oop")
    patient = data.get("patient", "Demo Patient A")
    if scenario not in ("oop", "nhis", "leak"):
        return jsonify(error="Invalid scenario"), 400
    rid = str(uuid.uuid4())
    enc = "ENC-" + str(uuid.uuid4())[:8].upper()
    totals = {"oop": (350, 200, 150, 50), "nhis": (420, 0, 420, 0), "leak": (300, 300, 0, 50)}[scenario]
    c = get_db()
    c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, enc, scenario, patient, "RUNNING", *totals, now()))
    event(c, rid, "Orchestrator", "Run accepted; synthetic encounter created", "active", {"scenario": scenario, "encounter_id": enc})
    sequence = [13, 0, 1, 2, 3, 4, 5]
    if scenario == "oop":
        sequence += [6, 7]
    elif scenario == "nhis":
        sequence += [8, 9, 10]
    else:
        sequence += [6, 7]
    sequence += [11, 12]
    for idx in sequence:
        name = AGENTS[idx][0]
        payload = {"agent_id": idx, "scenario": scenario, "encounter_id": enc, "human_approval_required": idx in (5, 9, 10)}
        if idx == 2 and scenario == "nhis":
            payload["finding"] = "Required documentation field missing"
        if idx == 3 and scenario == "leak":
            payload.update({"finding": "Delivered service absent from bill", "potential_amount": 50})
        if idx == 4 and scenario == "nhis":
            payload["finding"] = "Configured tariff mismatch flagged for review"
        if idx == 6 and scenario in ("oop", "leak"):
            payload["payment_method"] = "Mobile Money (simulated)"
        if idx == 9 and scenario == "nhis":
            payload["claim_status"] = "Held for human review"
        event(c, rid, name, "Agent executed; output recorded", "done", payload)
    if scenario in ("oop", "leak"):
        rev_id = str(uuid.uuid4())
        c.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)", (rev_id, rid, "missed_charge", "Service documented as delivered but charge absent", 50, "pending", now(), None))
    else:
        rev_id = str(uuid.uuid4())
        c.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)", (rev_id, rid, "claim_validation", "Missing documentation and tariff inconsistency require reviewer", 420, "pending", now(), None))
    c.execute("UPDATE runs SET status=? WHERE id=?", ("REVIEW_REQUIRED", rid))
    event(c, rid, "Orchestrator", "Workflow completed; item paused at human review gate", "review_required", {"review_id": rev_id})
    audit(c, "System", "system", "Orchestrator", "Workflow executed", "run", rid, previous_state=None, new_state="REVIEW_REQUIRED")
    c.commit()
    c.close()
    return jsonify(run_id=rid, encounter_id=enc, status="REVIEW_REQUIRED")

@app.post("/api/reviews/<review_id>/resolve")
def resolve(review_id):
    data = request.get_json(force=True)
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify(error="decision must be approve or reject"), 400
    c = get_db()
    rev = c.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not rev:
        c.close()
        return jsonify(error="Review not found"), 404
    if rev["status"] != "pending":
        c.close()
        return jsonify(error="Review already resolved"), 409
    status = "approved" if decision == "approve" else "rejected"
    c.execute("UPDATE reviews SET status=?, resolved_at=? WHERE id=?", (status, now(), review_id))
    event(c, rev["run_id"], "Human Reviewer", f"{decision.title()}d {rev['kind']} in simulation", status, {"review_id": review_id, "amount": rev["amount"]})
    c.execute("UPDATE runs SET status=? WHERE id=?", ("APPROVED" if decision == "approve" else "REJECTED", rev["run_id"]))
    audit(c, "Human Reviewer", "user", "Human Reviewer", f"Review {decision}d", "review", review_id, previous_state="pending", new_state=status)
    c.commit()
    c.close()
    return jsonify(ok=True, status=status)

@app.get("/api/metrics")
def metrics():
    c = get_db()
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) total, COALESCE(SUM(collected),0) collected, COALESCE(SUM(outstanding),0) outstanding FROM runs").fetchone()
    pend = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amount FROM reviews WHERE status='pending'").fetchone()
    c.close()
    return jsonify(runs=r["n"], total=r["total"], collected=r["collected"], outstanding=r["outstanding"], pending_reviews=pend["n"], pending_amount=pend["amount"])

@app.post("/api/reset")
def reset():
    c = get_db()
    c.execute("PRAGMA foreign_keys = OFF")
    tables = [
        "events","reviews","runs","claim_followup_attempts","claim_followups","appeals",
        "collection_attempts","collection_tasks","payment_promises","patient_payments","patient_balances",
        "fraud_investigations","fraud_alerts","leakage_cases","exceptions","decision_support",
        "clinical_suggestions","encounter_transitions","registration_timeline","workflow_runs",
        "claim_vetting","documentation_reviews","denials","payments","claims","services","bills","encounters","patients",
        "agent_executions","audit_trail",
        "reconciliations","reconciliation_line_items","payment_matches","underpayment_cases","recovery_workflow",
        "orchestrator_tasks","agent_states","agent_performance","review_tasks",
    ]
    for t in tables:
        try: c.execute(f"DELETE FROM {t}")
        except Exception: pass
    c.execute("PRAGMA foreign_keys = ON")
    c.commit(); c.close()
    return jsonify(ok=True)

# ---------------- Patients ----------------
@app.get("/api/patients")
def list_patients():
    c = get_db()
    rows = c.execute("SELECT * FROM patients ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.get("/api/patients/<pid>")
def get_patient(pid):
    c = get_db()
    row = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (pid, pid)).fetchone()
    if not row:
        c.close(); return jsonify(error="Patient not found"), 404
    timeline = c.execute("SELECT * FROM registration_timeline WHERE patient_id=? ORDER BY ts", (row["id"],)).fetchall()
    encounters = c.execute("SELECT * FROM encounters WHERE patient_id=? ORDER BY created_at DESC", (row["id"],)).fetchall()
    c.close()
    return jsonify({"patient": dict(row), "timeline": [dict(x) for x in timeline], "encounters": [dict(x) for x in encounters]})

@app.post("/api/patients")
def create_patient():
    data = request.get_json(force=True)
    if data.get("orchestrated", True):
        res = orch.register_patient_full(data)
        if isinstance(res, dict) and res.get("error"):
            return jsonify(res), 400
        return jsonify(res), 201
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    if not first_name or not last_name:
        return jsonify(error="first_name and last_name are required"), 400
    c = get_db()
    pid = str(uuid.uuid4())
    patient_id = data.get("patient_id") or ("MRN-" + str(uuid.uuid4())[:8].upper())
    existing = c.execute("SELECT id FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
    if existing:
        c.close(); return jsonify(error="Patient ID already exists"), 409
    c.execute(
        """INSERT INTO patients(id, patient_id, first_name, last_name, date_of_birth, gender, phone, email, address, insurance_type, insurance_number, status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, patient_id, first_name, last_name, data.get("date_of_birth"), data.get("gender"),
         data.get("phone"), data.get("email"), data.get("address"), data.get("insurance_type"),
         data.get("insurance_number"), "active", now(), now()),
    )
    audit(c, "User", "user", "Patient Registration", "Patient registered", "patient", pid, new_state="active")
    c.commit()
    row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

# ---------------- Encounters ----------------
@app.get("/api/encounters")
def list_encounters():
    c = get_db()
    rows = c.execute("SELECT * FROM encounters ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.get("/api/encounters/<eid>")
def get_encounter(eid):
    c = get_db()
    row = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not row:
        c.close(); return jsonify(error="Encounter not found"), 404
    trans = c.execute("SELECT * FROM encounter_transitions WHERE encounter_id=? ORDER BY ts", (row["id"],)).fetchall()
    services = c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (row["id"],)).fetchall()
    suggestions = c.execute("SELECT * FROM clinical_suggestions WHERE encounter_id=? ORDER BY created_at DESC", (row["id"],)).fetchall()
    c.close()
    return jsonify({"encounter": dict(row), "transitions": [dict(x) for x in trans], "services": [dict(x) for x in services], "clinical_suggestions": [dict(x) for x in suggestions]})

@app.post("/api/encounters")
def create_encounter():
    data = request.get_json(force=True)
    patient_id = data.get("patient_id")
    if not patient_id:
        return jsonify(error="patient_id is required"), 400
    c = get_db()
    patient = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    if not patient:
        c.close(); return jsonify(error="Patient not found"), 404
    eid = str(uuid.uuid4())
    encounter_id = data.get("encounter_id") or ("ENC-" + str(uuid.uuid4())[:8].upper())
    status = data.get("status", "registered")
    c.execute(
        """INSERT INTO encounters(id, encounter_id, patient_id, patient_name, encounter_type, status, admission_date, discharge_date, reason, attending_physician, documentation_complete, created_at, updated_at, department)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, encounter_id, patient["id"], f"{patient['first_name']} {patient['last_name']}",
         data.get("encounter_type", "outpatient"), status,
         data.get("admission_date") or now(), data.get("discharge_date"), data.get("reason"),
         data.get("attending_physician"), 0, now(), now(), data.get("department")),
    )
    add_transition(c, eid, None, status, "Encounter Tracking Agent", "Encounter created")
    # auto create copay balance (simulated configurable rule)
    try:
        collections_mod.ensure_balance(patient["id"], eid, patient["insurance_type"] or "self_pay", data.get("encounter_type","outpatient"))
    except Exception:
        pass
    audit(c, "Encounter Tracking Agent", "agent", "Encounter Tracking", "Encounter created", "encounter", eid, new_state=status)
    c.commit()
    row = c.execute("SELECT * FROM encounters WHERE id=?", (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.post("/api/encounters/<eid>/status")
def update_encounter_status(eid):
    data = request.get_json(force=True)
    new_status = data.get("status")
    allowed = ["registered","consultation","diagnostics","treatment","billing","payment","claims","completed"]
    if new_status not in allowed:
        return jsonify(error="Invalid status"), 400
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    prev = enc["status"]
    c.execute("UPDATE encounters SET status=?, updated_at=? WHERE id=?", (new_status, now(), enc["id"]))
    add_transition(c, enc["id"], prev, new_status, "Encounter Tracking Agent", data.get("note",""))
    audit(c, "Encounter Tracking Agent", "agent", "Encounter Tracking", f"Status changed to {new_status}", "encounter", enc["id"], previous_state=prev, new_state=new_status)
    c.commit(); c.close()
    return jsonify(ok=True, status=new_status)

@app.post("/api/encounters/<eid>/complete")
def complete_encounter(eid):
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    prev = enc["status"]
    c.execute("UPDATE encounters SET status='completed', updated_at=? WHERE id=?", (now(), enc["id"]))
    add_transition(c, enc["id"], prev, "completed", "User", "Encounter completed")
    audit(c, "User", "user", "Encounter Management", "Encounter completed", "encounter", enc["id"], previous_state=prev, new_state="completed")
    # Event-driven trigger: completed encounter → documentation review
    try:
        review_documentation(c, enc["id"])
        orch.create_orchestrator_task(
            c, "documentation_review", "encounter", enc["id"],
            agent_name="Clinical Documentation Intelligence Agent",
            direction="agent_to_human",
            priority="medium",
            context=f"Encounter {enc['encounter_id']} completed — documentation review required",
            ai_recommendation="Review clinical documentation for completeness and coding accuracy",
            financial_impact=0.0,
            auto_assign=True,
        )
    except Exception:
        pass
    c.commit(); c.close()
    return jsonify(ok=True)

@app.get("/api/encounters/<eid>/timeline")
def encounter_timeline(eid):
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    trans = c.execute("SELECT * FROM encounter_transitions WHERE encounter_id=? ORDER BY ts", (enc["id"],)).fetchall()
    c.close()
    return jsonify([dict(x) for x in trans])

# ---------------- Services ----------------
@app.get("/api/encounters/<eid>/services")
def list_services(eid):
    c = get_db()
    enc = c.execute("SELECT id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    rows = c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (enc["id"],)).fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/encounters/<eid>/services")
def add_service(eid):
    data = request.get_json(force=True)
    name = data.get("service_name", "").strip()
    if not name:
        return jsonify(error="service_name is required"), 400
    c = get_db()
    enc = c.execute("SELECT id FROM encounters WHERE id=? OR encounter_id=?", (eid, eid)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    sid = str(uuid.uuid4())
    qty = int(data.get("quantity", 1))
    unit_price = float(data.get("unit_price", 0) or 0)
    c.execute(
        """INSERT INTO services(id, encounter_id, service_name, description, quantity, unit_price, total_price, billed, bill_id, created_at)
        VALUES(?,?,?,?,?,?,?,0,NULL,?)""",
        (sid, enc["id"], name, data.get("description"), qty, unit_price, qty * unit_price, now()),
    )
    c.commit()
    row = c.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


# ---------------- Bills ----------------
@app.get("/api/bills")
def list_bills():
    c = get_db()
    rows = c.execute("SELECT * FROM bills ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/bills")
def create_bill():
    data = request.get_json(force=True)
    encounter_id = data.get("encounter_id")
    patient_id = data.get("patient_id")
    if not encounter_id or not patient_id:
        return jsonify(error="encounter_id and patient_id are required"), 400
    c = get_db()
    enc = c.execute("SELECT id, patient_id FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    pat = c.execute("SELECT id FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    if not pat:
        c.close(); return jsonify(error="Patient not found"), 404
    bid = str(uuid.uuid4())
    bill_number = data.get("bill_number") or ("BILL-" + str(uuid.uuid4())[:8].upper())
    subtotal = float(data.get("subtotal", 0) or 0)
    tax = float(data.get("tax", 0) or 0)
    discount = float(data.get("discount", 0) or 0)
    total = subtotal + tax - discount
    c.execute(
        "INSERT INTO bills(id, bill_number, encounter_id, patient_id, subtotal, tax, discount, total, paid, outstanding, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,0,?, 'draft', ?, ?)",
        (bid, bill_number, enc["id"], pat["id"], subtotal, tax, discount, total, total, now(), now()),
    )
    audit(c, "Billing Agent", "agent", "Billing", "Bill created", "bill", bid, new_state="draft")
    c.commit()
    row = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.get("/api/bills/<bid>")
def get_bill(bid):
    c = get_db()
    row = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    c.close()
    if not row:
        return jsonify(error="Bill not found"), 404
    return jsonify(dict(row))

@app.post("/api/bills/<bid>/pay")
def billing_pay_route(bid):
    data = request.get_json(force=True)
    amount = float(data.get("amount", 0) or 0)
    method = data.get("payment_method", "unspecified")
    reference = data.get("reference", "")
    if amount <= 0:
        return jsonify(error="amount must be positive"), 400
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    bd = dict(bill)
    new_paid = float(bd["paid"] or 0) + amount
    new_out = max(float(bd["total"] or 0) - new_paid, 0.0)
    status = "paid" if new_out <= 0.01 else "partially_paid"
    c.execute("UPDATE bills SET paid=?, outstanding=?, status=?, updated_at=? WHERE id=?", (new_paid, new_out, status, now(), bd["id"]))
    pay_id = str(uuid.uuid4())
    c.execute("INSERT INTO payments(id, claim_id, bill_id, patient_id, amount, payment_method, payment_date, status, reference, created_at) VALUES(?,?,?,?,?,?,?,'completed',?,?)",
              (pay_id, None, bd["id"], bd["patient_id"], amount, method, now(), reference, now()))
    audit(c, "User", "user", "Billing", "Payment recorded", "bill", bd["id"], previous_state=f"paid={bd['paid']}", new_state=f"paid={new_paid}, status={status}")
    c.commit(); c.close()
    return jsonify(ok=True, paid=new_paid, outstanding=new_out, status=status)

@app.get("/api/bills/<bid>/validate")
def billing_validate_route(bid):
    result = validate_bill(bid)
    return (jsonify(result[0]), result[1]) if isinstance(result, tuple) else jsonify(result)

# ---------------- Bill Lifecycle (Phase 2: Billing Module) ----------------

BILL_STATUSES = ["draft", "validation", "review", "approved", "claims_ready", "submitted"]

BILL_TRANSITIONS = {
    "draft": ["validation"],
    "validation": ["review", "draft"],
    "review": ["approved", "draft"],
    "approved": ["claims_ready"],
    "claims_ready": ["submitted"],
    "submitted": [],
}

def _bill_transition_allowed(current, target):
    return target in BILL_TRANSITIONS.get(current, [])

@app.get("/api/bills/<bid>/workflow-status")
def billing_workflow_status(bid):
    """Return bill workflow status and allowed transitions."""
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    c.close()
    if not bill:
        return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    allowed = BILL_TRANSITIONS.get(current, [])
    return jsonify({
        "bill_id": bill["id"],
        "bill_number": bill["bill_number"],
        "current_status": current,
        "allowed_transitions": allowed,
        "total": bill["total"],
        "paid": bill["paid"],
        "outstanding": bill["outstanding"],
    })

@app.post("/api/bills/<bid>/transition")
def billing_transition_route(bid):
    """Transition a bill through its lifecycle statuses."""
    data = request.get_json(force=True)
    to_status = data.get("to_status")
    actor = data.get("actor", "User")
    reason = data.get("reason", "")
    if not to_status:
        return jsonify(error="to_status is required"), 400
    if to_status not in BILL_STATUSES:
        return jsonify(error=f"Invalid status. Must be one of: {', '.join(BILL_STATUSES)}"), 400
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    if not _bill_transition_allowed(current, to_status):
        c.close()
        return jsonify(error=f"Transition from {current} to {to_status} not allowed. Allowed: {BILL_TRANSITIONS.get(current, [])}"), 400
    prev = current
    c.execute("UPDATE bills SET status=?, updated_at=? WHERE id=?", (to_status, now(), bill["id"]))
    audit(c, actor, "user", "Billing", f"Bill status transitioned to {to_status}", "bill", bill["id"], previous_state=prev, new_state=to_status, input_summary=json.dumps({"reason": reason}))
    c.commit(); c.close()
    return jsonify({"ok": True, "bill_id": bill["id"], "bill_number": bill["bill_number"], "previous_status": prev, "current_status": to_status})

@app.post("/api/bills/<bid>/approve")
def billing_approve_route(bid):
    """Approve a bill in review status."""
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    if current != "review":
        c.close(); return jsonify(error=f"Bill must be in 'review' status to approve. Current: {current}"), 400
    c.execute("UPDATE bills SET status='approved', updated_at=? WHERE id=?", (now(), bill["id"]))
    audit(c, actor, "user", "Billing", "Bill approved", "bill", bill["id"], previous_state=current, new_state="approved", input_summary=json.dumps({"notes": notes}))
    c.commit(); c.close()
    return jsonify({"ok": True, "bill_id": bill["id"], "bill_number": bill["bill_number"], "status": "approved"})

@app.post("/api/bills/<bid>/reject")
def billing_reject_route(bid):
    """Reject a bill, returning it to draft for correction."""
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    reason = data.get("reason", "")
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    if current not in ("review", "validation"):
        c.close(); return jsonify(error=f"Bill must be in 'review' or 'validation' status to reject. Current: {current}"), 400
    c.execute("UPDATE bills SET status='draft', updated_at=? WHERE id=?", (now(), bill["id"]))
    audit(c, actor, "user", "Billing", "Bill rejected", "bill", bill["id"], previous_state=current, new_state="draft", input_summary=json.dumps({"reason": reason}))
    c.commit(); c.close()
    return jsonify({"ok": True, "bill_id": bill["id"], "bill_number": bill["bill_number"], "status": "draft"})

@app.post("/api/bills/<bid>/correct")
def billing_correct_route(bid):
    """Mark a bill as needing correction, returning to validation."""
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    reason = data.get("reason", "")
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    if current != "review":
        c.close(); return jsonify(error=f"Bill must be in 'review' status to request correction. Current: {current}"), 400
    c.execute("UPDATE bills SET status='validation', updated_at=? WHERE id=?", (now(), bill["id"]))
    audit(c, actor, "user", "Billing", "Bill correction requested", "bill", bill["id"], previous_state=current, new_state="validation", input_summary=json.dumps({"reason": reason}))
    c.commit(); c.close()
    return jsonify({"ok": True, "bill_id": bill["id"], "bill_number": bill["bill_number"], "status": "validation"})

@app.get("/api/bills/<bid>/line-items")
def billing_line_items_list(bid):
    """List line items for a bill."""
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    rows = c.execute("SELECT * FROM bill_line_items WHERE bill_id=? ORDER BY created_at", (bill["id"],)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/bills/<bid>/line-items")
def billing_line_item_create(bid):
    """Add a line item to a bill."""
    data = request.get_json(force=True)
    service_id = data.get("service_id")
    line_code = data.get("line_code", "")
    description = data.get("description", "")
    amount = float(data.get("amount", 0) or 0)
    if amount <= 0:
        return jsonify(error="amount must be positive"), 400
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    if service_id:
        svc = c.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        if not svc:
            c.close(); return jsonify(error="Service not found"), 404
    lid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO bill_line_items(id, bill_id, service_id, line_code, description, amount, created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (lid, bill["id"], service_id, line_code, description, amount, now())
    )
    c.commit(); row = c.execute("SELECT * FROM bill_line_items WHERE id=?", (lid,)).fetchone(); c.close()
    return jsonify(dict(row)), 201

@app.post("/api/bills/<bid>/to-claim")
def billing_to_claim_route(bid):
    """Create a claim from an approved bill (bill-to-claim integration)."""
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    payer_type = data.get("payer_type", "self_pay")
    payer_name = data.get("payer_name", "Self Pay")
    claim_type = data.get("claim_type", "professional")
    diagnosis_codes = data.get("diagnosis_codes")
    procedure_codes = data.get("procedure_codes")
    c = get_db()
    bill = c.execute("SELECT * FROM bills WHERE id=? OR bill_number=?", (bid, bid)).fetchone()
    if not bill:
        c.close(); return jsonify(error="Bill not found"), 404
    current = bill["status"] or "draft"
    if current not in ("approved", "claims_ready"):
        c.close(); return jsonify(error=f"Bill must be 'approved' or 'claims_ready' to create a claim. Current: {current}"), 400
    # Check for existing claim from this bill
    existing = c.execute("SELECT id FROM claims WHERE bill_id=? LIMIT 1", (bill["id"],)).fetchone()
    if existing:
        c.close(); return jsonify(error="Claim already exists for this bill"), 409
    enc = c.execute("SELECT * FROM encounters WHERE id=?", (bill["encounter_id"],)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    cid = str(uuid.uuid4())
    claim_number = data.get("claim_number") or ("CLM-" + str(uuid.uuid4())[:8].upper())
    dx = json.dumps(diagnosis_codes) if diagnosis_codes else None
    proc = json.dumps(procedure_codes) if procedure_codes else None
    total = float(bill["total"] or 0)
    c.execute(
        """INSERT INTO claims(id, claim_number, encounter_id, patient_id, bill_id, payer_type, payer_name, claim_type, diagnosis_codes, procedure_codes, total_charges, status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?)""",
        (cid, claim_number, enc["id"], bill["patient_id"], bill["id"], payer_type, payer_name, claim_type, dx, proc, total, now(), now())
    )
    # Advance bill to claims_ready
    c.execute("UPDATE bills SET status='claims_ready', updated_at=? WHERE id=?", (now(), bill["id"]))
    audit(c, actor, "user", "Billing", "Claim created from bill", "bill", bill["id"], previous_state=current, new_state="claims_ready", input_summary=json.dumps({"claim_id": cid, "claim_number": claim_number}))
    c.commit(); row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone(); c.close()
    return jsonify(dict(row)), 201

# ---------------- Claims ----------------
@app.get("/api/claims")
def list_claims():
    c = get_db()
    rows = c.execute("SELECT * FROM claims ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.get("/api/claims/<cid>")
def get_claim(cid):
    c = get_db()
    row = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (cid, cid)).fetchone()
    c.close()
    if not row: return jsonify(error="Claim not found"), 404
    return jsonify(dict(row))

@app.post("/api/claims")
def create_claim():
    data = request.get_json(force=True)
    encounter_id = data.get("encounter_id")
    patient_id = data.get("patient_id")
    if not encounter_id or not patient_id:
        return jsonify(error="encounter_id and patient_id are required"), 400
    c = get_db()
    enc = c.execute("SELECT id, patient_id FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc: c.close(); return jsonify(error="Encounter not found"), 404
    pat = c.execute("SELECT id FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    if not pat: c.close(); return jsonify(error="Patient not found"), 404
    cid = str(uuid.uuid4())
    claim_number = data.get("claim_number") or ("CLM-" + str(uuid.uuid4())[:8].upper())
    dx = json.dumps(data.get("diagnosis_codes")) if data.get("diagnosis_codes") else None
    proc = json.dumps(data.get("procedure_codes")) if data.get("procedure_codes") else None
    total = float(data.get("total_charges", 0) or 0)
    c.execute("INSERT INTO claims(id, claim_number, encounter_id, patient_id, bill_id, payer_type, payer_name, claim_type, diagnosis_codes, procedure_codes, total_charges, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?)",
              (cid, claim_number, enc["id"], pat["id"], data.get("bill_id"), data.get("payer_type","self_pay"), data.get("payer_name"), data.get("claim_type","professional"), dx, proc, total, now(), now()))
    audit(c, "Claims Preparation", "agent", "Claims Preparation", "Claim created", "claim", cid, new_state="draft")
    # Event-driven trigger: claim created → claims vetting task
    try:
        orch.create_orchestrator_task(
            c, "claim_vet", "claim", cid,
            agent_name="Claims Validation Agent",
            direction="agent_to_human",
            priority="medium",
            context=f"Claim {claim_number} created as draft — vetting required before submission",
            ai_recommendation="Vet claim against payer rules, coding standards, and missing charges",
            financial_impact=total,
            auto_assign=True,
        )
    except Exception:
        pass
    c.commit(); row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone(); c.close()
    return jsonify(dict(row)), 201

@app.post("/api/claims/<cid>/submit")
def submit_claim(cid):
    c = get_db()
    claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (cid, cid)).fetchone()
    if not claim: c.close(); return jsonify(error="Claim not found"), 404
    c.execute("UPDATE claims SET status='submitted', submitted_at=?, updated_at=? WHERE id=?", (now(), now(), claim["id"]))
    audit(c, "User", "user", "Claims Preparation", "Claim submitted (simulated)", "claim", claim["id"], previous_state=claim["status"], new_state="submitted")
    c.commit(); c.close()
    try: followup_mod.ensure_followup(cid)
    except Exception: pass
    return jsonify(ok=True, status="submitted")

@app.post("/api/claims/<cid>/vet")
def vet_claim_route(cid):
    r = vet_claim(cid); return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/claims/<cid>/standards")
def standards_claim_route(cid):
    r = run_standards_rules(cid); return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

# ---------------- Payer rules ----------------
@app.get("/api/payer-rules")
def list_payer_rules():
    c = get_db(); rows = c.execute("SELECT * FROM payer_rules ORDER BY created_at").fetchall(); c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/payer-rules")
def create_payer_rule():
    data = request.get_json(force=True)
    if not data.get("rule_name") or not data.get("rule_type"): return jsonify(error="rule_name and rule_type required"), 400
    c = get_db(); rid = str(uuid.uuid4())
    c.execute("INSERT INTO payer_rules(id, rule_name, rule_type, description, condition, severity, is_active, is_simulated, created_at, updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
              (rid, data["rule_name"], data["rule_type"], data.get("description"), json.dumps(data.get("condition",{})), data.get("severity","medium"), 1, now(), now()))
    audit(c, "User", "user", "Standards Review", "Payer rule created", "payer_rule", rid, new_state="active")
    c.commit(); row = c.execute("SELECT * FROM payer_rules WHERE id=?", (rid,)).fetchone(); c.close()
    return jsonify(dict(row)), 201

# ---------------- Documentation reviews ----------------
@app.post("/api/encounters/<eid>/documentation-review")
def documentation_review_route(eid):
    r = review_documentation(eid); return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.get("/api/documentation-reviews")
def list_documentation_reviews():
    c = get_db(); rows = c.execute("SELECT * FROM documentation_reviews ORDER BY created_at DESC").fetchall(); c.close()
    return jsonify([dict(x) for x in rows])

# ---------------- Eligibility & Coverage ----------------
@app.get("/api/eligibility/patients/<patient_id>")
def eligibility_patient_route(patient_id):
    return jsonify(check_eligibility(patient_id))

@app.get("/api/eligibility/encounters/<encounter_id>")
def eligibility_encounter_route(encounter_id):
    return jsonify(check_encounter_eligibility(encounter_id))

@app.post("/api/eligibility/encounters/<encounter_id>/task")
def eligibility_task_route(encounter_id):
    data = request.get_json(silent=True) or {}
    patient_id = data.get("patient_id")
    priority = data.get("priority", "medium")
    if not patient_id:
        return jsonify(error="patient_id is required"), 400
    c = get_db()
    result = create_eligibility_task(c, encounter_id, patient_id, priority)
    c.commit(); c.close()
    return jsonify(result)

# ---------------- Prior Authorization ----------------
@app.get("/api/prior-auth/encounters/<encounter_id>")
def prior_auth_encounter_route(encounter_id):
    return jsonify(check_authorizations(encounter_id))

@app.post("/api/prior-auth/encounters/<encounter_id>/task")
def prior_auth_task_route(encounter_id):
    data = request.get_json(silent=True) or {}
    patient_id = data.get("patient_id")
    priority = data.get("priority", "high")
    if not patient_id:
        return jsonify(error="patient_id is required"), 400
    c = get_db()
    result = create_auth_task(c, encounter_id, patient_id, priority)
    c.commit(); c.close()
    return jsonify(result)

@app.post("/api/prior-auth/authorizations")
def prior_auth_create_route():
    data = request.get_json(force=True)
    patient_id = data.get("patient_id")
    service_name = data.get("service_name")
    amount = float(data.get("amount", 0) or 0)
    payer = data.get("payer", "self_pay")
    authorized_by = data.get("authorized_by", "User")
    expires_at = data.get("expires_at")
    if not patient_id or not service_name:
        return jsonify(error="patient_id and service_name are required"), 400
    c = get_db()
    result = create_authorization(c, patient_id, service_name, amount, payer, authorized_by, expires_at)
    c.commit(); c.close()
    return jsonify(result), 201

@app.post("/api/documentation-reviews/<rid>/resolve")
def resolve_documentation_review_route(rid):
    data = request.get_json(force=True)
    r = resolve_documentation_review(rid, data.get("decision"), data.get("reviewer_notes",""))
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

# ---------------- Billing agent ----------------
@app.post("/api/encounters/<eid>/bill")
def billing_generate_route(eid):
    r = generate_bill(eid); return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

# ---------------- Leakage ----------------
@app.get("/api/leakage")
def leakage_route():
    snap = leakage_mod.detect_leakage(create_tasks=True)
    return jsonify(snap)

@app.get("/api/leakage/cases")
def leakage_cases_list_route():
    filters = request.args.to_dict()
    return jsonify(leakage_mod.list_leakage_cases(filters=filters if filters else None))

@app.get("/api/leakage/cases/<case_id>")
def leakage_case_get_route(case_id):
    case = leakage_mod.get_leakage_case(case_id)
    if not case:
        return jsonify(error="Leakage case not found"), 404
    return jsonify(case)

@app.post("/api/leakage/cases/<case_id>/assign")
def leakage_case_assign_route(case_id):
    data = request.get_json(force=True)
    assigned_user = data.get("assigned_user", "Unassigned")
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.assign_leakage_case(case_id, assigned_user, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/prioritize")
def leakage_case_prioritize_route(case_id):
    data = request.get_json(force=True)
    priority = data.get("priority", "medium")
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.prioritize_leakage_case(case_id, priority, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/confirm")
def leakage_case_confirm_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.confirm_leakage_case(case_id, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/require-action")
def leakage_case_require_action_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.require_action(case_id, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/dismiss")
def leakage_case_dismiss_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.dismiss_leakage_case(case_id, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/false-positive")
def leakage_case_false_positive_route(case_id):
    data = request.get_json(force=True)
    reason = data.get("false_positive_reason", "")
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.mark_false_positive(case_id, reason, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/reopen")
def leakage_case_reopen_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.reopen_leakage_case(case_id, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/start-action")
def leakage_case_start_action_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.start_corrective_action(case_id, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/resolve")
def leakage_case_resolve_route(case_id):
    data = request.get_json(force=True)
    resolved_by = data.get("resolved_by", "User")
    resolution_notes = data.get("resolution_notes", None)
    r = leakage_mod.resolve_leakage_case(case_id, resolved_by, resolution_notes)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/notes")
def leakage_case_notes_route(case_id):
    data = request.get_json(force=True)
    notes = data.get("notes", "")
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.add_case_notes(case_id, notes, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/confirmed-amount")
def leakage_case_confirmed_amount_route(case_id):
    data = request.get_json(force=True)
    amount = float(data.get("amount", 0))
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.record_confirmed_amount(case_id, amount, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.post("/api/leakage/cases/<case_id>/recovered-amount")
def leakage_case_recovered_amount_route(case_id):
    data = request.get_json(force=True)
    amount = float(data.get("amount", 0))
    resolved_by = data.get("resolved_by", "User")
    r = leakage_mod.record_recovered_amount(case_id, amount, resolved_by)
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)

@app.get("/api/leakage/analytics")
def leakage_analytics_route():
    return jsonify(leakage_mod.leakage_analytics())

@app.get("/api/leakage/categories")
def leakage_categories_route():
    return jsonify({
        "categories": leakage_mod.CATEGORY_MAP,
        "severities": leakage_mod.SEVERITY_MAP,
        "priorities": leakage_mod.PRIORITY_MAP,
        "lifecycle": {
            "valid_transitions": leakage_mod.VALID_TRANSITIONS,
            "terminal_states": list(leakage_mod.TERMINAL_STATES),
            "confirmed_states": list(leakage_mod.CONFIRMED_STATES),
        },
    })

# ---------------- Denials ----------------
@app.post("/api/denials/detect")
def denial_detect_route(): return jsonify(detect_denials())
@app.get("/api/denials")
def denial_list_route(): return jsonify(list_denials())
@app.post("/api/denials/<did>/resolve")
def denial_resolve_route(did):
    data = request.get_json(force=True)
    r = resolve_denial(did, data.get("decision"), data.get("corrective_action",""), bool(data.get("resubmit",False)))
    return (jsonify(r[0]), r[1]) if isinstance(r, tuple) else jsonify(r)
@app.get("/api/denials/patterns")
def denial_patterns_route(): return jsonify(denial_patterns())

# ---------------- Appeals ----------------
@app.post("/api/appeals/detect")
def appeals_detect_route():
    data = request.get_json(silent=True) or {}
    return jsonify(appeals_mod.detect_and_create_appeals(data.get("claim_id")))
@app.get("/api/appeals")
def appeals_list_route(): return jsonify(appeals_mod.list_appeals(request.args.to_dict()))
@app.get("/api/appeals/<aid>")
def appeals_get_route(aid):
    r = appeals_mod.get_appeal(aid); return (jsonify(r) if r else (jsonify(error="Not found"),404))
@app.post("/api/appeals/<aid>/status")
def appeals_status_route(aid):
    data = request.get_json(force=True)
    return jsonify(appeals_mod.update_appeal_status(aid, data.get("status",""), "User", data.get("notes","")))
@app.get("/api/appeals/metrics")
def appeals_metrics_route(): return jsonify(appeals_mod.appeal_metrics())

# ---------------- Follow-ups ----------------
@app.post("/api/followups/ensure")
def followup_ensure_route():
    data = request.get_json(force=True)
    return jsonify(followup_mod.ensure_followup(data.get("claim_id"), data.get("assigned_staff","Unassigned"), data.get("priority","medium")))
@app.get("/api/followups")
def followup_list_route(): return jsonify(followup_mod.list_followups(request.args.to_dict()))
@app.post("/api/followups/<fid>/update")
def followup_update_route(fid):
    return jsonify(followup_mod.update_followup(fid, request.get_json(force=True), "User"))
@app.post("/api/followups/<fid>/attempt")
def followup_attempt_route(fid):
    d = request.get_json(force=True)
    return jsonify(followup_mod.add_attempt(fid, d.get("method","phone"), d.get("note",""), d.get("outcome","")))
@app.post("/api/followups/detect-overdue")
def followup_overdue_route():
    d = request.get_json(silent=True) or {}
    return jsonify(followup_mod.detect_overdue(int(d.get("days_threshold",14))))
@app.get("/api/followups/metrics")
def followup_metrics_route(): return jsonify(followup_mod.followup_metrics())

# ---------------- Collections ----------------
@app.get("/api/collections/balances")
def collections_balances_route(): return jsonify(collections_mod.list_balances(request.args.to_dict()))
@app.post("/api/collections/balances/ensure")
def collections_ensure_route():
    d = request.get_json(force=True)
    return jsonify(collections_mod.ensure_balance(d.get("patient_id"), d.get("encounter_id"), d.get("payer_type","self_pay"), d.get("encounter_type","outpatient")))
@app.post("/api/collections/balances/<bid>/status")
def collections_balance_status_route(bid):
    d = request.get_json(force=True)
    return jsonify(collections_mod.set_balance_status(bid, d.get("status",""), "User", d.get("note","")))
@app.post("/api/collections/payments")
def collections_payment_route():
    d = request.get_json(force=True)
    r = collections_mod.record_payment(d.get("balance_id"), float(d.get("amount",0)), d.get("payment_method","Cash"), d.get("reference",""), "User", d.get("payment_status","confirmed"))
    return (jsonify(r),400) if isinstance(r,dict) and r.get("error") else jsonify(r)
@app.post("/api/collections/payments/<pid>/reverse")
def collections_reverse_route(pid):
    d = request.get_json(silent=True) or {}
    return jsonify(collections_mod.reverse_payment(pid, "User", d.get("reason","")))
@app.get("/api/collections/payments")
def collections_payments_list_route(): return jsonify(collections_mod.list_patient_payments(request.args.get("balance_id")))
@app.get("/api/collections/tasks")
def collections_tasks_route(): return jsonify(collections_mod.list_collection_tasks())
@app.post("/api/collections/tasks")
def collections_task_create_route():
    d = request.get_json(force=True)
    return jsonify(collections_mod.create_collection_task(d.get("balance_id"), d.get("priority","medium"), d.get("assigned_staff","Unassigned"), d.get("note","")))
@app.post("/api/collections/tasks/<tid>/attempt")
def collections_attempt_route(tid):
    d = request.get_json(force=True)
    return jsonify(collections_mod.add_collection_attempt(tid, d.get("channel","SMS"), d.get("template","outstanding_balance"), d.get("outcome","sent"), d.get("note","")))
@app.get("/api/collections/metrics")
def collections_metrics_route(): return jsonify(collections_mod.collections_metrics())
@app.get("/api/collections/template/<kind>")
def collections_template_route(kind):
    return jsonify(collections_mod.communication_template(kind, request.args.get("name","Patient"), float(request.args.get("amount",0)), request.args.get("due_date","")))

# ---------------- Financial Integrity ----------------
@app.post("/api/integrity/scan")
def integrity_scan_route(): return jsonify(fraud_mod.run_detection())
@app.get("/api/integrity/alerts")
def integrity_alerts_route(): return jsonify(fraud_mod.list_alerts(request.args.get("status")))
@app.get("/api/integrity/alerts/<aid>")
def integrity_alert_route(aid):
    r = fraud_mod.get_alert(aid); return (jsonify(r) if r.get("alert") else (jsonify(error="Not found"),404))
@app.post("/api/integrity/alerts/<aid>/investigation")
def integrity_update_route(aid):
    return jsonify(fraud_mod.update_investigation(aid, request.get_json(force=True), "User"))
@app.get("/api/integrity/metrics")
def integrity_metrics_route(): return jsonify(fraud_mod.integrity_metrics())

# ---------------- Analytics ----------------
@app.get("/api/analytics/overview")
def analytics_overview_route(): return jsonify(analytics_mod.overview(request.args.get("start"), request.args.get("end")))
@app.get("/api/analytics/claims")
def analytics_claims_route(): return jsonify(analytics_mod.claims_analytics(request.args.get("start"), request.args.get("end"), request.args.get("payer"), request.args.get("status")))
@app.get("/api/analytics/revenue")
def analytics_revenue_route(): return jsonify(analytics_mod.revenue_analytics(request.args.get("start"), request.args.get("end")))
@app.get("/api/analytics/leakage")
def analytics_leakage_route(): return jsonify(analytics_mod.leakage_analytics())
@app.get("/api/analytics/ar-aging")
def analytics_ar_route(): return jsonify(analytics_mod.ar_aging())
@app.get("/api/analytics/agents")
def analytics_agents_route(): return jsonify(analytics_mod.agent_performance())

def _csv_response(content, filename):
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.get("/api/reports/daily-revenue.csv")
def report_daily_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT date(created_at) day, SUM(total) billed, SUM(paid) collected, SUM(outstanding) outstanding FROM bills GROUP BY day ORDER BY day DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["day","billed","collected","outstanding"]),"daily_revenue.csv")
@app.get("/api/reports/claims.csv")
def report_claims_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT claim_number,payer_name,payer_type,status,total_charges,approved_amount,paid_amount,denial_reason,created_at FROM claims ORDER BY created_at DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["claim_number","payer_name","payer_type","status","total_charges","approved_amount","paid_amount","denial_reason","created_at"]),"claims_report.csv")
@app.get("/api/reports/rejection.csv")
def report_rejection_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT claim_number,payer_name,denial_reason,denial_category,total_charges,status,created_at FROM claims WHERE status IN ('denied','rejected') ORDER BY created_at DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["claim_number","payer_name","denial_reason","denial_category","total_charges","status","created_at"]),"rejection_report.csv")
@app.get("/api/reports/leakage.csv")
def report_leakage_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT category,amount,estimated,entity_type,entity_id,status,evidence,recommended_action,created_at FROM leakage_cases ORDER BY created_at DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["category","amount","estimated","entity_type","entity_id","status","evidence","recommended_action","created_at"]),"leakage_report.csv")
@app.get("/api/reports/ar-aging.csv")
def report_ar_route():
    data = analytics_mod.ar_aging(); flat=[]
    for b,v in data.get("buckets",{}).items():
        for item in v.get("items",[]): item["bucket"]=b; flat.append(item)
    return _csv_response(analytics_mod.export_csv(flat,["bucket","claim_number","payer","outstanding","age_days"]),"ar_aging.csv")
@app.get("/api/reports/agent-performance.csv")
def report_agents_route():
    return _csv_response(analytics_mod.export_csv(analytics_mod.agent_performance().get("agents",[]),["agent","tasks","success","failed","needs_review","avg_duration_ms","error_rate"]),"agent_performance.csv")
@app.get("/api/reports/appeals.csv")
def report_appeals_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT a.*, c.claim_number FROM appeals a LEFT JOIN claims c ON a.claim_id=c.id ORDER BY a.created_at DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["claim_number","denial_category","status","payer_name","amount_under_appeal","deadline","created_at"]),"appeals_report.csv")
@app.get("/api/reports/collections.csv")
def report_collections_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT b.*, p.patient_id mrn, e.encounter_id enc_no FROM patient_balances b LEFT JOIN patients p ON b.patient_id=p.id LEFT JOIN encounters e ON b.encounter_id=e.id").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["mrn","enc_no","expected_copayment","amount_paid","outstanding","status","created_at"]),"collections_report.csv")
@app.get("/api/reports/integrity.csv")
def report_integrity_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT detection_date,risk_score,risk_level,anomaly_category,estimated_exposure,status,detection_rule,explanation FROM fraud_alerts ORDER BY created_at DESC").fetchall()]; c.close()
    return _csv_response(analytics_mod.export_csv(rows,["detection_date","risk_score","risk_level","anomaly_category","estimated_exposure","status","detection_rule","explanation"]),"integrity_report.csv")

# ---------------- Exceptions / Audit / Orchestrator ----------------
@app.get("/api/exceptions")
def exceptions_list_route():
    c=get_db(); rows=c.execute("SELECT * FROM exceptions ORDER BY created_at DESC").fetchall(); c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/exceptions/<xid>/resolve")
def exceptions_resolve_route(xid):
    data = request.get_json(force=True)
    c=get_db(); r=c.execute("SELECT * FROM exceptions WHERE id=?",(xid,)).fetchone()
    if not r: c.close(); return jsonify(error="Not found"),404
    status=data.get("status","resolved")
    c.execute("UPDATE exceptions SET status=?, assigned_reviewer=?, resolution_notes=?, updated_at=?, resolved_at=? WHERE id=?",
              (status,data.get("assigned_reviewer"),data.get("resolution_notes"),now(), now() if status in ("resolved","closed") else None, xid))
    audit(c,"Human Reviewer","user","Exception Management",f"Exception {status}","exception",xid,previous_state=r["status"],new_state=status,human_reviewer=data.get("assigned_reviewer"),review_decision=status)
    c.commit(); c.close(); return jsonify(ok=True)

@app.get("/api/audit")
def audit_list_route():
    c=get_db(); q="SELECT * FROM audit_trail WHERE 1=1"; params=[]
    for k in ("agent_name","entity_type","entity_id","workflow_id","actor"):
        v=request.args.get(k)
        if v: q+=f" AND {k}=?"; params.append(v)
    q+=" ORDER BY timestamp DESC LIMIT 500"
    rows=c.execute(q,params).fetchall(); c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/audit/export.csv")
def audit_export_route():
    c=get_db(); rows=[dict(r) for r in c.execute("SELECT * FROM audit_trail ORDER BY timestamp DESC").fetchall()]; c.close()
    cols=["id","timestamp","actor","actor_type","agent_name","action","entity_type","entity_id","previous_state","new_state","recommendation","decision","execution_duration_ms","workflow_id","input_summary","output_summary","exception","human_reviewer","review_decision"]
    return _csv_response(analytics_mod.export_csv(rows,cols),"audit_trail.csv")

@app.get("/api/orchestrator/state")
def orchestrator_state_route():
    return jsonify({"agents": orch.get_agent_states(), "ts": now()})

@app.get("/api/orchestrator/activity")
def orchestrator_activity_route():
    c=get_db(); rows=c.execute("SELECT * FROM agent_executions ORDER BY created_at DESC LIMIT 50").fetchall(); c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/stream")
def stream_route():
    import time as _t
    def gen():
        while True:
            c=get_db(); rows=c.execute("SELECT * FROM agent_executions ORDER BY created_at DESC LIMIT 20").fetchall(); c.close()
            data=json.dumps({"ts": now(), "agents": orch.get_agent_states(), "recent": [dict(r) for r in rows]})
            yield f"data: {data}\n\n"
            _t.sleep(3)
    return Response(gen(), mimetype="text/event-stream")

# ---------------- AI Orchestrator: Task Lifecycle & Workspaces ----------------
ORCHESTRATOR_TASK_STATUSES = ["created","assigned","started","completed","reviewed","escalated","blocked"]

@app.get("/api/orchestrator/tasks")
def orchestrator_tasks_list():
    c = get_db()
    agent = request.args.get("agent")
    status = request.args.get("status")
    direction = request.args.get("direction")
    q = "SELECT * FROM orchestrator_tasks WHERE 1=1"; params=[]
    if agent: q += " AND agent_name=?"; params.append(agent)
    if status: q += " AND status=?"; params.append(status)
    if direction: q += " AND direction=?"; params.append(direction)
    q += " ORDER BY created_at DESC"
    rows = [dict(r) for r in c.execute(q, params).fetchall()]
    c.close()
    return jsonify(rows)

@app.post("/api/orchestrator/tasks")
def orchestrator_task_create():
    data = request.get_json(force=True)
    task_type = data.get("task_type")
    entity_type = data.get("entity_type")
    entity_id = data.get("entity_id")
    agent_name = data.get("agent_name","Orchestrator")
    direction = data.get("direction","agent_to_human")
    priority = data.get("priority","medium")
    context = data.get("context","")
    evidence = data.get("evidence","")
    ai_recommendation = data.get("ai_recommendation","")
    financial_impact = float(data.get("financial_impact") or 0)
    assigned_to = data.get("assigned_to")
    assigned_by = data.get("assigned_by","System")
    if not task_type or not entity_type or not entity_id:
        return jsonify(error="task_type, entity_type, entity_id required"), 400
    c = get_db()
    tid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO orchestrator_tasks(id,task_type,entity_type,entity_id,status,priority,assigned_to,assigned_by,agent_name,direction,context,evidence,ai_recommendation,financial_impact,created_at)
        VALUES(?,?,'created',?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, task_type, entity_type, entity_id, priority, assigned_to, assigned_by, agent_name, direction, context, evidence, ai_recommendation, financial_impact, now())
    )
    c.commit(); c.close()
    return jsonify({"task_id": tid, "status": "created"}), 201

@app.post("/api/orchestrator/tasks/<task_id>/assign")
def orchestrator_task_assign(task_id):
    data = request.get_json(force=True)
    agent_name = data.get("agent_name")
    assigned_to = data.get("assigned_to")
    direction = data.get("direction","agent_to_human")
    context = data.get("context","")
    c = get_db()
    t = c.execute("SELECT * FROM orchestrator_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return jsonify(error="Task not found"), 404
    c.execute(
        """UPDATE orchestrator_tasks SET status='assigned', agent_name=COALESCE(?,agent_name), assigned_to=COALESCE(?,assigned_to), direction=COALESCE(?,direction), context=COALESCE(?,context) WHERE id=?""",
        (agent_name, assigned_to, direction, context, task_id)
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "task_id": task_id, "status": "assigned"})

@app.post("/api/orchestrator/tasks/<task_id>/start")
def orchestrator_task_start(task_id):
    data = request.get_json(force=True)
    actor = data.get("actor","User")
    c = get_db()
    t = c.execute("SELECT * FROM orchestrator_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return jsonify(error="Task not found"), 404
    c.execute("UPDATE orchestrator_tasks SET status='started', started_at=? WHERE id=?", (now(), task_id))
    c.commit(); c.close()
    return jsonify({"ok": True, "task_id": task_id, "status": "started"})

@app.post("/api/orchestrator/tasks/<task_id>/complete")
def orchestrator_task_complete(task_id):
    data = request.get_json(force=True)
    actor = data.get("actor","User")
    c = get_db()
    t = c.execute("SELECT * FROM orchestrator_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return jsonify(error="Task not found"), 404
    c.execute(
        """UPDATE orchestrator_tasks SET status='completed', completed_at=? WHERE id=?""",
        (now(), task_id)
    )
    audit(c, actor, "user", "AI Orchestrator", f"Task completed: {t['task_type']}", "orchestrator_task", task_id, new_state="completed")
    c.commit(); c.close()
    return jsonify({"ok": True, "task_id": task_id, "status": "completed"})

@app.post("/api/orchestrator/tasks/<task_id>/escalate")
def orchestrator_task_escalate(task_id):
    data = request.get_json(force=True)
    reason = data.get("reason","")
    escalate_to = data.get("escalate_to","")
    c = get_db()
    t = c.execute("SELECT * FROM orchestrator_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return jsonify(error="Task not found"), 404
    c.execute(
        """UPDATE orchestrator_tasks SET status='escalated', assigned_to=COALESCE(?,assigned_to), context=COALESCE(?,context) WHERE id=?""",
        (escalate_to, f"Escalated: {reason}", task_id)
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "task_id": task_id, "status": "escalated"})

@app.get("/api/orchestrator/workspace/<agent_name>")
def orchestrator_workspace(agent_name):
    c = get_db()
    tasks = c.execute(
        "SELECT * FROM orchestrator_tasks WHERE agent_name=? ORDER BY created_at DESC", (agent_name,)
    ).fetchall()
    state = orch.AGENT_STATES.get(agent_name, {"name":agent_name,"status":"Idle","current_task":None,"tasks_completed":0,"exceptions":0})
    c.close()
    return jsonify({
        "agent": agent_name,
        "state": state,
        "tasks": [dict(r) for r in tasks],
        "task_count": len(tasks),
        "pending": sum(1 for r in tasks if r["status"] in ("created","assigned","started"))
    })

@app.post("/api/orchestrator/seed-ghanaian")
def orchestrator_seed_ghanaian():
    c = get_db()
    now_ts = now()
    gh_first_names = ["Kwame","Ama","Kofi","Akua","Yaw","Efua","Kwasi","Akosua","Kwaku","Yaa","Kwadwo","Abena","Kwabena","Afua","Kofi","Ama","Kwame","Akua","Yaw","Efua"]
    gh_last_names = ["Mensah","Osei","Asante","Boateng","Agyeman","Owusu","Kufuor","Darko","Bediako","Frimpong","Addo","Acheampong","Nyamekye","Ofori","Tawia","Nkrumah","Danquah","Busia","Aidoo","Aikins"]
    facilities = c.execute("SELECT id, code FROM facilities WHERE is_active=1").fetchall()
    if not facilities:
        c.close(); return jsonify(error="No facilities found"), 400
    created = {"patients":0,"encounters":0,"claims":0,"tasks":0}
    for i in range(20):
        pid = str(uuid.uuid4())
        mrn = "MRN-GH" + str(uuid.uuid4())[:6].upper()
        first = gh_first_names[i % len(gh_first_names)]
        last = gh_last_names[i % len(gh_last_names)]
        payer = "nhis" if i % 3 == 0 else ("private" if i % 3 == 1 else "self_pay")
        enc_type = "inpatient" if i % 4 == 0 else "outpatient"
        status = "completed" if i % 5 != 0 else "billing"
        fac = facilities[i % len(facilities)]
        c.execute(
            """INSERT INTO patients(id,patient_id,first_name,last_name,insurance_type,status,created_at,updated_at,facility_id)
            VALUES(?,?,?,?,?,'active',?,?,?)""",
            (pid, mrn, first, last, payer, now_ts, now_ts, fac["id"])
        )
        created["patients"] += 1
        eid = str(uuid.uuid4())
        eno = "ENC-GH" + str(uuid.uuid4())[:6].upper()
        c.execute(
            """INSERT INTO encounters(id,encounter_id,patient_id,patient_name,encounter_type,status,admission_date,reason,documentation_complete,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, eno, pid, f"{first} {last}", enc_type, status, now_ts, "Synthetic Ghanaian encounter", 1 if status=="completed" else 0, now_ts, now_ts)
        )
        add_transition(c, eid, None, status, "Encounter Tracking Agent", "Synthetic Ghanaian encounter seeded")
        created["encounters"] += 1
        total = round(150 + (i * 37.5), 2)
        clid = str(uuid.uuid4())
        clnum = "CLM-GH" + str(uuid.uuid4())[:6].upper()
        c.execute(
            """INSERT INTO claims(id,claim_number,encounter_id,patient_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,paid_amount,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
            (clid, clnum, eid, pid, payer, "NHIS Ghana" if payer=="nhis" else "Private Insurer", '["Z71.9"]','["99212"]', total, 0, 'draft', now_ts, now_ts)
        )
        created["claims"] += 1
        tid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO orchestrator_tasks(id,task_type,entity_type,entity_id,status,priority,agent_name,direction,context,created_at)
            VALUES(?,?,?,?,'created','medium','Claims Validation Agent','agent_to_human',?,?)""",
            (tid, "claim_review", "claim", clid, "Synthetic Ghanaian claim awaiting validation", now_ts)
        )
        created["tasks"] += 1
    c.commit(); c.close()
    return jsonify({"ok": True, "seeded": created, "note": "All data synthetic — Ghanaian names, NHIS/private/self-pay, GHS currency, inpatient/outpatient"})

# ---------------- AI Orchestrator: Delegation, Agent Detail, Metrics ----------------

@app.post("/api/orchestrator/tasks/<task_id>/delegate")
def orchestrator_task_delegate(task_id):
    """Delegate a task from one agent to another (agent-to-agent routing)."""
    data = request.get_json(force=True)
    from_agent = data.get("from_agent")
    to_agent = data.get("to_agent")
    if not from_agent or not to_agent:
        return jsonify(error="from_agent and to_agent required"), 400
    c = get_db()
    t = c.execute("SELECT * FROM orchestrator_tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        c.close(); return jsonify(error="Task not found"), 404
    result = orch.delegate_task(c, from_agent, to_agent, t["task_type"], t["entity_type"],
                                t["entity_id"], context=t["context"] or "",
                                priority=t["priority"] or "medium",
                                direction="agent_to_agent", assigned_by=data.get("assigned_by","System"),
                                evidence=t["evidence"] or "", ai_recommendation=t["ai_recommendation"] or "",
                                financial_impact=float(t["financial_impact"] or 0))
    c.execute(
        """UPDATE orchestrator_tasks SET status='assigned', agent_name=?, assigned_to=NULL WHERE id=?""",
        (to_agent, task_id)
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "task_id": task_id, "delegated": result})

@app.get("/api/orchestrator/agents/<agent_name>/detail")
def orchestrator_agent_detail(agent_name):
    """Full detail view for a single agent: state, tasks, performance, recent executions."""
    c = get_db()
    detail = orch.get_agent_detail(c, agent_name)
    c.close()
    return jsonify(detail)

@app.get("/api/orchestrator/metrics")
def orchestrator_metrics():
    """Orchestrator-level metrics: total tasks, by status, by agent, by direction."""
    c = get_db()
    total = c.execute("SELECT COUNT(*) n FROM orchestrator_tasks").fetchone()["n"]
    by_status = {}
    for row in c.execute("SELECT status, COUNT(*) n FROM orchestrator_tasks GROUP BY status"):
        by_status[row["status"]] = row["n"]
    by_direction = {}
    for row in c.execute("SELECT direction, COUNT(*) n FROM orchestrator_tasks GROUP BY direction"):
        by_direction[row["direction"]] = row["n"]
    by_agent = {}
    for row in c.execute("SELECT agent_name, COUNT(*) n FROM orchestrator_tasks GROUP BY agent_name"):
        by_agent[row["agent_name"]] = row["n"]
    pending = c.execute("SELECT COUNT(*) n FROM orchestrator_tasks WHERE status IN ('created','assigned','started')").fetchone()["n"]
    completed = c.execute("SELECT COUNT(*) n FROM orchestrator_tasks WHERE status='completed'").fetchone()["n"]
    escalated = c.execute("SELECT COUNT(*) n FROM orchestrator_tasks WHERE status='escalated'").fetchone()["n"]
    blocked = c.execute("SELECT COUNT(*) n FROM orchestrator_tasks WHERE status='blocked'").fetchone()["n"]
    c.close()
    return jsonify({
        "total_tasks": total,
        "pending": pending,
        "completed": completed,
        "escalated": escalated,
        "blocked": blocked,
        "by_status": by_status,
        "by_direction": by_direction,
        "by_agent": by_agent,
        "ts": now()
    })

# ---------------- Demo seeding ----------------
def _seed_patient(c, first, last, payer="self_pay"):
    pid=str(uuid.uuid4()); mrn="MRN-"+str(uuid.uuid4())[:8].upper()
    c.execute("INSERT INTO patients(id,patient_id,first_name,last_name,insurance_type,status,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",(pid,mrn,first,last,payer,now(),now()))
    add_timeline(c,pid,"Registration Agent","Seeded demo patient",f"{mrn} created","done")
    return pid,mrn
def _seed_encounter(c, patient_id, name, enc_type="outpatient", status="completed", reason="Demo visit", department=None):
    eid=str(uuid.uuid4()); eno="ENC-"+str(uuid.uuid4())[:8].upper()
    c.execute("INSERT INTO encounters(id,encounter_id,patient_id,patient_name,encounter_type,status,admission_date,reason,documentation_complete,department,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",(eid,eno,patient_id,name,enc_type,status,now(),reason,department,now(),now()))
    add_transition(c,eid,None,status,"Encounter Tracking Agent","Demo encounter created")
    return eid,eno

@app.post("/api/demo/seed-all")
def demo_seed_all():
    c = get_db()
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    # Idempotency: unless force, skip if patients already seeded
    existing = c.execute("SELECT COUNT(*) n FROM patients").fetchone()["n"]
    if existing > 0 and not force:
        c.close()
        return jsonify(ok=True, message="Demo data already seeded. Use /api/reset or ?force=1 to reseed.", already_seeded=True)
    if force:
        # Clear all demo tables for a clean, deterministic reseed
        c.execute("PRAGMA foreign_keys = OFF")
        _seed_clear_tables = [
            "events","reviews","runs","claim_followup_attempts","claim_followups","appeals",
            "collection_attempts","collection_tasks","payment_promises","patient_payments","patient_balances",
            "fraud_investigations","fraud_alerts","leakage_cases","exceptions","decision_support",
            "clinical_suggestions","encounter_transitions","registration_timeline","workflow_runs",
            "claim_vetting","documentation_reviews","denials","payments","claims","services","bills","encounters","patients",
            "agent_executions","audit_trail","reconciliations","reconciliation_line_items","payment_matches",
            "underpayment_cases","recovery_workflow","orchestrator_tasks","agent_states","agent_performance",
            "review_tasks","coding_suggestions","authorizations","claim_states","bill_line_items","charge_line_items",
        ]
        for t in _seed_clear_tables:
            try: c.execute(f"DELETE FROM {t}")
            except Exception: pass
        c.execute("PRAGMA foreign_keys = ON")
        c.commit()

    # Helper to create claim state
    def _claim_state(c, claim_id, state, reason="", actor="System"):
        sid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO claim_states(id, claim_id, current_state, previous_state, state_reason, transitioned_by, transitioned_at, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (sid, claim_id, state, None, reason, actor, now(), now(), now())
        )

    def _auth(c, patient_id, encounter_id, payer_name, payer_type, proc_code, svc_desc, status, req_docs=""):
        aid = str(uuid.uuid4())
        auth_number = "AUTH-" + str(uuid.uuid4())[:8].upper()
        c.execute(
            """INSERT INTO authorizations(id, auth_number, patient_id, encounter_id, payer_name, payer_type,
                procedure_code, service_description, status, requested_at, required_documents, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, auth_number, patient_id, encounter_id, payer_name, payer_type, proc_code, svc_desc, status, now(), req_docs, now(), now())
        )
        return aid, auth_number

    def _coding(c, encounter_id, claim_id, code_type, code_val, code_desc, conf, evidence, status="pending_review"):
        sid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO coding_suggestions(id, encounter_id, claim_id, code_type, code_value, code_description,
                confidence, evidence, status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid, encounter_id, claim_id, code_type, code_val, code_desc, conf, evidence, status, now())
        )
        return sid

    # ---- Patient 1: NHIS, Emergency, completed, billed, claim drafted (CLEAN CLAIM PATH) ----
    p1,m1=_seed_patient(c,"Ama","Mensah","nhis"); e1,en1=_seed_encounter(c,p1,"Ama Mensah","outpatient","completed","Routine check","Emergency")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e1,"Emergency Consultation",1,150,150,now()))
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e1,"Triage Assessment",1,80,80,now()))
    b1=str(uuid.uuid4()); c.execute("INSERT INTO bills(id,bill_number,encounter_id,patient_id,subtotal,tax,discount,total,paid,outstanding,status,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,'draft',?,?)",(b1,"BILL-"+str(uuid.uuid4())[:6].upper(),e1,p1,230,230,230,now(),now()))
    cl1=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,bill_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?,184,184, 'approved',?,?)",(cl1,"CLM-"+str(uuid.uuid4())[:6].upper(),e1,p1,b1,"nhis","NHIS Demo",'["Z00.0"]','["99213"]',230,now(),now()))
    _claim_state(c, cl1, "approved", "Claim validated and approved", "Claims Validation Agent")
    _auth(c, p1, e1, "NHIS Demo", "nhis", "99213", "Emergency consultation", "approved", "NHIS card, referral letter")
    _coding(c, e1, cl1, "ICD-10", "Z00.0", "Encounter for general adult medical exam", 0.92, "Clinical note supports preventive visit", "reviewed_approve")
    _coding(c, e1, cl1, "CPT", "99213", "Office outpatient visit, established patient", 0.88, "E/M documentation meets level 3 criteria", "reviewed_approve")

    # ---- Patient 2: NHIS, Internal Medicine, completed, denied claim (REJECTED CLAIM PATH) ----
    p2,m2=_seed_patient(c,"Kojo","Owusu","nhis"); e2,en2=_seed_encounter(c,p2,"Kojo Owusu","outpatient","completed","Fever","Internal Medicine")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e2,"Physician Consultation",1,120,120,now()))
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e2,"Malaria Test",1,60,60,now()))
    cl2=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,denial_reason,denial_category,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,'denied','Missing documentation to support services billed','documentation_missing',?,?)",(cl2,"CLM-"+str(uuid.uuid4())[:6].upper(),e2,p2,"nhis","NHIS Demo",'["R50.9"]','["99213"]',180,now(),now()))
    _claim_state(c, cl2, "denied", "Denied: missing documentation", "Claims Validation Agent")
    _auth(c, p2, e2, "NHIS Demo", "nhis", "99213", "Physician consultation", "pending", "NHIS card, clinical notes")
    _coding(c, e2, cl2, "ICD-10", "R50.9", "Fever, unspecified", 0.75, "Documentation does not specify fever duration", "pending_review")

    # ---- Patient 3: Private, Pediatrics, completed, unbilled service (LEAKAGE) ----
    p3,m3=_seed_patient(c,"Efua","Boateng","private"); e3,en3=_seed_encounter(c,p3,"Efua Boateng","outpatient","completed","Lab work","Pediatrics")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e3,"Pediatric Consultation",1,200,200,now()))
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,0,?)",(str(uuid.uuid4()),e3,"Lab Panel",1,300,300,now()))  # unbilled
    b3=str(uuid.uuid4()); c.execute("INSERT INTO bills(id,bill_number,encounter_id,patient_id,subtotal,tax,discount,total,paid,outstanding,status,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,'draft',?,?)",(b3,"BILL-"+str(uuid.uuid4())[:6].upper(),e3,p3,200,200,200,now(),now()))
    _auth(c, p3, e3, "Private Insurer", "private", "", "Pediatric lab panel", "approved", "Referral, pre-auth form")

    # ---- Patient 4: Self-pay, Surgery, completed, billed, paid partially ----
    p4,m4=_seed_patient(c,"Yaw","Asante","self_pay"); e4,en4=_seed_encounter(c,p4,"Yaw Asante","outpatient","completed","Consult","Surgery")
    for _ in range(2): c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e4,"Surgical Consultation",1,100,100,now()))
    b4=str(uuid.uuid4()); c.execute("INSERT INTO bills(id,bill_number,encounter_id,patient_id,subtotal,tax,discount,total,paid,outstanding,status,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,'draft',?,?)",(b4,"BILL-"+str(uuid.uuid4())[:6].upper(),e4,p4,200,200,200,now(),now()))

    # ---- Patient 5: NHIS, Laboratory, submitted claim, pending ----
    p5,m5=_seed_patient(c,"Akua","Adjei","nhis"); e5,en5=_seed_encounter(c,p5,"Akua Adjei","outpatient","completed","Blood work","Laboratory")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e5,"CBC Test",1,90,90,now()))
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e5,"Blood Glucose",1,70,70,now()))
    cl5=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,'submitted',?,?)",(cl5,"CLM-"+str(uuid.uuid4())[:6].upper(),e5,p5,"nhis","NHIS Demo",'["E11.9"]','["80053"]',160,now(),now()))
    _claim_state(c, cl5, "submitted", "Claim submitted to payer", "Claims Submission Agent")

    # ---- Patient 6: Private, Radiology, denied claim (CODING INCONSISTENCY) ----
    p6,m6=_seed_patient(c,"Kofi","Darko","private"); e6,en6=_seed_encounter(c,p6,"Kofi Darko","outpatient","completed","X-ray","Radiology")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e6,"Chest X-ray",1,250,250,now()))
    cl6=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,denial_reason,denial_category,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,'denied','Coding inconsistency','coding_error',?,?)",(cl6,"CLM-"+str(uuid.uuid4())[:6].upper(),e6,p6,"private","Private Insurer",'["J44.9"]','["71020"]',250,now(),now()))
    _claim_state(c, cl6, "denied", "Denied: coding inconsistency", "Claims Validation Agent")
    _coding(c, e6, cl6, "ICD-10", "J44.9", "Chronic obstructive pulmonary disease", 0.68, "Diagnosis not supported by encounter documentation", "pending_review")

    # ---- Patient 7: Self-pay, Cardiology, registered, no services yet ----
    p7,m7=_seed_patient(c,"Nana","Quaye","self_pay"); e7,en7=_seed_encounter(c,p7,"Nana Quaye","outpatient","registered","Visit","Cardiology")

    # ---- Patient 8: NHIS, Pharmacy, completed, billed, underpayment scenario (UNDERPAYMENT) ----
    p8,m8=_seed_patient(c,"Kwame","Osei","nhis"); e8,en8=_seed_encounter(c,p8,"Kwame Osei","outpatient","completed","Medication review","Pharmacy")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e8,"Pharmacy Consultation",1,180,180,now()))
    b8=str(uuid.uuid4()); c.execute("INSERT INTO bills(id,bill_number,encounter_id,patient_id,subtotal,tax,discount,total,paid,outstanding,status,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,'draft',?,?)",(b8,"BILL-"+str(uuid.uuid4())[:6].upper(),e8,p8,180,180,180,now(),now()))
    cl8=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,bill_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,paid_amount,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?,144,144,100, 'paid',?,?)",(cl8,"CLM-"+str(uuid.uuid4())[:6].upper(),e8,p8,b8,"nhis","NHIS Demo",'["Z71.9"]','["99212"]',180,now(),now()))
    _claim_state(c, cl8, "paid", "Claim paid by payer", "Reconciliation Agent")

    # ---- Patient 9: NHIS, Obstetrics, completed, claim vetted, ready for submission ----
    p9,m9=_seed_patient(c,"Adjoa","Frimpong","nhis"); e9,en9=_seed_encounter(c,p9,"Adjoa Frimpong","outpatient","completed","Prenatal visit","Obstetrics")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e9,"Obstetric Consultation",1,220,220,now()))
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e9,"Ultrasound Scan",1,350,350,now()))
    b9=str(uuid.uuid4()); c.execute("INSERT INTO bills(id,bill_number,encounter_id,patient_id,subtotal,tax,discount,total,paid,outstanding,status,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,'draft',?,?)",(b9,"BILL-"+str(uuid.uuid4())[:6].upper(),e9,p9,570,570,570,now(),now()))
    cl9=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,bill_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?,0,0,'vetted',?,?)",(cl9,"CLM-"+str(uuid.uuid4())[:6].upper(),e9,p9,b9,"nhis","NHIS Demo",'["O09.5"]','["76801"]',570,now(),now()))
    _claim_state(c, cl9, "vetted", "Claim passed validation, ready for submission", "Claims Validation Agent")
    _coding(c, e9, cl9, "ICD-10", "O09.5", "Supervision of elderly multigravida pregnancy", 0.95, "Prenatal documentation complete", "reviewed_approve")

    # ---- Patient 10: Private, Surgery, post-op, claim under appeal ----
    p10,m10=_seed_patient(c,"Kwesi","Mensah","private"); e10,en10=_seed_encounter(c,p10,"Kwesi Mensah","outpatient","completed","Post-op follow-up","Surgery")
    c.execute("INSERT INTO services(id,encounter_id,service_name,quantity,unit_price,total_price,billed,created_at) VALUES(?,?,?,?,?,?,1,?)",(str(uuid.uuid4()),e10,"Post-operative Consultation",1,180,180,now()))
    cl10=str(uuid.uuid4()); c.execute("INSERT INTO claims(id,claim_number,encounter_id,patient_id,payer_type,payer_name,diagnosis_codes,procedure_codes,total_charges,approved_amount,expected_reimbursement,status,denial_reason,denial_category,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,'denied','Prior authorization required','authorization_missing',?,?)",(cl10,"CLM-"+str(uuid.uuid4())[:6].upper(),e10,p10,"private","Private Insurer",'["M17.0"]','["27447"]',180,now(),now()))
    _claim_state(c, cl10, "denied", "Denied: missing prior authorization", "Claims Validation Agent")
    _auth(c, p10, e10, "Private Insurer", "private", "27447", "Knee arthroplasty", "rejected", "Op report, imaging, auth form")

    c.commit(); c.close()

    # Run agents to populate derived records
    try: collections_mod.ensure_balance(p7,e7,"self_pay","outpatient")
    except Exception: pass
    try: detect_denials(); appeals_mod.detect_and_create_appeals(); followup_mod.ensure_followup(cl2)
    except Exception: pass
    try: leakage_mod.detect_leakage(create_tasks=True)
    except Exception: pass
    try: fraud_mod.run_detection()
    except Exception: pass
    try: billing_mod.generate_bill(e1); billing_mod.generate_bill(e3); billing_mod.generate_bill(e4); billing_mod.generate_bill(e8); billing_mod.generate_bill(e9)
    except Exception: pass

    # Reopen DB connection (closed after primary seeding at line 869)
    c = get_db()

    # ---- Comprehensive data so NO section is empty ----
    # 1) Patient balances for EVERY encounter (self-pay/AR/patient-finance)
    try:
        enc_all = c.execute("SELECT id, encounter_id, patient_id, encounter_type, status FROM encounters").fetchall()
        methods = ['Cash', 'Mobile Money', 'Card', 'Bank Transfer', 'NHIS']
        for i, e in enumerate(enc_all):
            ed = dict(e)
            # compute charges from services
            svc_total = c.execute("SELECT COALESCE(SUM(total_price),0) s FROM services WHERE encounter_id=?", (ed["id"],)).fetchone()["s"]
            svc_total = float(svc_total or 0)
            if svc_total <= 0:
                svc_total = 150.0
            copay = round(svc_total * 0.1, 2)
            # vary payment state deterministically
            if i % 4 == 0:
                paid = svc_total; outstanding = 0.0; status = "paid"
            elif i % 4 == 1:
                paid = round(svc_total * 0.5, 2); outstanding = round(svc_total - paid, 2); status = "partial"
            elif i % 4 == 2:
                paid = 0.0; outstanding = svc_total; status = "outstanding"
            else:
                paid = 0.0; outstanding = svc_total; status = "pending"
            c.execute(
                """INSERT INTO patient_balances(id, patient_id, encounter_id, expected_copayment,
                    amount_due, amount_paid, outstanding, status, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), ed["patient_id"], ed["id"], copay,
                 svc_total, paid, outstanding, status, now(), now()),
            )
    except Exception as e:
        print(f"Balance seeding error: {e}")

    # 2) Bill line items for EVERY bill (so billing/leakage line-item views have data)
    try:
        bill_rows = c.execute("SELECT id, encounter_id, subtotal FROM bills").fetchall()
        for b in bill_rows:
            bd = dict(b)
            existing_li = c.execute("SELECT COUNT(*) n FROM bill_line_items WHERE bill_id=?", (bd["id"],)).fetchone()["n"]
            if existing_li:
                continue
            svcs = c.execute("SELECT id, service_name, total_price FROM services WHERE encounter_id=?", (bd["encounter_id"],)).fetchall()
            if svcs:
                for s in svcs:
                    sd = dict(s)
                    c.execute(
                        "INSERT INTO bill_line_items(id, bill_id, service_id, line_code, description, amount, created_at) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), bd["id"], sd["id"], "LINE-" + str(uuid.uuid4())[:4].upper(),
                         sd["service_name"], float(sd["total_price"] or 0), now()),
                    )
            else:
                c.execute(
                    "INSERT INTO bill_line_items(id, bill_id, service_id, line_code, description, amount, created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), bd["id"], None, "LINE-1", "Service charge", float(bd["subtotal"] or 0), now()),
                )
    except Exception as e:
        print(f"Line item seeding error: {e}")

    # 3) Claim follow-ups for EVERY denied claim (AR & follow-up)
    try:
        denied = c.execute("SELECT id, claim_number, patient_id FROM claims WHERE status IN ('denied','rejected')").fetchall()
        for cl in denied:
            cd = dict(cl)
            ex = c.execute("SELECT COUNT(*) n FROM claim_followups WHERE claim_id=?", (cd["id"],)).fetchone()["n"]
            if ex:
                continue
            c.execute(
                """INSERT INTO claim_followups(id, claim_id, status, priority, assigned_staff,
                    due_date, next_followup_date, notes, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), cd["id"], "open", "high", "Unassigned",
                 now(), now(), "Auto-created for denied claim follow-up", now(), now()),
            )
    except Exception as e:
        print(f"Follow-up seeding error: {e}")

    # 4) Denials for every denied/rejected claim (denial management)
    try:
        denied2 = c.execute("SELECT * FROM claims WHERE status IN ('denied','rejected')").fetchall()
        for cl in denied2:
            cd = dict(cl)
            ex = c.execute("SELECT COUNT(*) n FROM denials WHERE claim_id=?", (cd["id"],)).fetchone()["n"]
            if ex:
                continue
            c.execute(
                """INSERT INTO denials(id, claim_id, denial_date, denial_reason, denial_category,
                    corrective_action, status, resubmission_count, resolved_at, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), cd["id"], now(), cd.get("denial_reason") or "Denied by payer",
                 cd.get("denial_category") or "other", "Review and correct", "open", 0, None, now()),
            )
    except Exception as e:
        print(f"Denial seeding error: {e}")

    # Seed documentation reviews for encounters
    try:
        enc_rows = c.execute("SELECT id, encounter_id, patient_id FROM encounters WHERE status='completed' LIMIT 10").fetchall()
        review_types = ['clinical', 'coding', 'documentation']
        statuses = ['pending', 'approved', 'rejected', 'corrections_requested']
        for i, enc in enumerate(enc_rows):
            enc_d = dict(enc)
            rid = str(uuid.uuid4())
            findings = json.dumps([{"finding": "Missing clinical note signature", "explanation": "Provider signature not found on encounter documentation", "recommended_correction": "Add provider signature and date", "field": "signature", "severity": "medium"}])
            c.execute(
                """INSERT INTO documentation_reviews(id, encounter_id, review_type, findings, status, human_review_required, reviewer_decision, reviewer_notes, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (rid, enc_d["id"], review_types[i % len(review_types)], findings, statuses[i % len(statuses)], 1, None, None, now())
            )
    except Exception as e:
        print(f"Doc review seeding error: {e}")

    # Seed patient payments for claims with approved_amount > 0 (linked to a real balance)
    try:
        paid_claims = c.execute("SELECT id, claim_number, patient_id, encounter_id, approved_amount, paid_amount FROM claims WHERE approved_amount > 0").fetchall()
        methods = ['Cash', 'Mobile Money', 'Card', 'Bank Transfer', 'NHIS']
        for i, cl in enumerate(paid_claims):
            cl_d = dict(cl)
            amt = float(cl_d["paid_amount"] or cl_d["approved_amount"] or 0)
            if amt > 0:
                pid = str(uuid.uuid4())
                bal = c.execute("SELECT id FROM patient_balances WHERE encounter_id=? LIMIT 1", (cl_d["encounter_id"],)).fetchone()
                c.execute(
                    """INSERT INTO patient_payments(id, balance_id, patient_id, encounter_id, amount, payment_method, payment_date, payment_status, reference, recorded_by, reconciliation_status, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, bal["id"] if bal else None, cl_d["patient_id"], cl_d["encounter_id"], amt, methods[i % len(methods)], now(), 'confirmed', f"PAY-{i+1:03d}", 'System', 'reconciled', now())
                )
    except Exception as e:
        print(f"Patient payment seeding error: {e}")

    # Seed review tasks
    try:
        # Get a valid user ID for assigned_to FK
        user_row = c.execute("SELECT id FROM users LIMIT 1").fetchone()
        assigned_to = user_row["id"] if user_row else None
        rev_rows = c.execute("SELECT id FROM documentation_reviews WHERE status='pending' LIMIT 5").fetchall()
        for i, rev in enumerate(rev_rows):
            rev_d = dict(rev)
            tid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO review_tasks(id, task_type, entity_type, entity_id, status, assigned_to, priority, context, created_at, resolved_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tid, 'documentation_review', 'documentation_review', rev_d["id"], 'pending', assigned_to, 'high', 'Documentation review required for encounter', now(), None)
            )
    except Exception as e:
        print(f"Review task seeding error: {e}")

    # Seed agent executions
    try:
        agents = [
            ('Reconciliation Agent', 'reconcile_claim', 'claim', 'completed'),
            ('Underpayment Detection Agent', 'detect_underpayments', 'system', 'completed'),
            ('Denial Management Agent', 'detect_denials', 'claim', 'completed'),
            ('Revenue Integrity Agent', 'service_to_revenue_completeness', 'encounter', 'completed'),
            ('Fraud Detection Agent', 'run_detection', 'system', 'completed'),
        ]
        for i, (aname, action, entity_type, status) in enumerate(agents):
            eid = str(uuid.uuid4())
            c.execute(
                """INSERT INTO agent_executions(id, agent_name, task_name, entity_type, entity_id, status, started_at, completed_at, input_data, output_data, duration_ms, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, aname, action, entity_type, None, status, now(), now(), json.dumps({"records_processed": i+1}), json.dumps({"records_processed": i+1}), 150+i*50, now())
            )
    except Exception as e:
        print(f"Agent execution seeding error: {e}")

    # Seed reconciliations for claims with approved_amount > 0
    try:
        rec_claims = c.execute("SELECT id, claim_number, patient_id, approved_amount, expected_reimbursement, payer_name FROM claims WHERE approved_amount > 0 LIMIT 5").fetchall()
        for i, cl in enumerate(rec_claims):
            cl_d = dict(cl)
            rid = str(uuid.uuid4())
            exp = float(cl_d["expected_reimbursement"] or cl_d["approved_amount"] or 0)
            rec_amt = exp * 0.95 if i % 2 == 0 else exp
            out = round(exp - rec_amt, 2)
            var = round(rec_amt - exp, 2)
            status = "Reconciled" if abs(var) <= 0.01 else ("Underpaid" if var < 0 else "Overpaid")
            c.execute(
                """INSERT INTO reconciliations(id, claim_id, claim_number, patient_id, payer_name, payer_type,
                billed_amount, approved_amount, expected_reimbursement, adjustments_total,
                total_payments_received, reconciled_payments, outstanding_balance, variance,
                status, reconciliation_date, reviewed_by, review_decision, review_notes,
                created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, cl_d["id"], cl_d["claim_number"], cl_d["patient_id"], cl_d["payer_name"], cl_d.get("payer_type","nhis"),
                 exp, float(cl_d["approved_amount"] or 0), exp, 0,
                 rec_amt, rec_amt, out, var,
                 status, now(), 'System', 'approved', 'Auto-reconciled during seeding', now(), now())
            )
    except Exception as e:
        print(f"Reconciliation seeding error: {e}")

    c.commit(); c.close()

    # Final: populate revenue leakage cases so the Leakage Detection section is never empty
    _leak_result = None
    try:
        _leak_result = leakage_mod.detect_leakage(create_tasks=True)
    except Exception as e:
        import traceback
        _leak_result = {"error": str(e), "trace": traceback.format_exc()[-400:]}

    return jsonify(ok=True, message="Synthetic demo scenarios seeded. All data synthetic.", leakage=_leak_result if isinstance(_leak_result, dict) else str(_leak_result))


# ---------------- RCM Evidence Intelligence ----------------
@app.post("/api/evidence/documents")
def evidence_store_document():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    encounter_id = data.get("encounter_id")
    filename = data.get("filename")
    doc_type = data.get("doc_type","clinical_note")
    page_texts = data.get("page_texts") or []
    source_path = data.get("source_path")
    if not filename:
        return jsonify(error="filename is required"), 400
    r = evidence_mod.store_synthetic_document(claim_id, encounter_id, filename, doc_type, page_texts, source_path)
    if isinstance(r, tuple): return jsonify(r[0]), r[1]
    return jsonify(r), 201

@app.get("/api/evidence/documents")
def evidence_list_documents():
    claim_id = request.args.get("claim_id")
    c = get_db()
    q = "SELECT * FROM rcm_documents WHERE 1=1"; params=[]
    if claim_id: q += " AND claim_id=?"; params.append(claim_id)
    q += " ORDER BY created_at DESC"
    rows = [dict(r) for r in c.execute(q, params).fetchall()]
    c.close()
    return jsonify(rows)

@app.get("/api/evidence/documents/<did>/pages")
def evidence_document_pages(did):
    c = get_db()
    rows = c.execute("SELECT * FROM rcm_document_pages WHERE document_id=? ORDER BY page_number", (did,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/evidence/analyze")
def evidence_analyze_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    if not claim_id: return jsonify(error="claim_id is required"), 400
    r = evidence_mod.analyze_evidence(claim_id)
    if isinstance(r, tuple): return jsonify(r[0]), r[1]
    return jsonify(r)

@app.get("/api/evidence/findings")
def evidence_findings_route():
    claim_id = request.args.get("claim_id")
    status = request.args.get("status")
    return jsonify(evidence_mod.list_findings(claim_id, status))

@app.get("/api/evidence/findings/<fid>/citations")
def evidence_citations_route(fid):
    return jsonify(evidence_mod.list_citations(fid))

@app.get("/api/evidence/chronology/<claim_id>")
def evidence_chronology_route(claim_id):
    return jsonify(evidence_mod.get_chronology(claim_id))

@app.post("/api/evidence/findings/<fid>/review")
def evidence_review_route(fid):
    data = request.get_json(force=True)
    r = evidence_mod.review_finding(
        fid, data.get("decision"), data.get("reviewer","User"),
        data.get("notes",""), data.get("corrected_description",""),
        data.get("evidence_relevance",""), data.get("missing_evidence_note",""))
    if isinstance(r, tuple): return jsonify(r[0]), r[1]
    return jsonify(r)

@app.post("/api/evidence/feedback")
def evidence_feedback_route():
    data = request.get_json(force=True)
    r = evidence_mod.record_feedback(
        data.get("finding_id"), data.get("reviewer","User"), data.get("action",""),
        data.get("reason",""), data.get("corrected_description",""),
        bool(data.get("marked_irrelevant", False)), data.get("missing_evidence",""),
        data.get("original_description",""))
    if isinstance(r, tuple): return jsonify(r[0]), r[1]
    return jsonify(r)

@app.get("/api/evidence/package/<claim_id>")
def evidence_package_route(claim_id):
    r = evidence_mod.prepare_appeal_evidence_package(claim_id)
    if isinstance(r, tuple): return jsonify(r[0]), r[1]
    return jsonify(r)

@app.get("/api/evidence/metrics")
def evidence_metrics_route():
    return jsonify(evidence_mod.evidence_metrics())

@app.get("/api/evidence/runs")
def evidence_runs_route():
    c = get_db()
    rows = c.execute("SELECT * FROM evidence_analysis_runs ORDER BY started_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ---------------- Reconciliation & Revenue Recovery ----------------
@app.get("/api/reconciliation/metrics")
def reconciliation_metrics_route():
    return jsonify(reconciliation_mod.reconciliation_metrics())

@app.get("/api/reconciliations")
def reconciliations_list_route():
    return jsonify(reconciliation_mod.list_reconciliations(request.args.to_dict()))

@app.post("/api/reconciliations/run")
def reconciliation_run_route():
    data = request.get_json(silent=True) or {}
    claim_id = data.get("claim_id")
    actor = data.get("actor", "User")
    if claim_id:
        return jsonify(reconciliation_mod.reconcile_claim(claim_id, actor))
    return jsonify(reconciliation_mod.reconcile_all_claims(actor))

@app.get("/api/reconciliations/<claim_id>")
def reconciliation_get_route(claim_id):
    r = reconciliation_mod.get_reconciliation(claim_id)
    if not r:
        return jsonify(error="Reconciliation not found"), 404
    return jsonify(r)

@app.get("/api/payments")
def payments_list():
    c = get_db()
    rows = c.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/payments/match")
def payment_match_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    payment_id = data.get("payment_id")
    matched_amount = data.get("matched_amount", 0)
    matched_by = data.get("matched_by", "User")
    notes = data.get("notes", "")
    if not claim_id or not payment_id:
        return jsonify(error="claim_id and payment_id are required"), 400
    return jsonify(reconciliation_mod.match_payment(claim_id, payment_id, matched_amount, matched_by, notes))

@app.post("/api/payment-matches/<match_id>/reconcile")
def payment_match_reconcile_route(match_id):
    data = request.get_json(force=True)
    decision = data.get("decision", "reconciled")
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    return jsonify(reconciliation_mod.reconcile_payment_match(match_id, decision, actor, notes))

@app.get("/api/underpayments")
def underpayments_list_route():
    return jsonify(reconciliation_mod.list_underpayment_cases(request.args.to_dict()))

@app.post("/api/underpayments/detect")
def underpayments_detect_route():
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "System")
    return jsonify(reconciliation_mod.detect_underpayments(actor))

@app.get("/api/underpayments/<case_id>")
def underpayment_get_route(case_id):
    r = reconciliation_mod.get_underpayment_case(case_id)
    if not r:
        return jsonify(error="Underpayment case not found"), 404
    return jsonify(r)

@app.post("/api/reconciliations/<rec_id>/review")
def reconciliation_review_route(rec_id):
    data = request.get_json(force=True)
    decision = data.get("decision")
    reviewer = data.get("reviewer", "User")
    notes = data.get("notes", "")
    corrected_expected = data.get("corrected_expected")
    return jsonify(reconciliation_mod.review_reconciliation(rec_id, decision, reviewer, notes, corrected_expected))

@app.post("/api/recovery/workflow")
def recovery_workflow_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    underpayment_case_id = data.get("underpayment_case_id")
    stage = data.get("stage")
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    financial_value = data.get("financial_value", 0)
    if not claim_id or not stage:
        return jsonify(error="claim_id and stage are required"), 400
    return jsonify(reconciliation_mod.create_recovery_workflow(claim_id, underpayment_case_id, stage, actor, notes, financial_value))

@app.get("/api/recovery/workflow/<claim_id>")
def recovery_workflow_list_route(claim_id):
    return jsonify(reconciliation_mod.list_recovery_workflow(claim_id))

@app.post("/api/payer-response/simulated")
def simulated_payer_response_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    response_type = data.get("response_type")
    amount = data.get("amount", 0)
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    if not claim_id or not response_type:
        return jsonify(error="claim_id and response_type are required"), 400
    return jsonify(reconciliation_mod.record_simulated_payer_response(claim_id, response_type, amount, actor, notes))

@app.post("/api/payment/simulated")
def simulated_payment_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    amount = data.get("amount", 0)
    method = data.get("payment_method", "Cash")
    reference = data.get("reference", "")
    actor = data.get("actor", "User")
    payment_status = data.get("payment_status", "completed")
    if not claim_id:
        return jsonify(error="claim_id is required"), 400
    result = reconciliation_mod.record_simulated_payment(claim_id, amount, method, reference, actor, payment_status)
    # Event-driven trigger: payment recorded → reconciliation task
    try:
        c = get_db()
        claim = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
        if claim:
            orch.create_orchestrator_task(
                c, "payment_reconciliation", "claim", claim["id"],
                agent_name="Payment and Reconciliation Agent",
                direction="agent_to_human",
                priority="medium",
                context=f"Payment of {amount} recorded for claim {claim['claim_number']} — reconciliation required",
                ai_recommendation="Match payment to claim and bill, detect underpayments",
                financial_impact=float(amount),
                auto_assign=True,
            )
        c.commit(); c.close()
    except Exception:
        pass
    return jsonify(result)

@app.get("/api/reports/reconciliation.csv")
def report_reconciliation_route():
    content = reconciliation_mod.export_reconciliation_csv()
    return _csv_response(content, "reconciliation_report.csv")

# ---------------- Revenue Integrity Intelligence (embedded) ----------------
@app.post("/api/revenue-integrity/service-to-revenue/<encounter_id>")
def revenue_service_to_revenue_route(encounter_id):
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "System")
    return jsonify(revenue_integrity_mod.check_service_to_revenue_completeness(encounter_id, actor))

@app.post("/api/revenue-integrity/closed-claim-leakage")
def revenue_closed_claim_leakage_route():
    data = request.get_json(silent=True) or {}
    claim_id = data.get("claim_id")
    actor = data.get("actor", "System")
    return jsonify(revenue_integrity_mod.detect_closed_claim_leakage(claim_id, actor))

@app.post("/api/revenue-integrity/payer-rule/<rule_id>/update")
def revenue_payer_rule_update_route(rule_id):
    data = request.get_json(force=True)
    new_condition = data.get("new_condition")
    changed_by = data.get("changed_by", "Admin")
    change_reason = data.get("change_reason", "")
    if not new_condition:
        return jsonify(error="new_condition is required"), 400
    return jsonify(revenue_integrity_mod.update_payer_rule(rule_id, new_condition, changed_by, change_reason))

@app.get("/api/revenue-integrity/payer-drift")
def revenue_payer_drift_route():
    actor = request.args.get("actor", "System")
    return jsonify(revenue_integrity_mod.detect_payer_policy_drift(actor))

@app.get("/api/revenue-integrity/prioritize")
def revenue_prioritize_route():
    actor = request.args.get("actor", "System")
    return jsonify(revenue_integrity_mod.prioritize_revenue_cases(actor))

@app.post("/api/revenue-integrity/cross-department-gaps")
def revenue_cross_department_gaps_route():
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "System")
    return jsonify(revenue_integrity_mod.detect_cross_department_gaps(actor))

@app.post("/api/revenue-integrity/write-off-audit")
def revenue_write_off_audit_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    amount = data.get("amount", 0)
    adjustment_type = data.get("adjustment_type", "write_off")
    reason = data.get("reason", "")
    actor = data.get("actor", "User")
    if not claim_id:
        return jsonify(error="claim_id is required"), 400
    return jsonify(revenue_integrity_mod.audit_write_off(claim_id, amount, adjustment_type, reason, actor))

@app.get("/api/revenue-integrity/write-off-audits")
def revenue_write_off_audits_route():
    return jsonify(revenue_integrity_mod.list_write_off_audits(request.args.to_dict()))

@app.post("/api/revenue-integrity/leakage-patterns")
def revenue_leakage_patterns_route():
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "System")
    return jsonify(revenue_integrity_mod.detect_leakage_patterns(actor))

@app.post("/api/revenue-integrity/recovery-case")
def revenue_recovery_case_route():
    data = request.get_json(force=True)
    claim_id = data.get("claim_id")
    discrepancy_type = data.get("discrepancy_type")
    expected_amount = data.get("expected_amount", 0)
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    if not claim_id or not discrepancy_type:
        return jsonify(error="claim_id and discrepancy_type are required"), 400
    return jsonify(revenue_integrity_mod.create_recovery_case(claim_id, discrepancy_type, expected_amount, actor, notes))

@app.post("/api/revenue-integrity/recovery-case/<case_id>/payment")
def revenue_recovery_payment_route(case_id):
    data = request.get_json(force=True)
    amount = data.get("amount", 0)
    actor = data.get("actor", "User")
    reference = data.get("reference", "")
    return jsonify(revenue_integrity_mod.record_recovery_payment(case_id, amount, actor, reference))

@app.get("/api/revenue-integrity/recovery-cases")
def revenue_recovery_cases_route():
    return jsonify(revenue_integrity_mod.list_recovery_cases(request.args.to_dict()))

@app.get("/api/revenue-integrity/analytics")
def revenue_integrity_analytics_route():
    return jsonify(revenue_integrity_mod.revenue_integrity_analytics())

# ---------------- Revenue Command Center (Dashboard) ----------------
@app.get("/api/dashboard/overview")
def dashboard_overview():
    c = get_db()
    bills = [dict(r) for r in c.execute("SELECT * FROM bills").fetchall()]
    claims = [dict(r) for r in c.execute("SELECT * FROM claims").fetchall()]
    encounters = [dict(r) for r in c.execute("SELECT * FROM encounters").fetchall()]
    patients = [dict(r) for r in c.execute("SELECT * FROM patients").fetchall()]
    payments = [dict(r) for r in c.execute("SELECT * FROM patient_payments WHERE payment_status='confirmed'").fetchall()]
    reconciliations = [dict(r) for r in c.execute("SELECT * FROM reconciliations").fetchall()]
    exceptions = [dict(r) for r in c.execute("SELECT * FROM exceptions WHERE status='open'").fetchall()]
    review_tasks = [dict(r) for r in c.execute("SELECT * FROM review_tasks WHERE status='pending'").fetchall()]
    denials = [dict(r) for r in c.execute("SELECT * FROM denials WHERE status NOT IN ('closed','resolved')").fetchall()]

    total_charges = sum(float(b.get("total") or 0) for b in bills)
    total_collected = sum(float(b.get("paid") or 0) for b in bills)
    total_collected += sum(float(p.get("amount") or 0) for p in payments)
    total_outstanding = sum(float(b.get("outstanding") or 0) for b in bills)
    total_claims = len(claims)
    claims_submitted = sum(1 for c_ in claims if c_.get("status") not in ("draft",))
    claims_pending = sum(1 for c_ in claims if c_.get("status") in ("submitted","vetted"))
    claims_rejected = sum(1 for c_ in claims if c_.get("status") in ("denied","rejected"))
    claims_attention = sum(1 for c_ in claims if c_.get("status") in ("submitted","vetted","needs_correction","resubmission_required"))
    unreconciled = sum(float(r.get("outstanding_balance") or 0) for r in reconciliations if r.get("review_decision") != "approved")

    c.close()
    return jsonify({
        "currency": "GHS",
        "kpis": {
            "total_charges": round(total_charges,2),
            "total_collected": round(total_collected,2),
            "total_outstanding": round(total_outstanding,2),
            "total_claims": total_claims,
            "claims_submitted": claims_submitted,
            "claims_pending": claims_pending,
            "claims_rejected": claims_rejected,
            "claims_requiring_attention": claims_attention,
            "encounters_active": len(encounters),
            "patients_registered": len(patients),
            "unreconciled_payments": round(unreconciled,2),
            "open_exceptions": len(exceptions),
            "pending_reviews": len(review_tasks),
            "open_denials": len(denials),
        },
        "denial_rate": round((claims_rejected/claims_submitted*100) if claims_submitted else 0,1),
        "collection_rate": round((total_collected/total_charges*100) if total_charges else 0,1),
    })

@app.get("/api/dashboard/activity")
def dashboard_activity():
    c = get_db()
    rows = c.execute(
        """SELECT at.*, c.claim_number, e.encounter_id
        FROM audit_trail at
        LEFT JOIN claims c ON at.entity_id=c.id
        LEFT JOIN encounters e ON at.entity_id=e.id
        ORDER BY at.timestamp DESC LIMIT 20"""
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/dashboard/issues")
def dashboard_issues():
    c = get_db()
    exceptions = [dict(r) for r in c.execute(
        "SELECT * FROM exceptions WHERE status='open' ORDER BY created_at DESC LIMIT 20"
    ).fetchall()]
    review_tasks = [dict(r) for r in c.execute(
        "SELECT * FROM review_tasks WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
    ).fetchall()]
    c.close()
    return jsonify({"exceptions": exceptions, "review_tasks": review_tasks})

# ---------------- Encounter Lifecycle ----------------
ENCOUNTER_STEPS = [
    ("Registration", 1),
    ("Eligibility Verification", 2),
    ("Clinical Encounter", 3),
    ("Documentation Review", 4),
    ("Service Delivery", 5),
    ("Charge Capture", 6),
    ("Billing", 7),
    ("Claims Submission", 8),
    ("Payment", 9),
    ("Reconciliation", 10),
    ("Encounter Closure", 11),
]

@app.get("/api/encounters/<enc_id>/workflow")
def encounter_workflow(enc_id):
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (enc_id, enc_id)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    steps = c.execute(
        "SELECT * FROM workflow_steps WHERE encounter_id=? ORDER BY step_order", (enc["id"],)
    ).fetchall()
    transitions = c.execute(
        "SELECT * FROM encounter_transitions WHERE encounter_id=? ORDER BY ts", (enc["id"],)
    ).fetchall()
    c.close()
    return jsonify({
        "encounter": dict(enc),
        "steps": [dict(s) for s in steps],
        "transitions": [dict(t) for t in transitions],
    })

@app.post("/api/encounters/<enc_id>/transition")
def encounter_transition(enc_id):
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (enc_id, enc_id)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    data = request.get_json(force=True)
    to_status = data.get("to_status")
    actor = data.get("actor", "User")
    note = data.get("note", "")
    step_name = data.get("step_name")
    if not to_status:
        c.close(); return jsonify(error="to_status is required"), 400
    prev = enc["status"]
    c.execute("UPDATE encounters SET status=?, updated_at=? WHERE id=?", (to_status, now(), enc["id"]))
    add_transition(c, enc["id"], prev, to_status, actor, note)
    # Advance workflow step if provided
    if step_name:
        existing = c.execute(
            "SELECT id FROM workflow_steps WHERE encounter_id=? AND step_name=?", (enc["id"], step_name)
        ).fetchone()
        if not existing:
            step_order = next((o for n,o in ENCOUNTER_STEPS if n==step_name), 99)
            c.execute(
                "INSERT INTO workflow_steps(id,encounter_id,step_name,step_order,status,started_at,completed_at,actor,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), enc["id"], step_name, step_order, "completed", now(), now(), actor, note, now())
            )
    c.commit(); c.close()
    return jsonify({"ok": True, "encounter_id": enc["id"], "previous_status": prev, "new_status": to_status})

# ---------------- Review Tasks ----------------
@app.get("/api/review-tasks")
def review_tasks_list():
    c = get_db()
    rows = c.execute(
        """SELECT rt.*, u.username as assigned_username
        FROM review_tasks rt LEFT JOIN users u ON rt.assigned_to=u.id
        ORDER BY rt.created_at DESC"""
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/review-tasks")
def review_tasks_create():
    c = get_db()
    data = request.get_json(force=True)
    task_type = data.get("task_type")
    entity_type = data.get("entity_type")
    entity_id = data.get("entity_id")
    if not task_type or not entity_type or not entity_id:
        c.close(); return jsonify(error="task_type, entity_type, entity_id required"), 400
    tid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO review_tasks(id,task_type,entity_type,entity_id,status,priority,assigned_to,context,evidence,ai_recommendation,financial_impact,created_at)
        VALUES(?,?,?,?,'pending','medium',?,?,?,?,?,?)""",
        (tid, task_type, entity_type, entity_id, data.get("assigned_to"),
         data.get("context",""), data.get("evidence",""), data.get("ai_recommendation",""),
         float(data.get("financial_impact") or 0), now())
    )
    c.commit(); c.close()
    return jsonify({"task_id": tid, "status": "pending"})

@app.post("/api/review-tasks/<task_id>/resolve")
def review_tasks_resolve(task_id):
    c = get_db()
    data = request.get_json(force=True)
    decision = data.get("decision")
    reviewer = data.get("reviewer", "User")
    notes = data.get("notes", "")
    if decision not in ("approve","reject","request_correction","assign","escalate","resolve"):
        c.close(); return jsonify(error="Invalid decision"), 400
    task = c.execute("SELECT * FROM review_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        c.close(); return jsonify(error="Review task not found"), 404
    new_status = "resolved" if decision == "resolve" else decision
    c.execute(
        "UPDATE review_tasks SET status=?, resolved_at=? WHERE id=?",
        (new_status, now(), task_id)
    )
    c.commit(); c.close()
    return jsonify({"task_id": task_id, "status": new_status, "decision": decision})

# ---------------- Departments ----------------
@app.get("/api/departments")
def departments_list():
    c = get_db()
    rows = c.execute("SELECT * FROM departments WHERE is_active=1 ORDER BY name").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ---------------- Users ----------------
@app.get("/api/users")
def users_list():
    c = get_db()
    rows = c.execute("SELECT id, username, email, role, is_active FROM users WHERE is_active=1 ORDER BY username").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ---------------- Payment Allocations ----------------
@app.get("/api/payment-allocations")
def payment_allocations_list():
    c = get_db()
    rows = c.execute("SELECT * FROM payment_allocations ORDER BY allocated_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/payment-allocations")
def payment_allocations_create():
    c = get_db()
    data = request.get_json(force=True)
    payment_id = data.get("payment_id")
    claim_id = data.get("claim_id")
    bill_id = data.get("bill_id")
    amount = float(data.get("amount") or 0)
    allocated_by = data.get("allocated_by", "User")
    if not payment_id or amount <= 0:
        c.close(); return jsonify(error="payment_id and positive amount required"), 400
    # Validate payment exists
    pay = c.execute("SELECT id FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not pay:
        c.close(); return jsonify(error="Payment not found"), 404
    if claim_id:
        cl = c.execute("SELECT id FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not cl:
            c.close(); return jsonify(error="Claim not found"), 404
    if bill_id:
        bl = c.execute("SELECT id FROM bills WHERE id=?", (bill_id,)).fetchone()
        if not bl:
            c.close(); return jsonify(error="Bill not found"), 404
    aid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO payment_allocations(id,payment_id,claim_id,bill_id,amount,allocated_at,allocated_by)
        VALUES(?,?,?,?,?,?,?)""",
        (aid, payment_id, claim_id, bill_id, amount, now(), allocated_by)
    )
    c.commit(); c.close()
    return jsonify({"allocation_id": aid, "amount": amount})

# ---------------- Workflow Steps ----------------
@app.get("/api/workflow-steps")
def workflow_steps_list():
    c = get_db()
    rows = c.execute("SELECT * FROM workflow_steps ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ---------------- Facilities ----------------
@app.get("/api/facilities")
def facilities_list():
    c = get_db()
    rows = c.execute("SELECT * FROM facilities WHERE is_active=1 ORDER BY name").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ---------------- Authorizations ----------------
@app.get("/api/authorizations")
def authorizations_list():
    c = get_db()
    rows = c.execute("SELECT * FROM authorizations ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/authorizations")
def authorizations_create():
    data = request.get_json(force=True)
    patient_id = data.get("patient_id")
    encounter_id = data.get("encounter_id")
    if not patient_id or not encounter_id:
        return jsonify(error="patient_id and encounter_id are required"), 400
    c = get_db()
    aid = str(uuid.uuid4())
    auth_number = "AUTH-" + str(uuid.uuid4())[:8].upper()
    c.execute(
        """INSERT INTO authorizations(id, auth_number, patient_id, encounter_id, payer_name, payer_type,
            procedure_code, service_description, status, requested_at, required_documents, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
        (aid, auth_number, patient_id, encounter_id, data.get("payer_name","NHIS Demo"),
         data.get("payer_type","nhis"), data.get("procedure_code",""), data.get("service_description",""),
         now(), now(), data.get("required_documents",""), now(), now())
    )
    audit(c, "Authorization Agent", "agent", "Authorization", "Authorization requested", "authorization", aid, new_state="pending")
    c.commit()
    row = c.execute("SELECT * FROM authorizations WHERE id=?", (aid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.post("/api/authorizations/<aid>/decision")
def authorizations_decision(aid):
    data = request.get_json(force=True)
    decision = data.get("decision")
    if decision not in ("approve","reject","request_documents"):
        return jsonify(error="decision must be approve, reject, or request_documents"), 400
    c = get_db()
    auth = c.execute("SELECT * FROM authorizations WHERE id=? OR auth_number=?", (aid, aid)).fetchone()
    if not auth:
        c.close(); return jsonify(error="Authorization not found"), 404
    new_status = {"approve":"approved","reject":"rejected","request_documents":"documents_requested"}[decision]
    c.execute(
        """UPDATE authorizations SET status=?, updated_at=?, approving_staff=?, rejection_reason=?, approved_at=?
        WHERE id=?""",
        (new_status, now(), data.get("reviewer","User"), data.get("rejection_reason",""),
         now() if decision=="approve" else None, auth["id"])
    )
    audit(c, data.get("reviewer","User"), "user", "Authorization", f"Authorization {decision}d", "authorization", auth["id"], previous_state=auth["status"], new_state=new_status)
    c.commit(); c.close()
    return jsonify({"ok": True, "status": new_status})

# ---------------- Coding Suggestions ----------------
@app.get("/api/coding-suggestions")
def coding_suggestions_list():
    c = get_db()
    encounter_id = request.args.get("encounter_id")
    status = request.args.get("status")
    q = "SELECT * FROM coding_suggestions WHERE 1=1"; params=[]
    if encounter_id: q+=" AND encounter_id=?"; params.append(encounter_id)
    if status: q+=" AND status=?"; params.append(status)
    q+=" ORDER BY created_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/coding-suggestions")
def coding_suggestions_create():
    data = request.get_json(force=True)
    encounter_id = data.get("encounter_id")
    if not encounter_id:
        return jsonify(error="encounter_id is required"), 400
    c = get_db()
    sid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO coding_suggestions(id, encounter_id, claim_id, code_type, code_value, code_description,
            confidence, evidence, status, created_at)
        VALUES(?,?,?,?,?,?,?,?,'pending_review',?)""",
        (sid, encounter_id, data.get("claim_id"), data.get("code_type","ICD-10"),
         data.get("code_value",""), data.get("code_description",""), float(data.get("confidence") or 0.85),
         data.get("evidence",""), now())
    )
    c.commit()
    row = c.execute("SELECT * FROM coding_suggestions WHERE id=?", (sid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.post("/api/coding-suggestions/<sid>/resolve")
def coding_suggestions_resolve(sid):
    data = request.get_json(force=True)
    decision = data.get("decision")
    if decision not in ("approve","reject","correct"):
        return jsonify(error="decision must be approve, reject, or correct"), 400
    c = get_db()
    sug = c.execute("SELECT * FROM coding_suggestions WHERE id=?", (sid,)).fetchone()
    if not sug:
        c.close(); return jsonify(error="Coding suggestion not found"), 404
    c.execute(
        """UPDATE coding_suggestions SET status=?, reviewer=?, reviewer_decision=?, reviewer_notes=?, resolved_at=?
        WHERE id=?""",
        (f"reviewed_{decision}", data.get("reviewer","User"), decision, data.get("notes",""), now(), sid)
    )
    audit(c, data.get("reviewer","User"), "user", "Coding Intelligence", f"Coding suggestion {decision}d", "coding_suggestion", sid, previous_state=sug["status"], new_state=f"reviewed_{decision}")
    c.commit(); c.close()
    return jsonify({"ok": True, "status": f"reviewed_{decision}"})

# ---------------- Claim States ----------------
@app.get("/api/claim-states")
def claim_states_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claim_states ORDER BY updated_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/claim-states/<claim_id>/transition")
def claim_state_transition(claim_id):
    data = request.get_json(force=True)
    to_state = data.get("to_state")
    if not to_state:
        return jsonify(error="to_state is required"), 400
    c = get_db()
    state = c.execute("SELECT * FROM claim_states WHERE claim_id=? OR claim_id=(SELECT id FROM claims WHERE claim_number=?)", (claim_id, claim_id)).fetchone()
    if not state:
        # Create new claim state
        claim = c.execute("SELECT id FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
        if not claim:
            c.close(); return jsonify(error="Claim not found"), 404
        sid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO claim_states(id, claim_id, current_state, previous_state, state_reason, transitioned_by, transitioned_at, created_at, updated_at)
            VALUES(?,?,'draft',NULL,?,?,?,?,?)""",
            (sid, claim["id"], data.get("reason","Initial state"), data.get("actor","User"), now(), now(), now())
        )
        state_id = sid
        prev_state = None
        new_state = "draft"
    else:
        state_id = state["id"]
        prev_state = state["current_state"]
        new_state = to_state
        c.execute(
            """UPDATE claim_states SET previous_state=?, current_state=?, state_reason=?, transitioned_by=?, transitioned_at=?, updated_at=?
            WHERE id=?""",
            (prev_state, to_state, data.get("reason",""), data.get("actor","User"), now(), now(), state_id)
        )
    # Also update claim status
    claim = c.execute("SELECT id FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if claim:
        c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", (to_state, now(), claim["id"]))
    audit(c, data.get("actor","User"), "user", "Claims Validation", f"Claim state transitioned to {to_state}", "claim", claim["id"] if claim else claim_id, previous_state=prev_state, new_state=new_state)
    c.commit(); c.close()
    return jsonify({"ok": True, "claim_id": claim_id, "previous_state": prev_state, "current_state": new_state})

# ---------------- Demo Scenarios ----------------
@app.get("/api/demo/scenarios")
def demo_scenarios_list():
    c = get_db()
    rows = c.execute("SELECT * FROM demo_scenarios ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/demo/scenarios/<sid>/steps")
def demo_scenario_steps(sid):
    c = get_db()
    rows = c.execute("SELECT * FROM demo_scenario_steps WHERE scenario_id=? ORDER BY step_order", (sid,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/demo/scenarios/<sid>/step/<int:step_order>/complete")
def demo_step_complete(sid, step_order):
    data = request.get_json(silent=True) or {}
    c = get_db()
    c.execute(
        """UPDATE demo_scenario_steps SET status='completed', completed_at=?, evidence=?
        WHERE scenario_id=? AND step_order=?""",
        (now(), data.get("evidence",""), sid, step_order)
    )
    c.execute(
        "UPDATE demo_scenarios SET current_step=?, updated_at=? WHERE id=?",
        (step_order, now(), sid)
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "step": step_order, "status": "completed"})

@app.post("/api/demo/scenarios/<sid>/reset")
def demo_scenario_reset(sid):
    c = get_db()
    c.execute("UPDATE demo_scenario_steps SET status='pending', completed_at=NULL, evidence=NULL WHERE scenario_id=?", (sid,))
    c.execute("UPDATE demo_scenarios SET current_step=0, status='ready', updated_at=? WHERE id=?", (now(), sid))
    c.commit(); c.close()
    return jsonify({"ok": True, "status": "reset"})

# ---------------- CLAIM-it Integration Workspace ----------------
def _claimit_log(c, claim_ref, facility_id, actor, actor_type, action, previous_status, new_status, evidence=""):
    lid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO claimit_activity_log(id, claim_ref, facility_id, actor, actor_type, action, previous_status, new_status, timestamp, evidence)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (lid, claim_ref, facility_id, actor, actor_type, action, previous_status, new_status, now(), evidence)
    )
    return lid

@app.get("/api/claimit/config")
def claimit_config_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_integration_config ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/claimit/config/<cfg_id>")
def claimit_config_get(cfg_id):
    c = get_db()
    row = c.execute("SELECT * FROM claimit_integration_config WHERE id=?", (cfg_id,)).fetchone()
    c.close()
    if not row:
        return jsonify(error="Integration config not found"), 404
    return jsonify(dict(row))

@app.post("/api/claimit/config/<cfg_id>")
def claimit_config_update(cfg_id):
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    c = get_db()
    row = c.execute("SELECT * FROM claimit_integration_config WHERE id=?", (cfg_id,)).fetchone()
    if not row:
        c.close(); return jsonify(error="Integration config not found"), 404
    updates = []
    vals = []
    for field in ("integration_mode", "status"):
        if field in data:
            updates.append(f"{field}=?"); vals.append(data[field])
    if updates:
        vals.append(now()); vals.append(cfg_id)
        c.execute(f"UPDATE claimit_integration_config SET {','.join(updates)}, updated_at=? WHERE id=?", vals)
        c.commit()
    c.close()
    return jsonify({"ok": True, "config_id": cfg_id})

@app.get("/api/claimit/queue")
def claimit_queue_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_exchange_queue ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.get("/api/claimit/queue/<qid>")
def claimit_queue_get(qid):
    c = get_db()
    row = c.execute("SELECT * FROM claimit_exchange_queue WHERE id=?", (qid,)).fetchone()
    c.close()
    if not row:
        return jsonify(error="Queue item not found"), 404
    return jsonify(dict(row))

@app.post("/api/claimit/queue/<qid>/action")
def claimit_queue_action(qid):
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    action_type = data.get("action_type")
    note = data.get("note", "")
    if not action_type:
        return jsonify(error="action_type is required"), 400
    c = get_db()
    q = c.execute("SELECT * FROM claimit_exchange_queue WHERE id=?", (qid,)).fetchone()
    if not q:
        c.close(); return jsonify(error="Queue item not found"), 404
    prev_pnrcm = q["pnrcm_status"]; prev_claimit = q["claimit_status"]
    last_action = q["last_action"]; next_action = q["next_action"]
    if action_type == "export_prepare":
        last_action = "Export prepared"; next_action = "Transfer to CLAIM-it"
        new_pnrcm = "vetted"; new_claimit = "ready_for_export"
    elif action_type == "transfer":
        last_action = "Transferred to CLAIM-it"; next_action = "Awaiting validation"
        new_pnrcm = "submitted"; new_claimit = "awaiting_validation"
    elif action_type == "validate":
        last_action = "Validation result received"; next_action = "Resolve issues" if data.get("result")=="failed" else "Feedback received"
        new_pnrcm = "denied" if data.get("result")=="failed" else "paid"
        new_claimit = "validation_failed" if data.get("result")=="failed" else "feedback_received"
    elif action_type == "correct":
        last_action = "Correction applied"; next_action = "Resubmit to CLAIM-it"
        new_pnrcm = "vetted"; new_claimit = "ready_for_export"
    elif action_type == "reconcile":
        last_action = "Payment reconciled"; next_action = None
        new_pnrcm = "paid"; new_claimit = "feedback_received"
    else:
        c.close(); return jsonify(error="Unknown action_type"), 400
    c.execute(
        """UPDATE claimit_exchange_queue SET pnrcm_status=?, claimit_status=?, last_action=?, next_action=?, updated_at=? WHERE id=?""",
        (new_pnrcm, new_claimit, last_action, next_action, now(), qid)
    )
    _claimit_log(c, q["claim_ref"], q["facility_id"], actor, "user", action_type, prev_pnrcm+"|"+prev_claimit, new_pnrcm+"|"+new_claimit, note)
    c.commit(); c.close()
    return jsonify({"ok": True, "queue_id": qid, "previous": {"pnrcm": prev_pnrcm, "claimit": prev_claimit}, "new": {"pnrcm": new_pnrcm, "claimit": new_claimit}})

@app.get("/api/claimit/issues")
def claimit_issues_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_validation_issues ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/claimit/issues/<iid>/resolve")
def claimit_issue_resolve(iid):
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    c = get_db()
    row = c.execute("SELECT * FROM claimit_validation_issues WHERE id=?", (iid,)).fetchone()
    if not row:
        c.close(); return jsonify(error="Issue not found"), 404
    c.execute(
        """UPDATE claimit_validation_issues SET status='resolved', resolved_at=? WHERE id=?""",
        (now(), iid)
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "issue_id": iid, "status": "resolved", "resolved_by": actor})

@app.get("/api/claimit/files")
def claimit_files_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_file_exchange ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/claimit/files")
def claimit_file_prepare():
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    facility_id = data.get("facility_id")
    file_name = data.get("file_name", f"outbound_claims_{now()[:10]}.json")
    file_type = data.get("file_type", "outbound")
    claim_count = int(data.get("claim_count", 0))
    c = get_db()
    fid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO claimit_file_exchange(id, file_name, file_type, facility_id, claim_count, status, created_at, validation_summary, error_summary, audit_trail)
        VALUES(?,?,?,?,?,'prepared',?,?,?,?)""",
        (fid, file_name, file_type, facility_id, claim_count, now(), json.dumps({"claims":claim_count,"status":"prepared"}), json.dumps({"errors":0}), json.dumps([{"action":"prepared","by":actor,"at":now()}]))
    )
    c.commit(); c.close()
    return jsonify({"ok": True, "file_id": fid, "file_name": file_name, "status": "prepared"})

@app.post("/api/claimit/files/<fid>/transfer")
def claimit_file_transfer(fid):
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    c = get_db()
    row = c.execute("SELECT * FROM claimit_file_exchange WHERE id=?", (fid,)).fetchone()
    if not row:
        c.close(); return jsonify(error="File not found"), 404
    c.execute("UPDATE claimit_file_exchange SET status='transferred', transferred_at=? WHERE id=?", (now(), fid))
    c.commit(); c.close()
    return jsonify({"ok": True, "file_id": fid, "status": "transferred"})

@app.get("/api/claimit/feedback")
def claimit_feedback_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_feedback ORDER BY created_at DESC").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/claimit/feedback/<fid>/process")
def claimit_feedback_process(fid):
    data = request.get_json(force=True)
    actor = data.get("actor", "User")
    c = get_db()
    row = c.execute("SELECT * FROM claimit_feedback WHERE id=?", (fid,)).fetchone()
    if not row:
        c.close(); return jsonify(error="Feedback not found"), 404
    c.execute("UPDATE claimit_feedback SET processed=1, processed_at=? WHERE id=?", (now(), fid))
    c.commit(); c.close()
    return jsonify({"ok": True, "feedback_id": fid, "processed": True, "processed_by": actor})

@app.get("/api/claimit/log")
def claimit_log_list():
    c = get_db()
    rows = c.execute("SELECT * FROM claimit_activity_log ORDER BY timestamp DESC LIMIT 100").fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.post("/api/claimit/sync")
def claimit_sync():
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "System")
    c = get_db()
    configs = c.execute("SELECT * FROM claimit_integration_config").fetchall()
    results = []
    for cfg in configs:
        queue_count = c.execute("SELECT COUNT(*) n FROM claimit_exchange_queue WHERE claimit_status IN ('not_exported','ready_for_export')").fetchone()["n"]
        pending_feedback = c.execute("SELECT COUNT(*) n FROM claimit_feedback WHERE processed=0").fetchone()["n"]
        c.execute(
            "UPDATE claimit_integration_config SET last_sync=?, pending_transfers=?, pending_feedback=?, updated_at=? WHERE id=?",
            (now(), queue_count, pending_feedback, now(), cfg["id"])
        )
        results.append({"facility_id": cfg["facility_id"], "mode": cfg["integration_mode"], "pending_transfers": queue_count, "pending_feedback": pending_feedback})
    c.commit(); c.close()
    return jsonify({"ok": True, "synced_at": now(), "synced_by": actor, "results": results})

@app.get("/api/claimit/dashboard")
def claimit_dashboard():
    c = get_db()
    total = c.execute("SELECT COUNT(*) n FROM claimit_exchange_queue").fetchone()["n"]
    by_status = c.execute("SELECT claimit_status, COUNT(*) n FROM claimit_exchange_queue GROUP BY claimit_status").fetchall()
    issues_open = c.execute("SELECT COUNT(*) n FROM claimit_validation_issues WHERE status='open'").fetchone()["n"]
    feedback_unprocessed = c.execute("SELECT COUNT(*) n FROM claimit_feedback WHERE processed=0").fetchone()["n"]
    c.close()
    return jsonify({
        "total_claims": total,
        "by_status": [dict(r) for r in by_status],
        "open_issues": issues_open,
        "unprocessed_feedback": feedback_unprocessed,
        "generated_at": now()
    })

# ---------------- Security & Access Governance (Phase 7) ----------------

@app.before_request
def _security_logging():
    """Log access for state-changing requests and sensitive endpoints."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") or request.path.startswith("/api/security"):
        # Defer actual logging to after request via after_request
        pass

@app.after_request
def _security_after_request(response):
    """Log access events for mutating requests."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            path = request.path
            # Skip health/static/noise
            if path in ("/api/health", "/api/agents", "/api/metrics"):
                return response
            resource = path.rsplit("/", 1)[-1] or path
            resource_id = ""
            # Try to extract an ID from the path
            parts = path.rstrip("/").split("/")
            if len(parts) >= 3 and parts[-1] and parts[-1].isdigit() is False and len(parts[-1]) > 5:
                resource_id = parts[-1]
            detail = f"{request.method} {path}"
            if request.is_json:
                try:
                    data = request.get_json(silent=True) or {}
                    if data.get("actor"):
                        detail += f" actor={data.get('actor')}"
                except Exception:
                    pass
            security.log_access(
                action=request.method,
                resource=resource,
                resource_id=resource_id,
                status="ok" if response.status_code < 400 else "error",
                detail=detail,
            )
        except Exception:
            pass
    return response


@app.get("/api/security/role-check")
def security_role_check():
    """Return the current user's role and facility (for frontend RBAC UI)."""
    username = security._get_current_username()
    if not username:
        return jsonify(error="Authentication required"), 401
    c = get_db()
    try:
        user = security._get_user_role(c, username)
        if not user:
            c.close()
            return jsonify(error="User not found or inactive"), 403
        c.close()
        return jsonify({
            "username": username,
            "role": user.get("role"),
            "facility_id": user.get("facility_id"),
            "is_demo": security.is_demo_data(),
        })
    finally:
        c.close()


@app.get("/api/security/access-log")
@security.require_role(["administrator", "revenue_cycle_manager"])
def security_access_log():
    """Return recent session access log entries (admin/manager only)."""
    c = get_db()
    try:
        limit = int(request.args.get("limit", 50))
        rows = c.execute(
            """SELECT * FROM session_access_log ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        c.close()
        return jsonify([dict(r) for r in rows])
    finally:
        c.close()


@app.post("/api/security/authorize")
def security_authorize():
    """Check if the current user is authorized for a sensitive action.

    Body: { "action": "write_off_approval|financial_write|patient_merge|..." }
    """
    data = request.get_json(force=True)
    action = data.get("action", "")
    username = security._get_current_username()
    if not username:
        return jsonify(error="Authentication required"), 401
    c = get_db()
    try:
        user = security._get_user_role(c, username)
        if not user:
            c.close()
            return jsonify(error="User not found or inactive"), 403
        role = user.get("role", "")
        authorized = False
        reason = ""
        if action == "write_off_approval":
            authorized = role in security.WRITE_OFF_APPROVAL_ROLES
            reason = "Write-off approval requires administrator, revenue_cycle_manager, or finance_officer role."
        elif action == "financial_write":
            authorized = role in security.SENSITIVE_FINANCIAL_ROLES
            reason = "Financial write operations require administrator, revenue_cycle_manager, finance_officer, or billing_officer role."
        elif action == "patient_merge":
            authorized = role in ("administrator", "revenue_cycle_manager")
            reason = "Patient merge requires administrator or revenue_cycle_manager role."
        else:
            authorized = role in security.SENSITIVE_FINANCIAL_ROLES
            reason = "Unknown action; defaulting to financial write authorization."
        c.close()
        return jsonify({
            "username": username,
            "role": role,
            "action": action,
            "authorized": authorized,
            "reason": reason,
        })
    finally:
        c.close()

# ---------------- Phase 1: Command Center - Enhanced Overview & Actions ----------------

@app.get("/api/command-center/overview")
def command_center_overview_route():
    return jsonify(rev_enh.command_center_overview())


@app.get("/api/command-center/actions")
def command_center_actions_route():
    limit = int(request.args.get("limit", 20))
    return jsonify(rev_enh.command_center_actions(limit=limit))


# ---------------- Phase 2: Revenue Integrity - Leakage Findings ----------------

@app.get("/api/revenue-integrity/leakage-findings")
def revenue_leakage_findings_route():
    filters = {
        "status": request.args.get("status"),
        "category": request.args.get("category"),
        "severity": request.args.get("severity"),
    }
    filters = {k: v for k, v in filters.items() if v}
    return jsonify(rev_enh.leakage_findings(filters=filters if filters else None))


# ---------------- Phase 3: Payer Intelligence ----------------

@app.get("/api/analytics/payer-intelligence")
def analytics_payer_intelligence_route():
    payer = request.args.get("payer")
    filters = {"payer": payer} if payer else None
    return jsonify(rev_enh.payer_intelligence(filters=filters))


# ---------------- Phase 4: Revenue Cycle Performance Scorecard ----------------

@app.get("/api/analytics/scorecard")
def analytics_scorecard_route():
    start = request.args.get("start")
    end = request.args.get("end")
    facility_id = request.args.get("facility_id")
    department = request.args.get("department")
    return jsonify(rev_enh.performance_scorecard(
        start=start, end=end, facility_id=facility_id, department=department
    ))


# ---------------- Phase 5: Human Review & Accountability ----------------

@app.get("/api/review-tasks/enhanced")
def review_tasks_enhanced_route():
    filters = {
        "status": request.args.get("status"),
        "priority": request.args.get("priority"),
        "assigned_to": request.args.get("assigned_to"),
    }
    filters = {k: v for k, v in filters.items() if v}
    return jsonify(rev_enh.enhanced_review_tasks(filters=filters if filters else None))


@app.post("/api/review-tasks/<task_id>/resolve-enhanced")
def review_tasks_resolve_enhanced_route(task_id):
    data = request.get_json(force=True)
    decision = data.get("decision")
    reviewer = data.get("reviewer", "User")
    notes = data.get("notes", "")
    human_decision = data.get("human_decision")
    result = rev_enh.resolve_review_task(
        task_id, decision, reviewer, notes, human_decision
    )
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)


# ---------------- Phase 6: Revenue Impact Tracking ----------------

@app.get("/api/revenue-impact")
def revenue_impact_list_route():
    filters = {
        "lifecycle_stage": request.args.get("lifecycle_stage"),
        "outcome_category": request.args.get("outcome_category"),
        "related_claim_id": request.args.get("related_claim_id"),
        "source_finding_type": request.args.get("source_finding_type"),
    }
    filters = {k: v for k, v in filters.items() if v}
    return jsonify(rev_enh.list_revenue_impacts(filters=filters if filters else None))


@app.post("/api/revenue-impact")
def revenue_impact_create_route():
    data = request.get_json(force=True)
    source_finding_id = data.get("source_finding_id")
    source_finding_type = data.get("source_finding_type", "leakage_finding")
    original_issue = data.get("original_issue", "")
    estimated_amount = float(data.get("estimated_amount", 0) or 0)
    related_claim_id = data.get("related_claim_id")
    related_encounter_id = data.get("related_encounter_id")
    related_charge_id = data.get("related_charge_id")
    related_payment_id = data.get("related_payment_id")
    assigned_owner = data.get("assigned_owner")
    actor = data.get("actor", "User")
    evidence = data.get("evidence")

    if not source_finding_id or not original_issue:
        return jsonify(error="source_finding_id and original_issue are required"), 400

    result = rev_enh.create_revenue_impact(
        source_finding_id=source_finding_id,
        source_finding_type=source_finding_type,
        original_issue=original_issue,
        estimated_amount=estimated_amount,
        related_claim_id=related_claim_id,
        related_encounter_id=related_encounter_id,
        related_charge_id=related_charge_id,
        related_payment_id=related_payment_id,
        assigned_owner=assigned_owner,
        actor=actor,
        evidence=evidence,
    )
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result), 201


@app.post("/api/revenue-impact/<impact_id>/stage")
def revenue_impact_stage_route(impact_id):
    data = request.get_json(force=True)
    new_stage = data.get("stage")
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    if not new_stage:
        return jsonify(error="stage is required"), 400
    result = rev_enh.update_revenue_impact_stage(impact_id, new_stage, actor, notes)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)


@app.post("/api/revenue-impact/<impact_id>/outcome")
def revenue_impact_outcome_route(impact_id):
    data = request.get_json(force=True)
    outcome_category = data.get("outcome_category")
    confirmed_amount = float(data.get("confirmed_amount", 0) or 0)
    actor = data.get("actor", "User")
    verified_by = data.get("verified_by")
    evidence = data.get("evidence")
    if not outcome_category:
        return jsonify(error="outcome_category is required"), 400
    result = rev_enh.record_revenue_impact_outcome(
        impact_id, outcome_category, confirmed_amount, actor, verified_by, evidence
    )
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)


@app.post("/api/revenue-impact/<impact_id>/close")
def revenue_impact_close_route(impact_id):
    data = request.get_json(silent=True) or {}
    actor = data.get("actor", "User")
    notes = data.get("notes", "")
    result = rev_enh.close_revenue_impact(impact_id, actor, notes)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)


# ---------------- Phase 4: Revenue Context Layer ----------------

@app.get("/api/context/patient/<patient_id>")
def revenue_context_patient(patient_id):
    """Full revenue context chain for a patient:
    patient → encounters → services → bills → claims → payments"""
    c = get_db()
    pat = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone()
    if not pat:
        c.close(); return jsonify(error="Patient not found"), 404
    pd = dict(pat)
    encounters = [dict(r) for r in c.execute("SELECT * FROM encounters WHERE patient_id=? ORDER BY created_at DESC", (pd["id"],)).fetchall()]
    result = {
        "patient": pd,
        "encounters": [],
        "currency": "GHS",
    }
    for enc in encounters:
        ed = dict(enc)
        services = [dict(r) for r in c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (ed["id"],)).fetchall()]
        bills = [dict(r) for r in c.execute("SELECT * FROM bills WHERE encounter_id=? ORDER BY created_at DESC", (ed["id"],)).fetchall()]
        claim_list = []
        for b in bills:
            claims_for_bill = [dict(r) for r in c.execute("SELECT * FROM claims WHERE bill_id=? ORDER BY created_at DESC", (b["id"],)).fetchall()]
            for cl in claims_for_bill:
                payments = [dict(r) for r in c.execute("SELECT * FROM payments WHERE claim_id=? ORDER BY created_at DESC", (cl["id"],)).fetchall()]
                allocations = [dict(r) for r in c.execute("SELECT * FROM payment_allocations WHERE claim_id=? ORDER BY allocated_at DESC", (cl["id"],)).fetchall()]
                cl["payments"] = payments
                cl["allocations"] = allocations
                claim_list.append(cl)
        b_with_claims = []
        for b in bills:
            bd = dict(b)
            bd["claims"] = [cl for cl in claim_list if cl.get("bill_id") == b["id"]]
            b_with_claims.append(bd)
        ed["services"] = services
        ed["bills"] = b_with_claims
        result["encounters"].append(ed)
    c.close()
    return jsonify(result)


@app.get("/api/context/encounter/<encounter_id>")
def revenue_context_encounter(encounter_id):
    """Revenue context chain for a single encounter:
    encounter → services → bills → claims → payments"""
    c = get_db()
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (encounter_id, encounter_id)).fetchone()
    if not enc:
        c.close(); return jsonify(error="Encounter not found"), 404
    ed = dict(enc)
    pat = c.execute("SELECT * FROM patients WHERE id=?", (ed["patient_id"],)).fetchone()
    services = [dict(r) for r in c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (ed["id"],)).fetchall()]
    bills = [dict(r) for r in c.execute("SELECT * FROM bills WHERE encounter_id=? ORDER BY created_at DESC", (ed["id"],)).fetchall()]
    claim_list = []
    for b in bills:
        claims_for_bill = [dict(r) for r in c.execute("SELECT * FROM claims WHERE bill_id=? ORDER BY created_at DESC", (b["id"],)).fetchall()]
        for cl in claims_for_bill:
            payments = [dict(r) for r in c.execute("SELECT * FROM payments WHERE claim_id=? ORDER BY created_at DESC", (cl["id"],)).fetchall()]
            allocations = [dict(r) for r in c.execute("SELECT * FROM payment_allocations WHERE claim_id=? ORDER BY allocated_at DESC", (cl["id"],)).fetchall()]
            cl["payments"] = payments
            cl["allocations"] = allocations
            claim_list.append(cl)
    b_with_claims = []
    for b in bills:
        bd = dict(b)
        bd["claims"] = [cl for cl in claim_list if cl.get("bill_id") == b["id"]]
        b_with_claims.append(bd)
    c.close()
    return jsonify({
        "encounter": ed,
        "patient": dict(pat) if pat else None,
        "services": services,
        "bills": b_with_claims,
        "currency": "GHS",
    })


@app.get("/api/context/claim/<claim_id>")
def revenue_context_claim(claim_id):
    """Revenue context chain for a single claim:
    claim → bill → encounter → patient → services → payments"""
    c = get_db()
    cl = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not cl:
        c.close(); return jsonify(error="Claim not found"), 404
    cd = dict(cl)
    bill = None
    services = []
    enc = None
    pat = None
    if cd.get("bill_id"):
        bill = c.execute("SELECT * FROM bills WHERE id=?", (cd["bill_id"],)).fetchone()
        if bill:
            bd = dict(bill)
            enc = c.execute("SELECT * FROM encounters WHERE id=?", (bd["encounter_id"],)).fetchone()
            if enc:
                ed = dict(enc)
                services = [dict(r) for r in c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (ed["id"],)).fetchall()]
                pat = c.execute("SELECT * FROM patients WHERE id=?", (ed["patient_id"],)).fetchone()
            bd["services"] = services
            bill = bd
    payments = [dict(r) for r in c.execute("SELECT * FROM payments WHERE claim_id=? ORDER BY created_at DESC", (cd["id"],)).fetchall()]
    allocations = [dict(r) for r in c.execute("SELECT * FROM payment_allocations WHERE claim_id=? ORDER BY allocated_at DESC", (cd["id"],)).fetchall()]
    c.close()
    return jsonify({
        "claim": cd,
        "payments": payments,
        "allocations": allocations,
        "bill": dict(bill) if bill else None,
        "encounter": dict(enc) if enc else None,
        "services": services,
        "patient": dict(pat) if pat else None,
        "currency": "GHS",
    })


# ---------------- Phase 8: Integrated Revenue Integrity Check ----------------

@app.post("/api/revenue-integrity/integrated-check")
def revenue_integrity_integrated_route():
    actor = request.args.get("actor", "System")
    return jsonify(rev_enh.run_integrated_revenue_integrity(actor))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
