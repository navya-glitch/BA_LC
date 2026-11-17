"""
ba_engine.py - lifecycle catalogue, dependencies, staleness, ROUTER, readiness gates and
the BRD / FRD / PRD decision. Pure Python (no Streamlit) so it can be unit-tested.

State `S` is any mapping (a dict or st.session_state).
"""
from datetime import datetime

# ---------------------------------------------------------------- phases
PHASES = [
    {"id": "initiation", "title": "1. Initiation & Planning"},
    {"id": "elicitation", "title": "2. Elicitation"},
    {"id": "analysis", "title": "3. Analysis & Documentation"},
    {"id": "validation", "title": "4. Validation, Prioritisation & Sign-off"},
    {"id": "design", "title": "5. Design Support & Backlog (Jira)"},
    {"id": "testing", "title": "6. Testing Support (UAT)"},
    {"id": "deploy", "title": "7. Deployment & Change Management"},
    {"id": "closure", "title": "8. Evaluation & Closure"},
]
PHASE_TITLE = {p["id"]: p["title"] for p in PHASES}


def _a(key, phase, title, owner, needs=(), kind="llm", **kw):
    d = {"key": key, "phase": phase, "title": title, "owner": owner,
         "needs": list(needs), "kind": kind}
    d.update(kw)
    return d


