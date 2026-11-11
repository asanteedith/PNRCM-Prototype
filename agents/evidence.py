"""RCM Evidence Intelligence Agent.

Deterministic, rules-based evidence analysis over existing structured records plus
stored synthetic documents. Never fabricates page numbers, quotations, document
references, or citations. Clearly distinguishes verified facts from AI-style
interpretations. All findings are decision support and require human review.
"""
from __future__ import annotations
import json, uuid, datetime
from typing import Any, Dict, List, Tuple
from db import get_db, now, audit, agent_execution_start, agent_execution_complete

SEVERITY_ORDER = {"low":0,"medium":1,"high":2,"critical":3}
FINDING_CATEGORIES = [
    "Missing documentation","Documentation inconsistency","Claim-data discrepancy",
    "Potential coding inconsistency","Potential billing issue","Denial-related evidence",
    "Underpayment-related evidence","Duplicate documentation","Unresolved documentation question",
]
REVIEW_STATUSES = ["Pending Review","Approved","Rejected","Needs More Information","Corrected"]

def _finding_id(): return str(uuid.uuid4())

def _get_or_create_document(c, claim_id: str, encounter_id: str, filename: str, doc_type: str,
                            page_texts: List[str], source_path: str = None) -> Dict[str, Any]:
    existing = c.execute("SELECT id FROM rcm_documents WHERE claim_id=? AND filename=? ORDER BY created_at DESC LIMIT 1",
                         (claim_id, filename)).fetchone()
    did = existing["id"] if existing else str(uuid.uuid4())
    if not existing:
        combined = "\n".join(page_texts)
        c.execute(
            "INSERT INTO rcm_documents(id,claim_id,encounter_id,filename,doc_type,page_count,content_text,source_path,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (did, claim_id, encounter_id, filename, doc_type, max(len(page_texts),1), combined,
             source_path or "", json.dumps({"synthetic": True}), now()))
        for i, t in enumerate(page_texts, start=1):
            pid = str(uuid.uuid4())
            c.execute("INSERT INTO rcm_document_pages(id,document_id,page_number,text,created_at) VALUES(?,?,?,?,?)",
                      (pid, did, i, t, now()))
    return {"document_id": did, "page_count": max(len(page_texts),1), "doc_type": doc_type, "filename": filename}

def _search_document_pages(c, document_id: str, query_terms: List[str]) -> List[Dict[str, Any]]:
    hits = []
    if not document_id: return hits
    pages = c.execute("SELECT id,page_number,text FROM rcm_document_pages WHERE document_id=? ORDER BY page_number", (document_id,)).fetchall()
    terms = [t.lower() for t in query_terms if t]
    if not terms:
        for pg in pages:
            hits.append({"page_id": pg["id"], "page_number": pg["page_number"], "text": pg["text"], "matched_terms": []})
        return hits
    for pg in pages:
        text = pg["text"] or ""
        low = text.lower()
        matched = [t for t in terms if t in low]
        if matched:
            hits.append({"page_id": pg["id"], "page_number": pg["page_number"], "text": text, "matched_terms": matched})
    return hits

