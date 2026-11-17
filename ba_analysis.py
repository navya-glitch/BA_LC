"""
ba_analysis.py - pure-Python helpers behind the new BA tools (no Streamlit, no network):
  * requirement extraction: prompts, validation, duplicate / ambiguity checks, traceability
  * diagrams: Graphviz DOT, fishbone SVG, decision table, Mermaid text, plus spec validation
  * JSON / data: flatten, compare two documents, compare source vs target structure, mapping spec
The LLM only ever returns a structured spec (JSON). Everything here validates that spec before it is drawn,
so a bad model answer produces a clear message instead of a broken diagram.
"""
import csv
import difflib
import html
import io
import json
import re
import textwrap

from ba_exporters import rows_to_md

# ================================================================== shared
def norm_text(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse_json_loose(text):
    """Parse JSON, tolerating a ```json fence or text around it. Raises ValueError with a readable message."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Nothing to parse - the box is empty.")
    m = re.match(r"^```[A-Za-z]*\s*\n(.*?)\n?```\s*$", t, flags=re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError as first:
        a, b = min([i for i in (t.find("{"), t.find("[")) if i >= 0] or [-1]), max(t.rfind("}"), t.rfind("]"))
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Not valid JSON: {first.msg} (line {first.lineno}, column {first.colno}).")


# ================================================================== requirements
REQ_TYPES = ["Business requirement", "Functional", "Non-functional", "Business rule", "Constraint", "Assumption",
             "Dependency", "Open question"]
PRIORITIES = ["", "Must", "Should", "Could", "Won't"]
STATUSES = ["Proposed", "Approved", "Rejected", "Deferred"]
VAGUE = ["fast", "quick", "quickly", "user-friendly", "user friendly", "easy", "easily", "flexible", "appropriate",
         "as needed", "if needed", "etc", "and so on", "approximately", "adequate", "reasonable", "robust",
         "seamless", "efficient", "efficiently", "minimal", "optimal", "best", "modern", "intuitive", "tbd", "soon",
         "as required", "where possible", "sufficient"]


def split_windows(text, size=6000):
    """Long input -> overlapping windows so nothing is silently cut off before extraction."""
    from ba_kb import chunk_text
    return [c["text"] for c in chunk_text(text, size=size, overlap=300)] or [""]


def prompt_extract(source_name):
    return (f"Extract every requirement-like statement from the SOURCE TEXT below (source: {source_name}). "
            "Classify each as one of: " + ", ".join(REQ_TYPES) + ".\n"
            "Rules:\n"
            "- Use ONLY what the text says. Do not add requirements that are not in the text.\n"
            "- 'statement' = one clear, atomic sentence in the form 'The system shall ...' or 'The business rule is ...'; "
            "split compound statements.\n"
            "- 'source_quote' = an exact verbatim excerpt (max 30 words) copied from the text that supports the statement.\n"
            "- 'priority_hint' = must | should | could | wont | '' ONLY if the text signals it (e.g. 'must', 'nice to have').\n"
            "- 'ambiguity' = what is unclear or untestable (empty string if clear). Never invent numbers.\n"
            "- Questions, doubts and unresolved items go in type 'Open question'.\n"
            'Return JSON: {"items":[{"type":"","statement":"","priority_hint":"","source_quote":"","stakeholder":"",'
            '"ambiguity":""}]}')


def _canon_type(t):
    t = norm_text(t)
    for r in REQ_TYPES:
        if norm_text(r) == t:
            return r
    for r in REQ_TYPES:
        if t and (t in norm_text(r) or norm_text(r) in t):
            return r
    if "non" in t and "func" in t or "nfr" in t or "performance" in t:
        return "Non-functional"
    if "rule" in t:
        return "Business rule"
    if "question" in t:
        return "Open question"
    return "Functional" if "func" in t else "Business requirement"


def _canon_priority(p):
    p = norm_text(p)
    return {"must": "Must", "should": "Should", "could": "Could", "wont": "Won't", "won t": "Won't"}.get(p, "")


def quality_flags(stmt, rtype):
    flags, low = [], " " + norm_text(stmt) + " "
    hits = [w for w in VAGUE if " " + norm_text(w) + " " in low]
    if hits:
        flags.append("Vague wording: " + ", ".join(sorted(set(hits))[:4]))
    if rtype == "Non-functional" and not re.search(r"\d", stmt):
        flags.append("Not measurable (no number or limit)")
    if low.count(" and ") >= 3:
        flags.append("Compound - consider splitting")
    if rtype in ("Functional", "Business requirement") and not re.search(r"\b(shall|must|will|should|can|able)\b", low):
        flags.append("No clear obligation (shall / must)")
    return flags


def next_req_number(existing):
    nums = [int(m.group(1)) for r in existing for m in [re.match(r"RQ-(\d+)$", str(r.get("id", "")))] if m]
    return (max(nums) + 1) if nums else 1


def normalize_items(items, source_text, source_name, existing):
    """Validate model output -> candidate rows. Nothing here is trusted until a person accepts it."""
    out, n = [], next_req_number(existing)
    pool = [(r["id"], norm_text(r["statement"])) for r in existing]
    src_norm = norm_text(source_text)
    for it in items or []:
        if not isinstance(it, dict):
            continue
        stmt = re.sub(r"\s+", " ", str(it.get("statement", ""))).strip()
        if len(stmt) < 8:
            continue
        rtype = _canon_type(it.get("type", ""))
        quote = re.sub(r"\s+", " ", str(it.get("source_quote", ""))).strip()
        qn = norm_text(quote)
        verified = bool(qn) and qn in src_norm
        flags = quality_flags(stmt, rtype)
        amb = str(it.get("ambiguity", "") or "").strip()
        if amb:
            flags.append("Unclear: " + amb[:120])
        if not verified:
            flags.append("Quote not found in source - check")
        dup, sn = "", norm_text(stmt)
        for rid, ps in pool:
            if ps and difflib.SequenceMatcher(None, sn, ps).ratio() >= 0.86:
                dup = rid
                break
        rid = f"RQ-{n:03d}"
        n += 1
        pool.append((rid, sn))
        out.append({"id": rid, "type": rtype, "statement": stmt, "priority": _canon_priority(it.get("priority_hint", "")),
                    "status": "Proposed", "source": source_name, "quote": quote[:300], "quote_verified": verified,
                    "stakeholder": str(it.get("stakeholder", "") or "")[:80], "flags": flags, "duplicate_of": dup,
                    "notes": ""})
    return out


def prompt_conflicts():
    return ("Review the REQUIREMENTS REGISTER and find problems. Use only the IDs given. Return JSON "
            '{"conflicts":[{"ids":["RQ-001","RQ-002"],"issue":""}],"duplicates":[{"ids":["",""],"issue":""}],'
            '"gaps":[{"area":"","issue":""}],"untestable":[{"id":"","issue":""}]}. '
            "'conflicts' = two statements that cannot both be true. 'gaps' = important areas with no requirement at all "
            "(e.g. security, audit, data retention, performance, error handling, reporting) judged against the project "
            "description only - do not invent project facts. Empty lists are fine.")


def normalize_conflicts(res, reqs):
    ids = {r["id"] for r in reqs}
    rows = []

    def add(kind, rec, idlist, text):
        good = [i for i in idlist if i in ids]
        if idlist and not good:
            return
        rows.append({"Kind": kind, "IDs": ", ".join(good), "Finding": str(text)[:400]})
    for c in (res or {}).get("conflicts", []) or []:
        add("Conflict", c, c.get("ids", []) if isinstance(c, dict) else [], c.get("issue", "") if isinstance(c, dict) else c)
    for c in (res or {}).get("duplicates", []) or []:
        add("Duplicate", c, c.get("ids", []) if isinstance(c, dict) else [], c.get("issue", "") if isinstance(c, dict) else c)
    for c in (res or {}).get("untestable", []) or []:
        if isinstance(c, dict):
            add("Untestable", c, [c.get("id", "")], c.get("issue", ""))
    for c in (res or {}).get("gaps", []) or []:
        if isinstance(c, dict):
            rows.append({"Kind": "Gap", "IDs": "", "Finding": f"{c.get('area', '')}: {c.get('issue', '')}"[:400]})
    return rows


def trace_matrix(reqs, stories, defects, findings):
    """Link register IDs to stories ('Req IDs' column), defects and validation findings ('req_id'). Pure text match."""
    def blob(r):
        return " ".join(str(v) for v in r.values())
    rows = []
    for q in reqs:
        pat = re.compile(r"\b" + re.escape(q["id"]) + r"\b", re.I)
        st = [s.get("Summary", "") or s.get("Jira key", "") for s in stories if pat.search(blob(s))]
        df = [d.get("id", "") for d in defects if pat.search(blob(d))]
        fn = [f.get("id", "") for f in findings if pat.search(blob(f))]
        rows.append({"ID": q["id"], "Type": q["type"], "Priority": q["priority"], "Status": q["status"],
                     "Requirement": q["statement"][:110], "Stories": len(st), "Story summaries": "; ".join(st)[:140],
                     "Defects": ", ".join(df), "Validation findings": ", ".join(fn),
                     "Coverage": "No story" if not st and q["type"] not in ("Assumption", "Open question") else "OK"})
    return rows


# ================================================================== diagram guide
DIAGRAM_GUIDE = [
    {"Diagram": "Fishbone (Ishikawa)", "Use it when": "You must find WHY a problem happens and want causes grouped by category",
     "BOP migration example": "Why premiums differ from legacy: rules, data, configuration, environment, people",
     "Typical phase": "Defect analysis / lessons learned"},
    {"Diagram": "5 Whys", "Use it when": "One specific problem with a single chain of causes",
     "BOP migration example": "Why did a renewal show +18% instead of the 12% cap?", "Typical phase": "UAT / hypercare"},
    {"Diagram": "Context diagram", "Use it when": "Agreeing scope and system boundaries with sponsors and architects",
     "BOP migration example": "Rating engine versus billing, documents, statistical reporting, underwriters",
     "Typical phase": "Initiation / scoping"},
    {"Diagram": "Process flow / swimlane (As-Is, To-Be)", "Use it when": "Showing how work flows across roles and systems, before and after a change",
     "BOP migration example": "Quote to bind in legacy versus in PolicyCenter", "Typical phase": "Analysis, enhancements"},
    {"Diagram": "Decision table", "Use it when": "Rules with many conditions and outcomes that must be tested exhaustively",
     "BOP migration example": "BOP eligibility, schedule rating approval levels, renewal cap", "Typical phase": "Requirements"},
    {"Diagram": "State diagram", "Use it when": "Something moves through statuses with allowed transitions",
     "BOP migration example": "Quote, policy and endorsement lifecycle", "Typical phase": "Requirements / design"},
    {"Diagram": "Sequence diagram", "Use it when": "Explaining system-to-system calls and their order",
     "BOP migration example": "Quote screen to rating service to billing", "Typical phase": "Design / integration"},
    {"Diagram": "Data mapping / data dictionary", "Use it when": "Moving or exchanging data between systems",
     "BOP migration example": "Legacy territory table to PolicyCenter territory model", "Typical phase": "Migration / integration"},
    {"Diagram": "Stakeholder map / RACI", "Use it when": "Deciding who to involve, inform and who decides",
     "BOP migration example": "Sponsor, Compliance, underwriters, actuaries", "Typical phase": "Initiation"},
    {"Diagram": "Gap analysis", "Use it when": "Comparing current capability with the target",
     "BOP migration example": "Legacy rating features versus PolicyCenter capabilities", "Typical phase": "Strategy / analysis"},
]


# ================================================================== graphviz helpers
def _q(s, width=26):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    lines = textwrap.wrap(s, width=width) or [""]
    return "\\n".join(l.replace("\\", "/").replace('"', "'") for l in lines)


def _id(s, used=None, prefix="n"):
    base = re.sub(r"[^A-Za-z0-9_]", "_", str(s or "")).strip("_") or prefix
    if base[0].isdigit():
        base = prefix + "_" + base
    if used is not None:
        cand, i = base, 2
        while cand in used:
            cand, i = f"{base}_{i}", i + 1
        used.add(cand)
        return cand
    return base


# ------------------------------------------------------------------ process flow (As-Is / To-Be)
STEP_TYPES = ["start", "task", "decision", "system", "document", "end"]
CHANGES = ["unchanged", "new", "modified", "removed"]


def prompt_process(kind, topic, context_note=""):
    base = ("Return JSON for a process flow: "
            '{"title":"","lanes":["role or system names"],"steps":[{"id":"s1","label":"short verb phrase","lane":"",'
            '"type":"start|task|decision|system|document|end","change":"unchanged|new|modified|removed","source":"[K#-#] or [USER] or INFERRED",'
            '"next":[{"to":"s2","label":"Yes / No / blank"}]}]}. '
            "Rules: 6 to 25 steps; exactly one start; at least one end; decisions have 2+ outgoing edges with labels; "
            "'lanes' are the roles or systems that perform the step; every step cites where it came from - use [K#-#] tags from "
            "the context, [USER] for the user's own description, or INFERRED when you are filling a gap. Do not invent systems, "
            "roles or numbers that are not in the context.")
    if kind == "asis":
        return (f"Draw the CURRENT STATE (As-Is) process for: {topic}.\n{base}\nAll steps must have change='unchanged'.")
    return (f"Draw the FUTURE STATE (To-Be) process after this enhancement or change: {topic}.\n"
            "Start from the AS-IS spec given in the context. Keep unchanged steps with the same ids. Mark steps you add "
            "change='new', steps whose content changes change='modified', and steps that disappear change='removed' "
            f"(keep removed steps in the list so the difference is visible).\n{base}\n{context_note}")


def clean_process_spec(spec):
    """-> (spec, notes). Repairs what it can, reports what it dropped."""
    notes = []
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise ValueError("The diagram spec has no 'steps'. Try again or edit the JSON.")
    used, idmap, steps = set(), {}, []
    for i, s in enumerate(spec["steps"][:40], 1):
        if not isinstance(s, dict):
            continue
        orig = str(s.get("id", f"s{i}"))
        nid = _id(orig, used, "s")
        idmap[orig] = nid
        t = str(s.get("type", "task")).lower()
        ch = str(s.get("change", "unchanged")).lower()
        steps.append({"id": nid, "label": str(s.get("label", nid))[:120], "lane": str(s.get("lane", "") or "Process")[:60],
                      "type": t if t in STEP_TYPES else "task", "change": ch if ch in CHANGES else "unchanged",
                      "source": str(s.get("source", "") or "INFERRED")[:60], "next": s.get("next", [])})
    ids = {s["id"] for s in steps}
    for s in steps:
        nx = []
        for e in s["next"] if isinstance(s["next"], list) else [s["next"]]:
            to, lab = (e.get("to"), e.get("label", "")) if isinstance(e, dict) else (e, "")
            to = idmap.get(str(to), _id(to)) if to is not None else None
            if to in ids:
                nx.append({"to": to, "label": str(lab or "")[:40]})
            elif to:
                notes.append(f"Dropped a link from '{s['label']}' to unknown step '{to}'.")
        s["next"] = nx
    lanes = [str(l) for l in spec.get("lanes", []) if str(l).strip()]
    for s in steps:
        if s["lane"] not in lanes:
            lanes.append(s["lane"])
    if sum(1 for s in steps if s["type"] == "start") != 1:
        notes.append("Expected exactly one 'start' step - check the diagram.")
    if not any(s["type"] == "end" for s in steps):
        notes.append("No 'end' step - check the diagram.")
    return {"title": str(spec.get("title", "Process"))[:100], "lanes": lanes, "steps": steps}, notes


_STYLE = {"start": 'shape=oval fillcolor="#D5F5E3"', "end": 'shape=oval fillcolor="#F6DDCC"',
          "decision": 'shape=diamond fillcolor="#FCF3CF"', "system": 'shape=component fillcolor="#D6EAF8"',
          "document": 'shape=note fillcolor="#F2F3F4"', "task": 'shape=box style="rounded,filled" fillcolor="#FFFFFF"'}
_CHANGE = {"new": 'fillcolor="#ABEBC6" penwidth=2 color="#1E8449"', "modified": 'fillcolor="#FAD7A0" penwidth=2 color="#B9770E"',
           "removed": 'style="dashed,filled" fillcolor="#F5B7B1" fontcolor="#7B241C" color="#922B21"'}


def process_dot(spec, show_changes=False):
    out = ['digraph G {', 'rankdir=LR; compound=true; labelloc=t; fontname="Helvetica"; fontsize=14;',
           f'label="{_q(spec["title"], 60)}";',
           'node [fontname="Helvetica" fontsize=10 style="filled" fillcolor="#FFFFFF" margin="0.12,0.06"];',
           'edge [fontname="Helvetica" fontsize=9 color="#566573"];']
    for li, lane in enumerate(spec["lanes"]):
        members = [s for s in spec["steps"] if s["lane"] == lane]
        if not members:
            continue
        out.append(f'subgraph cluster_{li} {{ label="{_q(lane, 30)}"; style="filled,rounded"; color="#CBD5E1"; '
                   'fillcolor="#F4F6F9"; fontname="Helvetica"; fontsize=11;')
        for s in members:
            style = _STYLE[s["type"]]
            extra = _CHANGE.get(s["change"], "") if show_changes else ""
            out.append(f'{s["id"]} [label="{_q(s["label"])}" {style} {extra}];')
        out.append("}")
    for s in spec["steps"]:
        for e in s["next"]:
            lab = f' [label="{_q(e["label"], 20)}"]' if e["label"] else ""
            dash = ' [style=dashed]' if show_changes and s["change"] == "removed" and not lab else ""
            out.append(f'{s["id"]} -> {e["to"]}{lab or dash};')
    if show_changes:
        out.append('subgraph cluster_legend { label="Legend"; style="rounded"; color="#CBD5E1"; fontsize=10; '
                   'lg1 [label="New" shape=box ' + _CHANGE["new"] + ' style=filled]; '
                   'lg2 [label="Modified" shape=box ' + _CHANGE["modified"] + ' style=filled]; '
                   'lg3 [label="Removed" shape=box ' + _CHANGE["removed"] + ']; lg1 -> lg2 -> lg3 [style=invis]; }')
    out.append("}")
    return "\n".join(out)


def process_changes(spec):
    return [{"Change": s["change"].title(), "Step": s["label"], "Lane": s["lane"], "Source": s["source"]}
            for s in spec["steps"] if s["change"] != "unchanged"]


# ------------------------------------------------------------------ fishbone
FISH_CATEGORIES = ["People", "Process", "Technology / Systems", "Data", "Rules & Regulation", "Environment"]


def prompt_fishbone(problem, has_defects):
    return (f"Build a fishbone (Ishikawa) cause analysis for this PROBLEM: {problem}\n"
            "Use these categories (skip a category with no evidence): " + ", ".join(FISH_CATEGORIES) + ".\n"
            "Rules: 2 to 5 causes per category, each max 12 words; every cause must be supported by the context - "
            "cite the evidence in 'evidence' using [K#-#], [D-id] (defect id) or [USER]; if a cause is only a hypothesis "
            "write evidence 'HYPOTHESIS - verify'. Do not invent facts.\n"
            + ("Defects and their root causes are in the context - use them.\n" if has_defects else "") +
            'Return JSON: {"problem":"","categories":[{"name":"","causes":[{"text":"","evidence":""}]}]}')


def clean_fishbone(spec, fallback_problem=""):
    if not isinstance(spec, dict) or not isinstance(spec.get("categories"), list):
        raise ValueError("The fishbone spec has no 'categories'.")
    cats = []
    for c in spec["categories"][:8]:
        if not isinstance(c, dict):
            continue
        causes = []
        for x in (c.get("causes") or [])[:6]:
            if isinstance(x, dict) and str(x.get("text", "")).strip():
                causes.append({"text": str(x["text"]).strip()[:140], "evidence": str(x.get("evidence", "") or "HYPOTHESIS - verify")[:80]})
            elif isinstance(x, str) and x.strip():
                causes.append({"text": x.strip()[:140], "evidence": "HYPOTHESIS - verify"})
        if causes:
            cats.append({"name": str(c.get("name", "Other"))[:30], "causes": causes})
    if not cats:
        raise ValueError("The fishbone spec has no causes.")
    return {"problem": str(spec.get("problem") or fallback_problem)[:140], "categories": cats}


def fishbone_svg(spec, max_chars=30):
    """Draws a real fishbone: spine, angled ribs alternating top / bottom, causes along each rib."""
    cats, esc = spec["categories"], html.escape
    cols = (len(cats) + 1) // 2
    colw, W, H, spine = 300, 0, 560, 280
    W = 100 + colw * cols + 230
    head_x = W - 220
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         'font-family="Helvetica, Arial, sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>',
         f'<line x1="30" y1="{spine}" x2="{head_x}" y2="{spine}" stroke="#34495E" stroke-width="4"/>',
         f'<polygon points="{head_x},{spine - 8} {head_x + 14},{spine} {head_x},{spine + 8}" fill="#34495E"/>']
    prob = textwrap.wrap(spec["problem"], 24)[:5] or [""]
    p.append(f'<rect x="{head_x + 14}" y="{spine - 55}" width="200" height="110" rx="10" fill="#FDEDEC" stroke="#C0392B" stroke-width="2"/>')
    for i, ln in enumerate(prob):
        p.append(f'<text x="{head_x + 114}" y="{spine - 28 + i * 17}" text-anchor="middle" font-size="12" font-weight="bold" fill="#922B21">{esc(ln)}</text>')
    for i, c in enumerate(cats):
        top, col = i % 2 == 0, i // 2
        x_sp = 100 + colw * col + 215                      # where the rib meets the spine
        x_out, y_out = x_sp - 95, (62 if top else H - 62)
        p.append(f'<line x1="{x_out}" y1="{y_out}" x2="{x_sp}" y2="{spine}" stroke="#2980B9" stroke-width="3"/>')
        by = y_out - (32 if top else -6)
        p.append(f'<rect x="{x_out - 80}" y="{by}" width="160" height="26" rx="6" fill="#D6EAF8" stroke="#2980B9"/>')
        p.append(f'<text x="{x_out}" y="{by + 18}" text-anchor="middle" font-size="12" font-weight="bold" fill="#1B4F72">{esc(c["name"])}</text>')
        m = len(c["causes"])
        for k, cause in enumerate(c["causes"]):
            t = (k + 1) / (m + 1)
            x = x_out + (x_sp - x_out) * t
            y = y_out + (spine - y_out) * t
            txt = cause["text"] if len(cause["text"]) <= max_chars else cause["text"][:max_chars - 1] + "\u2026"
            p.append(f'<line x1="{x - 22:.0f}" y1="{y:.0f}" x2="{x:.0f}" y2="{y:.0f}" stroke="#7F8C8D" stroke-width="1.5"/>')
            p.append(f'<text x="{x - 26:.0f}" y="{y - 3:.0f}" text-anchor="end" font-size="10.5" fill="#2C3E50">{esc(txt)}</text>')
    p.append("</svg>")
    return "".join(p)


def fishbone_rows(spec):
    return [{"Category": c["name"], "Cause": x["text"], "Evidence": x["evidence"]} for c in spec["categories"] for x in c["causes"]]


# ------------------------------------------------------------------ 5 whys
def prompt_whys(problem):
    return (f"Do a 5-Whys analysis for this PROBLEM: {problem}\nEach 'why' must follow from the previous answer and be supported by "
            "the context; cite evidence ([K#-#], [D-id], [USER]) or write 'HYPOTHESIS - verify'. Stop early if the root cause is clear. "
            'Return JSON: {"problem":"","whys":[{"question":"Why ...?","answer":"","evidence":""}],"root_cause":"","suggested_action":""}')


def clean_whys(spec, fallback=""):
    if not isinstance(spec, dict) or not isinstance(spec.get("whys"), list) or not spec["whys"]:
        raise ValueError("The 5 Whys spec has no 'whys'.")
    ws = [{"question": str(w.get("question", ""))[:160], "answer": str(w.get("answer", ""))[:200],
           "evidence": str(w.get("evidence", "") or "HYPOTHESIS - verify")[:80]} for w in spec["whys"][:7] if isinstance(w, dict)]
    return {"problem": str(spec.get("problem") or fallback)[:160], "whys": ws, "root_cause": str(spec.get("root_cause", ""))[:200],
            "suggested_action": str(spec.get("suggested_action", ""))[:200]}


def whys_dot(spec):
    o = ['digraph G { rankdir=TB; node [shape=box style="rounded,filled" fillcolor="#FFFFFF" fontname="Helvetica" fontsize=10]; '
         'edge [color="#566573"];', f'p [label="PROBLEM\\n{_q(spec["problem"], 40)}" fillcolor="#FDEDEC" color="#C0392B"];']
    prev = "p"
    for i, w in enumerate(spec["whys"], 1):
        o.append(f'w{i} [label="Why {i}: {_q(w["question"].replace("Why", "").strip(" ?"), 40)}?\\n{_q(w["answer"], 40)}"];')
        o.append(f"{prev} -> w{i};")
        prev = f"w{i}"
    if spec["root_cause"]:
        o.append(f'r [label="ROOT CAUSE\\n{_q(spec["root_cause"], 40)}" fillcolor="#D5F5E3" color="#1E8449" penwidth=2];')
        o.append(f"{prev} -> r;")
        prev = "r"
    if spec["suggested_action"]:
        o.append(f'a [label="ACTION\\n{_q(spec["suggested_action"], 40)}" fillcolor="#D6EAF8"]; {prev} -> a;')
    o.append("}")
    return "\n".join(o)


# ------------------------------------------------------------------ context diagram
def prompt_context(system):
    return (f"Draw a CONTEXT diagram for: {system}. List only external actors and external systems that exchange data with it, "
            "and what data flows. Use only the context; cite with 'source' ([K#-#], [USER] or INFERRED). "
            'Return JSON: {"system":"","flows":[{"entity":"","kind":"actor|system","direction":"in|out|both","data":"","source":""}]}')


def clean_context(spec, fallback=""):
    if not isinstance(spec, dict) or not isinstance(spec.get("flows"), list) or not spec["flows"]:
        raise ValueError("The context spec has no 'flows'.")
    fl = []
    for f in spec["flows"][:20]:
        if isinstance(f, dict) and str(f.get("entity", "")).strip():
            d = str(f.get("direction", "both")).lower()
            fl.append({"entity": str(f["entity"])[:50], "kind": "actor" if str(f.get("kind", "")).lower() == "actor" else "system",
                       "direction": d if d in ("in", "out", "both") else "both", "data": str(f.get("data", ""))[:60],
                       "source": str(f.get("source", "") or "INFERRED")[:40]})
    if not fl:
        raise ValueError("The context spec has no usable flows.")
    return {"system": str(spec.get("system") or fallback)[:80], "flows": fl}


def context_dot(spec):
    o = ['digraph G { rankdir=LR; node [fontname="Helvetica" fontsize=10 style=filled]; edge [fontname="Helvetica" fontsize=9 color="#566573"];',
         f'sys [label="{_q(spec["system"], 20)}" shape=ellipse fillcolor="#D6EAF8" penwidth=2 color="#2471A3" fontsize=12];']
    used = set()
    for f in spec["flows"]:
        nid = _id(f["entity"], used, "e")
        shape = 'shape=box style="rounded,filled" fillcolor="#FDF2E9"' if f["kind"] == "actor" else 'shape=box fillcolor="#EAF2F8"'
        o.append(f'{nid} [label="{_q(f["entity"], 22)}" {shape}];')
        lab = f'label="{_q(f["data"], 22)}"' if f["data"] else ""
        if f["direction"] == "in":
            o.append(f"{nid} -> sys [{lab}];")
        elif f["direction"] == "out":
            o.append(f"sys -> {nid} [{lab}];")
        else:
            o.append(f"sys -> {nid} [dir=both {lab}];")
    o.append("}")
    return "\n".join(o)


# ------------------------------------------------------------------ state diagram
def prompt_state(entity):
    return (f"Draw a STATE diagram of the lifecycle of: {entity}. Use only statuses and transitions found in the context; "
            'cite with "source". Return JSON: {"entity":"","states":[{"name":"","kind":"initial|normal|final"}],'
            '"transitions":[{"from":"","to":"","event":"","source":""}]}')


def clean_state(spec, fallback=""):
    if not isinstance(spec, dict) or not isinstance(spec.get("transitions"), list) or not spec["transitions"]:
        raise ValueError("The state spec has no 'transitions'.")
    names = {}
    for s in spec.get("states", []) or []:
        if isinstance(s, dict) and str(s.get("name", "")).strip():
            names[str(s["name"]).strip()] = str(s.get("kind", "normal")).lower()
    tr = []
    for t in spec["transitions"][:40]:
        if isinstance(t, dict) and str(t.get("from", "")).strip() and str(t.get("to", "")).strip():
            a, b = str(t["from"]).strip(), str(t["to"]).strip()
            names.setdefault(a, "normal")
            names.setdefault(b, "normal")
            tr.append({"from": a, "to": b, "event": str(t.get("event", ""))[:50], "source": str(t.get("source", "") or "INFERRED")[:40]})
    if not tr:
        raise ValueError("The state spec has no usable transitions.")
    return {"entity": str(spec.get("entity") or fallback)[:80],
            "states": [{"name": n, "kind": k if k in ("initial", "normal", "final") else "normal"} for n, k in names.items()],
            "transitions": tr}


def state_dot(spec):
    o = ['digraph G { rankdir=LR; node [fontname="Helvetica" fontsize=10 shape=box style="rounded,filled" fillcolor="#FFFFFF"]; '
         'edge [fontname="Helvetica" fontsize=9 color="#566573"];', f'label="{_q(spec["entity"], 60)}"; labelloc=t;']
    used, ids = set(), {}
    for s in spec["states"]:
        ids[s["name"]] = _id(s["name"], used, "st")
        extra = {'initial': 'fillcolor="#D5F5E3" penwidth=2', "final": 'fillcolor="#F6DDCC" peripheries=2'}.get(s["kind"], "")
        o.append(f'{ids[s["name"]]} [label="{_q(s["name"], 22)}" {extra}];')
    for t in spec["transitions"]:
        lab = f' [label="{_q(t["event"], 22)}"]' if t["event"] else ""
        o.append(f'{ids[t["from"]]} -> {ids[t["to"]]}{lab};')
    o.append("}")
    return "\n".join(o)


# ------------------------------------------------------------------ decision table
def prompt_decision(topic):
    return (f"Build a DECISION TABLE for: {topic}. Use only rules found in the context; if a combination is not covered by the "
            "context, add the rule with then='NOT DEFINED - business to decide'. Cite each rule's origin in 'source'. "
            'Return JSON: {"title":"","conditions":["cond A","cond B"],"rules":[{"name":"R1","when":{"cond A":"value","cond B":"value"},'
            '"then":"action / outcome","source":""}]}')


def clean_decision(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("rules"), list) or not spec["rules"]:
        raise ValueError("The decision table spec has no 'rules'.")
    conds = [str(c) for c in spec.get("conditions", []) if str(c).strip()]
    for r in spec["rules"]:
        if isinstance(r, dict) and isinstance(r.get("when"), dict):
            for c in r["when"]:
                if c not in conds:
                    conds.append(str(c))
    rules = []
    for i, r in enumerate(spec["rules"][:60], 1):
        if isinstance(r, dict):
            then = r.get("then", "")
            rules.append({"name": str(r.get("name", f"R{i}")), "when": {str(k): str(v) for k, v in (r.get("when") or {}).items()},
                          "then": "; ".join(map(str, then)) if isinstance(then, list) else str(then),
                          "source": str(r.get("source", "") or "INFERRED")[:40]})
    return {"title": str(spec.get("title", "Decision table"))[:100], "conditions": conds, "rules": rules}


def decision_rows(spec):
    return [{"Rule": r["name"], **{c: r["when"].get(c, "-") for c in spec["conditions"]}, "Outcome": r["then"], "Source": r["source"]}
            for r in spec["rules"]]


def decision_md(spec):
    rows = decision_rows(spec)
    hdr = list(rows[0].keys())
    return f"### {spec['title']}\n\n" + rows_to_md(hdr, [[r[h] for h in hdr] for r in rows])


# ------------------------------------------------------------------ sequence (Mermaid text)
def prompt_sequence(topic):
    return (f"Describe a SEQUENCE of calls for: {topic}. Participants are systems or roles named in the context. "
            'Return JSON: {"title":"","participants":[""],"messages":[{"from":"","to":"","text":"","kind":"sync|reply|async","source":""}]}')


def clean_sequence(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("messages"), list) or not spec["messages"]:
        raise ValueError("The sequence spec has no 'messages'.")
    parts = [str(p).strip() for p in spec.get("participants", []) if str(p).strip()]
    msgs = []
    for m in spec["messages"][:40]:
        if isinstance(m, dict) and str(m.get("from", "")).strip() and str(m.get("to", "")).strip():
            for k in ("from", "to"):
                if str(m[k]).strip() not in parts:
                    parts.append(str(m[k]).strip())
            kind = str(m.get("kind", "sync")).lower()
            msgs.append({"from": str(m["from"]).strip(), "to": str(m["to"]).strip(), "text": str(m.get("text", ""))[:80],
                         "kind": kind if kind in ("sync", "reply", "async") else "sync", "source": str(m.get("source", "") or "INFERRED")[:40]})
    if not msgs:
        raise ValueError("The sequence spec has no usable messages.")
    return {"title": str(spec.get("title", "Sequence"))[:100], "participants": parts, "messages": msgs}


def sequence_mermaid(spec):
    ids = {p: f"P{i}" for i, p in enumerate(spec["participants"], 1)}
    arrow = {"sync": "->>", "reply": "-->>", "async": "-)"}
    o = ["sequenceDiagram", f"    title {spec['title']}"]
    o += [f"    participant {ids[p]} as {p.replace(':', ' ')}" for p in spec["participants"]]
    o += [f"    {ids[m['from']]}{arrow[m['kind']]}{ids[m['to']]}: {m['text'].replace(':', ' ').replace(';', ',')}" for m in spec["messages"]]
    return "\n".join(o)


# ================================================================== JSON / data analysis
def _jtype(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    return "array" if isinstance(v, list) else "object"


def flatten(obj, prefix="", out=None, depth=0):
    """JSON sample -> {path: {'type', 'sample'}}. Arrays use [] and are merged across their first items."""
    out = {} if out is None else out
    if depth > 12:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(obj, list):
        p = prefix + "[]"
        if not obj:
            out.setdefault(p, {"type": "array(empty)", "sample": ""})
        for item in obj[:5]:
            if isinstance(item, (dict, list)):
                flatten(item, p, out, depth + 1)
            else:
                out.setdefault(p, {"type": _jtype(item), "sample": str(item)[:40]})
    else:
        cur = out.get(prefix)
        t = _jtype(obj)
        if cur and cur["type"] != t and {cur["type"], t} <= {"integer", "number"}:
            t = "number"
        elif cur and cur["type"] not in (t, "null"):
            t = cur["type"] if t == "null" else f"{cur['type']}|{t}"
        out[prefix] = {"type": t, "sample": "" if obj is None else str(obj)[:40]}
    return out


def _schema_fields(node, defs, prefix="", out=None, depth=0):
    out = {} if out is None else out
    if depth > 12 or not isinstance(node, dict):
        return out
    ref = node.get("$ref")
    if ref:
        target = defs.get(str(ref).split("/")[-1])
        if target is not None:
            return _schema_fields(target, defs, prefix, out, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            for sub in node[key]:
                _schema_fields(sub, defs, prefix, out, depth + 1)
    t = node.get("type")
    if isinstance(node.get("properties"), dict):
        for k, v in node["properties"].items():
            _schema_fields(v, defs, f"{prefix}.{k}" if prefix else k, out, depth + 1)
    elif t == "array":
        items = node.get("items", {})
        if isinstance(items, dict) and (items.get("properties") or items.get("$ref") or items.get("type") == "array"):
            _schema_fields(items, defs, prefix + "[]", out, depth + 1)
        else:
            out[prefix + "[]"] = {"type": f"array<{(items or {}).get('type', 'any')}>", "sample": ""}
    elif prefix:
        tt = "|".join(t) if isinstance(t, list) else (t or ("enum" if node.get("enum") else "any"))
        fmt = f" ({node['format']})" if node.get("format") else ""
        out[prefix] = {"type": f"{tt}{fmt}", "sample": ""}
    return out


def fields_from_text(text):
    """Accepts a JSON sample, a JSON Schema, or a CSV / delimited header row. -> (fields, kind)."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Empty input.")
    if t[0] in "{[":
        obj = parse_json_loose(t)
        if isinstance(obj, dict) and (obj.get("$schema") or ("properties" in obj and obj.get("type") in ("object", None))):
            defs = {**(obj.get("definitions") or {}), **(obj.get("$defs") or {})}
            f = _schema_fields(obj, defs)
            if f:
                return f, "JSON Schema"
        return flatten(obj), "JSON sample"
    first = next((l for l in t.splitlines() if l.strip()), "")
    delim = max([",", "\t", ";", "|"], key=first.count)
    cols = [c.strip().strip('"') for c in next(csv.reader([first], delimiter=delim)) if c.strip()]
    if len(cols) < 2:
        raise ValueError("Could not read this as JSON, a JSON Schema or a delimited header row.")
    return {c: {"type": "string", "sample": ""} for c in cols}, "Header row"