# ---------------------------------------------------------------- artifacts (ORDER matters)
ARTIFACTS = [
    # ---- 1 Initiation & Planning
    _a("intake", "initiation", "Intake: project & people", "User / BA", kind="special"),
    _a("worktype", "initiation", "Work type", "BA", ["intake"], json=True,
       prompt=("Classify the work type as one of: integration, new build, enhancement, migration, "
               "reporting/analytics, regulatory/compliance change, other. If it is an integration also "
               "identify source_system, target_system, interface_style (API/file/messaging), "
               "real_time_or_batch, data_sensitivity. List up to 6 key_questions for the business. "
               "Return JSON with keys: work_type, confidence, source_system, target_system, "
               "interface_style, real_time_or_batch, data_sensitivity, key_questions.")),
    _a("sources", "initiation", "Sources (Confluence / documents)", "User", ["intake"], kind="special"),
    _a("problem", "initiation", "Problem & AS-IS", "Business Head / PO + SMEs", ["worktype", "sources"],
       prompt=("Write: (1) Problem/opportunity statement (3 sentences), (2) 5-Whys root cause chain, "
               "(3) AS-IS summary: process, systems/interfaces, pain points, (4) gaps in the "
               "documentation. Cite sources. Flag stale sources.")),
    _a("scope", "initiation", "Vision & scope", "Sponsor / PO", ["problem"],
       prompt=("Draft a Vision & Scope: vision, objectives, IN scope, OUT of scope, success metrics "
               "(name KPIs only, no invented targets) and open questions for the business. Cite sources.")),
    _a("stakeholders", "initiation", "Stakeholders", "BA", ["scope"], kind="special"),
    _a("business", "initiation", "Business case & feasibility", "Sponsor + Architect + Finance",
       ["scope", "stakeholders"],
       prompt=("Draft (1) Business case: objectives, benefit CATEGORIES, cost CATEGORIES; every figure "
               "'TBD - business to provide'. (2) Feasibility: technical, operational, regulatory/"
               "compliance, vendor/dependency, each with rating and reason. (3) Options incl. do-nothing.")),
    _a("risks", "initiation", "Risks, assumptions, constraints", "Team (owner per risk)",
       ["scope", "stakeholders", "business"],
       prompt=("Produce a markdown table: Risk | Probability (H/M/L) | Impact (H/M/L) | Mitigation | "
               "Owner role. Then bullet lists of Assumptions and Constraints. Include risks specific to "
               "the work type and domain and lessons from any past-project pages in the context.")),
    _a("plans", "initiation", "BA approach, comms & timeline", "BA + PM + PMO",
       ["scope", "stakeholders", "risks"],
       prompt=("Draft concisely: (1) BA approach with elicitation techniques chosen for this work type and "
               "why, (2) communication plan by stakeholder group, (3) requirements management: templates, "
               "IDs, traceability, change control, (4) indicative timeline in relative weeks only, with "
               "dependencies.")),
    _a("charter", "initiation", "Charter & Go/No-Go pack", "Sponsor",
       ["problem", "scope", "stakeholders", "business", "risks", "plans"],
       prompt=("Create a one-page Project Charter summary and a Go/No-Go decision pack: options, advisory "
               "recommendation, conditions to proceed, and decisions needed from the Sponsor. The decision "
               "belongs to the Sponsor.")),
    # ---- 2 Elicitation
    _a("elic_plan", "elicitation", "Elicitation plan", "BA", ["worktype", "scope", "stakeholders", "plans"],
       prompt=("Create an elicitation plan as a markdown table: Session | Stakeholders | Technique "
               "(interview, workshop, observation, document analysis, survey, prototype) | Objective | "
               "Key questions | Expected output. Cover themes: current process, pain points, rules and "
               "regulations, data, interfaces, exceptions, success measures, non-functional needs. Tailor to "
               "the work type (for integration: interface contract, data mapping, error handling, volumes, "
               "SLAs, environments). Sequence in relative days.")),
    _a("elic_notes", "elicitation", "Elicitation evidence (notes, transcripts)", "BA", ["elic_plan"],
       kind="special"),
    _a("req_raw", "elicitation", "Candidate requirements", "BA", ["elic_notes", "scope"],
       prompt=("Extract candidate requirements ONLY from the elicitation evidence. Markdown table: Cand ID | "
               "Candidate requirement | Type | Source [E#/C#/D#] | Stakeholder | Confidence (H/M/L). Mark "
               "anything you inferred as INFERRED. Then lists: Conflicts between sources, Ambiguities, "
               "Missing information (and who to ask).")),
    # ---- 3 Analysis & Documentation
    _a("doctype", "analysis", "Requirement document type (BRD / FRD / PRD)", "BA + Sponsor",
       ["worktype", "scope"], kind="special"),
    _a("req_catalog", "analysis", "Requirements catalogue", "BA", ["req_raw", "scope", "doctype"],
       prompt=("Produce the requirements catalogue as a markdown table: Req ID (BR-/FR-/NFR-/DR-/IR- "
               "prefixes) | Type | Requirement statement | Rationale | Source | Acceptance criteria | "
               "Depends on. Consolidate duplicates, keep each requirement atomic, testable and unambiguous, "
               "avoid solution bias, keep traceability to candidate IDs. Add relevant NFRs for the work "
               "type and domain (security, audit, retention, performance, availability) marked 'INFERRED - "
               "confirm' unless sourced.")),
    _a("to_be", "analysis", "TO-BE process & use cases", "BA + SMEs", ["req_catalog", "problem"],
       prompt=("Describe the TO-BE process: actors, triggers, pre/post-conditions, numbered main flow, "
               "alternate and exception flows, business rules. Then a Mermaid flowchart in a ```mermaid "
               "block. For integrations include the interface/data flow with error handling and retry points.")),
    _a("req_doc", "analysis", "Requirements document", "BA + Sponsor",
       ["doctype", "req_catalog", "to_be", "scope", "stakeholders", "business", "risks"],
       prompt_fn="req_doc"),
    # ---- 4 Validation, Prioritisation & Sign-off
    _a("validation", "validation", "Requirements validation", "BA + SMEs + QA", ["req_doc", "req_catalog"],
       kind="special"),
    _a("prioritization", "validation", "Prioritisation (MoSCoW)", "PO + Sponsor", ["req_catalog", "validation"],
       prompt=("Prioritise with MoSCoW. Markdown table: Req ID | Requirement (short) | Priority "
               "(Must/Should/Could/Won't) | Business value | Effort (S/M/L - to be confirmed by Dev/"
               "Architect) | Dependency | Rationale | Proposed release. Base priority on objectives, risks and "
               "regulatory obligations. Never invent cost figures. Flag any Must that depends on a Could.")),
    _a("signoff", "validation", "Sign-off", "Sponsor / PO / Compliance", ["req_doc", "prioritization"],
       kind="special"),
    # ---- 5 Design support & backlog
    _a("design_support", "design", "Design support pack", "Architect + Dev Lead",
       ["req_doc", "req_catalog", "prioritization", "worktype"],
       prompt=("Prepare a design-support pack: (1) design questions and decisions needed, (2) key "
               "requirements mapped to likely components (as hypotheses), (3) for integrations: interface "
               "contract checklist and a data-mapping template table, (4) NFR checks, (5) assumptions to "
               "confirm, (6) clarification log table: ID | Question | Raised by | Owner | Status.")),
    _a("stories", "design", "User stories (Jira)", "PO + Dev Lead", ["req_catalog", "prioritization", "signoff"],
       kind="special"),
    # ---- 6 Testing support
    _a("uat_plan", "testing", "UAT plan & scenarios", "BA + Business testers",
       ["req_catalog", "stories", "prioritization"],
       prompt=("Create a UAT plan: objectives, scope, entry and exit criteria, roles, test data and "
               "environment needs, defect severity definitions and triage, schedule in relative days. Then the "
               "scenario table: Scenario ID | Req ID(s) | Scenario (Given/When/Then) | Expected result | "
               "Priority.")),
    _a("rtm", "testing", "Traceability matrix", "BA + QA", ["req_catalog", "stories", "uat_plan"],
       prompt=("Build the Requirements Traceability Matrix as a markdown table: Req ID | Requirement (short) "
               "| Priority | Story / Jira key(s) | UAT scenario ID(s) | Coverage (Covered/GAP). Mark any "
               "requirement without a story or a scenario as GAP. Use the story keys given in the stories "
               "artifact; do not invent keys.")),
    _a("uat_results", "testing", "UAT results & defects", "Business testers + BA", ["uat_plan", "rtm"],
       kind="special"),
    # ---- 7 Deployment & change management
    _a("readiness", "deploy", "Go-live readiness & change plan", "PM + BA + Ops",
       ["req_doc", "uat_results", "risks", "plans"],
       inputs=[{"id": "release", "label": "Release details (dates, environments, support model) - optional",
                "required": False}],
       prompt=("Produce a go-live readiness pack: checklist table Item | Category (Business, Technical, "
               "Data, Training, Compliance, Support) | Owner role | Status (Pending) | Evidence needed; "
               "go/no-go criteria; rollback criteria and outline; communication and training plan; hypercare "
               "plan. Use the supplied release details; unknown dates are TBD.")),
    # ---- 8 Evaluation & closure
    _a("pir", "closure", "Post-implementation review", "Sponsor + BA", ["charter", "scope", "business", "uat_results"],
       inputs=[{"id": "actuals", "label": "Actual results / KPI data after go-live (required; the agent will "
                                          "not invent numbers)", "required": True}],
       prompt=("Write a post-implementation review comparing expected benefits/KPIs from the business case "
               "and scope with the ACTUAL results supplied. If an actual value is not supplied write 'Not "
               "provided'. Include: objectives met?, KPI table (KPI | Target | Actual | Variance | Comment), "
               "requirements delivered vs descoped, adoption, issues, recommendations.")),
    _a("lessons", "closure", "Lessons learned & closure report", "BA + PM", ["risks", "plans", "pir"],
       prompt=("Write the lessons-learned and closure report: what went well, what did not, root causes, "
               "recommendations, open items and handover, document archive list, closure checklist.")),
]
ART = {a["key"]: a for a in ARTIFACTS}
ORDER = [a["key"] for a in ARTIFACTS]


