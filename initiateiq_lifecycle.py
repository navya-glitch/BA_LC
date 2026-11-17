#!/usr/bin/env python3
"""
InitiateIQ - Full BA Lifecycle Orchestrator (Streamlit UI + OpenAI API)

    pip install -r requirements.txt
    set OPENAI_API_KEY=sk-...   (Windows)   or enter the key in the sidebar
    streamlit run initiateiq_lifecycle.py

Nothing project-specific is hardcoded: every input comes from the UI.
The ROUTER (ba_engine.route) decides the next step from gates, dependencies, loop-back flags
raised by validation / UAT / change control, and staleness when upstream artifacts change.
User stories are created in Jira only after requirements are validated, prioritised and signed off.
"""
import hashlib
import json
import os
import time
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
import difflib

import pandas as pd
import requests
import streamlit as st

from ba_engine import (ART, ORDER, PHASES, PHASE_TITLE, STATE_KEYS, AUDIENCES, DETAILS, DOC_DEFINITIONS,
                       INITIATIVES, METHODS, audit, change_target, changed_upstream, clear_flag,
                       defects_target, est_tokens, findings_target, invalidate, keep_as_is, mark_approved,
                       missing_needs, new_state, outline_for, phase_artifacts, phase_progress, route,
                       score_doctype, set_flag, status, story_readiness)
from ba_exporters import FORMATS, build, rows_to_md, clean_md
from ba_integrations import Confluence, IntegrationError, Jira

try:    # optional extra tools (requirements register, knowledge base, diagrams, data mapping); app works without them
    from ba_tools_ui import TOOL_PAGES, EXT_KEYS, render_tool
except Exception as _tools_err:      # noqa: BLE001 - never let the optional tools stop the main app
    TOOL_PAGES, EXT_KEYS, render_tool = {}, [], None
    TOOLS_ERR = str(_tools_err)
EXT_KEYS = list(EXT_KEYS) + ["scope_phases", "scope_runs"]

st.set_page_config(page_title="InitiateIQ - BA Lifecycle Orchestrator", layout="wide")
S = st.session_state


def hide_record_screen_item():
    """Hides only the 'Record screen' entry of Streamlit's top-right menu. Everything else in the menu stays."""
    try:
        import streamlit.components.v1 as components
        components.html("""
<script>
(function () {
  var doc = window.parent.document;
  function hide() {
    doc.querySelectorAll('[role="menuitem"], li, button, div, span').forEach(function (el) {
      if (el.children.length === 0 && /^\\s*record (a )?screen(cast)?\\s*$/i.test(el.textContent || '')) {
        var row = el.closest('[role="menuitem"], li') || el.parentElement;
        if (row) { row.style.display = 'none'; }
      }
    });
  }
  hide();
  if (!window.parent.__hideRecordObs) {
    window.parent.__hideRecordObs = new MutationObserver(hide);
    window.parent.__hideRecordObs.observe(doc.body, {childList: true, subtree: true});
  }
})();
</script>""", height=0)
    except Exception:
        pass


DF_KEYS = ["roles_df", "signoff_df", "findings_df", "defects_df", "stories_df"]
DEFAULT_ROLES = ["Business Head / Sponsor", "Product Owner", "Business Analyst", "Developer / Dev Lead",
                 "Architect", "QA", "PM / Scrum Master", "Security", "DevOps", "SME", "Compliance"]
ICON = {"approved": "\u2714", "todo": "\u25CB", "draft": "\u270E", "edited": "\u270E",
        "stale": "\u27F2", "revisit": "\u27F2"}
PAGES = {"Dashboard & Router": "_dashboard", "Run selected phases": "_scope", "Change control": "_change",
         "Deliverables & Email": "_deliver", "Audit trail": "_audit"}
PAGES.update(TOOL_PAGES)

# ================================================================== state
def init_state():
    for k, v in new_state().items():
        S.setdefault(k, v)
    S.setdefault("roles_df", pd.DataFrame({"Role": DEFAULT_ROLES, "Name": [""] * len(DEFAULT_ROLES),
                                            "Email": [""] * len(DEFAULT_ROLES)}))
    for k in DF_KEYS[1:]:
        S.setdefault(k, None)
    S.setdefault("deliverables", [])
    S.setdefault("dv_counter", 0)
    S.setdefault("scope_phases", [p["id"] for p in PHASES])
    S.setdefault("scope_runs", {})


def recs(df):
    """DataFrame -> list of dicts with NaN turned into ''."""
    if df is None:
        return []
    return [{k: ("" if pd.isna(v) else v) for k, v in r.items()} for r in df.to_dict("records")]


def named_roles():
    df = S.roles_df
    return df[df["Name"].astype(str).str.strip() != ""]


# ================================================================== OpenAI
DEFAULT_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"]


def _api_base():
    return (S.get("openai_base") or "https://api.openai.com/v1").rstrip("/")


def _api_key():
    return (S.get("openai_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


@st.cache_data(ttl=300, show_spinner=False)
def list_models(base, key):
    r = requests.get(base + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
    r.raise_for_status()
    ids = sorted(m["id"] for m in r.json().get("data", []))
    chat = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))
            and not any(x in i for x in ("embedding", "audio", "realtime", "tts", "transcribe", "image", "search"))]
    return chat or ids


def persona():
    it = S.intake
    return (f"You are a senior Business Analyst with 20+ years of experience in the {it.get('domain', 'given')} "
            "domain, supporting the full BA lifecycle.\nRules:\n"
            "- Use only facts from the provided context. Cite sources as [C#], [D#], [E#] or [USER].\n"
            "- NEVER invent numbers (budget, ROI, volumes, dates, story points). Write 'TBD - business to provide'.\n"
            "- State assumptions explicitly. Say 'not found' instead of guessing. Keep requirement IDs verbatim.\n"
            "- Apply domain knowledge: regulation and compliance, audit, data privacy, SLAs, legacy systems, "
            "vendor dependencies, operational impact.\n"
            f"- Domain focus areas from the user: {it.get('focus') or 'none'}.\n"
            "- Be concise and specific. Output only the requested format.")


def _api_error(r):
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except Exception:
        msg = r.text
    return f"HTTP {r.status_code}: {msg[:500]}"


def llm(task, context="", json_mode=False):
    """Calls the OpenAI Chat Completions API with streaming so the user sees live progress."""
    key = _api_key()
    if not key:
        raise RuntimeError("No OpenAI API key. Enter it in the sidebar (OpenAI section) or set OPENAI_API_KEY.")
    payload = {"model": S.get("model"), "stream": True,
               "max_completion_tokens": int(S.get("max_out", 8192)),
               "temperature": S.get("temperature", 0.2),
               "messages": [{"role": "system", "content": persona()},
                            {"role": "user", "content": f"CONTEXT:\n{context}\n\nTASK:\n{task}"}]}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    ph = st.empty()
    t0 = time.time()
    for attempt in range(3):   # retry without params that a given model rejects
        parts, n_chunks, last = [], 0, 0.0
        ph.info("Sent to OpenAI. Waiting for the first token...")
        try:
            with requests.post(_api_base() + "/chat/completions", json=payload, headers=headers, stream=True,
                               timeout=(10, int(S.get("req_timeout", 300)))) as r:
                if r.status_code != 200:
                    err = _api_error(r)
                    low = err.lower()
                    if r.status_code == 400 and "temperature" in low and "temperature" in payload:
                        payload.pop("temperature")
                        continue
                    if r.status_code == 400 and "response_format" in low and "response_format" in payload:
                        payload.pop("response_format")
                        continue
                    if r.status_code == 401:
                        raise RuntimeError("OpenAI rejected the API key (401). Check the key in the sidebar.")
                    if r.status_code == 429:
                        raise RuntimeError("OpenAI rate limit or quota reached (429). Wait a bit or check billing. " + err)
                    raise RuntimeError("OpenAI call failed. " + err)
                r.encoding = "utf-8"
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    ch = json.loads(data).get("choices") or [{}]
                    piece = (ch[0].get("delta") or {}).get("content") or ""
                    if piece:
                        parts.append(piece)
                        n_chunks += 1
                    now = time.time()
                    if now - last > 0.4:
                        last = now
                        ph.info(f"Generating... {now - t0:.0f}s elapsed, ~{n_chunks} chunks so far\n\n"
                                f"...{''.join(parts)[-600:]}")
            break
        except requests.exceptions.ReadTimeout:
            ph.empty()
            raise RuntimeError("OpenAI stopped responding (timeout). Raise the request timeout in the sidebar and try again.")
        except requests.RequestException as e:
            ph.empty()
            raise RuntimeError(f"Could not reach OpenAI ({e}). Check internet, proxy and base URL.")
    ph.empty()
    out = "".join(parts).strip()
    if not out:
        raise RuntimeError("The model returned an empty response. For o-series / gpt-5 models raise "
                           "'Max output tokens' (reasoning uses part of it), or pick another model.")
    return out