def _leaf(path):
    return re.sub(r"[^a-z0-9]", "", path.split(".")[-1].replace("[]", "").lower())


def _compat(a, b):
    a, b = a.split(" ")[0], b.split(" ")[0]
    if a == b or "any" in (a, b) or "null" in (a, b):
        return True
    num = {"integer", "number"}
    return {a, b} <= num or a.startswith("array") and b.startswith("array") or "|" in a or "|" in b


ABBR = {"no": "number", "num": "number", "nbr": "number", "dt": "date", "cd": "code", "amt": "amount", "pct": "percent",
        "prem": "premium", "eff": "effective", "exp": "expiration", "terr": "territory", "constr": "construction",
        "prot": "protection", "bldg": "building", "occ": "occurrence", "agg": "aggregate", "flg": "flag", "loc": "location",
        "ind": "indicator", "st": "state", "addr": "address", "desc": "description", "tot": "total", "mod": "modifier",
        "sched": "schedule", "cov": "coverage", "lmt": "limit", "ded": "deductible", "qty": "quantity", "ph": "phone"}


def _tokens(name):
    name = re.sub(r"\[\]", " ", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    out = []
    for t in re.split(r"[^A-Za-z0-9]+", name):
        t = t.lower()
        for suf in ("ing", "es", "s", "e"):
            if len(t) > 4 and t.endswith(suf):
                t = t[: -len(suf)]
                break
        if t:
            out.append(t)
    return out


def _tok_eq(a, b):
    if a == b or ABBR.get(a, "") == b or ABBR.get(b, "") == a:
        return True
    lo, hi = (a, b) if len(a) <= len(b) else (b, a)
    if len(lo) >= 3 and hi.startswith(lo):
        return True
    if len(lo) >= 3 and lo[0] == hi[0]:                 # sprnk ~ sprinkler (letters in order)
        it = iter(hi)
        return all(ch in it for ch in lo)
    return False


def _tok_score(sp, tp):
    """Dice-style overlap of the words in two field names (leaf name, plus a small bonus for the parent name)."""
    ts, tt = _tokens(sp.split(".")[-1]), _tokens(tp.split(".")[-1])
    if not ts or not tt:
        return 0.0
    hit = sum(1 for x in ts if any(_tok_eq(x, y) for y in tt))
    score = 2 * hit / (len(ts) + len(tt))
    ps, pt = sp.split(".")[:-1], tp.split(".")[:-1]
    if score and ps and pt and any(_tok_eq(x, y) for x in _tokens(ps[-1]) for y in _tokens(pt[-1])):
        score += 0.1
    return score


MAP_COLS = ["Status", "Source path", "Source type", "Target path", "Target type", "Match", "Notes"]


def compare_fields(src, tgt):
    """Deterministic structure comparison. No AI. -> rows with MAP_COLS."""
    rows, left_s, left_t = [], dict(src), dict(tgt)

    def pair(s, t, how, note=""):
        ok = _compat(left_s[s]["type"], left_t[t]["type"]) if s in left_s and t in left_t else True
        rows.append({"Status": "Matched" if ok else "Type mismatch", "Source path": s, "Source type": src[s]["type"],
                     "Target path": t, "Target type": tgt[t]["type"], "Match": how,
                     "Notes": note if ok else f"Type differs: {src[s]['type']} -> {tgt[t]['type']}. {note}".strip()})
        left_s.pop(s, None)
        left_t.pop(t, None)
    low_t = {p.lower(): p for p in left_t}
    for s in list(left_s):
        if s.lower() in low_t and low_t[s.lower()] in left_t:
            pair(s, low_t[s.lower()], "Exact path")
    by_leaf = {}
    for t in left_t:
        by_leaf.setdefault(_leaf(t), []).append(t)
    for s in list(left_s):
        cands = [t for t in by_leaf.get(_leaf(s), []) if t in left_t]
        if len(cands) == 1:
            pair(s, cands[0], "Same field name", "Different location - confirm")
        elif len(cands) > 1:
            best = max(cands, key=lambda t: difflib.SequenceMatcher(None, s.lower(), t.lower()).ratio())
            pair(s, best, "Same field name", f"{len(cands)} candidates - confirm")
    # word-based pass: handles abbreviations and different naming styles (EFF_DT ~ effectiveDate, PREM_PROPERTY ~ propertyPremium)
    best = {}
    for s_ in left_s:
        sc = sorted(((_tok_score(s_, t_), t_) for t_ in left_t), reverse=True)
        if sc and sc[0][0] >= 0.6 and (len(sc) == 1 or sc[0][0] - sc[1][0] >= 0.1):
            best[s_] = sc[0]
    taken = {}
    for s_, (score, t_) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        if t_ not in taken:
            taken[t_] = s_
            pair(s_, t_, "Abbreviation match", "Names share the same words - review")
    for s in list(left_s):
        if not left_t:
            break
        ls = _leaf(s)

        def sim(t):
            lt = _leaf(t)
            r = difflib.SequenceMatcher(None, ls, lt).ratio()
            if len(ls) >= 4 and len(lt) >= 4 and (ls in lt or lt in ls):
                r = max(r, 0.85)          # e.g. territory vs territoryCode
            return r
        scored = sorted(((sim(t), t) for t in left_t), reverse=True)
        if scored and scored[0][0] >= 0.82 and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.05):
            pair(s, scored[0][1], "Similar name", "Review - names are similar, not identical")
    for s in left_s:
        rows.append({"Status": "Source only", "Source path": s, "Source type": src[s]["type"], "Target path": "", "Target type": "",
                     "Match": "", "Notes": "No target field found"})
    for t in left_t:
        rows.append({"Status": "Target only", "Source path": "", "Source type": "", "Target path": t, "Target type": tgt[t]["type"],
                     "Match": "", "Notes": "Not fed by any source field"})
    return rows