def phase_artifacts(pid):
    return [a["key"] for a in ARTIFACTS if a["phase"] == pid]


# ---------------------------------------------------------------- state
def new_state():
    return {"outputs": {}, "approved": {}, "approved_text": {}, "versions": {}, "based_on": {},
            "invalid": {}, "flags": {}, "audit": [], "changes": [], "inputs": {}, "intake": {},
            "pages": [], "docs": [], "elic_docs": [], "resolution": "", "mismatches": [],
            "informal": {}, "doc_decision": {}, "jira_created": {}, "jira_epics": {}, "loc": "intake"}


STATE_KEYS = list(new_state().keys())


def audit(S, event, detail=""):
    S["audit"].append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "event": event, "detail": detail})


# ---------------------------------------------------------------- status / staleness
def changed_upstream(S, key):
    rec = S["based_on"].get(key, {})
    return [n for n, v in rec.items() if S["versions"].get(n, 0) != v]


def status(S, key):
    """todo | draft | approved | edited | stale | revisit"""
    if key in S["invalid"]:
        return "revisit"
    if not S["outputs"].get(key):
        return "todo"
    if not S["approved"].get(key):
        return "draft"
    if S["outputs"][key] != S["approved_text"].get(key):
        return "edited"
    # stale if an upstream version changed, or an upstream artifact is itself out of date (transitive)
    if changed_upstream(S, key) or any(status(S, n) in ("stale", "revisit", "edited")
                                       for n in ART[key]["needs"]):
        return "stale"
    return "approved"