def _chronology_for_claim(c, claim_id: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    cl = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not cl: return events
    cld = dict(cl)
    enc_id = cld.get("encounter_id")
    patient_id = cld.get("patient_id")

    # Patient registration from patients created_at
    pat = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (patient_id, patient_id)).fetchone() if patient_id else None
    if pat:
        events.append({"event_type":"Patient registration","event_time":pat["created_at"],"event_source":"patients","related_reference":pat["patient_id"],
                       "details":"Patient record created","is_unknown":0})
    else:
        events.append({"event_type":"Patient registration","event_time":None,"event_source":"patients","related_reference":patient_id or "",
                       "details":"Patient record not found","is_unknown":1})

    # Encounter events
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (enc_id, enc_id)).fetchone() if enc_id else None
    encd = dict(enc) if enc else {}
    if enc:
        if encd.get("created_at"):
            events.append({"event_type":"Encounter created","event_time":encd["created_at"],"event_source":"encounters","related_reference":encd["encounter_id"],
                           "details":"Encounter record created","is_unknown":0})
        if encd.get("status") and encd["status"]!="registered":
            events.append({"event_type":f"Encounter status changed to {encd['status']}","event_time":encd.get("updated_at") or encd["created_at"],"event_source":"encounters","related_reference":encd["encounter_id"],
                           "details":f"Status {encd['status']}","is_unknown":0})
        if encd.get("admission_date"):
            events.append({"event_type":"Service rendered / encounter date","event_time":encd["admission_date"],"event_source":"encounters","related_reference":encd["encounter_id"],
                           "details":(encd.get("reason") or "")[:120],"is_unknown":0})
        # transitions
        trans = c.execute("SELECT * FROM encounter_transitions WHERE encounter_id=? ORDER BY ts", (encd["id"],)).fetchall()
        for tr in trans:
            trd = dict(tr)
            events.append({"event_type":f"Encounter transition {(trd.get('to_status') or trd.get('from_status'))}","event_time":trd["ts"],"event_source":"encounter_transitions","related_reference":encd["encounter_id"],
                           "details":(trd.get("note") or "")[:120],"is_unknown":0})

    # Documents
    docs = c.execute("SELECT * FROM rcm_documents WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()
    for d in docs:
        events.append({"event_type":"Documentation created","event_time":d["created_at"],"event_source":"rcm_documents","related_reference":d["filename"],
                       "details":f"{d['doc_type']} document stored","is_unknown":0})

    # Claim lifecycle
    claim_events = [
        ("Claim prepared","draft","created_at"),
        ("Claim submitted","submitted","submitted_at"),
        ("Claim vetted","vetted","updated_at"),
        ("Claim denied/rejected", None, "updated_at"),
    ]
    for label, wanted_status, ts_field in claim_events:
        ts = cld.get(ts_field)
        if wanted_status and cld.get("status") != wanted_status and not (wanted_status in ("denied/rejected",) and cld.get("status") in ("denied","rejected")):
            continue
        events.append({"event_type":label,"event_time":ts,"event_source":"claims","related_reference":cld.get("claim_number"),
                       "details":f"Claim status {cld.get('status')}","is_unknown":0 if ts else 1})

    # Appeals
    appeals = [dict(r) for r in c.execute("SELECT * FROM appeals WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()]
    for ap in appeals:
        events.append({"event_type":"Appeal drafted","event_time":ap["created_at"],"event_source":"appeals","related_reference":(ap.get("appeal_letter") or "")[:30],
                       "details":f"Status {ap.get('status')}","is_unknown":0})
        if ap.get("updated_at") and ap.get("status") in ("SUBMITTED","APPROVED","DENIED","RESOLVED"):
            events.append({"event_type":f"Appeal {ap['status'].title()}","event_time":ap["updated_at"],"event_source":"appeals","related_reference":(ap.get("appeal_letter") or "")[:30],
                           "details":"Appeal lifecycle transition","is_unknown":0})

    # Payments / reconciliation
    pays = [dict(r) for r in c.execute("SELECT * FROM payments WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()]
    for py in pays:
        events.append({"event_type":"Payment received","event_time":py["payment_date"] or py["created_at"],"event_source":"payments","related_reference":py.get("reference",""),
                       "details":f"{py.get('payment_method','')} GHS {py.get('amount') or 0}","is_unknown":0})

    # Normalize unknown times
    for e in events:
        if not e["event_time"]: e["event_time"] = None; e["is_unknown"] = 1
    events.sort(key=lambda x: (x["event_time"] or "", x["event_type"]))
    return events

def _build_findings_for_claim(c, cl: dict, encd: dict, docs: list) -> List[dict]:
    findings = []
    cld = dict(cl)
    claim_number = cld.get("claim_number") or cld["id"]
    status = cld.get("status") or ""
    denial_reason = cld.get("denial_reason") or ""
    dx = cld.get("diagnosis_codes")
    proc = cld.get("procedure_codes")
    total = float(cld.get("total_charges") or 0)

    # Get claim services for billing checks
    services = []
    if encd:
        svc_rows = c.execute("SELECT * FROM services WHERE encounter_id=? ORDER BY created_at", (encd["id"],)).fetchall()
        services = [dict(s) for s in svc_rows]
    billed_sum = sum(float(s.get("total_price") or 0) for s in services if s.get("billed"))
    unbilled = [s for s in services if not s.get("billed")]

    docs_by_claim = docs  # already filtered
    doc_lookup = {d["id"]: d for d in docs_by_claim}

    # Helper: search across docs for terms
    def find_text(terms: List[str]) -> Tuple[str, str, int, str]:
        for d in docs_by_claim:
            hits = _search_document_pages(c, d["id"], terms)
            if hits:
                h = hits[0]
                return d["id"], d["filename"], h["page_number"], h["text"]
        return "", "", 0, ""

    # 1. Missing documentation
    if not docs_by_claim:
        findings.append({
            "category":"Missing documentation",
            "description":f"No supporting documents are attached to claim {claim_number}.",
            "severity":"high",
            "why_it_matters":"Denials often cite missing documentation; without attached records the claim cannot be evidenced.",
            "recommended_action":"Attach supporting clinical notes, orders, or reports before resubmission or appeal.",
            "confidence":"High (structured record check)",
            "interpretation_type":"Verified fact",
            "relevant_claim_field":"documents",
            "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":None,
        })
    else:
        # Check documentation completeness signals from existing claim fields
        missing_fields = []
        if not cld.get("diagnosis_codes"): missing_fields.append("diagnosis_codes")
        if not cld.get("procedure_codes"): missing_fields.append("procedure_codes")
        if not cld.get("payer_name"): missing_fields.append("payer_name")
        if missing_fields:
            findings.append({
                "category":"Missing documentation",
                "description":f"Claim {claim_number} is missing recorded fields: {', '.join(missing_fields)}.",
                "severity":"medium",
                "why_it_matters":"Missing required claim fields can lead to rejection even if supporting documents exist.",
                "recommended_action":"Complete claim metadata before resubmission.",
                "confidence":"High (structured record check)",
                "interpretation_type":"Verified fact",
                "relevant_claim_field":", ".join(missing_fields),
                "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":None,
            })

    # 2. Claim-data discrepancy: total vs billed services
    if billed_sum > 0 and abs(total - billed_sum) > 0.01:
        doc_id, filename, page, text = find_text([claim_number, "billed", "total"])
        findings.append({
            "category":"Claim-data discrepancy",
            "description":f"Claim total GHS {total:.2f} does not match summed billed services GHS {billed_sum:.2f} for claim {claim_number}.",
            "severity":"high",
            "why_it_matters":"Totals that disagree with billed service lines can trigger denial or underpayment review.",
            "recommended_action":"Reconcile claim total with billed service lines and cite the service schedule.",
            "confidence":"High (structured record check)",
            "interpretation_type":"Verified fact",
            "relevant_claim_field":"total_charges",
            "source_document":filename or None,"page_number":page or None,"relevant_text":text or None,"claim_field_or_line":f"Claim total {total} vs billed services {billed_sum}",
        })

    # 3. Unbilled services (missing charges)
    if unbilled:
        names = ", ".join(s.get("service_name") or "service" for s in unbilled[:5])
        findings.append({
            "category":"Missing documentation",
            "description":f"{len(unbilled)} delivered service(s) on the encounter are not billed: {names}.",
            "severity":"medium",
            "why_it_matters":"Unbilled delivered services represent revenue leakage and missing charge documentation.",
            "recommended_action":"Add charges for delivered services or mark them non-billable.",
            "confidence":"High (structured record check)",
            "interpretation_type":"Verified fact",
            "relevant_claim_field":"services",
            "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":names,
        })

    # 4. Denial-related evidence
    if status in ("denied","rejected") and denial_reason:
        findings.append({
            "category":"Denial-related evidence",
            "description":f"Claim {claim_number} is {status}. Recorded denial reason: {denial_reason}.",
            "severity":"high",
            "why_it_matters":"The denial reason identifies which evidence category must be addressed in appeal preparation.",
            "recommended_action":"Gather documentation directly addressing the stated denial reason.",
            "confidence":"High (structured record check)",
            "interpretation_type":"Verified fact",
            "relevant_claim_field":"denial_reason",
            "source_document":None,"page_number":None,"relevant_text":denial_reason,"claim_field_or_line":"denial_reason",
        })
        if "document" in denial_reason.lower() or "support" in denial_reason.lower():
            findings.append({
                "category":"Missing documentation",
                "description":f"Denial reason indicates missing or insufficient supporting documentation.",
                "severity":"critical",
                "why_it_matters":"Appeal must include the missing documentation references.",
                "recommended_action":"Locate and cite the missing supporting documents before appeal drafting.",
                "confidence":"High (structured record check)",
                "interpretation_type":"Verified fact",
                "relevant_claim_field":"denial_reason",
                "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":"denial_reason",
            })

    # 5. Potential coding inconsistency (deterministic heuristic only if both codes present)
    if dx and proc:
        try:
            dx_list = json.loads(dx) if isinstance(dx, str) else dx
            proc_list = json.loads(proc) if isinstance(proc, str) else proc
            if dx_list and proc_list:
                findings.append({
                    "category":"Potential coding inconsistency",
                    "description":f"Claim {claim_number} records {len(dx_list)} diagnosis code(s) and {len(proc_list)} procedure code(s); consistency requires human coding review.",
                    "severity":"medium",
                    "why_it_matters":"Diagnosis-procedure alignment affects medical necessity and claim acceptance.",
                    "recommended_action":"Have a certified coder review diagnosis/procedure pairing.",
                    "confidence":"Low (structured heuristic only; not a coding system lookup)",
                    "interpretation_type":"AI-generated interpretation (simulated)",
                    "relevant_claim_field":"diagnosis_codes/procedure_codes",
                    "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":f"dx={dx}, proc={proc}",
                })
        except Exception:
            pass

    # 6. Underpayment-related evidence
    if cld.get("approved_amount") and cld.get("paid_amount") and float(cld["approved_amount"]) > float(cld["paid_amount"]) + 0.01:
        diff = float(cld["approved_amount"]) - float(cld["paid_amount"])
        findings.append({
            "category":"Underpayment-related evidence",
            "description":f"Claim {claim_number} approved GHS {float(cld['approved_amount']):.2f} but paid GHS {float(cld['paid_amount']):.2f}; underpayment GHS {diff:.2f}.",
            "severity":"high",
            "why_it_matters":"Underpayment should be followed up with payer.",
            "recommended_action":"Reconcile remittance and initiate underpayment follow-up.",
            "confidence":"High (structured record check)",
            "interpretation_type":"Verified fact",
            "relevant_claim_field":"paid_amount/approved_amount",
            "source_document":None,"page_number":None,"relevant_text":None,"claim_field_or_line":f"approved {cld['approved_amount']} vs paid {cld['paid_amount']}",
        })

    # Persist findings to evidence_findings with citations where available
    created_ids = []
    for f in findings:
        fid = _finding_id()
        c.execute(
            """INSERT INTO evidence_findings(id,run_id,claim_id,category,description,severity,why_it_matters,recommended_action,confidence,interpretation_type,review_status,relevant_claim_field,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, None, cld["id"], f["category"], f["description"], f["severity"], f["why_it_matters"], f["recommended_action"],
             f["confidence"], f["interpretation_type"], "Pending Review", f["relevant_claim_field"], now(), now()))
        # Add citation if source available
        if f.get("source_document"):
            cid = _finding_id()
            c.execute(
                """INSERT INTO evidence_citations(id,finding_id,document_id,document_filename,page_number,relevant_text,claim_field_or_line,citation_status,created_at)
                   VALUES(?,?,?,?,?,?,?,'verified',?)""",
                (cid, fid, f.get("source_document_id"), f.get("source_document"), f.get("page_number"), f.get("relevant_text"), f.get("claim_field_or_line"), now()))
        created_ids.append({"finding_id": fid, "category": f["category"], "severity": f["severity"]})
    return created_ids

def analyze_evidence(claim_id: str) -> Dict[str, Any]:
    """Run deterministic evidence analysis for a claim. Persists findings/citations."""
    c = get_db()
    start = datetime.datetime.now(datetime.timezone.utc)
    cl = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not cl:
        c.close(); return {"error":"Claim not found"}, 404
    cld = dict(cl)
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (cld.get("encounter_id"), cld.get("encounter_id"))).fetchone()
    encd = dict(enc) if enc else {}
    docs = c.execute("SELECT * FROM rcm_documents WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()

    rid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO evidence_analysis_runs(id,claim_id,status,method,summary,started_at,created_at)
           VALUES(?,?,'running','deterministic',?,?,?)""",
        (rid, cld["id"], f"Evidence analysis for claim {cld.get('claim_number')}", now(), now()))

    created = _build_findings_for_claim(c, cl, encd, [dict(d) for d in docs])
    chronology = _chronology_for_claim(c, claim_id)

    # Persist chronology events
    for ev in chronology:
        cid = str(uuid.uuid4())
        c.execute(
            """INSERT INTO claim_chronology_events(id,claim_id,event_type,event_time,event_source,related_reference,details,is_unknown,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid, cld["id"], ev["event_type"], ev["event_time"], ev["event_source"], ev["related_reference"], ev["details"], 1 if ev["is_unknown"] else 0, now()))

    end = datetime.datetime.now(datetime.timezone.utc)
    duration_ms = int((end - start).total_seconds()*1000)
    c.execute(
        "UPDATE evidence_analysis_runs SET status='completed',completed_at=?,duration_ms=? WHERE id=?",
        (now(), duration_ms, rid))
    audit(c, "RCM Evidence Intelligence Agent","agent","Evidence Analysis","Claim evidence analysis completed","evidence_analysis_run",rid,
          previous_state=None,new_state="completed",recommendation=f"{len(created)} findings",
          execution_duration_ms=duration_ms)
    c.commit(); c.close()
    return {
        "run_id": rid,
        "claim_id": cld["id"],
        "claim_number": cld.get("claim_number"),
        "status":"completed",
        "method":"deterministic (simulated; no paid AI)",
        "findings_count": len(created),
        "findings": created,
        "chronology_count": len(chronology),
        "summary": f"Analyzed claim {cld.get('claim_number')}: {len(created)} findings, {len(chronology)} chronology events.",
    }

def list_findings(claim_id: str = None, status: str = None) -> List[dict]:
    c = get_db()
    q = "SELECT * FROM evidence_findings WHERE 1=1"; params=[]
    if claim_id: q+=" AND claim_id=?"; params.append(claim_id)
    if status: q+=" AND review_status=?"; params.append(status)
    q+=" ORDER BY created_at DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [dict(r) for r in rows]

def list_citations(finding_id: str) -> List[dict]:
    c = get_db()
    rows = c.execute("SELECT * FROM evidence_citations WHERE finding_id=? ORDER BY created_at", (finding_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def get_chronology(claim_id: str) -> List[dict]:
    c = get_db()
    rows = c.execute("SELECT * FROM claim_chronology_events WHERE claim_id=? ORDER BY event_time", (claim_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def review_finding(finding_id: str, decision: str, reviewer: str, notes: str = "", corrected_description: str = "",
                   evidence_relevance: str = "", missing_evidence_note: str = "") -> Dict[str, Any]:
    if decision not in REVIEW_STATUSES:
        return {"error": f"decision must be one of {REVIEW_STATUSES}"}, 400
    c = get_db()
    f = c.execute("SELECT * FROM evidence_findings WHERE id=?", (finding_id,)).fetchone()
    if not f:
        c.close(); return {"error":"Finding not found"}, 404
    prev = f["review_status"]
    c.execute(
        """INSERT INTO evidence_review_decisions(id,finding_id,claim_id,reviewer,decision,corrected_description,notes,evidence_relevance,missing_evidence_note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), finding_id, f["claim_id"], reviewer, decision, corrected_description, notes, evidence_relevance, missing_evidence_note, now()))
    new_desc = corrected_description if corrected_description else f["description"]
    c.execute("UPDATE evidence_findings SET review_status=?, updated_at=?, description=? WHERE id=?",
              (decision, now(), new_desc, finding_id))
    audit(c, reviewer,"user","RCM Evidence Intelligence","Finding reviewed","evidence_finding",finding_id,
          previous_state=prev,new_state=decision,decision=decision)
    c.commit(); c.close()
    return {"ok":True,"finding_id":finding_id,"review_status":decision}

def record_feedback(finding_id: str, reviewer: str, action: str, reason: str = "", corrected_description: str = "",
                    marked_irrelevant: bool = False, missing_evidence: str = "", original_description: str = "") -> Dict[str, Any]:
    c = get_db()
    f = c.execute("SELECT * FROM evidence_findings WHERE id=?", (finding_id,)).fetchone()
    if not f:
        c.close(); return {"error":"Finding not found"}, 404
    fid = str(uuid.uuid4())
    c.execute(
        """INSERT INTO evidence_feedback(id,finding_id,claim_id,reviewer,action,reason,corrected_description,marked_irrelevant,missing_evidence,original_description,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, finding_id, f["claim_id"], reviewer, action, reason, corrected_description, 1 if marked_irrelevant else 0,
         missing_evidence, original_description, now()))
    audit(c, reviewer,"user","RCM Evidence Intelligence","Reviewer feedback recorded","evidence_feedback",fid,
          previous_state=f["review_status"],new_state=f["review_status"],decision=action)
    c.commit(); c.close()
    return {"ok":True,"feedback_id":fid}

def prepare_appeal_evidence_package(claim_id: str) -> Dict[str, Any]:
    """Prepare appeal evidence package using approved findings + existing appeals module structure."""
    c = get_db()
    cl = c.execute("SELECT * FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone()
    if not cl:
        c.close(); return {"error":"Claim not found"}, 404
    cld = dict(cl)
    enc = c.execute("SELECT * FROM encounters WHERE id=? OR encounter_id=?", (cld.get("encounter_id"), cld.get("encounter_id"))).fetchone()
    encd = dict(enc) if enc else {}
    pat = c.execute("SELECT * FROM patients WHERE id=? OR patient_id=?", (cld.get("patient_id"), cld.get("patient_id"))).fetchone()
    patd = dict(pat) if pat else {}
    docs = c.execute("SELECT * FROM rcm_documents WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()
    docs = [dict(d) for d in docs]

    findings = [dict(r) for r in c.execute("SELECT * FROM evidence_findings WHERE claim_id=? ORDER BY created_at", (cld["id"],)).fetchall()]
    approved_findings = [f for f in findings if f["review_status"] == "Approved"]
    pending = [f for f in findings if f["review_status"] == "Pending Review"]

    # Citations for approved findings
    evidence_citations = []
    missing_evidence_checklist = []
    for f in approved_findings:
        cits = c.execute("SELECT * FROM evidence_citations WHERE finding_id=?", (f["id"],)).fetchall()
        for cit in cits:
            evidence_citations.append(dict(cit))
        if not cits:
            missing_evidence_checklist.append({"finding_id": f["id"], "category": f["category"], "description": f["description"],
                                               "note": "No verified source citation; finding relies on structured record only."})
    # If denial mentions missing docs but no documents stored
    if cld.get("denial_reason") and ("document" in cld["denial_reason"].lower() or "support" in cld["denial_reason"].lower()) and not docs:
        missing_evidence_checklist.append({"finding_id": None, "category": "Missing documentation",
                                           "description": "Denial cites missing supporting documentation but no documents are stored for this claim.",
                                           "note": "Obtain supporting documents before appeal submission."})

    denied = cld.get("status") in ("denied","rejected")
    package = {
        "claim_id": cld["id"],
        "claim_number": cld.get("claim_number"),
        "patient": {"id": patd.get("patient_id"), "name": f"{patd.get('first_name','')} {patd.get('last_name','')}"} if patd else None,
        "encounter": {"encounter_id": encd.get("encounter_id"), "status": encd.get("status"), "type": encd.get("encounter_type")} if encd else None,
        "denial": {"status": cld.get("status"), "reason": cld.get("denial_reason"), "total_charges": cld.get("total_charges")} if denied else None,
        "chronology": _chronology_for_claim(c, claim_id),
        "approved_findings": approved_findings,
        "pending_findings": pending,
        "evidence_citations": evidence_citations,
        "missing_evidence_checklist": missing_evidence_checklist,
        "documents": [{"id": d["id"],"filename": d["filename"],"doc_type": d["doc_type"],"page_count": d["page_count"],"created_at": d["created_at"]} for d in docs],
        "distinction": {
            "verified_facts": "Findings marked Verified fact derive from structured database records or stored document text.",
            "ai_interpretations": "Findings marked AI-generated interpretation (simulated) are deterministic heuristics, not clinical or payer advice.",
            "missing_evidence": "Items with no verified citation require human-supplied documentation before appeal submission.",
            "human_approval_required": True,
        },
        "note": "This is a decision-support evidence package. It does not submit an appeal and does not fabricate evidence.",
    }
    c.close()
    return package

def evidence_metrics() -> Dict[str, Any]:
    c = get_db()
    runs = c.execute("SELECT * FROM evidence_analysis_runs").fetchall()
    findings = c.execute("SELECT * FROM evidence_findings").fetchall()
    decisions = c.execute("SELECT * FROM evidence_review_decisions").fetchall()
    feedback = c.execute("SELECT * FROM evidence_feedback").fetchall()
    docs = c.execute("SELECT * FROM rcm_documents").fetchall()
    c.close()
    by_status = {}
    for f in findings:
        by_status[f["review_status"]] = by_status.get(f["review_status"],0)+1
    by_cat = {}
    for f in findings:
        by_cat[f["category"]] = by_cat.get(f["category"],0)+1
    return {
        "analysis_runs": len(runs),
        "total_findings": len(findings),
        "by_review_status": by_status,
        "by_category": by_cat,
        "review_decisions": len(decisions),
        "feedback_records": len(feedback),
        "stored_documents": len(docs),
        "method": "deterministic simulated rules; no paid AI",
    }

def store_synthetic_document(claim_id: str, encounter_id: str, filename: str, doc_type: str, page_texts: List[str], source_path: str = None) -> Dict[str, Any]:
    c = get_db()
    cl = c.execute("SELECT 1 FROM claims WHERE id=? OR claim_number=?", (claim_id, claim_id)).fetchone() if claim_id else None
    if claim_id and not cl:
        c.close(); return {"error":"Claim not found"}, 404
    info = _get_or_create_document(c, claim_id, encounter_id, filename, doc_type, page_texts, source_path)
    did = info["document_id"]
    c.commit()
    row = c.execute("SELECT * FROM rcm_documents WHERE id=?", (did,)).fetchone()
    c.close()
    return {"document_id": did, "claim_id": claim_id, "filename": row["filename"], "doc_type": row["doc_type"],
            "page_count": row["page_count"], "created_at": row["created_at"], "synthetic": True}