SPEC_COLS = ["Source path", "Source type", "Target path", "Target type", "Transformation / rule", "Default", "Validation",
             "Confidence", "Method", "Status", "Notes"]


def spec_rows_from_compare(rows):
    out = []
    conf = {"Exact path": "High", "Same field name": "Medium", "Abbreviation match": "Medium", "Similar name": "Low"}
    for r in rows:
        if r["Source path"] and r["Target path"]:
            out.append({"Source path": r["Source path"], "Source type": r["Source type"], "Target path": r["Target path"],
                        "Target type": r["Target type"], "Transformation / rule": "Direct copy" if r["Status"] == "Matched" else "Convert type - define",
                        "Default": "", "Validation": "", "Confidence": conf.get(r["Match"], "Low"), "Method": r["Match"],
                        "Status": "Proposed", "Notes": r["Notes"]})
        elif r["Source path"]:
            out.append({"Source path": r["Source path"], "Source type": r["Source type"], "Target path": "", "Target type": "",
                        "Transformation / rule": "", "Default": "", "Validation": "", "Confidence": "", "Method": "Unmapped",
                        "Status": "Needs decision", "Notes": r["Notes"]})
    for r in rows:
        if r["Status"] == "Target only":
            out.append({"Source path": "", "Source type": "", "Target path": r["Target path"], "Target type": r["Target type"],
                        "Transformation / rule": "", "Default": "", "Validation": "", "Confidence": "", "Method": "Unmapped",
                        "Status": "Needs decision", "Notes": "Target field has no source"})
    return out