def missing_needs(S, key):
    return [n for n in ART[key]["needs"] if status(S, n) != "approved"]


def mark_approved(S, key, text=None):
    if text is not None:
        S["outputs"][key] = text
    S["versions"][key] = S["versions"].get(key, 0) + 1
    S["approved"][key] = True
    S["approved_text"][key] = S["outputs"][key]
    S["based_on"][key] = {n: S["versions"].get(n, 0) for n in ART[key]["needs"]}
    S["invalid"].pop(key, None)
    audit(S, "APPROVED", f"{ART[key]['title']} -> v{S['versions'][key]}")


def keep_as_is(S, key):
    """Reviewer confirms the artifact is still valid after an upstream change."""
    S["based_on"][key] = {n: S["versions"].get(n, 0) for n in ART[key]["needs"]}
    S["invalid"].pop(key, None)
    audit(S, "KEPT AS IS", f"{ART[key]['title']} reviewed after upstream change")


def invalidate(S, keys, reason):
    for k in keys:
        if k in ART and status(S, k) != "todo":
            S["invalid"][k] = reason


def set_flag(S, name, target, reason, also_invalidate=()):
    S["flags"][name] = {"open": True, "target": target, "reason": reason}
    invalidate(S, [target, *also_invalidate], reason)
    audit(S, "ROUTED BACK", f"[{name}] to '{ART[target]['title']}': {reason}")


def clear_flag(S, name):
    if name in S["flags"]:
        S["flags"][name]["open"] = False


# ---------------------------------------------------------------- router
def route(S):
    """Decide where the work goes next. Deterministic and explainable.
    Priority: 1) open routing flags (validation / UAT / change), 2) first artifact that is not
    approved (todo, draft, edited, stale, revisit) in lifecycle order, 3) lifecycle complete."""
    for name, f in S["flags"].items():
        # a flag steers the router only until its target has been reworked and re-approved
        if f.get("open") and f.get("target") in ART and status(S, f["target"]) != "approved":
            return {"next_key": f["target"], "kind": "loop-back", "flag": name,
                    "reason": f"Loop-back from [{name}]: {f['reason']}"}
    for k in ORDER:
        st = status(S, k)
        if st == "approved":
            continue
        t = ART[k]["title"]
        reason = {
            "todo": f"Next step in the lifecycle: {t} ({PHASE_TITLE[ART[k]['phase']]}).",
            "draft": f"'{t}' has a draft waiting for your approval.",
            "edited": f"'{t}' was edited after approval; approve it again to publish a new version.",
            "stale": ("'%s' is out of date because upstream changed: %s." %
                      (t, ", ".join(ART[n]["title"] for n in ART[k]["needs"]
                                    if n in changed_upstream(S, k) or status(S, n) in
                                    ("stale", "revisit", "edited")))),
            "revisit": f"'{t}' must be revisited: {S['invalid'].get(k, '')}",
        }[st]
        return {"next_key": k, "kind": "revisit" if st in ("stale", "revisit") else "forward",
                "reason": reason}
    return {"next_key": None, "kind": "complete",
            "reason": "All lifecycle steps are approved. Use Change control for any new change request."}


