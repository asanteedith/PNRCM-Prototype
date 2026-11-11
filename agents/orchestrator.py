"""Orchestrator and end-to-end demo workflow helpers."""
from __future__ import annotations
import json, uuid, datetime, random
from typing import Any, Dict, List, Optional
from db import get_db, now, audit, agent_execution_start, agent_execution_complete, add_exception, add_transition, add_timeline

AGENT_REGISTRY = [
    {"name":"Registration Agent","role":"Registers patient and validates identity"},
    {"name":"Patient Identity Agent","role":"Detects duplicates and data quality issues"},
    {"name":"Encounter Tracking Agent","role":"Tracks encounter lifecycle and transitions"},
    {"name":"Clinical Documentation Intelligence Agent","role":"Flags documentation gaps (simulated, advisory)"},
    {"name":"Charge Capture Agent","role":"Captures billable services and detects missing charges"},
    {"name":"Billing Agent","role":"Generates and validates bills"},
    {"name":"Claims Validation Agent","role":"Vets claims before submission"},
    {"name":"Payment and Reconciliation Agent","role":"Records and reconciles payments"},
    {"name":"Revenue Leakage Detection Agent","role":"Detects potential leakage"},
    {"name":"Reporting Agent","role":"Summarizes revenue metrics"},
    {"name":"Human Review Agent","role":"Routes exceptions to human decision"},
]

AGENT_STATES = {}

TASK_STATUSES = ["created","assigned","started","completed","reviewed","escalated","blocked"]

def _init_agent_states_db(c):
    """Ensure every registered agent has a row in agent_states."""
    for a in AGENT_REGISTRY:
        existing = c.execute("SELECT 1 FROM agent_states WHERE agent_name=?", (a["name"],)).fetchone()
        if not existing:
            c.execute(
                """INSERT INTO agent_states(agent_name,role,status,current_task,started_at,completed_at,duration_ms,last_activity,tasks_completed,exceptions,output,updated_at)
                VALUES(?,?,'Idle',NULL,NULL,NULL,NULL,NULL,0,0,NULL,?)""",
                (a["name"], a["role"], now())
            )

def _load_states_from_db():
    """Load all agent_states from DB into AGENT_STATES cache."""
    c = get_db()
    _init_agent_states_db(c)
    rows = c.execute("SELECT * FROM agent_states").fetchall()
    c.close()
    AGENT_STATES.clear()
    for r in rows:
        d = dict(r)
        aname = d.pop("agent_name")
        d["name"] = aname
        AGENT_STATES[aname] = d

def _save_state_to_db(c, name: str, st: dict):
    """Persist a single agent state to agent_states."""
    c.execute(
        """INSERT INTO agent_states(agent_name,role,status,current_task,started_at,completed_at,duration_ms,last_activity,tasks_completed,exceptions,output,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(agent_name) DO UPDATE SET
            role=excluded.role, status=excluded.status, current_task=excluded.current_task,
            started_at=excluded.started_at, completed_at=excluded.completed_at,
            duration_ms=excluded.duration_ms, last_activity=excluded.last_activity,
            tasks_completed=excluded.tasks_completed, exceptions=excluded.exceptions,
            output=excluded.output, updated_at=excluded.updated_at""",
        (name, st.get("role",""), st.get("status","Idle"), st.get("current_task"),
         st.get("started_at"), st.get("completed_at"), st.get("duration_ms"),
         st.get("last_activity"), st.get("tasks_completed",0), st.get("exceptions",0),
         st.get("output"), now())
    )

def set_agent_state(name, status, task=None, result=None):
    """Set agent state — updates in-memory cache and persists to DB."""
    st = AGENT_STATES.get(name, {"name":name,"status":"Idle","current_task":None,"started_at":None,"duration_ms":None,"last_activity":None,"tasks_completed":0,"exceptions":0,"output":None})
    st["status"] = status
    if task is not None: st["current_task"] = task
    if status == "Running":
        st["started_at"] = now(); st["_t0"] = datetime.datetime.now(datetime.timezone.utc)
    if status in ("Completed","Failed","Needs Review","Blocked"):
        t0 = st.pop("_t0", None)
        if t0: st["duration_ms"] = int((datetime.datetime.now(datetime.timezone.utc)-t0).total_seconds()*1000)
        st["completed_at"] = now()
        if status == "Completed": st["tasks_completed"] = st.get("tasks_completed",0)+1
        if status in ("Failed","Needs Review","Blocked"): st["exceptions"] = st.get("exceptions",0)+1
    st["last_activity"] = now()
    if result is not None: st["output"] = result
    AGENT_STATES[name] = st
    # Persist to DB
    try:
        c = get_db()
        _save_state_to_db(c, name, st)
        c.commit(); c.close()
    except Exception:
        pass
    return st