def prompt_mapping(src_only, tgt_only):
    return ("Suggest field mappings between the UNMAPPED SOURCE fields and the UNMAPPED TARGET fields listed below, using the "
            "knowledge-base context for meaning. Rules: use ONLY field paths exactly as listed; never invent a field; "
            "if no sensible target exists put the source in 'no_match' with the reason; 'transformation' must be a concrete rule "
            "(format change, lookup, concatenation, default) or 'Direct copy'; confidence is high|medium|low and must be 'low' "
            "when you are guessing from the name alone; cite context in 'reason' ([K#-#]) when you used it.\n"
            "SOURCE FIELDS: " + json.dumps(src_only) + "\nTARGET FIELDS: " + json.dumps(tgt_only) + "\n"
            'Return JSON: {"mappings":[{"source":"","target":"","transformation":"","default":"","validation":"",'
            '"confidence":"","reason":""}],"no_match":[{"source":"","reason":""}]}')


def apply_ai_mappings(res, src, tgt):
    """-> (spec_rows, notes). Drops any path the model made up."""
    rows, notes, used_t = [], [], set()
    for m in (res or {}).get("mappings", []) or []:
        if not isinstance(m, dict):
            continue
        s, t = str(m.get("source", "")), str(m.get("target", ""))
        if s not in src or t not in tgt:
            notes.append(f"Ignored a suggestion with an unknown field ({s or '?'} -> {t or '?'}).")
            continue
        if t in used_t:
            notes.append(f"Target '{t}' was suggested twice; kept the first.")
            continue
        used_t.add(t)
        c = str(m.get("confidence", "low")).lower()
        rows.append({"Source path": s, "Source type": src[s]["type"], "Target path": t, "Target type": tgt[t]["type"],
                     "Transformation / rule": str(m.get("transformation", ""))[:200], "Default": str(m.get("default", ""))[:60],
                     "Validation": str(m.get("validation", ""))[:150], "Confidence": c.title() if c in ("high", "medium", "low") else "Low",
                     "Method": "AI suggestion", "Status": "Proposed", "Notes": str(m.get("reason", ""))[:200]})
    for n in (res or {}).get("no_match", []) or []:
        if isinstance(n, dict) and str(n.get("source", "")) in src:
            rows.append({"Source path": n["source"], "Source type": src[n["source"]]["type"], "Target path": "", "Target type": "",
                         "Transformation / rule": "", "Default": "", "Validation": "", "Confidence": "", "Method": "AI: no match",
                         "Status": "Needs decision", "Notes": str(n.get("reason", ""))[:200]})
    return rows, notes