def phase_progress(S):
    rows = []
    for p in PHASES:
        keys = phase_artifacts(p["id"])
        done = sum(status(S, k) == "approved" for k in keys)
        rows.append({"id": p["id"], "title": p["title"], "done": done, "total": len(keys)})
    return rows


def story_readiness(S):
    """Everything must be finalized before user stories go to Jira."""
    checks = [(f"{ART[k]['title']} approved and current", status(S, k) == "approved")
              for k in ("doctype", "req_doc", "validation", "prioritization", "signoff")]
    checks.append(("No open loop-back flag from validation", not S["flags"].get("validation", {}).get("open")))
    return checks


# ---------------------------------------------------------------- loop-back rules
FINDING_TARGET = {"missing": "elic_notes", "incomplete": "elic_notes", "conflict": "elic_notes",
                  "ambiguous": "req_catalog", "inconsistent": "req_catalog", "untestable": "req_catalog",
                  "out_of_scope": "scope"}
DEFECT_TARGET = {"requirement": "req_catalog", "design": "design_support"}


def findings_target(findings):
    """Earliest artifact that must be reworked to fix the open blocking findings."""
    c = [FINDING_TARGET.get(str(f.get("type", "")).lower(), "req_catalog") for f in findings
         if str(f.get("severity", "")).lower() == "blocking" and str(f.get("resolution", "Open")) == "Open"]
    return min(c, key=ORDER.index) if c else None


def defects_target(defects):
    c = [DEFECT_TARGET[str(d.get("root_cause", "")).lower()] for d in defects
         if str(d.get("status", "Open")) == "Open" and str(d.get("root_cause", "")).lower() in DEFECT_TARGET]
    return min(c, key=ORDER.index) if c else None


def change_target(impacted):
    v = [k for k in impacted if k in ART]
    return min(v, key=ORDER.index) if v else None


# ---------------------------------------------------------------- BRD / FRD / PRD decision
DOC_DEFINITIONS = {
    "BRD": "Business Requirements Document: why the change is needed, business objectives, scope, "
           "stakeholders and high-level business requirements. Audience: sponsor and business leaders.",
    "FRD": "Functional Requirements Document: detailed system behaviour: functions, use cases, business "
           "rules, data, interfaces and non-functional needs. Audience: developers, testers, vendors.",
    "PRD": "Product Requirements Document: product vision, target users, problems, features, success "
           "metrics and release scope. Audience: product, design and agile delivery teams.",
}
DOC_OUTLINES = {
    "BRD": ["Executive summary", "Business objectives and success metrics", "Background and problem statement",
            "Scope (in / out)", "Stakeholders and roles", "Business requirements", "Business process "
            "(AS-IS / TO-BE summary)", "Assumptions, constraints, dependencies", "Risks",
            "Cost-benefit summary (figures TBD)", "Compliance and regulatory considerations", "Glossary",
            "Approval and sign-off"],
    "FRD": ["Purpose and references", "System context and actors", "Functional requirements (by feature, with "
            "IDs traced to business requirements)", "Use cases and process flows including exceptions",
            "Business rules", "Data requirements", "Interface requirements (source, target, protocol, payload, "
            "error handling, retry, volumes TBD)", "Reports and notifications", "Non-functional requirements",
            "Roles and access", "Acceptance criteria and UAT references", "Traceability", "Open issues"],
    "PRD": ["Overview and vision", "Problem and opportunity", "Target users and personas",
            "Goals, non-goals and success metrics", "Features / epics with priority", "Release scope and "
            "phasing", "UX and design notes", "Functional requirements", "Non-functional requirements",
            "Analytics and KPI tracking", "Dependencies and risks", "Open questions", "Launch readiness"],
}
INITIATIVES = ["Internal business project / process change", "Software product or feature (product-led)",
               "System integration / technical change", "Regulatory / compliance change"]