def llm_json(task, context=""):
    raw = llm(task, context, json_mode=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        a, b = raw.find("{"), raw.rfind("}")
        try:
            return json.loads(raw[a:b + 1])
        except Exception:
            raise RuntimeError("The model returned invalid JSON. Try again or use a larger model.")


def token_check(text):
    t, ctx = est_tokens(text), int(S.get("num_ctx", 100000))
    if t > ctx * 0.8:
        st.warning(f"Context is about {t} tokens, near or above the context budget of {ctx}. Raise the budget in the "
                   "sidebar (check your model's limit) or reduce source size.")


# ================================================================== context
def build_context(key):
    a, it = ART[key], S.intake
    roles = "\n".join(f"- {r.Role}: {r.Name}" for r in named_roles().itertuples())
    parts = [f"[USER] Domain: {it.get('domain')}\nProject: {it.get('project')}\n"
             f"Description: {it.get('description')}\nGoals/notes: {it.get('notes') or 'none'}\n"
             f"Declared roles:\n{roles}"]
    lim = int(S.get("doc_chars", 4000))
    for n in a["needs"]:
        if n == "intake":
            continue
        if n == "sources":
            for p in S.pages:
                parts.append(f"[{p['id']}] {p['title']} (space {p['space']}, owner {p['owner']}, "
                              f"updated {p['updated']})\n{p['body'][:lim]}")
            for i, d in enumerate(S.docs, 1):
                parts.append(f"[D{i}] {d['name']}\n{d['text'][:lim]}")
        elif n == "elic_notes":
            for i, d in enumerate(S.elic_docs, 1):
                parts.append(f"[E{i}] {d['type']} - {d['name']}\n{d['text'][:lim]}")
        elif S.outputs.get(n):
            parts.append(f"## {ART[n]['title']}\n{S.outputs[n]}")
    return "\n\n".join(parts)


def req_doc_prompt():
    d = S.doc_decision
    choice = d.get("choice", "BRD")
    outline = d.get("outline") or outline_for(choice)
    defs = "\n".join(f"- {p.strip()}: {DOC_DEFINITIONS[p.strip()]}" for p in choice.split("+"))
    return (f"Write the complete {choice} using EXACTLY this outline (keep numbering and headings):\n{outline}\n\n"
            f"Definitions:\n{defs}\n\nRules: use requirement IDs from the catalogue verbatim; do not add "
            "requirements that are not in the catalogue; write 'TBD - business to provide' for unknown figures; "
            "cite sources; write 'Not applicable' instead of leaving a section blank; present business / "
            "functional requirements as tables with IDs.")


def prompt_for(a):
    return req_doc_prompt() if a.get("prompt_fn") == "req_doc" else a["prompt"]


# ================================================================== phase scope (overlay; ba_engine is unchanged)
def _all_phase_ids():
    return [p["id"] for p in PHASES]


def scope_phases():
    sel = [x for x in (S.get("scope_phases") or []) if x in _all_phase_ids()]
    return sel or _all_phase_ids()


def scope_active():
    return set(scope_phases()) != set(_all_phase_ids())


def in_scope(key):
    """Intake is always required (the agent's persona and context come from it)."""
    return key == "intake" or ART[key]["phase"] in scope_phases()


def scoped_missing(key):
    """Unmet prerequisites. Prerequisites that live in phases the user did not select are waived."""
    miss = missing_needs(S, key)
    return miss if not scope_active() else [n for n in miss if in_scope(n)]


def route_scoped():
    """Same as the engine router when all phases are selected. With a selection it recommends the next unapproved
    step inside the selected phases only."""
    r = route(S)
    if not scope_active():
        return r
    if r.get("kind") == "loop-back" and in_scope(r["next_key"]):
        return r
    for k in ORDER:
        if not in_scope(k):
            continue
        stt, t = status(S, k), ART[k]["title"]
        if stt == "approved":
            continue
        reason = {"todo": f"Next step in your selected phases: {t} ({PHASE_TITLE[ART[k]['phase']]}).",
                  "draft": f"'{t}' has a draft waiting for your approval.",
                  "edited": f"'{t}' was edited after approval; approve it again to publish a new version.",
                  "stale": f"'{t}' is out of date because upstream changed.",
                  "revisit": f"'{t}' must be revisited: {S.invalid.get(k, '')}"}[stt]
        return {"next_key": k, "kind": "revisit" if stt in ("stale", "revisit") else "forward",
                "reason": "Scope: " + reason}
    return {"next_key": None, "kind": "complete", "reason": "All steps in the selected phases are approved."}


def _h(text):
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:10]


def scoped_context(key):
    """build_context(), plus the raw project material when earlier phases were left out of scope."""
    ctx = build_context(key)
    a = ART[key]
    if not scope_active() or not any(n != "intake" and not in_scope(n) for n in a["needs"]):
        return ctx
    lim, extra = int(S.get("doc_chars", 4000)), []
    if "sources" not in a["needs"]:
        extra += [f"[{p['id']}] {p['title']} (space {p['space']}, owner {p['owner']}, updated {p['updated']})\n{p['body'][:lim]}"
                  for p in S.pages]
        extra += [f"[D{i}] {d['name']}\n{d['text'][:lim]}" for i, d in enumerate(S.docs, 1)]
    if "elic_notes" not in a["needs"]:
        extra += [f"[E{i}] {d['type']} - {d['name']}\n{d['text'][:lim]}" for i, d in enumerate(S.elic_docs, 1)]
    return ctx + ("\n\n## Raw project material (earlier phases were not selected)\n" + "\n\n".join(extra) if extra else "")


def make_draft(a):
    """One draft for one step. Shared by the step page and by 'Run selected phases'."""
    key = a["key"]
    ctx, task = scoped_context(key), prompt_for(a)
    if scope_active() and any(n != "intake" and not in_scope(n) for n in a["needs"]):
        task += ("\n\nNote: the user chose to skip earlier lifecycle phases. Where an upstream deliverable is missing, work "
                 "from the raw project material and the user's text, state every assumption explicitly, and write "
                 "'TBD - business to provide' instead of guessing.")
    extras = "\n".join(f"\n{f['label']}: {S.inputs.get(key + '.' + f['id'], '')}" for f in a.get("inputs", []))
    token_check(ctx + task + extras)
    if a.get("json"):
        return json.dumps(llm_json(task, ctx), indent=2)
    return llm(task + extras, ctx)


def scope_note(key):
    """Information on a step page about how a batch draft was built. Display only."""
    info = (S.get("scope_runs") or {}).get(key)
    if not info or status(S, key) == "approved":
        return
    changed = [ART[n]["title"] for n, h in info.get("hashes", {}).items() if _h(S.outputs.get(n, "")) != h]
    if changed:
        st.warning("Upstream content changed after this draft was generated (" + ", ".join(changed) +
                   "). Regenerate this draft so it reflects the latest text.")
    if info.get("built_on_draft"):
        st.info("Generated in a 'Run selected phases' run on " + info["time"] + " from drafts that were not yet approved: " +
                ", ".join(ART[n]["title"] for n in info["built_on_draft"]) + ". Review and approve those first, in order.")
    if info.get("waived"):
        st.caption("Skipped phases (not selected): " + ", ".join(info["waived"]) + ".")


# ================================================================== generic UI helpers
def goto(key):
    S.loc = key


def header(a):
    key, stt = a["key"], status(S, a["key"])
    st.caption(f"{PHASE_TITLE[a['phase']]}  |  decision owner: {a['owner']}")
    if stt == "stale":
        names = ", ".join(ART[n]["title"] for n in ART[key]["needs"]
                          if n in changed_upstream(S, key) or status(S, n) in ("stale", "revisit", "edited"))
        st.warning(f"Upstream changed ({names}). Review this step, regenerate, or confirm it is still valid.")
        if st.button("Reviewed: keep as is", key=f"keep_{key}"):
            keep_as_is(S, key)
            st.rerun()
    elif stt == "edited":
        st.info("Edited since approval. Approve again to publish a new version (downstream steps will be flagged).")
    elif stt == "approved":
        st.success(f"Approved (v{S.versions.get(key, 1)})")


def prereqs_missing(a):
    miss = scoped_missing(a["key"])
    if not miss:
        return False
    st.warning("This step depends on earlier steps that are not approved and current yet:")
    for n in miss:
        st.button(f"Go to: {ART[n]['title']}  [{status(S, n)}]", key=f"miss_{a['key']}_{n}",
                  on_click=goto, args=(n,))
    return True


def gen_box(key, gen_fn, btn="Generate draft with agent"):
    if not S.get("model") or not _api_key():
        st.warning("Enter your OpenAI API key and pick a model in the sidebar first.")
    if st.button(btn, key=f"gen_{key}", type="primary"):
        try:
            with st.spinner("Agent is working..."):
                text = clean_md(gen_fn())
            S.outputs[key] = text
            S[f"ta_{key}"] = text
        except Exception as e:
            st.error(str(e))
    if S.outputs.get(key):
        S.setdefault(f"ta_{key}", S.outputs[key])
        t_prev, t_edit = st.tabs(["Preview (formatted)", "Edit (markdown)"])
        with t_edit:   # runs first so the preview always shows the latest edit
            st.text_area("Review and edit. Your edited text is what flows downstream and into exports.",
                         height=430, key=f"ta_{key}")
        S.outputs[key] = S[f"ta_{key}"]
        with t_prev:
            st.markdown(clean_md(S[f"ta_{key}"]))
        return True
    return False


def _approve(key, text, after):
    """Runs as a button callback (before the script reruns), so it may also update widget state."""
    mark_approved(S, key, text)
    if text is not None and f"ta_{key}" in S:
        S[f"ta_{key}"] = text
    if after:
        after()
    nxt = route_scoped()
    S.loc = nxt["next_key"] or "_dashboard"
    S["toast"] = "Router: " + nxt["reason"]


def approve_btn(key, text=None, disabled=False, label="Approve and continue", after=None):
    st.caption(f"Human checkpoint. Decision owner: **{ART[key]['owner']}**")
    st.button(label, key=f"ok_{key}", type="primary", disabled=disabled, on_click=_approve,
              args=(key, text, after))


def need_intake():
    if status(S, "intake") != "approved":
        st.warning("Complete the intake first.")
        st.button("Go to intake", on_click=goto, args=("intake",))
        return True
    return False


def same_person(a, b):
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if not a or not b:
        return False
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.85


def is_old(updated, days):
    try:
        d = datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days > days
    except ValueError:
        return False


# ================================================================== generic LLM artifact page
def page_llm(a):
    key = a["key"]
    header(a)
    scope_note(key)
    if prereqs_missing(a):
        return
    for f in a.get("inputs", []):
        k = f"{key}.{f['id']}"
        S.inputs[k] = st.text_area(f["label"], value=S.inputs.get(k, ""), key=f"in_{k}", height=90)
    missing_in = [f for f in a.get("inputs", []) if f["required"] and not S.inputs.get(f"{key}.{f['id']}", "").strip()]
    if missing_in:
        st.info("Fill the required input above to enable generation.")
        return

    def gen():
        return make_draft(a)

    if gen_box(key, gen):
        approve_btn(key)