def get_agent_states():
    """Return all agent states, ensuring registry agents exist."""
    if not AGENT_STATES:
        _load_states_from_db()
    result = []
    for a in AGENT_REGISTRY:
        if a["name"] not in AGENT_STATES:
            AGENT_STATES[a["name"]] = {"name":a["name"],"role":a["role"],"status":"Idle","current_task":None,"started_at":None,"duration_ms":None,"last_activity":None,"tasks_completed":0,"exceptions":0,"output":None}
        else:
            AGENT_STATES[a["name"]]["role"] = a["role"]
            if "name" not in AGENT_STATES[a["name"]]:
                AGENT_STATES[a["name"]]["name"] = a["name"]
        result.append(AGENT_STATES[a["name"]])
    return result

def get_agent_state(name: str) -> dict:
    """Get a single agent's state."""
    states = get_agent_states()
    for s in states:
        if s.get("name") == name or s.get("agent_name") == name:
            return s
    return {"name":name,"role":"","status":"Idle","current_task":None,"tasks_completed":0,"exceptions":0,"output":None}

def get_agent_workload(c, agent_name: str) -> int:
    """Count pending + started tasks for an agent (workload metric)."""
    row = c.execute(
        "SELECT COUNT(*) n FROM orchestrator_tasks WHERE agent_name=? AND status IN ('created','assigned','started')",
        (agent_name,)
    ).fetchone()
    return row["n"] if row else 0

def route_to_best_agent(c, task_type: str, preferred_agents: Optional[List[str]] = None) -> Optional[str]:
    """Route a task to the agent with the lowest workload among eligible agents."""
    candidates = preferred_agents if preferred_agents else [a["name"] for a in AGENT_REGISTRY]
    best = None; best_load = None
    for name in candidates:
        load = get_agent_workload(c, name)
        if best_load is None or load < best_load:
            best = name; best_load = load
    return best

def delegate_task(c, from_agent: str, to_agent: str, task_type: str, entity_type: str,
                  entity_id: str, context: str = "", priority: str = "medium",
                  direction: str = "agent_to_agent", assigned_by: str = "System",
                  evidence: str = "", ai_recommendation: str = "",
                  financial_impact: float = 0) -> dict:
    """Create a delegated task from one agent to another."""
    tid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO orchestrator_tasks(id,task_type,entity_type,entity_id,status,priority,assigned_to,assigned_by,agent_name,direction,context,evidence,ai_recommendation,financial_impact,created_at)
        VALUES(?,?,?,?,'created',?,NULL,?,?,?,?,?,?,?,?)""",
        (tid, task_type, entity_type, entity_id, priority, assigned_by, to_agent, direction, context, evidence, ai_recommendation, financial_impact, now())
    )
    # Update the delegating agent's state
    set_agent_state(from_agent, "Running", f"Delegated {task_type} to {to_agent}",
                    {"delegated_to": to_agent, "task_type": task_type, "entity_type": entity_type, "entity_id": entity_id})
    # Update the receiving agent's state
    set_agent_state(to_agent, "Assigned", task_type,
                    {"delegated_from": from_agent, "entity_type": entity_type, "entity_id": entity_id})
    c.commit()
    return {"task_id": tid, "status": "created", "from_agent": from_agent, "to_agent": to_agent}

def create_orchestrator_task(c, task_type: str, entity_type: str, entity_id: str,
                             agent_name: Optional[str] = None, direction: str = "agent_to_human",
                             priority: str = "medium", context: str = "", evidence: str = "",
                             ai_recommendation: str = "", financial_impact: float = 0,
                             assigned_to: Optional[str] = None, assigned_by: str = "System",
                             auto_assign: bool = True) -> dict:
    """Create an orchestrator task, optionally auto-assigning to the best agent."""
    if not agent_name and auto_assign:
        agent_name = route_to_best_agent(c, task_type)
    tid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO orchestrator_tasks(id,task_type,entity_type,entity_id,status,priority,assigned_to,assigned_by,agent_name,direction,context,evidence,ai_recommendation,financial_impact,created_at)
        VALUES(?,?,?,?,'created',?,?,?,?,?,?,?,?,?,?)""",
        (tid, task_type, entity_type, entity_id, priority, assigned_to, assigned_by,
         agent_name or "Orchestrator", direction, context, evidence, ai_recommendation,
         financial_impact, now())
    )
    if agent_name:
        set_agent_state(agent_name, "Assigned", task_type,
                        {"entity_type": entity_type, "entity_id": entity_id, "task_id": tid})
    c.commit()
    return {"task_id": tid, "status": "created", "agent_name": agent_name}