AUDIENCES = ["Sponsor and business leaders", "Developers and testers", "Product and design team",
             "Mixed audience"]
DETAILS = ["High-level (business needs)", "Detailed (system behaviour)"]
METHODS = ["Waterfall", "Agile", "Hybrid"]


def score_doctype(a):
    """a: dict with methodology, initiative, audience, detail, outsourced(bool), regulated(bool)."""
    sc = {"BRD": 0, "FRD": 0, "PRD": 0}
    why = {"BRD": [], "FRD": [], "PRD": []}

    def add(t, p, r):
        sc[t] += p
        why[t].append(f"+{p} {r}")

    i, au, de, me = a["initiative"], a["audience"], a["detail"], a["methodology"]
    if i == INITIATIVES[0]:
        add("BRD", 2, "internal business change needs a business case"); add("FRD", 1, "process change needs system detail")
    elif i == INITIATIVES[1]:
        add("PRD", 3, "product-led work is described by a PRD")
    elif i == INITIATIVES[2]:
        add("FRD", 3, "integration needs precise interface and data behaviour"); add("BRD", 1, "business context still needed")
    elif i == INITIATIVES[3]:
        add("BRD", 2, "regulatory change must show business obligation"); add("FRD", 2, "audit needs exact behaviour")
    if au == AUDIENCES[0]:
        add("BRD", 2, "audience is sponsor / business")
    elif au == AUDIENCES[1]:
        add("FRD", 2, "audience is developers / testers")
    elif au == AUDIENCES[2]:
        add("PRD", 2, "audience is product / design")
    else:
        add("BRD", 1, "mixed audience"); add("FRD", 1, "mixed audience")
    if de == DETAILS[0]:
        add("BRD", 2, "high-level detail requested"); add("PRD", 1, "high-level detail requested")
    else:
        add("FRD", 2, "detailed system behaviour requested")
    if me == "Waterfall":
        add("BRD", 1, "waterfall favours BRD then FRD"); add("FRD", 1, "waterfall favours BRD then FRD")
    elif me == "Agile":
        add("PRD", 2, "agile teams prefer a living PRD plus stories")
    else:
        add("BRD", 1, "hybrid: business sign-off up front"); add("PRD", 1, "hybrid: iterative delivery")
    if a.get("outsourced"):
        add("FRD", 2, "vendor / outsourced build needs an unambiguous specification")
        add("BRD", 1, "contract needs business baseline")
    if a.get("regulated"):
        add("BRD", 1, "audit trail of business need"); add("FRD", 1, "audit trail of exact behaviour")
    prio = ["BRD", "FRD", "PRD"]
    top = max(prio, key=lambda t: (sc[t], -prio.index(t)))
    rec = top
    other = {"BRD": "FRD", "FRD": "BRD"}.get(top)
    if other and sc[top] - sc[other] <= 2 and (me != "Agile" or a.get("regulated") or a.get("outsourced")):
        rec = "BRD + FRD"
    return {"scores": sc, "why": why, "recommendation": rec}


def outline_for(choice):
    parts = [p.strip() for p in choice.split("+")]
    out = []
    for p in parts:
        out.append(f"# {p}\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(DOC_OUTLINES[p], 1)))
    return "\n\n".join(out)


# ---------------------------------------------------------------- misc
def est_tokens(text):
    return len(text) // 4