# ================================================================ 1 Intake / sources / stakeholders
def page_intake(a):
    header(a)
    it = S.intake
    for k_, f_ in (("intake_project", "project"), ("intake_domain", "domain"), ("intake_desc", "description"),
                   ("intake_focus", "focus"), ("intake_notes", "notes")):
        S.setdefault(k_, it.get(f_, ""))
    c1, c2 = st.columns(2)
    project = c1.text_input("Project name *", key="intake_project")
    domain = c2.text_input("Industry / domain *", key="intake_domain",
                            placeholder="e.g. banking, insurance, healthcare, retail")
    desc = st.text_area("What is the project about? *", height=110, key="intake_desc")
    focus = st.text_input("Domain focus areas (regulation, compliance, key concerns)", key="intake_focus")
    notes = st.text_area("Known goals, constraints, links (optional)", height=80, key="intake_notes")
    st.markdown("**Who is who?** Add names and emails; add rows for other roles. Leave unknown roles blank.")
    S.roles_df = st.data_editor(S.roles_df, num_rows="dynamic", use_container_width=True, key="roles_editor")
    if st.button("Save intake and continue", type="primary"):
        if not (project.strip() and domain.strip() and desc.strip()):
            st.error("Project name, domain and description are required.")
        elif named_roles().empty:
            st.error("Enter at least one named person.")
        else:
            S.intake = {"project": project.strip(), "domain": domain.strip(), "description": desc.strip(),
                        "focus": focus.strip(), "notes": notes.strip()}
            text = (f"Project: {project}\nDomain: {domain}\nDescription: {desc}\nRoles: " +
                    "; ".join(f"{r.Role}={r.Name}" for r in named_roles().itertuples()))
            mark_approved(S, "intake", text)
            nxt = route_scoped()
            S.loc = nxt["next_key"]
            S["toast"] = "Router: " + nxt["reason"]
            st.rerun()


def page_sources(a):
    header(a)
    if need_intake():
        return
    st.info("Read-only. The agent uses only the pages you tick. Configure Confluence in the sidebar, or skip it "
            "and use uploaded / pasted documents.")
    if st.button("Suggest search keywords from the project description"):
        try:
            with st.spinner("Thinking..."):
                ctx = f"{S.intake['description']}\n{S.intake.get('notes', '')}"
                out = llm_json('Return JSON {"keywords": [up to 8 short search terms a team would use in '
                               'Confluence titles or text for this project]}.', ctx)
            S["kw_input"] = ", ".join(out.get("keywords", []))
        except Exception as e:
            st.error(str(e))
    st.text_input("Keywords (comma separated)", key="kw_input")
    if st.button("Search Confluence", type="primary"):
        if not (S.cf_base and S.cf_token):
            st.error("Fill the Confluence connection in the sidebar first.")
        else:
            try:
                spaces = [x.strip() for x in S.cf_spaces.split(",") if x.strip()]
                kws = [x.strip() for x in S.get("kw_input", "").split(",") if x.strip()]
                with st.spinner("Searching Confluence..."):
                    cf = Confluence(S.cf_base, S.cf_ctx, S.cf_auth, S.cf_email, S.cf_token)
                    S["found_pages"] = cf.search(kws, spaces, int(S.cf_limit))
                if not S["found_pages"]:
                    st.warning("No pages found. Try other keywords or spaces.")
            except (IntegrationError, requests.RequestException) as e:
                st.error(str(e))
    found = S.get("found_pages", [])
    if found:
        df = pd.DataFrame([{"Use": True, "ID": p["id"], "Title": p["title"], "Space": p["space"],
                             "Owner": p["owner"], "Updated": p["updated"],
                             "Stale": is_old(p["updated"], int(S.stale_days)), "URL": p["url"]} for p in found])
        st.markdown("**Tick the pages the agent may read**")
        ed = st.data_editor(df, use_container_width=True, hide_index=True, key="pages_editor",
                            disabled=["ID", "Title", "Space", "Owner", "Updated", "Stale", "URL"])
        S["selected_ids"] = set(ed.loc[ed["Use"], "ID"])
    st.markdown("**Other documents** (optional)")
    ups = st.file_uploader("Upload .txt / .md / .csv / .json", type=["txt", "md", "csv", "json"],
                            accept_multiple_files=True, key="src_up")
    pasted = st.text_area("Or paste brief / notes / emails", height=110, key="src_paste")
    if st.button("Approve sources and continue", type="primary"):
        S.pages = [p for p in found if p["id"] in S.get("selected_ids", set())]
        docs = [{"name": u.name, "text": u.read().decode("utf-8", errors="ignore")} for u in ups]
        if pasted.strip():
            docs.append({"name": "Pasted notes", "text": pasted.strip()})
        S.docs = docs
        lines = [f"#{p['id']} {p['title']} (updated {p['updated']}) {p['url']}" for p in S.pages]
        lines += [f"[D{i}] {d['name']}" for i, d in enumerate(S.docs, 1)]
        mark_approved(S, "sources", "\n".join(lines) or "No external sources; based on user input only.")
        nxt = route_scoped()
        S.loc = nxt["next_key"]
        S["toast"] = "Router: " + nxt["reason"]
        st.rerun()


def page_stakeholders(a):
    key = "stakeholders"
    header(a)
    if prereqs_missing(a):
        return
    declared = named_roles()
    c1, c2 = st.columns(2)
    owner_role = c1.selectbox("Which declared role should own the requirements pages?",
                               ["(skip check)"] + list(declared["Role"]))
    S.setdefault("req_kw", "requirement")
    req_kw = c2.text_input("Page-title keyword that identifies requirements pages", key="req_kw")

    def analyse():
        names = [r.Name.strip() for r in declared.itertuples()]
        people = {}
        for p in S.pages:
            people.setdefault(p["owner"], []).append(f"owner of [{p['id']}]")
            if p["last_editor"]:
                people.setdefault(p["last_editor"], []).append(f"last editor of [{p['id']}]")
            for c in p["contributors"]:
                people.setdefault(c, []).append(f"contributor on [{p['id']}]")
            for c in p["commenters"]:
                people.setdefault(c, []).append(f"commenter on [{p['id']}]")
        people.pop("", None)
        mism = []
        if owner_role != "(skip check)" and req_kw.strip():
            expected = declared.loc[declared["Role"] == owner_role, "Name"].iloc[0]
            for p in S.pages:
                if req_kw.lower() in p["title"].lower() and not same_person(p["owner"], expected):
                    mism.append(f"Declared {owner_role} is '{expected}', but requirements page [{p['id']}] "
                                f"'{p['title']}' is owned by '{p['owner']}'. Confirm who owns the requirements.")
        S.mismatches = mism
        S.informal = {n: ev for n, ev in people.items() if not any(same_person(n, d) for d in names)}
        ctx = build_context(key) + f"\n\nPeople found in sources: {json.dumps(people)}\nMismatches: {mism}"
        return llm("Build a stakeholder register as a markdown table: Name | Role | Influence (H/M/L) | "
                   "Interest (H/M/L) | RACI (R/A/C/I) | Why they matter for this work type and domain. Include "
                   "declared people and informal stakeholders from the sources. Add typical roles still missing "
                   "for this work type as 'TO BE NAMED'. After the table list questions the BA should ask to "
                   "confirm hidden or political stakeholders.", ctx)

    if gen_box(key, analyse, "Analyse stakeholders"):
        if S.mismatches:
            st.warning("Mismatches between what you declared and what Confluence shows:")
            for m in S.mismatches:
                st.write("- " + m)
        if S.informal:
            st.markdown("**Informal stakeholders found in sources (not in your role list)**")
            st.dataframe(pd.DataFrame([{"Name": n, "Evidence": "; ".join(e)} for n, e in S.informal.items()]),
                         use_container_width=True, hide_index=True)
        S.resolution = st.text_area("Your resolution of the mismatches (required when mismatches exist)",
                                     value=S.resolution, height=80)
        extra = ""
        if S.mismatches:
            extra += "\n\n## Mismatch resolution\n" + "\n".join(f"- {m}" for m in S.mismatches) + f"\nResolution: {S.resolution}"
        if S.informal:
            extra += "\n\n## Informal stakeholders\n" + "\n".join(f"- {n}: {'; '.join(e)}" for n, e in S.informal.items())
        approve_btn(key, text=S.outputs[key].split("\n\n## Mismatch resolution")[0].split("\n\n## Informal")[0] + extra,
                    disabled=bool(S.mismatches) and not S.resolution.strip())


# ================================================================ 2 Elicitation evidence
def page_elic_notes(a):
    key = "elic_notes"
    header(a)
    if prereqs_missing(a):
        return
    st.info("Add the real evidence gathered in sessions: interview notes, workshop minutes, transcripts, emails, "
            "survey results, process documents. The agent extracts requirements ONLY from this evidence.")
    with st.form("add_evidence", clear_on_submit=True):
        c1, c2 = st.columns(2)
        typ = c1.selectbox("Evidence type", ["Interview", "Workshop", "Observation", "Survey", "Document",
                                              "Email / chat", "Other"])
        name = c2.text_input("Label (who / which session)")
        ups = st.file_uploader("Upload .txt / .md / .csv / .json", type=["txt", "md", "csv", "json"],
                                accept_multiple_files=True)
        txt = st.text_area("Or paste notes / transcript", height=150)
        if st.form_submit_button("Add evidence"):
            for u in ups:
                S.elic_docs.append({"type": typ, "name": name or u.name,
                                     "text": u.read().decode("utf-8", errors="ignore")})
            if txt.strip():
                S.elic_docs.append({"type": typ, "name": name or typ, "text": txt.strip()})
    if S.elic_docs:
        st.dataframe(pd.DataFrame([{"Ref#": f"E{i}", "Type": d["type"], "Label": d["name"], "Characters": len(d["text"])}
                                    for i, d in enumerate(S.elic_docs, 1)]), hide_index=True, use_container_width=True)
        rm = st.selectbox("Remove an item", ["-"] + [f"E{i}" for i in range(1, len(S.elic_docs) + 1)])
        if rm != "-" and st.button("Remove selected"):
            S.elic_docs.pop(int(rm[1:]) - 1)
            st.rerun()
    text = "\n".join(f"[E{i}] {d['type']} - {d['name']} ({len(d['text'])} chars)" for i, d in enumerate(S.elic_docs, 1))
    approve_btn(key, text=text, disabled=not S.elic_docs, label="Approve evidence and continue")