def get_agent_tasks(c, agent_name: str, status: Optional[str] = None) -> List[dict]:
    """Get all tasks for an agent, optionally filtered by status."""
    q = "SELECT * FROM orchestrator_tasks WHERE agent_name=?"
    params = [agent_name]
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY created_at DESC"
    rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]

def record_agent_performance(c, agent_name: str, task_type: str, status: str,
                             started_at: Optional[str] = None, completed_at: Optional[str] = None,
                             duration_ms: Optional[int] = None,
                             human_review_required: int = 0, error_message: Optional[str] = None) -> str:
    """Record an agent execution performance record."""
    pid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO agent_performance(id,agent_name,task_type,status,started_at,completed_at,duration_ms,human_review_required,error_message,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (pid, agent_name, task_type, status, started_at, completed_at, duration_ms,
         human_review_required, error_message, now())
    )
    c.commit()
    return pid

def get_agent_performance(c, agent_name: str) -> dict:
    """Get performance metrics for an agent."""
    rows = c.execute(
        "SELECT * FROM agent_performance WHERE agent_name=? ORDER BY created_at DESC",
        (agent_name,)
    ).fetchall()
    total = len(rows)
    completed = sum(1 for r in rows if r["status"] == "completed")
    failed = sum(1 for r in rows if r["status"] in ("failed","blocked"))
    human_review = sum(1 for r in rows if r["human_review_required"])
    avg_duration = None
    durations = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
    if durations:
        avg_duration = sum(durations) / len(durations)
    return {
        "agent_name": agent_name,
        "total_executions": total,
        "completed": completed,
        "failed": failed,
        "human_review_required": human_review,
        "success_rate": round((completed / total * 100) if total else 0, 1),
        "avg_duration_ms": round(avg_duration) if avg_duration else None
    }

def get_agent_detail(c, agent_name: str) -> dict:
    """Get full detail view for an agent: state, tasks, performance, recent executions."""
    state = get_agent_state(agent_name)
    # Ensure role from registry
    for a in AGENT_REGISTRY:
        if a["name"] == agent_name:
            state["role"] = a["role"]
            break
    tasks = get_agent_tasks(c, agent_name)
    performance = get_agent_performance(c, agent_name)
    recent_executions = c.execute(
        "SELECT * FROM agent_executions WHERE agent_name=? ORDER BY created_at DESC LIMIT 10",
        (agent_name,)
    ).fetchall()
    return {
        "agent": state,
        "tasks": [dict(r) for r in tasks],
        "task_count": len(tasks),
        "pending": sum(1 for r in tasks if r["status"] in ("created","assigned","started")),
        "completed": sum(1 for r in tasks if r["status"] == "completed"),
        "escalated": sum(1 for r in tasks if r["status"] == "escalated"),
        "blocked": sum(1 for r in tasks if r["status"] == "blocked"),
        "performance": performance,
        "recent_executions": [dict(r) for r in recent_executions],
    }

def _dup_check(c, first, last, dob, phone):
    matches = []
    if not (first and last): return matches
    rows = c.execute("SELECT * FROM patients").fetchall()
    for r in rows:
        score = 0
        reasons = []
        if (r["first_name"] or "").strip().lower() == first.strip().lower(): score += 1
        if (r["last_name"] or "").strip().lower() == last.strip().lower(): score += 1
        if dob and r["date_of_birth"] == dob: score += 2; reasons.append("same DOB")
        if phone and r["phone"] and r["phone"] == phone: score += 2; reasons.append("same phone")
        if score >= 3:
            matches.append({"patient_id": r["patient_id"], "id": r["id"], "score": score, "reasons": reasons})
    return matches


