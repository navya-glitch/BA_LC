"""
ba_tools_ui.py - Streamlit pages for the extra BA tools. Additive: the main app only calls render_tool(), and any
error inside a tool is shown on that page instead of stopping the app.

Pages:  Requirements register | Knowledge base | Diagrams | Data mapping & JSON
The main app passes in its own llm() / llm_json() so the model, key and settings stay in one place.
"""
import base64
import io
import json
import os
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

import ba_analysis as an
import ba_kb as kb

try:
    from ba_engine import ART, audit as _audit
except Exception:                      # tools must never depend on the engine to load
    ART, _audit = {}, None

TOOL_PAGES = {"Requirements register": "_x_reqs", "Knowledge base": "_x_kb", "Diagrams": "_x_diag",
              "Data mapping & JSON": "_x_map", "Screen mock-up": "_x_mock"}
EXT_KEYS = ["x_kb", "x_reqs", "x_diagrams", "x_maps", "x_enh", "x_proc", "x_cur", "x_cmp", "x_mocks", "x_mock_cur",
            "x_mk_prompt", "x_dg_topic", "x_rq_text", "x_rq_name", "x_ja_v", "x_jb_v", "x_ms_v", "x_mt_v", "x_impact",
            "x_jsum", "x_cands"]
cc = st.column_config


# ------------------------------------------------------------------ helpers
class Ctx:
    def __init__(self, S, llm, llm_json):
        self.S, self.llm, self.llm_json = S, llm, llm_json

    def log(self, event, detail=""):
        try:
            if _audit:
                _audit(self.S, event, detail)
        except Exception:
            pass


def ensure_state(S):
    S.setdefault("x_kb", kb.new_kb())
    S.setdefault("x_reqs", [])
    S.setdefault("x_diagrams", [])
    S.setdefault("x_maps", [])
    S.setdefault("x_enh", "")
    S.setdefault("x_proc", {})
    S.setdefault("x_cur", {})
    S.setdefault("x_mocks", [])
    S.setdefault("x_mock_cur", None)


def _recs(df):
    if df is None or not hasattr(df, "fillna"):
        return []
    return df.fillna("").to_dict("records")


