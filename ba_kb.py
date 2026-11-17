"""
ba_kb.py - project knowledge base for the BA agent. Pure Python (python-docx / openpyxl / pypdf optional).
No Streamlit, no network. Retrieval is BM25 keyword search, so it works offline and adds no dependencies.

kb = {"docs": [{"id": "K1", "name": ..., "type": ..., "added": ..., "hash": ..., "chars": n,
                "chunks": [{"id": "K1-1", "section": "...", "text": "..."}]}]}
"""
import hashlib
import io
import json
import math
import re
from collections import Counter
from datetime import datetime

STOP = set("""a an and are as at be but by for from has have if in into is it its of on or that the their then there these
this to was were will with we you your our not no do does did can could should would may might must shall about after
all also any been being than them they what when where which who whom why how""".split())

DOC_TYPES = ["Process document", "Business rules", "Regulatory / compliance", "Requirements", "Meeting notes",
             "Email", "Technical / design", "Data / mapping", "Test / UAT", "Other"]


# ------------------------------------------------------------------ text extraction
def _strip_html(h):
    h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", h)).strip()


def extract_text(name, data):
    """bytes -> text. Raises ValueError with a friendly message for unsupported or unreadable files."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    if ext in ("txt", "md", "csv", "json", "log", "tsv", "eml", "xml", "yaml", "yml"):
        return data.decode("utf-8", errors="replace")
    if ext in ("html", "htm"):
        return _strip_html(data.decode("utf-8", errors="replace"))
    if ext == "docx":
        try:
            from docx import Document
        except ImportError:
            raise ValueError("Reading .docx needs python-docx (pip install python-docx).")
        d = Document(io.BytesIO(data))
        out = []
        for p in d.paragraphs:
            if p.text.strip():
                style = (p.style.name or "") if p.style is not None else ""
                m = re.match(r"Heading (\d)", style)
                out.append(("#" * int(m.group(1)) + " " if m else "") + p.text.strip())
        for t in d.tables:
            out.append("")
            for r in t.rows:
                out.append("| " + " | ".join(c.text.strip().replace("\n", " ") for c in r.cells) + " |")
        return "\n".join(out)
    if ext in ("xlsx", "xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Reading .xlsx needs openpyxl (pip install openpyxl).")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None and str(c).strip() for c in row):
                    out.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
        return "\n".join(out)
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("Reading PDFs needs pypdf (pip install pypdf). Or paste the text instead.")
        try:
            rd = PdfReader(io.BytesIO(data))
            txt = "\n\n".join((pg.extract_text() or "") for pg in rd.pages).strip()
        except Exception as e:
            raise ValueError(f"Could not read the PDF ({e}). Paste the text instead.")
        if not txt:
            raise ValueError("No text found in the PDF (it may be a scan). Paste the text instead.")
        return txt
    raise ValueError(f"Unsupported file type .{ext}. Use txt, md, csv, json, html, docx, xlsx or pdf.")


# ------------------------------------------------------------------ chunking
def chunk_text(text, size=1200, overlap=150):
    """Split into chunks of about `size` characters, on paragraph boundaries, keeping the nearest heading
    as the 'section'. Returns [{'section': str, 'text': str}]."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    paras, section, blocks = re.split(r"\n\s*\n", text), "", []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        for ln in p.splitlines():
            m = re.match(r"^\s*#{1,6}\s+(.*)", ln)
            if m:
                section = m.group(1).strip()[:80]
        # break very long paragraphs
        while len(p) > size * 1.5:
            cut = p.rfind("\n", 0, size)
            if cut < size * 0.4:
                cut = p.rfind(". ", 0, size)
                cut = cut + 1 if cut > size * 0.4 else size
            blocks.append((section, p[:cut].strip()))
            p = p[max(cut - overlap, 0):].strip() if cut > overlap else p[cut:].strip()
        if p:
            blocks.append((section, p))
    chunks, cur, cur_sec = [], "", ""
    for sec, p in blocks:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append({"section": cur_sec, "text": cur})
            tail = cur[-overlap:] if overlap and len(cur) > overlap else ""
            if tail and "\n" in tail:
                tail = tail[tail.index("\n") + 1:]       # start the overlap on a whole line
            elif tail:
                tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            cur, cur_sec = (tail + "\n\n" + p).strip() if tail and not p.startswith("#") else p, sec
        else:
            if not cur:
                cur_sec = sec
            cur = (cur + "\n\n" + p).strip()
    if cur:
        chunks.append({"section": cur_sec, "text": cur})
    return chunks


# ------------------------------------------------------------------ store
def new_kb():
    return {"docs": []}


def _hash(text):
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode("utf-8", "ignore")).hexdigest()


def add_doc(kb, name, text, doc_type="Other"):
    """-> (doc_id, is_new). Same content added twice returns the existing id."""
    h = _hash(text)
    for d in kb["docs"]:
        if d["hash"] == h:
            return d["id"], False
    nums = [int(d["id"][1:]) for d in kb["docs"] if d["id"][1:].isdigit()]
    did = f"K{(max(nums) + 1) if nums else 1}"
    chunks = [{"id": f"{did}-{i}", "section": c["section"], "text": c["text"]}
              for i, c in enumerate(chunk_text(text), 1)]
    kb["docs"].append({"id": did, "name": name, "type": doc_type, "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "hash": h, "chars": len(text), "chunks": chunks})
    return did, True


def remove_doc(kb, doc_id):
    kb["docs"] = [d for d in kb["docs"] if d["id"] != doc_id]


def stats(kb):
    return {"docs": len(kb["docs"]), "chunks": sum(len(d["chunks"]) for d in kb["docs"]),
            "chars": sum(d["chars"] for d in kb["docs"])}


# ------------------------------------------------------------------ retrieval (BM25)
def tokenize(text):
    toks = re.findall(r"[a-z0-9][a-z0-9_]*", (text or "").lower())
    out = []
    for t in toks:
        if t in STOP or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def search(kb, query, k=6, doc_ids=None):
    """BM25 over all chunks. -> [{'chunk_id','doc_id','doc','section','score','text'}] best first."""
    q = tokenize(query)
    if not q:
        return []
    rows = []
    for d in kb["docs"]:
        if doc_ids and d["id"] not in doc_ids:
            continue
        for c in d["chunks"]:
            rows.append((d, c, tokenize(c["text"] + " " + c.get("section", "") + " " + d["name"])))
    if not rows:
        return []
    n = len(rows)
    avg = sum(len(r[2]) for r in rows) / n or 1
    df = Counter()
    for _, _, toks in rows:
        for t in set(toks):
            df[t] += 1
    k1, b, hits = 1.5, 0.75, []
    for d, c, toks in rows:
        tf, score, dl = Counter(toks), 0.0, len(toks)
        for t in set(q):
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / avg))
        if score > 0:
            hits.append({"chunk_id": c["id"], "doc_id": d["id"], "doc": d["name"], "section": c.get("section", ""),
                         "score": round(score, 3), "text": c["text"]})
    hits.sort(key=lambda h: -h["score"])
    return hits[:k]


def format_context(hits, max_chars=9000):
    """Hits -> prompt text with [K#-#] citation tags."""
    out, used = [], 0
    for h in hits:
        head = f"[{h['chunk_id']}] {h['doc']}" + (f" - {h['section']}" if h["section"] else "")
        block = f"{head}\n{h['text']}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def cited_ids(text):
    """Citation tags like [K2-3] found in a model answer."""
    return sorted(set(re.findall(r"\[(K\d+-\d+)\]", text or "")))


def all_chunk_ids(kb):
    return {c["id"] for d in kb["docs"] for c in d["chunks"]}