def register_patient_full(data: dict) -> Dict[str, Any]:
    c = get_db()
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    if not first or not last:
        c.close(); return {"error": "first_name and last_name required"}
    wf = str(uuid.uuid4())
    set_agent_state("Registration Agent","Running","Capture registration data")
    eid = agent_execution_start(c, "Registration Agent", "register_patient", "patient", None, data, wf)
    pid = str(uuid.uuid4())
    mrn = data.get("patient_id") or ("MRN-" + str(uuid.uuid4())[:8].upper())
    if c.execute("SELECT 1 FROM patients WHERE patient_id=?", (mrn,)).fetchone():
        c.close(); return {"error": "Patient ID already exists"}
    missing = [f for f in ["date_of_birth","gender","phone","insurance_type"] if not data.get(f)]
    dups = _dup_check(c, first, last, data.get("date_of_birth"), data.get("phone"))
    fin = {"payer_type": data.get("insurance_type") or "self_pay", "insurance_number": data.get("insurance_number"), "facility_id": data.get("facility_id"), "created": now(), "balance": 0}
    flags = {"duplicates": dups, "missing": missing}
    c.execute(
        """INSERT INTO patients(id,patient_id,first_name,last_name,date_of_birth,gender,phone,email,address,insurance_type,insurance_number,status,created_at,updated_at,facility_id,financial_profile,duplicate_flags,registration_notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)""",
        (pid, mrn, first, last, data.get("date_of_birth"), data.get("gender"), data.get("phone"), data.get("email"),
         data.get("address"), data.get("insurance_type"), data.get("insurance_number"), now(), now(),
         data.get("facility_id"), json.dumps(fin), json.dumps(flags), json.dumps({"source":"demo_registration"}))
    )
    add_timeline(c, pid, "Registration Agent", "Patient registered", f"MRN {mrn} created (synthetic)", "done")
    set_agent_state("Registration Agent","Completed","Capture registration data", {"mrn": mrn})
    agent_execution_complete(c, eid, "completed", {"patient_id": pid, "mrn": mrn}, duration_ms=50)

    set_agent_state("Patient Identity Agent","Running","Validate identity and detect duplicates")
    eid2 = agent_execution_start(c, "Patient Identity Agent", "identity_check", "patient", pid, {"mrn":mrn}, wf)
    if dups:
        add_timeline(c, pid, "Patient Identity Agent", "Possible duplicate detected", f"{len(dups)} potential match(es) found", "review")
        add_exception(c, "patient_identity", "medium", f"Possible duplicate for {mrn}", f"Matching demographics found for {len(dups)} existing record(s).", "Review and merge/confirm identity", "Patient Identity Agent", patient_id=pid)
        set_agent_state("Patient Identity Agent","Needs Review","Duplicate check", {"duplicates": len(dups)})
    else:
        add_timeline(c, pid, "Patient Identity Agent", "Identity validated", "No duplicate matches detected by demo rules", "done")
        set_agent_state("Patient Identity Agent","Completed","Identity validated", {"duplicates": 0})
    if missing:
        add_timeline(c, pid, "Patient Identity Agent", "Missing registration fields", ", ".join(missing), "warning")
        add_exception(c, "patient_identity", "low", f"Missing registration data for {mrn}", f"Missing: {', '.join(missing)}", "Collect missing fields", "Patient Identity Agent", patient_id=pid)
    agent_execution_complete(c, eid2, "completed", {"duplicates": dups, "missing": missing}, human_review_required=bool(dups), duration_ms=40)

    set_agent_state("Orchestrator","Running","Initiate revenue orchestration")
    add_timeline(c, pid, "Orchestrator", "Revenue workflow initiated", "Registration complete; ready for encounter creation", "done")
    audit(c, "Registration Agent", "agent", "Orchestrator", "Patient registration workflow completed", "patient", pid, new_state="active", workflow_id=wf,
         input_summary=json.dumps({"name":f"{first} {last}"}), output_summary=json.dumps({"mrn":mrn,"duplicates":len(dups),"missing":len(missing)}))
    set_agent_state("Orchestrator","Completed","Workflow initiated", {"patient_id": pid})
    c.commit()
    row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    timeline = [dict(r) for r in c.execute("SELECT * FROM registration_timeline WHERE patient_id=? ORDER BY ts", (pid,)).fetchall()]
    c.close()
    return {"patient": dict(row), "timeline": timeline, "workflow_id": wf, "duplicates": dups, "missing_fields": missing}