# ================================================================ 3 Document type decision
def page_doctype(a):
    key = a["key"]
    header(a)
    if prereqs_missing(a):
        return
    if not S.get("model") or not _api_key():
        st.warning("Enter your OpenAI API key and pick a model in the sidebar first.")
    ans = S.get("doctype_answers") or {}
    c1, c2 = st.columns(2)
    me = c1.selectbox("Methodology", METHODS, index=METHODS.index(ans.get("methodology", METHODS[0])))
    ini = c1.selectbox("Initiative type", INITIATIVES, index=INITIATIVES.index(ans.get("initiative", INITIATIVES[0])))
    au = c1.selectbox("Primary audience", AUDIENCES, index=AUDIENCES.index(ans.get("audience", AUDIENCES[0])))
    de = c2.selectbox("Level of detail needed", DETAILS, index=DETAILS.index(ans.get("detail", DETAILS[0])))
    out = c1.checkbox("Build is outsourced / vendor-delivered", value=ans.get("outsourced", False))
    reg = c2.checkbox("Regulated or audit-heavy environment", value=ans.get("regulated", False))
    answers = {"methodology": me, "initiative": ini, "audience": au, "detail": de, "outsourced": out, "regulated": reg}
    res = score_doctype(answers)
    st.success(f"Agent recommends: **{res['recommendation']}**")
    st.dataframe(pd.DataFrame([{"Type": t, "Score": res["scores"][t], "Why": "; ".join(res["why"][t])}
                                for t in ("BRD", "FRD", "PRD")]), hide_index=True, use_container_width=True)
    with st.expander("What do BRD, FRD and PRD mean?"):
        for t, v in DOC_DEFINITIONS.items():
            st.write(f"**{t}**: {v}")
    options = ["BRD", "FRD", "PRD", "BRD + FRD"]
    default = options.index(res["recommendation"]) if res["recommendation"] in options else 0
    choice = st.radio("Your decision (defaults to the recommendation; change it if you disagree)", options,
                      index=default, horizontal=True)
    outline = st.text_area("Section outline (edit to add or remove sections)", value=outline_for(choice),
                            height=300, key=f"outline_{choice}")
    text = f"Document type: {choice}\nRecommendation: {res['recommendation']}\nOutline:\n{outline}"

    def after():
        S.doc_decision = {"answers": answers, "choice": choice, "outline": outline}
    approve_btn(key, text=text, after=after, label="Confirm document type and continue")


# ================================================================ 4 Validation / sign-off
FIND_TYPES = ["missing", "incomplete", "conflict", "ambiguous", "inconsistent", "untestable", "out_of_scope"]


def page_validation(a):
    key = "validation"
    header(a)
    if prereqs_missing(a):
        return
    if not S.get("model") or not _api_key():
        st.warning("Enter your OpenAI API key and pick a model in the sidebar first.")
    if st.button("Run validation (agent)", type="primary"):
        try:
            with st.spinner("Validating requirements..."):
                ctx = build_context(key)
                token_check(ctx)
                out = llm_json(
                    "Verify the requirements for completeness, consistency, ambiguity, testability, traceability "
                    "to scope and feasibility. Return JSON {\"findings\":[{\"id\":\"V1\",\"req_id\":\"\",\"type\":"
                    f"\"one of {'|'.join(FIND_TYPES)}\",\"severity\":\"blocking|major|minor\",\"description\":\"\","
                    "\"suggested_fix\":\"\"}]}. 'blocking' means the requirement cannot be built or signed off as "
                    "written. Only report real issues you can point to; an empty list is acceptable.", ctx)
                rows = [{"id": f.get("id", f"V{i}"), "req_id": f.get("req_id", ""), "type": str(f.get("type", "")).lower(),
                         "severity": str(f.get("severity", "minor")).lower(), "description": f.get("description", ""),
                         "suggested_fix": f.get("suggested_fix", ""), "resolution": "Open", "note": ""}
                        for i, f in enumerate(out.get("findings", []), 1)]
                S.findings_df = pd.DataFrame(rows, columns=["id", "req_id", "type", "severity", "description",
                                                              "suggested_fix", "resolution", "note"])
        except Exception as e:
            st.error(str(e))
    if S.findings_df is None:
        st.info("Run the validation to get findings. You can also add your own rows.")
        return
    cc = st.column_config
    S.findings_df = st.data_editor(
        S.findings_df, num_rows="dynamic", use_container_width=True, key="findings_editor",
        column_config={"type": cc.SelectboxColumn("type", options=FIND_TYPES),
                       "severity": cc.SelectboxColumn("severity", options=["blocking", "major", "minor"]),
                       "resolution": cc.SelectboxColumn("resolution", options=["Open", "Resolved", "Accepted risk"])})
    rows = recs(S.findings_df)
    openb = [r for r in rows if str(r["severity"]).lower() == "blocking" and r["resolution"] in ("Open", "")]
    target = findings_target(rows)
    if openb:
        st.error(f"{len(openb)} blocking finding(s) are open. Resolve them, accept the risk with a note, or "
                 "send the work back.")
        if st.button(f"Send back to: {ART[target]['title']}"):
            set_flag(S, "validation", target, f"{len(openb)} blocking validation finding(s)")
            S.loc = target
            S["toast"] = f"Sent back to {ART[target]['title']}"
            st.rerun()
    else:
        st.success("No open blocking findings.")
    text = "## Validation findings\n" + rows_to_md(
        ["ID", "Req", "Type", "Severity", "Description", "Suggested fix", "Resolution", "Note"],
        [[r["id"], r["req_id"], r["type"], r["severity"], r["description"], r["suggested_fix"],
          r["resolution"], r["note"]] for r in rows]) if rows else "No findings."
    approve_btn(key, text=text, disabled=bool(openb), after=lambda: clear_flag(S, "validation"))


DECISIONS = ["Pending", "Approved", "Approved with conditions", "Rejected"]


def page_signoff(a):
    key = "signoff"
    header(a)
    if prereqs_missing(a):
        return
    st.caption("Send the requirements document for sign-off from 'Deliverables & Email', then record each decision here.")
    if S.signoff_df is None:
        r = named_roles()
        S.signoff_df = pd.DataFrame({"Name": r["Name"], "Role": r["Role"], "Email": r["Email"], "Required": True,
                                      "Decision": "Pending", "Date": "", "Comments": ""}).reset_index(drop=True)
    cc = st.column_config
    S.signoff_df = st.data_editor(S.signoff_df, num_rows="dynamic", use_container_width=True, key="signoff_editor",
                                   column_config={"Decision": cc.SelectboxColumn("Decision", options=DECISIONS),
                                                  "Required": cc.CheckboxColumn("Required")})
    rows = recs(S.signoff_df)
    rows = [r for r in rows if r["Required"]]
    rejected = [r for r in rows if r["Decision"] == "Rejected"]
    pending = [r for r in rows if r["Decision"] in ("Pending", "")]
    if rejected:
        st.error("Rejected by: " + ", ".join(r["Name"] for r in rejected))
        tgt = st.selectbox("Send back to", ["req_catalog", "elic_notes", "scope"],
                            format_func=lambda k: ART[k]["title"])
        if st.button("Send back"):
            set_flag(S, "signoff", tgt, "Rejected at sign-off by " + ", ".join(r["Name"] for r in rejected),
                     also_invalidate=["validation", "signoff"])
            S.loc = tgt
            st.rerun()
    elif pending:
        st.info(f"Waiting for {len(pending)} required decision(s).")
    text = "## Sign-off record\n" + rows_to_md(["Name", "Role", "Required", "Decision", "Date", "Comments"],
                                                 [[r["Name"], r["Role"], r["Required"], r["Decision"], r["Date"],
                                                   r["Comments"]] for r in recs(S.signoff_df)])
    approve_btn(key, text=text, disabled=not rows or bool(rejected) or bool(pending),
                label="Record sign-off and continue")


# ================================================================ 5 Stories -> Jira
STORY_COLS = ["Create", "Epic", "Summary", "As a", "I want", "So that", "Acceptance criteria", "Priority",
              "Req IDs", "Story points", "Jira key"]


def jira_client():
    return Jira(S.jira_base, S.jira_auth, S.jira_email, S.jira_token)


def pick(label, options, key, placeholder=""):
    if options:
        if S.get(key) not in options:
            S.pop(key, None)
        return st.selectbox(label, options, key=key)
    return st.text_input(label, key=key, placeholder=placeholder)


def story_description(r):
    ac = "\n".join(f"* {x.strip()}" for x in str(r["Acceptance criteria"]).splitlines() if x.strip())
    return (f"As a {r['As a']}\nI want {r['I want']}\nSo that {r['So that']}\n\n"
            f"h3. Acceptance criteria\n{ac}\n\nRequirements: {r['Req IDs']}\n"
            f"_Created by InitiateIQ from approved requirements ({S.intake.get('project', '')})._")