def spec_to_md(rows):
    return rows_to_md(SPEC_COLS, [[str(r.get(c, "")) for c in SPEC_COLS] for r in rows])


# ------------------------------------------------------------------ compare two JSON documents (values)
_KEYS = ("id", "code", "key", "name", "policyNumber", "coverageCode")


def _short(v, n=120):
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= n else s[:n - 1] + "\u2026"


def diff_json(a, b, ignore=(), sort_primitive_arrays=False, limit=2000):
    """Value-level comparison of two JSON documents. -> [{'Path','Change','A','B'}]."""
    ig, rows = {str(i).strip().lower() for i in ignore if str(i).strip()}, []

    def add(p, ch, x, y):
        if len(rows) < limit:
            rows.append({"Path": p or "(root)", "Change": ch, "A": _short(x) if x is not None else "", "B": _short(y) if y is not None else ""})

    def keyfield(arr):
        if arr and all(isinstance(x, dict) for x in arr):
            for k in _KEYS:
                vals = [x.get(k) for x in arr]
                if all(v is not None for v in vals) and len(set(map(str, vals))) == len(vals):
                    return k
        return None

    def walk(x, y, p):
        if len(rows) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in x:
                if k.lower() in ig:
                    continue
                kp = f"{p}.{k}" if p else k
                if k not in y:
                    add(kp, "Removed", x[k], None)
                else:
                    walk(x[k], y[k], kp)
            for k in y:
                if k.lower() not in ig and k not in x:
                    add(f"{p}.{k}" if p else k, "Added", None, y[k])
        elif isinstance(x, list) and isinstance(y, list):
            if sort_primitive_arrays and all(not isinstance(i, (dict, list)) for i in x + y):
                x, y = sorted(x, key=str), sorted(y, key=str)
            ka, kb = keyfield(x), keyfield(y)
            if ka and ka == kb:
                mx, my = {str(i[ka]): i for i in x}, {str(i[kb]): i for i in y}
                for k, v in mx.items():
                    if k in my:
                        walk(v, my[k], f"{p}[{ka}={k}]")
                    else:
                        add(f"{p}[{ka}={k}]", "Removed", v, None)
                for k, v in my.items():
                    if k not in mx:
                        add(f"{p}[{ka}={k}]", "Added", None, v)
            else:
                if len(x) != len(y):
                    add(p + "[]", "Array length", len(x), len(y))
                for i in range(min(len(x), len(y))):
                    walk(x[i], y[i], f"{p}[{i}]")
        elif _jtype(x) != _jtype(y) and not ({_jtype(x), _jtype(y)} <= {"integer", "number"}):
            add(p, "Type changed", x, y)
        elif x != y:
            add(p, "Value changed", x, y)
    walk(a, b, "")
    return rows