def _xlsx(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    return buf.getvalue()


def _kb_doc_text(doc):
    return "\n\n".join(c["text"] for c in doc["chunks"])


def _kb_context(c, query, k):
    hits = kb.search(c.S.x_kb, query, k=k)
    return hits, kb.format_context(hits)


def _req_lines(reqs, limit=80):
    return "\n".join(f"{r['id']} [{r['type']}] {r['statement']}" for r in reqs[:limit])


def _goto(loc):
    st.session_state["loc"] = loc


def _need_kb_note():
    st.info("The knowledge base is empty. Add documents on the **Knowledge base** page for better, cited results. "
            "You can still continue using only what you type here.")


# ================================================================== KNOWLEDGE BASE
def page_kb(c):
    S = c.S
    K = S.x_kb
    st.caption("Add project documents once. Every tool can then search them and cite the exact source, for example [K2-3].")
    s = kb.stats(K)
    m1, m2, m3 = st.columns(3)
    m1.metric("Documents", s["docs"])
    m2.metric("Searchable chunks", s["chunks"])
    m3.metric("Characters", f"{s['chars']:,}")
    t_add, t_search, t_ask, t_enh = st.tabs(["Add documents", "Search", "Ask the knowledge base", "Enhancement impact"])

    with t_add:
        dtype = st.selectbox("Document type", kb.DOC_TYPES, key="x_kb_type")
        ups = st.file_uploader("Upload files (txt, md, csv, json, html, docx, xlsx, pdf)", accept_multiple_files=True,
                               type=["txt", "md", "csv", "json", "html", "htm", "docx", "xlsx", "pdf", "eml", "tsv"],
                               key="x_kb_up")
        if st.button("Add uploaded files", key="x_kb_addup", disabled=not ups):
            added, notes = 0, []
            for f in ups:
                try:
                    txt = kb.extract_text(f.name, f.getvalue())
                    _, new = kb.add_doc(K, f.name, txt, dtype)
                    added += new
                    if not new:
                        notes.append(f"{f.name}: already in the knowledge base.")
                except ValueError as e:
                    notes.append(f"{f.name}: {e}")
            if added:
                c.log("Knowledge base", f"{added} document(s) added")
                st.success(f"Added {added} document(s).")
            for n in notes:
                st.warning(n)
        with st.expander("Or paste text (email, notes, meeting minutes)"):
            pname = st.text_input("Name", key="x_kb_pname", placeholder="e.g. Email from Compliance 25 Feb")
            ptxt = st.text_area("Text", height=160, key="x_kb_ptxt")
            if st.button("Add pasted text", key="x_kb_addp", disabled=not (pname.strip() and ptxt.strip())):
                _, new = kb.add_doc(K, pname.strip(), ptxt, dtype)
                st.success("Added." if new else "Already in the knowledge base.")
        with st.expander("Import what is already in this project"):
            st.caption("Copies Confluence pages, source documents and elicitation notes from the earlier steps.")
            inc_app = st.checkbox("Also include approved deliverables", value=False, key="x_kb_incapp")
            if st.button("Import from project", key="x_kb_imp"):
                n = 0
                for p in S.get("pages", []):
                    n += kb.add_doc(K, f"Confluence: {p.get('title', '')}", p.get("body", ""), "Process document")[1]
                for d in S.get("docs", []):
                    n += kb.add_doc(K, d.get("name", "source"), d.get("text", ""), "Other")[1]
                for d in S.get("elic_docs", []):
                    n += kb.add_doc(K, d.get("name", "notes"), d.get("text", ""), "Meeting notes")[1]
                if inc_app:
                    for key, txt in (S.get("approved_text") or {}).items():
                        n += kb.add_doc(K, f"Approved: {ART.get(key, {}).get('title', key)}", txt, "Requirements")[1]
                c.log("Knowledge base", f"{n} project document(s) imported")
                st.success(f"Imported {n} new document(s).")
        if K["docs"]:
            st.markdown("**Documents in the knowledge base**")
            st.dataframe(pd.DataFrame([{"ID": d["id"], "Name": d["name"], "Type": d["type"], "Added": d["added"],
                                        "Chunks": len(d["chunks"]), "Characters": d["chars"]} for d in K["docs"]]),
                         use_container_width=True, hide_index=True)
            rc1, rc2 = st.columns([3, 1])
            rm = rc1.selectbox("Remove a document", [f"{d['id']} - {d['name']}" for d in K["docs"]], key="x_kb_rm")
            if rc2.button("Remove", key="x_kb_rmbtn"):
                kb.remove_doc(K, rm.split(" - ")[0])
                c.log("Knowledge base", f"removed {rm}")
                st.success("Removed. Open another tab and come back to refresh the list.")

    with t_search:
        q = st.text_input("Search", key="x_kb_q", placeholder="e.g. schedule rating cap Florida")
        k = st.slider("Results", 3, 15, 6, key="x_kb_k")
        if q.strip():
            hits = kb.search(K, q, k=k)
            if not hits:
                st.warning("Nothing found. Try other words or add more documents.")
            for h in hits:
                with st.expander(f"[{h['chunk_id']}] {h['doc']}" + (f" - {h['section']}" if h["section"] else "") +
                                 f"   (score {h['score']})"):
                    st.text(h["text"])

    with t_ask:
        st.caption("The answer is built only from retrieved excerpts and must cite them. Anything not found is reported as missing.")
        question = st.text_area("Your question", key="x_kb_qa", height=90,
                                placeholder="e.g. Which states cap schedule rating below 25% and why?")
        if st.button("Answer from knowledge base", key="x_kb_ask", type="primary", disabled=not question.strip()):
            hits, ctx = _kb_context(c, question, 8)
            if not hits:
                st.warning("No relevant excerpts found in the knowledge base.")
            else:
                try:
                    with st.spinner("Reading the excerpts..."):
                        ans = c.llm("Answer the QUESTION using ONLY the CONTEXT. Cite every statement with its [K#-#] tag. "
                                    "If the context does not contain the answer, say 'Not found in the knowledge base' and list what "
                                    "is missing. Do not guess.\nQUESTION: " + question, ctx)
                    st.markdown(ans)
                    cited, valid = kb.cited_ids(ans), kb.all_chunk_ids(K)
                    bad = [x for x in cited if x not in valid]
                    if bad:
                        st.warning("These citations do not exist in the knowledge base, so ignore them: " + ", ".join(bad))
                    with st.expander("Excerpts used"):
                        for h in hits:
                            st.markdown(f"**[{h['chunk_id']}]** {h['doc']}")
                            st.text(h["text"][:600])
                except Exception as e:
                    st.error(str(e))

    with t_enh:
        st.caption("Describe a new enhancement or change. The agent finds what the documents say about the affected areas and "
                   "lists the impact. Then build As-Is and To-Be diagrams from it.")
        S.x_enh = st.text_area("Enhancement / change description", value=S.get("x_enh", ""), height=130, key="x_enh_w",
                               placeholder="e.g. Florida reduces the schedule rating cap from 25% to 15% from 1 Jan 2028 ...")
        if st.button("Analyse impact", key="x_enh_go", type="primary", disabled=not S.x_enh.strip()):
            hits, ctx = _kb_context(c, S.x_enh, 10)
            if not hits:
                _need_kb_note()
            reg = _req_lines(S.x_reqs)
            try:
                with st.spinner("Analysing impact..."):
                    task = ("Write an IMPACT ANALYSIS of the ENHANCEMENT using only the CONTEXT (knowledge base excerpts and the "
                            "requirements register). Use these markdown sections: ## Summary of the change, ## Affected processes, "
                            "## Affected business rules, ## Affected systems and interfaces, ## Affected data, "
                            "## Affected requirements (use RQ-ids from the register), ## Test impact, ## Missing information and "
                            "open questions, ## Suggested diagrams. Cite sources with [K#-#]. When the context has nothing on an "
                            "area write 'Not found in sources' - never guess.\nENHANCEMENT: " + S.x_enh)
                    S["x_impact"] = c.llm(task, (ctx + ("\n\nREQUIREMENTS REGISTER:\n" + reg if reg else "")).strip())
                c.log("Impact analysis", S.x_enh[:80])
            except Exception as e:
                st.error(str(e))
        if S.get("x_impact"):
            st.markdown(S["x_impact"])
            d1, d2 = st.columns(2)
            d1.download_button("Download impact analysis (.md)", S["x_impact"], file_name="impact_analysis.md", key="x_enh_dl")
            d2.button("Create As-Is / To-Be diagrams", key="x_enh_togo", on_click=_goto, args=("_x_diag",))


# ================================================================== REQUIREMENTS
def page_reqs(c):
    S = c.S
    t_ex, t_reg, t_tr, t_q = st.tabs(["1. Extract", "2. Register", "3. Traceability", "4. Quality checks"])

    with t_ex:
        st.caption("Paste or upload raw requirements, emails or notes. The agent splits them into separate items, quotes the "
                   "source sentence and flags vague, duplicate or unmeasurable ones. Nothing enters the register until you accept it.")
        src = st.radio("Source", ["Paste text", "Upload file", "Knowledge-base document"], horizontal=True, key="x_rq_src")
        name, text, to_kb = "Pasted notes", "", False
        if src == "Paste text":
            name = st.text_input("Name of this source", value=S.get("x_rq_name", "Pasted notes"), key="x_rq_name_w")
            S["x_rq_name"] = name
            S["x_rq_text"] = st.text_area("Paste requirements, emails or meeting notes", value=S.get("x_rq_text", ""),
                                          height=220, key="x_rq_text_w")
            text = S["x_rq_text"]
            to_kb = st.checkbox("Also add this text to the knowledge base", value=True, key="x_rq_tokb")
        elif src == "Upload file":
            f = st.file_uploader("File", type=["txt", "md", "csv", "json", "html", "docx", "xlsx", "pdf", "eml"], key="x_rq_up")
            if f:
                try:
                    name, text = f.name, kb.extract_text(f.name, f.getvalue())
                    st.caption(f"Read {len(text):,} characters from {f.name}.")
                except ValueError as e:
                    st.error(str(e))
            to_kb = st.checkbox("Also add this file to the knowledge base", value=True, key="x_rq_tokb2")
        else:
            docs = S.x_kb["docs"]
            if not docs:
                st.info("The knowledge base is empty.")
            else:
                pick = st.selectbox("Document", [f"{d['id']} - {d['name']}" for d in docs], key="x_rq_kbdoc")
                d = next(x for x in docs if x["id"] == pick.split(" - ")[0])
                name, text = d["name"], _kb_doc_text(d)
        if st.button("Extract requirements with agent", key="x_rq_go", type="primary", disabled=not text.strip()):
            try:
                wins = an.split_windows(text)
                items, bar = [], st.progress(0.0, text="Reading...")
                for i, w in enumerate(wins, 1):
                    res = c.llm_json(an.prompt_extract(name), f"SOURCE TEXT (part {i} of {len(wins)}):\n{w}")
                    items += res.get("items", []) if isinstance(res, dict) else []
                    bar.progress(i / len(wins), text=f"Part {i} of {len(wins)} done")
                bar.empty()
                S["x_cands"] = an.normalize_items(items, text, name, S.x_reqs)
                S["x_cand_n"] = S.get("x_cand_n", 0) + 1
                if to_kb and src != "Knowledge-base document":
                    kb.add_doc(S.x_kb, name, text, "Requirements")
                c.log("Requirement extraction", f"{len(S['x_cands'])} candidates from {name}")
                if not S["x_cands"]:
                    st.warning("The agent found no requirement-like statements in this text.")
            except Exception as e:
                st.error(str(e))
        cands = S.get("x_cands") or []
        if cands:
            st.markdown(f"**{len(cands)} candidates - review, edit and accept**")
            st.caption("'Quote OK' means the supporting sentence was found word-for-word in your source. "
                       "Rows already similar to the register are unticked.")
            df = pd.DataFrame([{"Accept": not r["duplicate_of"], "Type": r["type"], "Statement": r["statement"],
                                "Priority": r["priority"], "Quote OK": r["quote_verified"],
                                "Flags": " | ".join(r["flags"]), "Similar to": r["duplicate_of"], "Source quote": r["quote"]}
                               for r in cands])
            ed = st.data_editor(df, key=f"x_cand_ed_{S.get('x_cand_n', 0)}", use_container_width=True, hide_index=True,
                                disabled=["Quote OK", "Flags", "Similar to", "Source quote"],
                                column_config={"Accept": cc.CheckboxColumn(width="small"),
                                               "Type": cc.SelectboxColumn(options=an.REQ_TYPES),
                                               "Priority": cc.SelectboxColumn(options=an.PRIORITIES),
                                               "Statement": cc.TextColumn(width="large"),
                                               "Flags": cc.TextColumn(width="medium"),
                                               "Source quote": cc.TextColumn(width="medium")})
            b1, b2 = st.columns(2)
            if b1.button("Add accepted to register", key="x_cand_add", type="primary"):
                n = an.next_req_number(S.x_reqs)
                added = 0
                for orig, row in zip(cands, ed.to_dict("records")):
                    if row["Accept"] and str(row["Statement"]).strip():
                        S.x_reqs.append({**orig, "id": f"RQ-{n:03d}", "type": row["Type"], "statement": str(row["Statement"]).strip(),
                                         "priority": row["Priority"] or ""})
                        n += 1
                        added += 1
                S["x_cands"] = []
                c.log("Requirements register", f"{added} requirement(s) added")
                st.success(f"Added {added} to the register. Open the Register tab.")
            if b2.button("Discard these candidates", key="x_cand_drop"):
                S["x_cands"] = []
                st.info("Discarded.")

    with t_reg:
        reqs = S.x_reqs
        if not reqs:
            st.info("The register is empty. Extract requirements first, or add one manually below.")
        else:
            cnt = pd.Series([r["status"] for r in reqs]).value_counts().to_dict()
            m = st.columns(5)
            m[0].metric("Total", len(reqs))
            m[1].metric("Approved", cnt.get("Approved", 0))
            m[2].metric("Proposed", cnt.get("Proposed", 0))
            m[3].metric("With flags", sum(1 for r in reqs if r["flags"]))
            m[4].metric("Must", sum(1 for r in reqs if r["priority"] == "Must"))
            f1, f2, f3 = st.columns(3)
            ft = f1.multiselect("Type", an.REQ_TYPES, key="x_rg_ft")
            fs = f2.multiselect("Status", an.STATUSES, key="x_rg_fs")
            fq = f3.text_input("Contains", key="x_rg_fq")
            view = [r for r in reqs if (not ft or r["type"] in ft) and (not fs or r["status"] in fs)
                    and (not fq or fq.lower() in (r["statement"] + r["id"]).lower())]
            df = pd.DataFrame([{"ID": r["id"], "Type": r["type"], "Statement": r["statement"], "Priority": r["priority"],
                                "Status": r["status"], "Stakeholder": r.get("stakeholder", ""), "Notes": r.get("notes", ""),
                                "Flags": " | ".join(r["flags"]), "Source": r["source"], "Delete": False} for r in view])
            ed = st.data_editor(df, key=f"x_reg_ed_{abs(hash((tuple(ft), tuple(fs), fq, len(reqs), S.get('x_reg_gen', 0)))) % 10**8}",
                                use_container_width=True, hide_index=True,
                                disabled=["ID", "Flags", "Source"],
                                column_config={"Type": cc.SelectboxColumn(options=an.REQ_TYPES),
                                               "Priority": cc.SelectboxColumn(options=an.PRIORITIES),
                                               "Status": cc.SelectboxColumn(options=an.STATUSES),
                                               "Statement": cc.TextColumn(width="large"),
                                               "Delete": cc.CheckboxColumn(width="small")})
            if st.button("Save changes", key="x_reg_save", type="primary"):
                byid = {r["id"]: r for r in reqs}
                for row in ed.to_dict("records"):
                    r = byid.get(row["ID"])
                    if r is None:
                        continue
                    r.update(type=row["Type"], statement=str(row["Statement"]).strip(), priority=row["Priority"] or "",
                             status=row["Status"], stakeholder=str(row["Stakeholder"] or ""), notes=str(row["Notes"] or ""))
                    r["flags"] = [f for f in r["flags"] if f.startswith(("Quote not", "Unclear"))] + \
                        an.quality_flags(r["statement"], r["type"])
                gone = {row["ID"] for row in ed.to_dict("records") if row["Delete"]}
                S.x_reqs[:] = [r for r in S.x_reqs if r["id"] not in gone]
                S["x_reg_gen"] = S.get("x_reg_gen", 0) + 1
                c.log("Requirements register", f"saved edits, {len(gone)} deleted")
                st.success("Saved.")
            full = pd.DataFrame([{"ID": r["id"], "Type": r["type"], "Statement": r["statement"], "Priority": r["priority"],
                                  "Status": r["status"], "Stakeholder": r.get("stakeholder", ""), "Source": r["source"],
                                  "Source quote": r["quote"], "Quote verified": r["quote_verified"],
                                  "Flags": " | ".join(r["flags"]), "Notes": r.get("notes", "")} for r in reqs])
            e1, e2, e3 = st.columns(3)
            e1.download_button("Download Excel", _xlsx({"Requirements": full}), file_name="requirements_register.xlsx", key="x_reg_x")
            e2.download_button("Download CSV", full.to_csv(index=False), file_name="requirements_register.csv", key="x_reg_c")
            e3.download_button("Download Markdown", an.rows_to_md(list(full.columns[:7]), full.iloc[:, :7].astype(str).values.tolist()),
                               file_name="requirements_register.md", key="x_reg_m")
        with st.expander("Add a requirement manually"):
            mt = st.selectbox("Type", an.REQ_TYPES, key="x_man_t")
            ms = st.text_area("Statement", key="x_man_s", height=80)
            mp = st.selectbox("Priority", an.PRIORITIES, key="x_man_p")
            if st.button("Add", key="x_man_add", disabled=len(ms.strip()) < 8):
                S.x_reqs.append({"id": f"RQ-{an.next_req_number(S.x_reqs):03d}", "type": mt, "statement": ms.strip(), "priority": mp,
                                 "status": "Proposed", "source": "Manual", "quote": "", "quote_verified": False, "stakeholder": "",
                                 "flags": an.quality_flags(ms, mt), "duplicate_of": "", "notes": ""})
                st.success("Added.")

    with t_tr:
        if not S.x_reqs:
            st.info("Add requirements to the register first.")
        else:
            rows = an.trace_matrix(S.x_reqs, _recs(S.get("stories_df")), _recs(S.get("defects_df")), _recs(S.get("findings_df")))
            df = pd.DataFrame(rows)
            m = st.columns(3)
            m[0].metric("Requirements", len(df))
            m[1].metric("With at least one story", int((df["Stories"] > 0).sum()))
            m[2].metric("Without a story", int((df["Coverage"] == "No story").sum()))
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("Links are found by searching the Stories page ('Req IDs' column), the defect list and validation findings "
                       "for the register ID, for example RQ-004. Write register IDs there to connect them.")
            st.download_button("Download traceability matrix (.xlsx)", _xlsx({"Traceability": df}), file_name="traceability_matrix.xlsx",
                               key="x_tr_x")

    with t_q:
        reqs = S.x_reqs
        flagged = [{"ID": r["id"], "Type": r["type"], "Statement": r["statement"][:100], "Flags": " | ".join(r["flags"])}
                   for r in reqs if r["flags"]]
        st.markdown(f"**Automatic checks:** {len(flagged)} of {len(reqs)} requirements have flags (vague words, no measure, "
                    "compound, unverified quote).")
        if flagged:
            st.dataframe(pd.DataFrame(flagged), use_container_width=True, hide_index=True)
        if st.button("Find conflicts, duplicates and gaps with agent", key="x_q_go", disabled=len(reqs) < 2):
            try:
                ctx = f"PROJECT: {S.get('intake', {}).get('description', '')[:800]}\n\nREQUIREMENTS REGISTER:\n{_req_lines(reqs, 150)}"
                with st.spinner("Reviewing the register..."):
                    S["x_quality"] = an.normalize_conflicts(c.llm_json(an.prompt_conflicts(), ctx), reqs)
                c.log("Requirements quality review", f"{len(S['x_quality'])} finding(s)")
            except Exception as e:
                st.error(str(e))
        if S.get("x_quality") is not None and "x_quality" in S:
            if S["x_quality"]:
                st.dataframe(pd.DataFrame(S["x_quality"]), use_container_width=True, hide_index=True)
            else:
                st.success("The agent found no conflicts, duplicates or gaps.")


# ================================================================== DIAGRAMS
KINDS = {"process": "As-Is / To-Be process flow", "fishbone": "Fishbone (cause and effect)", "whys": "5 Whys",
         "context": "Context diagram", "state": "State diagram", "decision": "Decision table",
         "sequence": "Sequence diagram (Mermaid text)"}
TOPIC_HINT = {"process": "Process name, e.g. BOP quote to bind including rating",
              "fishbone": "Problem statement, e.g. Premiums differ from legacy on 0.9% of sampled policies",
              "whys": "Problem, e.g. Renewal showed +18% instead of the 12% cap",
              "context": "System in scope, e.g. Guidewire BOP rating",
              "state": "Entity, e.g. BOP policy / endorsement lifecycle",
              "decision": "Rules to tabulate, e.g. BOP eligibility and schedule rating approval",
              "sequence": "Interaction, e.g. Quote screen calls rating and billing"}


def _diagram_context(c, kind, topic, use_kb, k, use_reqs, use_defects):
    parts, hits = [], []
    if use_kb and c.S.x_kb["docs"]:
        hits, ctx = _kb_context(c, topic, k)
        if ctx:
            parts.append("KNOWLEDGE BASE EXCERPTS:\n" + ctx)
    if use_reqs and c.S.x_reqs:
        parts.append("REQUIREMENTS REGISTER:\n" + _req_lines(c.S.x_reqs))
    if use_defects:
        d = _recs(c.S.get("defects_df"))
        if d:
            parts.append("DEFECTS:\n" + "\n".join(f"[D-{x.get('id', '')}] {x.get('title', '')} (severity {x.get('severity', '')}, "
                                                  f"root cause {x.get('root_cause', '')})" for x in d[:60]))
    intake = c.S.get("intake", {})
    if intake.get("description"):
        parts.append("PROJECT: " + intake["description"][:600])
    return "\n\n".join(parts), hits


def _show_dot(dot, key):
    st.graphviz_chart(dot, use_container_width=True)
    st.download_button("Download diagram source (.dot)", dot, file_name=f"{key}.dot", key=f"x_dl_{key}")


def _spec_editor(kind, spec, clean, key):
    with st.expander("Edit the diagram data (JSON)"):
        txt = st.text_area("Change labels, add steps or fix links, then apply.", value=json.dumps(spec, indent=2), height=260,
                           key=f"x_ed_{key}_{hash(json.dumps(spec, sort_keys=True)) % 10**8}")
        if st.button("Apply edits", key=f"x_ap_{key}"):
            try:
                return clean(an.parse_json_loose(txt))
            except ValueError as e:
                st.error(str(e))
    return None


def _render(kind, spec, notes=None):
    """Draw a cleaned spec of any kind."""
    for n in notes or []:
        st.warning(n)
    if kind == "fishbone":
        components.html(an.fishbone_svg(spec), height=600, scrolling=True)
        rows = an.fishbone_rows(spec)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        hyp = sum(1 for r in rows if "HYPOTHESIS" in r["Evidence"].upper())
        if hyp:
            st.caption(f"{hyp} cause(s) are hypotheses with no document evidence - verify them before acting.")
        st.download_button("Download fishbone (.svg)", an.fishbone_svg(spec), file_name="fishbone.svg", key="x_dl_fish")
    elif kind == "whys":
        _show_dot(an.whys_dot(spec), "five_whys")
        st.dataframe(pd.DataFrame(spec["whys"]), use_container_width=True, hide_index=True)
    elif kind == "context":
        _show_dot(an.context_dot(spec), "context")
        st.dataframe(pd.DataFrame(spec["flows"]), use_container_width=True, hide_index=True)
    elif kind == "state":
        _show_dot(an.state_dot(spec), "state")
        st.dataframe(pd.DataFrame(spec["transitions"]), use_container_width=True, hide_index=True)
    elif kind == "decision":
        rows = an.decision_rows(spec)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.download_button("Download decision table (.md)", an.decision_md(spec), file_name="decision_table.md", key="x_dl_dec")
    elif kind == "sequence":
        mm = an.sequence_mermaid(spec)
        st.code(mm, language="text")
        st.caption("Paste this into mermaid.live or a Mermaid macro in Confluence / GitHub to see the diagram.")
        st.download_button("Download (.mmd)", mm, file_name="sequence.mmd", key="x_dl_seq")


CLEANERS = {"fishbone": an.clean_fishbone, "whys": an.clean_whys, "context": an.clean_context, "state": an.clean_state,
            "decision": an.clean_decision, "sequence": an.clean_sequence}
PROMPTS = {"fishbone": lambda t, d: an.prompt_fishbone(t, d), "whys": lambda t, d: an.prompt_whys(t),
           "context": lambda t, d: an.prompt_context(t), "state": lambda t, d: an.prompt_state(t),
           "decision": lambda t, d: an.prompt_decision(t), "sequence": lambda t, d: an.prompt_sequence(t)}


def page_diag(c):
    S = c.S
    with st.expander("Which diagram should I use, and when?", expanded=False):
        st.dataframe(pd.DataFrame(an.DIAGRAM_GUIDE), use_container_width=True, hide_index=True)
        st.markdown(
            "**Fishbone:** use it when you know the problem but not the cause, and want the team to think across categories "
            "(people, process, systems, data, rules, environment) instead of jumping to the first idea. Typical moments: UAT defect "
            "clusters, parity differences after migration, repeated production incidents.\n\n"
            "**5 Whys:** use it when one specific failure has a single chain of causes. If several causes branch out, use a fishbone.")
    kind = st.selectbox("Diagram", list(KINDS), format_func=lambda k: KINDS[k], key="x_dg_kind")
    c1, c2, c3 = st.columns(3)
    use_kb = c1.checkbox("Use knowledge base", value=bool(S.x_kb["docs"]), key="x_dg_kb")
    k = c2.slider("Excerpts", 4, 14, 8, key="x_dg_k")
    use_req = c3.checkbox("Use requirements register", value=bool(S.x_reqs), key="x_dg_req")
    use_def = kind == "fishbone" and st.checkbox("Include UAT defects from this project", value=S.get("defects_df") is not None,
                                                 key="x_dg_def")
    if use_kb and not S.x_kb["docs"]:
        _need_kb_note()

    S["x_dg_topic"] = st.text_area("Topic" if kind != "process" else "Process (current state)", value=S.get("x_dg_topic", ""),
                                   height=80, key="x_dg_topic_w", placeholder=TOPIC_HINT[kind])
    topic = S["x_dg_topic"]

    if kind == "process":
        S.x_enh = st.text_area("Enhancement / change (for the To-Be)", value=S.get("x_enh", ""), height=100, key="x_dg_enh_w",
                               placeholder="What will change? e.g. Rate lookups move to Guidewire rate books; FL cap drops to 15%")
        b1, b2 = st.columns(2)
        proc = S.x_proc
        if b1.button("1. Generate As-Is", key="x_dg_asis", type="primary", disabled=not topic.strip()):
            try:
                ctx, hits = _diagram_context(c, kind, topic, use_kb, k, use_req, False)
                with st.spinner("Drawing the current process..."):
                    res = c.llm_json(an.prompt_process("asis", topic), ctx)
                spec, notes = an.clean_process_spec(res)
                S.x_proc = {"asis": spec, "tobe": None, "notes": notes, "topic": topic}
                c.log("Diagram", f"As-Is: {topic[:60]}")
            except (ValueError, RuntimeError) as e:
                st.error(str(e))
        proc = S.x_proc
        if b2.button("2. Generate To-Be from As-Is + enhancement", key="x_dg_tobe", disabled=not (proc.get("asis") and S.x_enh.strip())):
            try:
                ctx, hits = _diagram_context(c, kind, S.x_enh + " " + topic, use_kb, k, use_req, False)
                ctx = "AS-IS SPEC (JSON):\n" + json.dumps(proc["asis"]) + "\n\n" + ctx
                with st.spinner("Drawing the future process..."):
                    res = c.llm_json(an.prompt_process("tobe", S.x_enh), ctx)
                spec, notes = an.clean_process_spec(res)
                S.x_proc = {**proc, "tobe": spec, "notes": notes}
                c.log("Diagram", f"To-Be: {S.x_enh[:60]}")
            except (ValueError, RuntimeError) as e:
                st.error(str(e))
        proc = S.x_proc
        if proc.get("asis"):
            tabs = st.tabs(["As-Is", "To-Be (changes highlighted)", "Side by side", "What changes"])
            with tabs[0]:
                _show_dot(an.process_dot(proc["asis"]), "as_is")
                new = _spec_editor("process", proc["asis"], lambda j: an.clean_process_spec(j)[0], "asis")
                if new:
                    S.x_proc = {**proc, "asis": new}
                    st.success("Applied. Change any widget to refresh the picture.")
            with tabs[1]:
                if proc.get("tobe"):
                    _show_dot(an.process_dot(proc["tobe"], show_changes=True), "to_be")
                    new = _spec_editor("process", proc["tobe"], lambda j: an.clean_process_spec(j)[0], "tobe")
                    if new:
                        S.x_proc = {**proc, "tobe": new}
                        st.success("Applied. Change any widget to refresh the picture.")
                else:
                    st.info("Describe the enhancement above and click 'Generate To-Be'.")
            with tabs[2]:
                if proc.get("tobe"):
                    l, r = st.columns(2)
                    l.markdown("**As-Is**")
                    l.graphviz_chart(an.process_dot(proc["asis"]), use_container_width=True)
                    r.markdown("**To-Be**")
                    r.graphviz_chart(an.process_dot(proc["tobe"], show_changes=True), use_container_width=True)
                else:
                    st.info("Generate the To-Be first.")
            with tabs[3]:
                if proc.get("tobe"):
                    ch = an.process_changes(proc["tobe"])
                    st.dataframe(pd.DataFrame(ch) if ch else pd.DataFrame([{"Change": "None", "Step": "", "Lane": "", "Source": ""}]),
                                 use_container_width=True, hide_index=True)
                    infer = sum(1 for s in proc["tobe"]["steps"] + proc["asis"]["steps"] if "INFER" in s["source"].upper())
                    if infer:
                        st.caption(f"{infer} step(s) are marked INFERRED (not found in documents). Confirm them with the process owner.")
                else:
                    st.info("Generate the To-Be first.")
            for n in proc.get("notes", []) or []:
                st.warning(n)
            if st.button("Save to project", key="x_dg_save_proc"):
                S.x_diagrams.append({"id": f"DG{len(S.x_diagrams) + 1}", "kind": "process", "title": proc.get("topic", topic)[:60] or "Process",
                                     "saved": datetime.now().strftime("%Y-%m-%d %H:%M"), "spec": proc})
                st.success("Saved with the project.")
    else:
        if st.button("Generate", key="x_dg_go", type="primary", disabled=not topic.strip()):
            try:
                ctx, hits = _diagram_context(c, kind, topic, use_kb, k, use_req, use_def)
                with st.spinner("Building the diagram data..."):
                    res = c.llm_json(PROMPTS[kind](topic, use_def), ctx)
                spec = CLEANERS[kind](res, topic) if kind in ("fishbone", "whys", "context", "state") else CLEANERS[kind](res)
                S.x_cur = {**S.x_cur, kind: spec}
                c.log("Diagram", f"{KINDS[kind]}: {topic[:60]}")
            except (ValueError, RuntimeError) as e:
                st.error(str(e))
        spec = S.x_cur.get(kind)
        if spec:
            _render(kind, spec)
            clean = (lambda j: CLEANERS[kind](j, topic)) if kind in ("fishbone", "whys", "context", "state") else CLEANERS[kind]
            new = _spec_editor(kind, spec, clean, kind)
            if new:
                S.x_cur = {**S.x_cur, kind: new}
                st.success("Applied. Change any widget to refresh the picture.")
            if st.button("Save to project", key=f"x_dg_save_{kind}"):
                S.x_diagrams.append({"id": f"DG{len(S.x_diagrams) + 1}", "kind": kind, "title": topic[:60] or KINDS[kind],
                                     "saved": datetime.now().strftime("%Y-%m-%d %H:%M"), "spec": spec})
                st.success("Saved with the project.")

    if S.x_diagrams:
        st.divider()
        st.markdown("**Saved diagrams**")
        pick = st.selectbox("Open a saved diagram", [f"{d['id']} - {KINDS.get(d['kind'], d['kind'])}: {d['title']} ({d['saved']})"
                                                     for d in S.x_diagrams], key="x_dg_saved")
        sd = next(d for d in S.x_diagrams if d["id"] == pick.split(" - ")[0])
        o1, o2 = st.columns(2)
        if o1.button("Load into the editor", key="x_dg_load"):
            if sd["kind"] == "process":
                S.x_proc = sd["spec"]
            else:
                S.x_cur = {**S.x_cur, sd["kind"]: sd["spec"]}
            st.success("Loaded. Select that diagram type above to see it.")
        if o2.button("Delete", key="x_dg_del"):
            S.x_diagrams[:] = [d for d in S.x_diagrams if d["id"] != sd["id"]]
            st.success("Deleted.")


# ================================================================== DATA MAPPING & JSON
def _read_input(label, key, default=""):
    f = st.file_uploader(f"{label} - upload (json, csv, txt)", type=["json", "csv", "txt", "tsv"], key=key + "_up")
    if f:
        return f.getvalue().decode("utf-8", errors="replace")
    S = st.session_state
    S[key + "_v"] = st.text_area(f"{label} - or paste", value=S.get(key + "_v", default), height=220, key=key + "_w")
    return S[key + "_v"]


def page_map(c):
    S = c.S
    t_diff, t_map = st.tabs(["Compare two JSON documents", "Source to target mapping"])

    with t_diff:
        st.caption("Value-level comparison: what was added, removed or changed between two JSON documents, for example "
                   "legacy output versus Guidewire output for the same policy. No AI is used for the comparison itself.")
        l, r = st.columns(2)
        with l:
            a_txt = _read_input("Document A (baseline)", "x_ja")
        with r:
            b_txt = _read_input("Document B (new)", "x_jb")
        o1, o2 = st.columns(2)
        ign = o1.text_input("Ignore these keys (comma separated)", key="x_j_ign", placeholder="timestamp, updatedAt, requestId")
        srt = o2.checkbox("Ignore the order of simple lists", value=False, key="x_j_sort")
        if st.button("Compare", key="x_j_go", type="primary", disabled=not (a_txt.strip() and b_txt.strip())):
            try:
                rows = an.diff_json(an.parse_json_loose(a_txt), an.parse_json_loose(b_txt), ign.split(","), srt)
                S["x_jdiff"] = rows
                c.log("JSON comparison", f"{len(rows)} difference(s)")
            except ValueError as e:
                st.error(str(e))
        rows = S.get("x_jdiff")
        if rows is not None:
            if not rows:
                st.success("The two documents are identical (after the ignore settings).")
            else:
                df = pd.DataFrame(rows)
                cnt = df["Change"].value_counts()
                mm = st.columns(min(len(cnt), 5))
                for i, (k, v) in enumerate(cnt.items()):
                    mm[i % len(mm)].metric(k, int(v))
                st.dataframe(df, use_container_width=True, hide_index=True)
                d1, d2 = st.columns(2)
                d1.download_button("Download differences (.xlsx)", _xlsx({"Differences": df}), file_name="json_differences.xlsx", key="x_j_x")
                if d2.button("Explain the differences with agent", key="x_j_ai"):
                    try:
                        ctx = ""
                        if S.x_kb["docs"]:
                            _, ctx = _kb_context(c, " ".join(sorted({x["Path"].split(".")[-1] for x in rows[:40]})), 6)
                        with st.spinner("Summarising..."):
                            S["x_jsum"] = c.llm(an.prompt_diff_summary(rows), ctx)
                    except Exception as e:
                        st.error(str(e))
                if S.get("x_jsum"):
                    st.markdown(S["x_jsum"])

    with t_map:
        st.caption("Upload a source and a target as a JSON sample, a JSON Schema, or a CSV header row. The code compares the "
                   "structures first; the agent only suggests mappings for fields the code could not match, and anything it "
                   "invents is dropped.")
        l, r = st.columns(2)
        with l:
            sname = st.text_input("Source system", value="Legacy (AS/400)", key="x_m_sn")
            s_txt = _read_input("Source", "x_ms")
        with r:
            tname = st.text_input("Target system", value="Guidewire PolicyCenter", key="x_m_tn")
            t_txt = _read_input("Target", "x_mt")
        if st.button("Analyse structures", key="x_m_go", type="primary", disabled=not (s_txt.strip() and t_txt.strip())):
            try:
                sf, sk = an.fields_from_text(s_txt)
                tf, tk = an.fields_from_text(t_txt)
                rows = an.compare_fields(sf, tf)
                S["x_cmp"] = {"src": sf, "tgt": tf, "rows": rows, "kinds": (sk, tk), "names": (sname, tname)}
                S.x_maps = an.spec_rows_from_compare(rows)
                S["x_spec_gen"] = S.get("x_spec_gen", 0) + 1
                c.log("Data mapping", f"{sname} -> {tname}: {len(rows)} comparison rows")
            except ValueError as e:
                st.error(str(e))
        cmp_ = S.get("x_cmp")
        if cmp_:
            rows = cmp_["rows"]
            cnt = pd.Series([x["Status"] for x in rows]).value_counts().to_dict()
            st.markdown(f"**{cmp_['names'][0]}** ({cmp_['kinds'][0]}, {len(cmp_['src'])} fields)  ->  "
                        f"**{cmp_['names'][1]}** ({cmp_['kinds'][1]}, {len(cmp_['tgt'])} fields)")
            mm = st.columns(4)
            for col, key in zip(mm, ["Matched", "Type mismatch", "Source only", "Target only"]):
                col.metric(key, cnt.get(key, 0))
            with st.expander("Structure comparison (no AI)"):
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            un_s = [p for p in cmp_["src"] if not any(x["Source path"] == p and x["Target path"] for x in rows)]
            un_t = [p for p in cmp_["tgt"] if not any(x["Target path"] == p and x["Source path"] for x in rows)]
            if (un_s and un_t) and st.button(f"Suggest mappings for {len(un_s)} unmapped source / {len(un_t)} unmapped target fields",
                                             key="x_m_ai"):
                try:
                    ctx = ""
                    if S.x_kb["docs"]:
                        _, ctx = _kb_context(c, " ".join(x.split(".")[-1] for x in un_s[:40] + un_t[:40]), 8)
                    sl = [{"path": p, "type": cmp_["src"][p]["type"], "sample": cmp_["src"][p]["sample"]} for p in un_s[:80]]
                    tl = [{"path": p, "type": cmp_["tgt"][p]["type"]} for p in un_t[:80]]
                    with st.spinner("Suggesting mappings..."):
                        res = c.llm_json(an.prompt_mapping(sl, tl), ctx)
                    new, notes = an.apply_ai_mappings(res, cmp_["src"], cmp_["tgt"])
                    keep_src = {n["Source path"] for n in new if n["Target path"]}
                    keep_tgt = {n["Target path"] for n in new if n["Target path"]}
                    S.x_maps = [m for m in S.x_maps if not (m["Status"] == "Needs decision" and
                                                            (m["Source path"] in keep_src or m["Target path"] in keep_tgt or
                                                             m["Source path"] in {n["Source path"] for n in new}))] + new
                    S["x_spec_gen"] = S.get("x_spec_gen", 0) + 1
                    for n in notes:
                        st.warning(n)
                    c.log("Data mapping", f"{len(new)} AI suggestion(s) merged")
                except (ValueError, RuntimeError) as e:
                    st.error(str(e))
            if S.x_maps:
                st.markdown("**Mapping specification (edit, then click Save)**")
                df = pd.DataFrame(S.x_maps, columns=an.SPEC_COLS)
                ed = st.data_editor(df, key=f"x_spec_ed_{S.get('x_spec_gen', 0)}", use_container_width=True, hide_index=True,
                                    disabled=["Source path", "Source type", "Target path", "Target type", "Method"],
                                    column_config={"Confidence": cc.SelectboxColumn(options=["", "High", "Medium", "Low"]),
                                                   "Status": cc.SelectboxColumn(options=["Proposed", "Approved", "Needs decision", "Rejected"]),
                                                   "Transformation / rule": cc.TextColumn(width="large"),
                                                   "Validation": cc.TextColumn(width="medium")})
                out = ed.fillna("")
                if st.button("Save mapping edits", key="x_m_save", type="primary"):
                    S.x_maps = out.to_dict("records")
                    S["x_spec_gen"] = S.get("x_spec_gen", 0) + 1
                    c.log("Data mapping", "mapping spec edits saved")
                    st.success("Saved.")
                stc = out["Status"].value_counts().to_dict()
                tmapped = len({p for p, s_ in zip(out["Target path"], out["Status"]) if p and s_ != "Rejected"})
                mm = st.columns(4)
                mm[0].metric("Approved", stc.get("Approved", 0))
                mm[1].metric("Proposed", stc.get("Proposed", 0))
                mm[2].metric("Needs decision", stc.get("Needs decision", 0))
                mm[3].metric("Target fields mapped", f"{tmapped}/{len(cmp_['tgt'])}")
                e1, e2, e3 = st.columns(3)
                e1.download_button("Download Excel", _xlsx({"Mapping": out, "Structure comparison": pd.DataFrame(rows)}),
                                   file_name="data_mapping_spec.xlsx", key="x_m_x")
                e2.download_button("Download CSV", out.to_csv(index=False), file_name="data_mapping_spec.csv", key="x_m_c")
                e3.download_button("Download Markdown", an.spec_to_md(out.to_dict("records")), file_name="data_mapping_spec.md", key="x_m_m")
            with st.expander("Add a mapping row manually (for example a constant or a derived field)"):
                m1, m2 = st.columns(2)
                msrc = m1.text_input("Source path (blank for a constant)", key="x_mr_s")
                mtgt = m2.selectbox("Target path", sorted(cmp_["tgt"]), key="x_mr_t")
                mrule = st.text_input("Transformation / rule", key="x_mr_r", placeholder="e.g. Constant 'USD'; concat(first, ' ', last)")
                if st.button("Add row", key="x_mr_add", disabled=not mrule.strip()):
                    S.x_maps = S.x_maps + [{"Source path": msrc.strip(), "Source type": cmp_["src"].get(msrc.strip(), {}).get("type", ""),
                                            "Target path": mtgt, "Target type": cmp_["tgt"][mtgt]["type"],
                                            "Transformation / rule": mrule.strip(), "Default": "", "Validation": "",
                                            "Confidence": "", "Method": "Manual", "Status": "Proposed", "Notes": ""}]
                    S["x_spec_gen"] = S.get("x_spec_gen", 0) + 1
                    st.success("Added.")


# ================================================================== SCREEN MOCK-UP
def _oa_key(S):
    return (S.get("openai_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def _oa_base(S):
    return (S.get("openai_base") or "https://api.openai.com/v1").rstrip("/")


def _oa_err(r):
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except Exception:
        msg = r.text
    hint = ""
    low = msg.lower()
    if r.status_code == 401:
        hint = " Check the API key in the sidebar."
    elif r.status_code == 429:
        hint = " Rate limit or quota reached; wait a little or check billing."
    elif "image" in low and r.status_code == 400:
        hint = " This model may not accept images. Pick a vision model such as gpt-4o or gpt-4.1 in the sidebar."
    elif "verif" in low:
        hint = " The image model may need your OpenAI organisation to be verified."
    return f"OpenAI call failed (HTTP {r.status_code}): {msg[:400]}.{hint}"


def _vision_chat(S, content, system, max_tokens=16000):
    if not _oa_key(S):
        raise RuntimeError("No OpenAI API key. Enter it in the sidebar (OpenAI section).")
    payload = {"model": S.get("model"), "max_completion_tokens": max_tokens,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    try:
        r = requests.post(_oa_base(S) + "/chat/completions", json=payload, timeout=(10, 300),
                          headers={"Authorization": f"Bearer {_oa_key(S)}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach OpenAI ({e}).")
    if r.status_code != 200:
        raise RuntimeError(_oa_err(r))
    ch = r.json()["choices"][0]
    if ch.get("finish_reason") == "length":
        raise RuntimeError("The mock-up was cut off because it was too long. Simplify the request or raise 'Max output tokens' "
                           "in the sidebar.")
    return ch["message"].get("content") or ""


def _make_image(S, model, prompt, size, image=None):
    h = {"Authorization": f"Bearer {_oa_key(S)}"}
    try:
        if image:
            r = requests.post(_oa_base(S) + "/images/edits", headers=h, timeout=(10, 300),
                              files={"image": (image[0], image[1], image[2])},
                              data={"model": model, "prompt": prompt, "size": size})
        else:
            r = requests.post(_oa_base(S) + "/images/generations", headers={**h, "Content-Type": "application/json"},
                              timeout=(10, 300), json={"model": model, "prompt": prompt, "size": size})
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach OpenAI ({e}).")
    if r.status_code != 200:
        raise RuntimeError(_oa_err(r))
    d = (r.json().get("data") or [{}])[0]
    if d.get("b64_json"):
        return base64.b64decode(d["b64_json"])
    if d.get("url"):
        return requests.get(d["url"], timeout=60).content
    raise RuntimeError("The image model returned no image.")


def _show_mock(c, cur):
    S = c.S
    h = st.slider("Preview height (px)", 300, 1600, 700, 50, key="x_mk_h")
    components.html(cur["html"], height=h, scrolling=True)
    l, r = st.columns(2)
    with l:
        st.markdown("**What changed**")
        for i, ch in enumerate(cur["changes"] or ["(no change list returned)"], 1):
            st.markdown(f"{i}. {ch}")
    with r:
        st.markdown("**Assumptions to confirm**")
        for a_ in cur["assumptions"] or ["None listed"]:
            st.markdown(f"- {a_}")
    d1, d2 = st.columns(2)
    d1.download_button("Download mock-up (.html)", cur["html"], file_name="mockup.html", key="x_mk_dl")
    if d2.button("Save to project", key="x_mk_save"):
        S.x_mocks.append({"id": f"MK{len(S.x_mocks) + 1}", "title": (cur.get("prompt") or "Mock-up")[:60],
                          "time": cur.get("time", ""), **{k: cur[k] for k in ("html", "changes", "assumptions", "prompt")}})
        st.success("Saved with the project.")


def page_mock(c):
    S = c.S
    st.caption("Upload a screenshot of the current screen (optional), say what you want, and the agent rebuilds the screen "
               "with your changes as an editable mock-up. You can also have it drawn as a picture (PNG).")
    up = st.file_uploader("Current screen (screenshot)", type=["png", "jpg", "jpeg", "webp"], key="x_mk_up")
    if up:
        st.image(up.getvalue(), caption=up.name, width=480)
    S["x_mk_prompt"] = st.text_area(
        "What should the new screen do or look like?", value=S.get("x_mk_prompt", ""), height=150, key="x_mk_prompt_w",
        placeholder="e.g. Add a 'Schedule rating' panel under Coverages with a % field, a reason box and an approval status "
                    "badge. Show a warning banner when the value is above 15%. Move the Save button to the bottom right.")
    c1, c2, c3 = st.columns(3)
    style = an.MOCK_STYLES[c1.selectbox("Style", list(an.MOCK_STYLES), key="x_mk_style")]
    width = an.MOCK_WIDTHS[c2.selectbox("Screen size", list(an.MOCK_WIDTHS), key="x_mk_width")]
    hl = c3.checkbox("Highlight what changed", value=True, key="x_mk_hl")
    k1, k2 = st.columns(2)
    use_kb = k1.checkbox("Use knowledge base (field names, rules)", value=bool(S.x_kb["docs"]), key="x_mk_kb")
    use_req = k2.checkbox("Use requirements register", value=bool(S.x_reqs), key="x_mk_req")
    if st.button("Create mock-up", key="x_mk_go", type="primary", disabled=not S["x_mk_prompt"].strip()):
        try:
            parts = []
            if use_kb and S.x_kb["docs"]:
                _, ctx = _kb_context(c, S["x_mk_prompt"], 6)
                if ctx:
                    parts.append("KNOWLEDGE BASE EXCERPTS:\n" + ctx)
            if use_req and S.x_reqs:
                parts.append("REQUIREMENTS REGISTER:\n" + _req_lines(S.x_reqs, 60))
            task = an.prompt_mockup(S["x_mk_prompt"], style, width, hl, bool(up))
            content = [{"type": "text", "text": task + ("\n\nCONTEXT:\n" + "\n\n".join(parts) if parts else "")}]
            if up:
                if len(up.getvalue()) > 18_000_000:
                    raise RuntimeError("The screenshot is larger than 18 MB. Use a smaller image.")
                mime = getattr(up, "type", None) or "image/png"
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64," + base64.b64encode(up.getvalue()).decode(), "detail": "high"}})
            with st.spinner("Designing the mock-up (this can take a minute)..."):
                reply = _vision_chat(S, content, "You are a senior UX designer and business analyst. You produce precise, "
                                     "safe, self-contained HTML mock-ups and never add JavaScript or external resources.")
            S["x_mock_cur"] = {**an.parse_mockup_reply(reply), "prompt": S["x_mk_prompt"],
                               "time": time.strftime("%Y-%m-%d %H:%M")}
            S.pop("x_mk_png", None)
            c.log("Mock-up", S["x_mk_prompt"][:80])
        except (ValueError, RuntimeError) as e:
            st.error(str(e))
    cur = S.get("x_mock_cur")
    if cur:
        _show_mock(c, cur)
        st.markdown("**Change this mock-up**")
        S["x_mk_refine"] = st.text_area("Tell the agent what to change", value=S.get("x_mk_refine", ""), height=80,
                                         key="x_mk_refine_w", placeholder="e.g. Make the table columns narrower and add a Status column")
        if st.button("Apply change", key="x_mk_ref", disabled=not S["x_mk_refine"].strip()):
            try:
                task = an.prompt_mockup(S["x_mk_refine"], style, width, hl, False, refine=True)
                content = [{"type": "text", "text": task + "\n\nCURRENT MOCK-UP HTML:\n" + cur["html"][:60000]}]
                with st.spinner("Updating the mock-up..."):
                    reply = _vision_chat(S, content, "You are a senior UX designer. Return the format requested, nothing else.")
                S["x_mock_cur"] = {**an.parse_mockup_reply(reply), "prompt": cur["prompt"] + " | " + S["x_mk_refine"],
                                   "time": time.strftime("%Y-%m-%d %H:%M")}
                c.log("Mock-up", "refined: " + S["x_mk_refine"][:60])
            except (ValueError, RuntimeError) as e:
                st.error(str(e))
        with st.expander("Also draw it as a picture (PNG) with the image model"):
            st.caption("The picture is a visual impression: small text can be imperfect, so treat the HTML mock-up as the accurate "
                       "version. Needs an image model your OpenAI account can use.")
            m1, m2 = st.columns(2)
            imodel = m1.text_input("Image model", value="gpt-image-1", key="x_mk_imodel")
            isize = m2.selectbox("Picture size", ["1536x1024", "1024x1024", "1024x1536"], key="x_mk_isize")
            if st.button("Create picture", key="x_mk_png_go"):
                try:
                    prompt = ("A clean UI screen mock-up for business software. " + (cur.get("prompt") or "") +
                              " Show the changes described. Legible English labels, realistic sample data, no watermark, "
                              "flat 2D screenshot style, no device frame.")
                    img = (up.name, up.getvalue(), getattr(up, "type", None) or "image/png") if up else None
                    with st.spinner("Drawing the picture..."):
                        S["x_mk_png"] = _make_image(S, imodel.strip(), prompt, isize, img)
                except (ValueError, RuntimeError) as e:
                    st.error(str(e))
            if S.get("x_mk_png"):
                st.image(S["x_mk_png"], use_container_width=True)
                st.download_button("Download picture (.png)", S["x_mk_png"], file_name="mockup.png", mime="image/png", key="x_mk_png_dl")
    if S.x_mocks:
        st.divider()
        pick = st.selectbox("Saved mock-ups", [f"{m['id']} - {m['title']} ({m['time']})" for m in S.x_mocks], key="x_mk_saved")
        sm = next(m for m in S.x_mocks if m["id"] == pick.split(" - ")[0])
        o1, o2 = st.columns(2)
        if o1.button("Load", key="x_mk_load"):
            S["x_mock_cur"] = {k: sm[k] for k in ("html", "changes", "assumptions", "prompt", "time")}
            st.success("Loaded. It appears above after the next click.")
        if o2.button("Delete", key="x_mk_del"):
            S.x_mocks[:] = [m for m in S.x_mocks if m["id"] != sm["id"]]
            st.success("Deleted.")


# ================================================================== entry point
_PAGES = {"_x_reqs": page_reqs, "_x_kb": page_kb, "_x_diag": page_diag, "_x_map": page_map, "_x_mock": page_mock}


def render_tool(loc, S, llm, llm_json):
    """Called by the main app. Never raises: a failure here must not take the rest of the app down."""
    try:
        ensure_state(S)
        st.title({v: k for k, v in TOOL_PAGES.items()}[loc])
        _PAGES[loc](Ctx(S, llm, llm_json))
    except Exception as e:
        st.error(f"This tool hit a problem and was stopped so the rest of the app keeps working: {e}")