def page_stories(a):
    key = "stories"
    header(a)
    if prereqs_missing(a):
        return
    checks = story_readiness(S)
    with st.expander("Readiness gate: everything must be finalized before stories are created in Jira",
                     expanded=not all(ok for _, ok in checks)):
        for label, ok in checks:
            st.write(("\u2714 " if ok else "\u2718 ") + label)
    if not all(ok for _, ok in checks):
        st.error("Not ready. Finish the open steps first.")
    nxt = route_scoped()
    if nxt["next_key"]:
        pass  # router will redirect if needed

    if S.stories_df is None:
        if not S.get("model") or not _api_key():
            st.warning("Enter your OpenAI API key and pick a model in the sidebar first.")
        if st.button("Write stories (agent)", type="primary"):
            try:
                with st.spinner("Writing stories..."):
                    ctx = build_context(key)
                    token_check(ctx)
                    out = llm_json(
                        "Create INVEST-compliant user stories from the approved requirements catalogue and "
                        "prioritisation. Return JSON {\"stories\":[{\"epic\":\"\",\"summary\":\"\",\"as_a\":\"\","
                        "\"i_want\":\"\",\"so_that\":\"\",\"acceptance_criteria\":[\"Given ... When ... Then ...\"],"
                        "\"priority\":\"Must|Should|Could\",\"requirement_ids\":[\"FR-001\"]}]}. Rules: cover every "
                        "Must and Should requirement; group related stories under a short epic name; each story fits "
                        "a sprint; at least 2 Given/When/Then criteria; do NOT estimate story points; do not invent "
                        "requirements; add stories for NFRs such as audit logging when NFRs exist.", ctx)
                    new = [{"Create": True, "Epic": s.get("epic", ""), "Summary": s.get("summary", ""), "As a": s.get("as_a", ""),
                            "I want": s.get("i_want", ""), "So that": s.get("so_that", ""),
                            "Acceptance criteria": "\n".join(s.get("acceptance_criteria", [])),
                            "Priority": s.get("priority", ""), "Req IDs": ", ".join(s.get("requirement_ids", [])),
                            "Story points": "", "Jira key": ""} for s in out.get("stories", [])]
                    kept = [r for r in recs(S.stories_df) if r["Jira key"]]
                    S.stories_df = pd.DataFrame(kept + new, columns=STORY_COLS)
                    if kept:
                        st.info(f"Kept {len(kept)} stories already created in Jira. New ones are listed after them; "
                                "the duplicate check will warn about overlaps.")
            except Exception as e:
                st.error(str(e))
    if S.stories_df is None:
        return
    S.stories_df["Create"] = S.stories_df["Create"].astype(bool)
    S.stories_df = st.data_editor(S.stories_df, num_rows="dynamic", use_container_width=True, key="stories_editor",
                                   disabled=["Jira key"],
                                   column_config={"Acceptance criteria": st.column_config.TextColumn(width="large")})
    st.subheader("Jira")
    if not (S.jira_base and S.jira_token and S.jira_project):
        st.info("Fill the Jira connection (URL, credentials, project key) in the sidebar to create issues. "
                "You can still approve the stories without Jira below.")
    else:
        if st.button("Load Jira project metadata (issue types, priorities)"):
            try:
                S["jira_meta"] = {**jira_client().project_meta(S.jira_project.strip()),
                                  "user": jira_client().myself()}
                st.success(f"Connected as {S.jira_meta['user']}.")
            except (IntegrationError, requests.RequestException) as e:
                st.error(str(e))
        meta = S.get("jira_meta") or {}
        rows = [r for r in recs(S.stories_df) if r["Create"] and not r["Jira key"] and r["Summary"]]
        st.write(f"{len(rows)} stor{'y' if len(rows) == 1 else 'ies'} selected for creation "
                 f"({len(S.jira_epics)} epic(s) already created).")
        if st.button("Preview (dry run)"):
            for r in rows[:3]:
                st.code(f"[{S.get('jira_story_type', 'Story')}] {r['Summary']}\n\n{story_description(r)}")
        try:
            dups = {r["Summary"]: jira_client().find_duplicates(S.jira_project.strip(), r["Summary"]) for r in rows[:15]}
            dups = {k: v for k, v in dups.items() if v}
            if dups:
                st.warning("Possible duplicates already in Jira: " + "; ".join(f"'{k}' -> {', '.join(v)}" for k, v in dups.items()))
            else:
                st.success("No duplicates found for the first 15 summaries.")
        except Exception as e:
            st.info(f"Duplicate check skipped: {e}")
        ok = st.checkbox("I reviewed the stories and approve creating them in Jira (this cannot be undone here)")
        if st.button("Create in Jira", type="primary", disabled=not (ok and rows and S.get("jira_story_type"))):
            create_in_jira(rows)
            st.rerun()
        for line in S.get("jira_log", []):
            st.write(line)

    created = [r for r in recs(S.stories_df) if r["Jira key"]]
    skip = st.checkbox("Approve without creating Jira issues (stories stay in the document only)")
    rows_all = recs(S.stories_df)
    text = "## User stories\n" + rows_to_md(
        ["Epic", "Summary", "As a", "I want", "So that", "Acceptance criteria", "Priority", "Req IDs", "Jira key"],
        [[r["Epic"], r["Summary"], r["As a"], r["I want"], r["So that"], r["Acceptance criteria"], r["Priority"],
          r["Req IDs"], r["Jira key"]] for r in rows_all])
    approve_btn(key, text=text, disabled=not (created or skip), label="Approve stories and continue")


def create_in_jira(rows):
    j, proj = jira_client(), S.jira_project.strip()
    labels = [x.strip() for x in S.get("jira_labels", "").split(",") if x.strip()]
    log = []
    try:
        for e in sorted({r["Epic"] for r in rows if str(r["Epic"]).strip() and r["Epic"] not in S.jira_epics}):
            res = j.create_issue(proj, S.get("jira_epic_type") or "Epic", e,
                                 f"Epic created by InitiateIQ for {S.intake['project']}", labels=labels,
                                 epic_name_field=S.get("jira_epic_name_field") or None, epic_name=e)
            S.jira_epics[e] = res["key"]
            log.append(f"Epic created: {res['key']} - {e}")
            audit(S, "JIRA EPIC", f"{res['key']} {e}")
    except (IntegrationError, requests.RequestException) as ex:
        log.append(f"Epic creation failed: {ex}")
    pmap = {"must": S.get("jira_pri_must"), "should": S.get("jira_pri_should"), "could": S.get("jira_pri_could")}
    df = S.stories_df.reset_index(drop=True)
    for i, r in enumerate(recs(df)):
        if not (r["Create"] and not r["Jira key"] and r["Summary"]):
            continue
        try:
            res = j.create_issue(
                proj, S.get("jira_story_type") or "Story", r["Summary"], story_description(r), labels=labels,
                priority=pmap.get(str(r["Priority"]).lower()) if S.get("jira_send_priority") else None,
                parent_key=S.jira_epics.get(r["Epic"]), epic_link_field=S.get("jira_epic_link_field") or None,
                story_points_field=S.get("jira_points_field") or None, story_points=r["Story points"])
            df.at[i, "Jira key"] = res["key"]
            S.jira_created[res["key"]] = r["Summary"]
            log.append(f"Created {res['key']} - {r['Summary']}" + (f" ({'; '.join(res['warnings'])})" if res["warnings"] else ""))
            audit(S, "JIRA STORY", f"{res['key']} {r['Summary']}")
        except (IntegrationError, requests.RequestException) as ex:
            log.append(f"FAILED '{r['Summary']}': {ex}")
    S.stories_df = df
    S["jira_log"] = log


# ================================================================ 6 UAT results
DEFECT_COLS = ["id", "title", "severity", "root_cause", "req_id", "status", "note"]
ROOTS = ["requirement", "design", "code", "data", "environment", "test"]


def page_uat_results(a):
    key = "uat_results"
    header(a)
    if prereqs_missing(a):
        return
    raw = st.text_area("Paste UAT execution results and defect list (any format)", height=160, key="uat_raw")
    ups = st.file_uploader("...or upload .txt / .csv", type=["txt", "csv", "md"], key="uat_up")
    if ups is not None:
        raw += "\n" + ups.read().decode("utf-8", errors="ignore")
    if st.button("Classify defects with agent", type="primary", disabled=not raw.strip()):
        try:
            with st.spinner("Classifying..."):
                out = llm_json("Extract each defect and classify its root cause. Return JSON {\"defects\":[{\"id\":"
                               "\"\",\"title\":\"\",\"severity\":\"critical|high|medium|low\",\"root_cause\":\"one of "
                               + "|".join(ROOTS) + "\",\"req_id\":\"\"}]}. 'requirement' means the requirement was "
                               "missing, wrong or ambiguous. Use only defects present in the input.",
                               build_context(key) + "\n\nUAT INPUT:\n" + raw[:8000])
                S.defects_df = pd.DataFrame([{"id": d.get("id", f"D{i}"), "title": d.get("title", ""),
                                               "severity": str(d.get("severity", "medium")).lower(),
                                               "root_cause": str(d.get("root_cause", "code")).lower(),
                                               "req_id": d.get("req_id", ""), "status": "Open", "note": ""}
                                              for i, d in enumerate(out.get("defects", []), 1)], columns=DEFECT_COLS)
        except Exception as e:
            st.error(str(e))
    if S.defects_df is None:
        st.info("Paste results and classify, or approve directly when UAT had no defects.")
    else:
        cc = st.column_config
        S.defects_df = st.data_editor(
            S.defects_df, num_rows="dynamic", use_container_width=True, key="defects_editor",
            column_config={"severity": cc.SelectboxColumn("severity", options=["critical", "high", "medium", "low"]),
                           "root_cause": cc.SelectboxColumn("root_cause", options=ROOTS),
                           "status": cc.SelectboxColumn("status", options=["Open", "Fixed", "Deferred", "Accepted"])})
        rows = recs(S.defects_df)
        open_sev = [r for r in rows if r["status"] == "Open" and r["severity"] in ("critical", "high")]
        tgt = defects_target(rows)
        if tgt:
            st.warning("Open defects caused by requirements or design. These must go back to the BA phase that owns them.")
            if st.button(f"Send back to: {ART[tgt]['title']}"):
                set_flag(S, "uat", tgt, "UAT defects with root cause in requirements/design")
                S.loc = tgt
                st.rerun()
        if open_sev:
            st.error(f"{len(open_sev)} open critical/high defect(s).")
        exit_ok = st.checkbox("UAT exit criteria met and business acceptance recorded")
        note = st.text_input("Acceptance note (who accepted, when)", key="uat_note")
        text = ("## UAT outcome\n" + f"Exit criteria met: {exit_ok}. {note}\n\n" +
                (rows_to_md(DEFECT_COLS, [[r[c] for c in DEFECT_COLS] for r in rows]) if rows else "No defects recorded."))
        approve_btn(key, text=text, disabled=bool(open_sev) or not exit_ok, after=lambda: clear_flag(S, "uat"),
                    label="Record UAT acceptance and continue")