def prompt_diff_summary(rows):
    return ("Summarise these differences between two JSON documents for a Business Analyst. Group related changes, say which look "
            "like intentional restructuring versus possible data problems, and list questions to ask the system owner. "
            "Use only the rows given; do not guess business meaning that is not in the context. Return concise markdown.\n"
            "DIFF ROWS (Path | Change | A | B):\n" + "\n".join(f"{r['Path']} | {r['Change']} | {r['A']} | {r['B']}" for r in rows[:150]))


# ================================================================== screen mock-up agent
MOCK_STYLES = {"Wireframe (grey, low fidelity)": "wireframe", "Clean UI (colour, higher fidelity)": "clean"}
MOCK_WIDTHS = {"Desktop (1280 px)": 1280, "Tablet (820 px)": 820, "Mobile (390 px)": 390}


def prompt_mockup(instruction, style, width, highlight, has_image, refine=False):
    look = ("Low-fidelity WIREFRAME: white and light-grey only, thin dark-grey borders, system sans-serif font, placeholder "
            "boxes for images and charts, no brand colours." if style == "wireframe" else
            "Clean modern UI at higher fidelity: a calm neutral palette with one accent colour, consistent spacing, "
            "rounded corners, readable tables and forms.")
    hl = ("Mark every element you ADD or CHANGE with a dashed orange outline (outline: 2px dashed #E67E22) and a small "
          "orange numbered badge (<span class=\"chg\">1</span>, 2, 3 ...). Number them in reading order. Do not mark "
          "unchanged elements." if highlight else "Do not mark changes visually.")
    src = ("The SCREENSHOT is the current screen. Rebuild its layout faithfully: same regions, labels, field names, "
           "table columns and navigation, then apply the requested changes. Do not invent features that were not "
           "asked for. " if has_image else
           "There is no screenshot, so design the screen from the request alone. ")
    if refine:
        src = ("The CURRENT MOCK-UP HTML is given in the context. Change only what the user asks for and keep everything "
               "else identical. ")
    return (f"Create a screen MOCK-UP as ONE self-contained HTML document.\nREQUEST: {instruction}\n"
            f"{src}\nLook: {look}\nCanvas width: about {width}px (use max-width and a responsive layout).\n{hl}\n"
            "Hard rules: inline <style> only; NO JavaScript; NO external files, fonts, images or links (use CSS shapes, "
            "inline SVG or grey boxes for pictures); use realistic but fictional sample data, never real personal data; "
            "keep labels in the same language as the request; if something is unclear, make a sensible choice and list it "
            "under ASSUMPTIONS instead of asking.\n"
            "Reply in EXACTLY this format and nothing else:\n<<<HTML>>>\n(the complete HTML document)\n<<<CHANGES>>>\n"
            "1. what was added or changed and where (one line each, numbers match the badges)\n<<<ASSUMPTIONS>>>\n"
            "- each assumption on its own line")