# ================================================================ run selected phases
SCOPE_ICON = {"run": "Generate", "manual": "Open and complete", "blocked": "Blocked", "skip": "Skip"}


def scope_plan(chain, regen):
    """What a 'Run selected phases' click would do, step by step. Pure planning, nothing is generated here."""
    plan, planned = [], set()
    for k in ORDER:
        if k == "intake" or not in_scope(k):
            continue
        a, stt = ART[k], status(S, k)
        row = {"key": k, "Step": a["title"], "Phase": PHASE_TITLE[a["phase"]], "Status": stt, "action": "skip", "Why": ""}
        if k in SPECIAL:
            row.update(action="manual", Why="Has its own screen and needs your input (open it, complete it, approve it)")
        elif stt == "approved":
            row["Why"] = "Already approved"
        elif stt in ("edited", "stale", "revisit"):
            row["Why"] = "Needs your review - open the step"
        elif S.outputs.get(k) and not regen:
            row["Why"] = "A draft already exists (tick 'Regenerate existing drafts' to replace it)"
        else:
            miss_in = [f["label"] for f in a.get("inputs", []) if f["required"] and not S.inputs.get(f"{k}.{f['id']}", "").strip()]
            blockers = []
            if miss_in:
                blockers.append("needs input: " + ", ".join(miss_in))
            for n in scoped_missing(k):
                if n == "intake":
                    blockers.append("complete and approve the Intake first")
                elif not (S.outputs.get(n) or n in planned):
                    blockers.append(f"'{ART[n]['title']}' has no draft yet")
                elif not chain and status(S, n) != "approved":
                    blockers.append(f"approve '{ART[n]['title']}' first")
            if blockers:
                row.update(action="blocked", Why="; ".join(blockers))
            else:
                row.update(action="run", Why="Will generate")
                on_draft = [ART[n]["title"] for n in scoped_missing(k) if n != "intake"]
                skipped = sorted({PHASE_TITLE[ART[n]["phase"]] for n in ART[k]["needs"] if n != "intake" and not in_scope(n)})
                notes = []
                if on_draft:
                    notes.append("builds on unapproved drafts: " + ", ".join(on_draft))
                if skipped:
                    notes.append("skipped phases waived: " + ", ".join(skipped))
                if notes:
                    row["Why"] += " (" + "; ".join(notes) + ")"
                planned.add(k)
        plan.append(row)
    return plan


def run_scope(plan):
    todo = [r for r in plan if r["action"] == "run"]
    bar, results = st.progress(0.0, text="Starting..."), []
    for i, r in enumerate(todo, 1):
        a = ART[r["key"]]
        bar.progress((i - 1) / len(todo), text=f"{i} of {len(todo)}: {a['title']}")
        try:
            text = clean_md(make_draft(a))
        except Exception as e:     # stop at the first failure; earlier drafts are kept
            results.append({"Step": a["title"], "Result": "Failed", "Detail": str(e)[:240]})
            break
        key = r["key"]
        S.outputs[key] = text
        S[f"ta_{key}"] = text
        built = [n for n in ART[key]["needs"] if n != "intake" and S.outputs.get(n)]
        S.scope_runs[key] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                             "built_on_draft": [n for n in built if status(S, n) != "approved"],
                             "hashes": {n: _h(S.outputs[n]) for n in built},
                             "waived": sorted({PHASE_TITLE[ART[n]["phase"]] for n in ART[key]["needs"]
                                               if n != "intake" and not in_scope(n)})}
        audit(S, "DRAFT (selected phases)", a["title"])
        results.append({"Step": a["title"], "Result": "Draft created", "Detail": ""})
    bar.empty()
    S["scope_last"] = results


def _scope_set(ids):
    S["scope_phases"] = list(ids)
    S["scope_w"] = list(ids)


def page_scope():
    st.title("Run selected phases")
    st.caption("Pick the phases you want analysed, then generate drafts for just those. Phases you leave out are skipped and "
               "their prerequisites are waived. Nothing is approved automatically: every draft still goes through its own "
               "human checkpoint, and the normal Router and approvals are unchanged.")
    ids = _all_phase_ids()
    titles = {p["id"]: p["title"] for p in PHASES}
    sel = st.multiselect("Phases in scope", ids, default=scope_phases(), format_func=lambda i: titles[i], key="scope_w")
    S["scope_phases"] = sel or ids
    if not sel:
        st.warning("Nothing selected, so all phases stay in scope.")
    b1, b2 = st.columns(2)
    b1.button("Select all phases", key="scope_all", on_click=_scope_set, args=(ids,))
    b2.button("Only phases that still have open steps", key="scope_open", on_click=_scope_set,
              args=([p["id"] for p in phase_progress(S) if p["done"] < p["total"]] or ids,))
    o1, o2 = st.columns(2)
    chain = o1.checkbox("Let drafts build on earlier drafts that are not approved yet", value=True, key="scope_chain",
                        help="Needed to produce several linked drafts in one run. You still approve them one at a time, in order.")
    regen = o2.checkbox("Regenerate existing drafts", value=False, key="scope_regen",
                        help="Replaces drafts that were never approved. Approved, edited and stale steps are never touched.")
    if status(S, "intake") != "approved":
        st.warning("The Intake step is always required, even when its phase is not selected.")
        st.button("Go to intake", key="scope_gi", on_click=goto, args=("intake",))
    plan = scope_plan(chain, regen)
    if plan:
        st.dataframe(pd.DataFrame([{"Step": r["Step"], "Phase": r["Phase"], "Status": r["Status"],
                                    "Plan": SCOPE_ICON[r["action"]], "Why": r["Why"]} for r in plan]),
                     use_container_width=True, hide_index=True)
    n_run = sum(1 for r in plan if r["action"] == "run")
    st.caption("Steps with their own screen (Intake, Sources, Stakeholders, Elicitation notes, Document type, Validation, "
               "Sign-off, Stories, UAT results) need your input, so a run continues only as far as the information you have provided.")
    if st.button(f"Generate drafts for the selected phases ({n_run} step{'s' if n_run != 1 else ''})", key="scope_go",
                 type="primary", disabled=n_run == 0):
        run_scope(plan)
        st.rerun()
    res = S.get("scope_last")
    if res:
        st.subheader("Last run")
        st.dataframe(pd.DataFrame(res), use_container_width=True, hide_index=True)
        made = [r["Step"] for r in res if r["Result"] == "Draft created"]
        if made:
            first = next(k for k in ORDER if ART[k]["title"] == made[0])
            st.success(f"{len(made)} draft(s) are ready for review. Approve them in order on each step's page.")
            st.button(f"Review first draft: {made[0]}", key="scope_rev", on_click=goto, args=(first,))
        if any(r["Result"] == "Failed" for r in res):
            st.error("The run stopped at the first failure. Drafts created before it are kept.")


# ================================================================ dashboard / change / deliver / audit
def page_dashboard():
    st.title("Lifecycle dashboard & router")
    r = route_scoped()
    (st.success if r["kind"] == "complete" else st.info)(f"**Router decision:** {r['reason']}")
    if r["next_key"]:
        st.button(f"Go to: {ART[r['next_key']]['title']}", type="primary", on_click=goto, args=(r["next_key"],))
    c1, c2 = st.columns([3, 1])
    ov = c1.selectbox("Override: jump to any step", ORDER, format_func=lambda k: f"{PHASE_TITLE[ART[k]['phase']]} > {ART[k]['title']}")
    if c2.button("Go anyway"):
        miss = missing_needs(S, ov)
        if miss:
            audit(S, "OVERRIDE", f"Jumped to '{ART[ov]['title']}' with unmet prerequisites")
        S.loc = ov
        st.rerun()
    st.subheader("Progress")
    for p in phase_progress(S):
        st.progress(p["done"] / p["total"], text=f"{p['title']}: {p['done']}/{p['total']} approved")
        with st.expander(f"Steps in {p['title']}"):
            for k in phase_artifacts(p["id"]):
                s = status(S, k)
                st.button(f"{ICON[s]}  {ART[k]['title']}  [{s}]", key=f"dash_{k}", on_click=goto, args=(k,))
    st.subheader("Open loop-backs")
    open_f = {n: f for n, f in S.flags.items() if f.get("open") and status(S, f["target"]) != "approved"}
    if open_f:
        for n, f in open_f.items():
            st.warning(f"[{n}] back to '{ART[f['target']]['title']}': {f['reason']}")
    else:
        st.write("None.")
    st.subheader("Jira readiness")
    for label, ok in story_readiness(S):
        st.write(("\u2714 " if ok else "\u2718 ") + label)


def page_change():
    st.title("Change control")
    if not S.outputs.get("scope"):
        st.warning("Change control starts once the scope exists.")
        return
    st.caption("A change request is analysed, the affected artifacts are flagged, and the router sends you back "
               "to the earliest affected step. Everything downstream becomes stale until re-approved.")
    req = st.text_area("Describe the change request", height=120, key="chg_req")
    if st.button("Analyse impact (agent)", type="primary", disabled=not req.strip()):
        try:
            with st.spinner("Analysing..."):
                avail = {k: ART[k]["title"] for k in ORDER if S.outputs.get(k)}
                ctx = (f"Project: {S.intake.get('project')}\nScope:\n{S.outputs.get('scope', '')}\n"
                       f"Existing artifacts (key: title): {json.dumps(avail)}\n"
                       f"Jira stories already created: {len(S.jira_created)}")
                out = llm_json("Assess this change request. Return JSON {\"summary\":\"\",\"severity\":\"low|medium|high\","
                               "\"impacted_artifacts\":[keys from the list that must be revisited],\"rationale\":\"\"}. "
                               "Only use keys from the list.\nCHANGE REQUEST: " + req, ctx)
                S["chg_draft"] = {"request": req, **out}
        except Exception as e:
            st.error(str(e))
    d = S.get("chg_draft")
    if d:
        st.write(f"**Summary:** {d.get('summary', '')}  |  **Severity:** {d.get('severity', '')}")
        st.write(f"**Rationale:** {d.get('rationale', '')}")
        opts = [k for k in ORDER if S.outputs.get(k)]
        sel = st.multiselect("Artifacts to revisit", opts, default=[k for k in d.get("impacted_artifacts", []) if k in opts],
                              format_func=lambda k: ART[k]["title"])
        if S.jira_created and any(k in sel for k in ("req_catalog", "stories", "prioritization")):
            st.warning(f"{len(S.jira_created)} Jira stories already exist. Update them in Jira or let the duplicate "
                       "check guide new stories.")
        if st.button("Accept change and route", disabled=not sel):
            tgt = change_target(sel)
            set_flag(S, "change", tgt, f"Change request: {d.get('summary', '')}", also_invalidate=sel)
            S.changes.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "request": d["request"],
                               "severity": d.get("severity"), "artifacts": sel})
            S.pop("chg_draft", None)
            S.loc = tgt
            st.rerun()
    if S.changes:
        st.subheader("Change log")
        st.dataframe(pd.DataFrame(S.changes), hide_index=True, use_container_width=True)


def build_deliverable(d):
    secs = {ART[k]["title"]: S.outputs[k] for k in d["artifacts"] if S.outputs.get(k)}
    sub = f"{S.intake.get('project', '')} | generated {date.today()} | DRAFT requires human validation"
    return build(d["format"], f"{S.intake.get('project', 'Project')} - {d['title']}", sub, secs)


def page_deliver():
    st.title("Deliverables & Email")
    avail = {k: ART[k]["title"] + ("" if status(S, k) == "approved" else " (not approved)")
             for k in ORDER if S.outputs.get(k)}
    if not avail:
        st.info("Nothing to export yet. Generate and approve some steps first.")
        return
    st.markdown("**Tell the agent what you need and in which format** (word, Excel or Markdown).")
    ask = st.text_area("Your request", height=80, key="dv_ask",
                        placeholder="e.g. the requirements document as word for the sponsor; requirements and user "
                                    "stories as Excel for the dev team")
    if st.button("Plan deliverables", type="primary", disabled=not ask.strip()):
        try:
            with st.spinner("Planning..."):
                dt = S.doc_decision.get("choice", "")
                out = llm_json(
                    "Turn the request into deliverables. Return JSON {\"deliverables\":[{\"title\":\"\","
                    "\"artifact_keys\":[],\"format\":\"word|excel|markdown|null\"}]}. Use only keys from the list. "
                    f"'req_doc' is the requirements document ({dt or 'type not chosen'}). Tables (requirements, "
                    "stories, prioritisation, RTM, risks) suit Excel; narrative suits Word. If the user did not say "
                    "which format a deliverable needs, set format to null.\nREQUEST: " + ask,
                    "Available artifacts (key: title): " + json.dumps(avail))
                fm = {"word": "Word (.docx)", "excel": "Excel (.xlsx)", "markdown": "Markdown (.md)"}
                for x in out.get("deliverables", []):
                    keys = [k for k in x.get("artifact_keys", []) if k in avail]
                    if keys:
                        S.dv_counter += 1
                        S.deliverables.append({"id": S.dv_counter, "title": x.get("title") or "Deliverable",
                                                "artifacts": keys, "format": fm.get(str(x.get("format")).lower(), "")})
        except Exception as e:
            st.error(str(e))
    if st.button("Add an empty deliverable"):
        S.dv_counter += 1
        S.deliverables.append({"id": S.dv_counter, "title": "Deliverable", "artifacts": [], "format": ""})
    choose = "- choose a format -"
    for d in list(S.deliverables):
        i = d["id"]
        with st.expander(d["title"] or "Deliverable", expanded=True):
            d["title"] = st.text_input("Title", d["title"], key=f"dv_t_{i}")
            d["artifacts"] = st.multiselect("Include", list(avail), default=[k for k in d["artifacts"] if k in avail],
                                             format_func=lambda k: avail[k], key=f"dv_a_{i}")
            opts = [choose] + list(FORMATS)
            cur = d["format"] if d["format"] in FORMATS else choose
            sel = st.selectbox("Format", opts, index=opts.index(cur), key=f"dv_f_{i}")
            d["format"] = "" if sel == choose else sel
            if not d["format"]:
                st.info("Which format do you want for this deliverable? Word suits documents, Excel suits tables.")
            elif d["artifacts"]:
                data, ext, mime = build_deliverable(d)
                st.download_button(f"Download .{ext}", data, file_name=f"{d['title'].replace(' ', '_')}.{ext}",
                                   mime=mime, key=f"dv_d_{i}")
            if st.button("Remove", key=f"dv_r_{i}"):
                S.deliverables = [x for x in S.deliverables if x["id"] != i]
                st.rerun()
    st.divider()
    st.subheader("Send deliverables for review or sign-off")
    ready = [d for d in S.deliverables if d["format"] and d["artifacts"]]
    seed = named_roles()
    if S.get("mail_df") is None:
        S["mail_df"] = pd.DataFrame({"Role": seed["Role"], "Name": seed["Name"], "Email": seed["Email"],
                                      "Review focus": ""}).reset_index(drop=True)
    S.mail_df = st.data_editor(S.mail_df, num_rows="dynamic", use_container_width=True, key="mail_editor")
    if S.informal:
        st.caption("Informal stakeholders found in sources: " + ", ".join(S.informal))
    to = [r for r in recs(S.mail_df) if str(r["Email"]).strip()]
    deadline = st.date_input("Review deadline", value=date.today() + timedelta(days=5))
    lines = ["Hello,", "", f"Please review the attached material for '{S.intake.get('project', '')}'."]
    lines += [f"- {r['Role']} ({r['Name']}): {r['Review focus'] or 'General review.'}" for r in to]
    lines += ["", "Please reply with comments or confirm approval.", "", "Regards,", "InitiateIQ on behalf of the BA"]
    lines += [f"- {r['Role']} ({r['Name']}): {r['Review focus'] or 'General review.'}" for r in to]
    subject = st.text_input("Subject", value=f"[Review by {deadline}] {S.intake.get('project', '')}")
    body = st.text_area("Body (editable)", value="\n".join(lines), height=230,
                         key=f"mail_body_{hash(tuple(str(r['Email']) for r in to))}_{deadline}")
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, S.smtp_from or "initiateiq@localhost"
    msg["To"] = ", ".join(str(r["Email"]).strip() for r in to)
    msg.set_content(body)
    for d in ready:
        data, ext, mime = build_deliverable(d)
        mt, st_ = mime.split("/")
        msg.add_attachment(data, maintype=mt, subtype=st_, filename=f"{d['title'].replace(' ', '_')}.{ext}")
    st.caption(f"{len(ready)} deliverable(s) will be attached.")
    st.download_button("Download email as .eml (dry run)", bytes(msg), file_name="review_email.eml", disabled=not to)
    ok = st.checkbox("I reviewed the recipients, body and attachments and approve sending")
    if st.button("Send email", type="primary", disabled=not (ok and to and S.smtp_host)):
        try:
            with smtplib.SMTP(S.smtp_host, int(S.smtp_port), timeout=30) as srv:
                if S.smtp_tls:
                    srv.starttls()
                if S.smtp_user:
                    srv.login(S.smtp_user, S.smtp_pass)
                srv.send_message(msg)
            audit(S, "EMAIL SENT", f"{subject} to {len(to)} recipient(s)")
            st.success(f"Sent to {len(to)} recipient(s).")
        except Exception as e:
            st.error(f"Send failed: {e}")
    if not S.smtp_host:
        st.info("Set the SMTP host in the sidebar to enable sending; until then use the .eml download.")


def page_audit():
    st.title("Audit trail")
    if S.audit:
        df = pd.DataFrame(S.audit)
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button("Download audit trail (.csv)", df.to_csv(index=False), file_name="audit_trail.csv")
    else:
        st.info("No events yet.")


# ================================================================ sidebar / persistence
def dump_project():
    data = {k: S[k] for k in STATE_KEYS}
    for k in EXT_KEYS:
        if k in S:
            data[k] = S[k]
    for k in DF_KEYS:
        df = S.get(k)
        data["_df_" + k] = None if df is None else json.loads(df.to_json(orient="records"))
    return json.dumps(data, indent=2, default=str)


def load_project(raw):
    data = json.loads(raw)
    for k in STATE_KEYS:
        if k in data:
            S[k] = data[k]
    for k in EXT_KEYS:
        if k in data:
            S[k] = data[k]
    for k in DF_KEYS:
        v = data.get("_df_" + k)
        S[k] = None if v is None else pd.DataFrame(v)
    for k in [x for x in list(S.keys()) if str(x).startswith("ta_")]:
        del S[k]
    S.pop("scope_w", None)
    for k in [x for x in list(S.keys()) if str(x).startswith("intake_")]:
        del S[k]                                   # intake boxes re-fill from the loaded intake
    S["_wsaved"] = dict(data.get("_wsaved", {}))   # typed-but-unsaved text that came with the file (autosave only)