def parse_mockup_reply(text):
    t = (text or "").strip()
    html_part, changes, assumptions = "", "", ""
    m = re.search(r"<<<HTML>>>(.*?)(?:<<<CHANGES>>>(.*?))?(?:<<<ASSUMPTIONS>>>(.*))?\Z", t, flags=re.S)
    if m:
        html_part, changes, assumptions = (m.group(1) or ""), (m.group(2) or ""), (m.group(3) or "")
    else:
        h = re.search(r"<!DOCTYPE html.*?</html>|<html.*?</html>", t, flags=re.S | re.I)
        html_part = h.group(0) if h else t
    fence = re.search(r"```(?:html)?\s*\n(.*?)```", html_part, flags=re.S)
    if fence:
        html_part = fence.group(1)
    html_part = html_part.strip()
    if "<" not in html_part:
        raise ValueError("The model did not return an HTML mock-up. Try again or simplify the request.")

    def lines(block):
        out = []
        for ln in block.splitlines():
            ln = re.sub(r"^\s*(?:\d+[\.\)]|[-*])\s*", "", ln).strip()
            if ln:
                out.append(ln)
        return out
    return {"html": sanitize_html(html_part), "changes": lines(changes), "assumptions": lines(assumptions)}


_CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
        "style-src 'unsafe-inline'; font-src data:\">")


def sanitize_html(h):
    """Model HTML is shown in an iframe. Strip anything that could run code or load outside content."""
    h = re.sub(r"(?is)<(script|iframe|object|embed|link|base|applet)\b.*?(?:</\1\s*>|/?>)", "", h)
    h = re.sub(r"(?is)<meta\b[^>]*http-equiv[^>]*>", "", h)
    h = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)javascript\s*:", "", h)
    h = re.sub(r"(?i)(href|src|action|formaction|xlink:href)\s*=\s*(\"|')\s*(https?:)?//[^\"']*\2", r'\1=\2#\2', h)
    h = re.sub(r"(?i)url\(\s*(['\"]?)\s*(https?:)?//[^)]*\)", "none", h)
    h = re.sub(r"(?i)@import[^;]*;", "", h)
    if not re.search(r"(?i)<html", h):
        h = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>" + h + "</body></html>")
    if re.search(r"(?i)<head[^>]*>", h):
        h = re.sub(r"(?i)(<head[^>]*>)", r"\1" + _CSP.replace("\\", "\\\\") + "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">", h, count=1)
    else:
        h = re.sub(r"(?i)(<html[^>]*>)", r"\1<head>" + _CSP.replace("\\", "\\\\") + "</head>", h, count=1)
    return h