# ---- keep the user's work across a browser refresh (Streamlit starts a new session on every refresh)
AUTOSAVE_FILE = os.environ.get("INITIATEIQ_AUTOSAVE") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  "autosave_project.json")
# plain text boxes whose content is kept. Passwords, tokens and API keys are deliberately NOT in this list.
PERSIST_WIDGETS = ("intake_project", "intake_domain", "intake_desc", "intake_focus", "intake_notes", "kw_input",
                   "src_paste", "uat_raw", "cf_base", "cf_ctx", "cf_auth", "cf_email", "cf_spaces", "jira_base",
                   "jira_auth", "jira_email", "jira_project", "smtp_host", "smtp_user", "smtp_from", "openai_base",
                   "model_custom", "req_kw", "uat_note")


def autoload():
    """First run of a new browser session: restore the last autosaved work."""
    if S.get("_autoload_done"):
        return
    try:
        if os.path.exists(AUTOSAVE_FILE):
            with open(AUTOSAVE_FILE, encoding="utf-8") as f:
                raw = f.read()
            load_project(raw)
            S["toast"] = "Restored your last session."
    except Exception:
        pass                                        # a damaged autosave must never stop the app
    S["_autoload_done"] = True


def restore_widget_text():
    """Streamlit forgets a box's text when you leave its page; put saved text back before the boxes are drawn."""
    for k, v in (S.get("_wsaved") or {}).items():
        if k in PERSIST_WIDGETS and k not in S:
            S[k] = v


def autosave():
    if not S.get("_autoload_done"):                 # state was just cleared (Start over): do not write
        return
    try:
        saved = S.setdefault("_wsaved", {})
        for k in PERSIST_WIDGETS:
            if k in S and isinstance(S[k], str):
                saved[k] = S[k]
        data = json.loads(dump_project())
        data["_wsaved"] = saved
        txt = json.dumps(data, default=str)
        h = hashlib.sha1(txt.encode("utf-8", "ignore")).hexdigest()
        if S.get("_autosave_hash") != h:
            tmp = AUTOSAVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            os.replace(tmp, AUTOSAVE_FILE)
            S["_autosave_hash"] = h
    except Exception:
        pass                                        # autosave is a convenience, never a reason to fail


def clear_autosave():
    try:
        if os.path.exists(AUTOSAVE_FILE):
            os.remove(AUTOSAVE_FILE)
    except Exception:
        pass


def art_label(k):
    return f"{ICON[status(S, k)]} {ART[k]['title']}"


def phase_nav_rows():
    """Per-phase progress for the navigation buttons (read-only; does not touch the router)."""
    return {p["id"]: p for p in phase_progress(S)}


def go_phase(pid):
    """Jump to a phase: land on its first step that is not yet approved (same landing rule as before)."""
    keys = phase_artifacts(pid)
    S.loc = next((k for k in keys if status(S, k) != "approved"), keys[0])


def sidebar():
    sb = st.sidebar
    if "TOOLS_ERR" in globals():
        sb.warning("Extra BA tools are disabled: " + TOOLS_ERR)
    sb.title("InitiateIQ")
    sb.caption("BA Lifecycle Orchestrator Agent")
    loc = S.loc
    cur_phase = ART[loc]["phase"] if loc in ART else None
    prog = phase_nav_rows()

    # ---- phase buttons: every phase is always one click away (navigation only; router logic is unchanged)
    sb.markdown("**Go to phase**")
    for p in PHASES:
        pr = prog[p["id"]]
        mark = ICON["approved"] if pr["total"] and pr["done"] == pr["total"] else ICON["todo"]
        is_cur = p["id"] == cur_phase
        off = " - not in scope" if scope_active() and p["id"] not in scope_phases() else ""
        sb.button(f"{mark} {p['title']}  ({pr['done']}/{pr['total']}){off}", key=f"navp_{p['id']}",
                  type="primary" if is_cur else "secondary", use_container_width=True,
                  disabled=is_cur, on_click=go_phase, args=(p["id"],))
    if scope_active():
        sb.caption(f"Scope: {len(scope_phases())} of {len(PHASES)} phases selected. Open 'Run selected phases' to change it.")
    sb.markdown("**Pages**")
    pcols = sb.columns(2)
    for i, (label, target) in enumerate(PAGES.items()):
        pcols[i % 2].button(label, key=f"navg_{target}", use_container_width=True,
                            type="primary" if loc == target else "secondary",
                            disabled=(loc == target), on_click=goto, args=(target,))

    def _on_art():
        m = {art_label(k): k for k in ORDER}
        if S.nav_art in m:
            S.loc = m[S.nav_art]

    if loc in ART:
        keys = phase_artifacts(ART[loc]["phase"])
        S.nav_art = art_label(loc)
        sb.selectbox("Step in this phase", [art_label(k) for k in keys], key="nav_art", on_change=_on_art)
    r = route_scoped()
    if r["next_key"]:
        sb.info(f"**Router:** {r['reason']}")
        if loc != r["next_key"]:
            sb.button("Go to recommended step", on_click=goto, args=(r["next_key"],))
    else:
        sb.success(r["reason"])
    key_ready = bool(_api_key())
    with sb.expander("OpenAI", expanded=not (S.get("model") and key_ready)):
        st.text_input("API key", type="password", key="openai_key", placeholder="sk-...",
                      help="Or set the OPENAI_API_KEY environment variable before starting Streamlit. "
                           "The key is kept only in this browser session and is not saved in exports.")
        if os.environ.get("OPENAI_API_KEY") and not S.get("openai_key"):
            st.caption("Using OPENAI_API_KEY from the environment.")
        S.setdefault("openai_base", "https://api.openai.com/v1")
        st.text_input("Base URL", key="openai_base",
                      help="Change only for Azure OpenAI or an OpenAI-compatible gateway.")
        models = DEFAULT_MODELS
        if key_ready:
            try:
                models = list_models(_api_base(), _api_key())
            except Exception:
                st.caption("Could not fetch the model list; showing common models.")
        st.selectbox("Model", models, key="model_pick")
        st.text_input("Or type a model name (overrides the list)", key="model_custom")
        S["model"] = (S.get("model_custom") or "").strip() or S.get("model_pick")
        st.slider("Temperature", 0.0, 1.0, 0.2, 0.05, key="temperature",
                  help="Ignored automatically for models that do not support it.")
        st.number_input("Context budget (tokens, for the size warning)", 4096, 1000000, 100000, 4096, key="num_ctx")
        st.number_input("Max characters per source document", 500, 30000, 4000, 500, key="doc_chars")
        st.number_input("Max output tokens per draft", 512, 32768, 8192, 512, key="max_out",
                        help="Reasoning models (o-series, gpt-5) spend part of this on thinking, so keep it high for them.")
        st.number_input("Request timeout (seconds)", 30, 1800, 300, 30, key="req_timeout")
    with sb.expander("Confluence (read-only)"):
        st.text_input("Base URL", placeholder="https://yourco.atlassian.net", key="cf_base")
        S.setdefault("cf_ctx", "/wiki")
        st.text_input("Context path", key="cf_ctx", help="Cloud: /wiki. Server/DC: usually empty.")
        st.selectbox("Auth type", ["Cloud (email + API token)", "Server/DC (personal access token)"], key="cf_auth")
        st.text_input("Email (Cloud only)", key="cf_email")
        st.text_input("API token / PAT", type="password", key="cf_token")
        st.text_input("Space keys (comma separated, blank = all)", key="cf_spaces")
        st.number_input("Max pages", 1, 50, 10, key="cf_limit")
        st.number_input("Mark stale after (days)", 30, 3650, 365, key="stale_days")
    with sb.expander("Jira (creates user stories)"):
        st.text_input("Base URL", placeholder="https://yourco.atlassian.net", key="jira_base")
        st.selectbox("Auth type", ["Cloud (email + API token)", "Server/DC (personal access token)"], key="jira_auth")
        st.text_input("Email (Cloud only)", key="jira_email")
        st.text_input("API token / PAT", type="password", key="jira_token")
        st.text_input("Project key", key="jira_project")
    with sb.expander("Email (SMTP)"):
        st.text_input("SMTP host", key="smtp_host")
        st.number_input("Port", 1, 65535, 587, key="smtp_port")
        st.checkbox("Use STARTTLS", value=True, key="smtp_tls")
        st.text_input("Username", key="smtp_user")
        st.text_input("Password", type="password", key="smtp_pass")
        st.text_input("From address", key="smtp_from")
    with sb.expander("Save / load project"):
        st.caption("The file holds project content only. Credentials are never saved.")
        name = (S.intake.get("project") or "project").replace(" ", "_")
        st.download_button("Save project (.json)", dump_project(), file_name=f"initiateiq_{name}.json")
        up = st.file_uploader("Load project", type=["json"], key="load_up")
        if up is not None and st.button("Load"):
            load_project(up.read().decode("utf-8"))
            st.rerun()
    if sb.button("Start over"):
        clear_autosave()
        for k in list(S.keys()):
            del S[k]
        st.rerun()
    sb.caption("Your work is saved automatically and comes back after a browser refresh. Press Ctrl+Enter in a box "
               "before refreshing so the last text is registered.")


# ================================================================ main
SPECIAL = {"intake": page_intake, "sources": page_sources, "stakeholders": page_stakeholders,
           "elic_notes": page_elic_notes, "doctype": page_doctype, "validation": page_validation,
           "signoff": page_signoff, "stories": page_stories, "uat_results": page_uat_results}


def main():
    autoload()
    try:
        _main()
    finally:
        autosave()


def _main():
    restore_widget_text()
    hide_record_screen_item()
    init_state()
    sidebar()
    if S.get("toast"):
        st.toast(S.pop("toast"))
    loc = S.loc
    if loc == "_dashboard":
        page_dashboard()
    elif loc == "_scope":
        page_scope()
    elif loc == "_change":
        page_change()
    elif loc == "_deliver":
        page_deliver()
    elif loc == "_audit":
        page_audit()
    elif loc in TOOL_PAGES.values() and render_tool:
        render_tool(loc, S, llm, llm_json)
    elif loc in ART:
        a = ART[loc]
        st.title(a["title"])
        SPECIAL.get(loc, page_llm)(a)
    else:
        S.loc = "intake"
        st.rerun()


main()
