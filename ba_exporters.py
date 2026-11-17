"""
ba_exporters.py - turn the agent's markdown artifacts into Word (.docx), Excel (.xlsx) or Markdown.
Pure Python (python-docx, openpyxl). No Streamlit.

sections = [(title, markdown_text), ...]
"""
import io
import re

FORMATS = {"Word (.docx)": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
           "Excel (.xlsx)": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
           "Markdown (.md)": ("md", "text/markdown")}


# ---------------------------------------------------------------- markdown parsing
_SEP = re.compile(r"^:?-{2,}:?$")


def clean_md(text):
    """Tidy model output so tables render and export properly:
    1) unwrap an outer ```markdown fence, 2) blank line before/after tables, 3) add a missing |---| header separator."""
    t = (text or "").strip()
    m = re.match(r"^```([A-Za-z]*)[ \t]*\r?\n(.*?)\r?\n?```\s*$", t, flags=re.S)
    if m:
        lang, inner = m.group(1).lower(), m.group(2)
        looks_md = bool(re.search(r"^\s*(\||#{1,6}\s)", inner, flags=re.M))
        if lang in ("markdown", "md") or (lang == "" and looks_md):
            t = inner.strip()
    out, lines, i = [], t.splitlines(), 0
    is_row = lambda x: x.strip().startswith("|") and x.strip().count("|") >= 2
    while i < len(lines):
        if is_row(lines[i]):
            if out and out[-1].strip():
                out.append("")
            block = []
            while i < len(lines) and is_row(lines[i]):
                block.append(lines[i].strip())
                i += 1
            cells = [c.strip() for c in block[0].strip("|").split("|")]
            if len(block) < 2 or not all(_SEP.match(c.replace(" ", "")) for c in
                                         [x.strip() for x in block[1].strip("|").split("|")] if c):
                block.insert(1, "|" + "|".join("---" for _ in cells) + "|")
            out.extend(block)
            if i < len(lines) and lines[i].strip():
                out.append("")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def parse_md(text):
    """-> list of blocks: (kind, payload). kinds: heading(level,text) para bullet(level,text)
    number(text) table(rows) code(text)"""
    blocks, lines, i, para = [], clean_md(text).splitlines(), 0, []

    def flush():
        if para:
            blocks.append(("para", " ".join(para)))
            para.clear()

    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if s.startswith("```"):
            flush()
            j, buf = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            blocks.append(("code", "\n".join(buf)))
            i = j + 1
            continue
        if s.startswith("|") and s.count("|") >= 2:
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(_SEP.match(c.replace(" ", "")) for c in cells if c) or not any(cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            flush()
            blocks.append(("heading", (len(m.group(1)), m.group(2).strip())))
        elif re.match(r"^[-*\u2022]\s+", s):
            flush()
            lvl = (len(raw) - len(raw.lstrip())) // 2
            blocks.append(("bullet", (min(lvl, 2), re.sub(r"^[-*\u2022]\s+", "", s))))
        elif re.match(r"^\d+[.)]\s+", s):
            flush()
            blocks.append(("number", re.sub(r"^\d+[.)]\s+", "", s)))
        elif not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()
    return blocks


def _plain(t):
    return re.sub(r"[*`]", "", t or "").strip()


# ---------------------------------------------------------------- Word
def build_docx(title, subtitle, sections):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    doc.add_heading(title, 0)
    if subtitle:
        doc.add_paragraph(subtitle).italic = True

    def runs(par, text):
        for n, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if part:
                r = par.add_run(part.replace("`", ""))
                r.bold = n % 2 == 1

    def shade(cell, color="D9E2F3"):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), color)
        tcPr.append(shd)

    for sec_title, md in sections:
        doc.add_heading(sec_title, 1)
        for kind, p in parse_md(md):
            if kind == "heading":
                doc.add_heading(_plain(p[1]), min(p[0] + 1, 4))
            elif kind == "para":
                runs(doc.add_paragraph(), p)
            elif kind == "bullet":
                style = "List Bullet" if p[0] == 0 else "List Bullet 2"
                runs(doc.add_paragraph(style=style), p[1])
            elif kind == "number":
                runs(doc.add_paragraph(style="List Number"), p)
            elif kind == "code":
                par = doc.add_paragraph()
                r = par.add_run(p)
                r.font.name = "Courier New"
                r.font.size = Pt(8)
            elif kind == "table":
                ncol = max(len(r) for r in p)
                t = doc.add_table(rows=0, cols=ncol)
                t.style = "Table Grid"
                for ri, row in enumerate(p):
                    cells = t.add_row().cells
                    for ci in range(ncol):
                        txt = _plain(row[ci]) if ci < len(row) else ""
                        cells[ci].text = txt
                        if ri == 0:
                            shade(cells[ci])
                            for par in cells[ci].paragraphs:
                                for r in par.runs:
                                    r.bold = True
                doc.add_paragraph()
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Excel
def _sheet_name(title, used):
    base = re.sub(r"[\[\]:*?/\\]", "", title).strip()[:28] or "Sheet"
    name, n = base, 2
    while name.lower() in used:
        name = f"{base[:25]}_{n}"
        n += 1
    used.add(name.lower())
    return name


def build_xlsx(sections):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    text_rows = []

    def write_table(ws, rows):
        ncol = max(len(r) for r in rows)
        for r in rows:
            ws.append([_plain(c) for c in r] + [""] * (ncol - len(r)))
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9E2F3")
        for row in ws.iter_rows():
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        for ci in range(1, ncol + 1):
            longest = max(len(str(ws.cell(r, ci).value or "")) for r in range(1, ws.max_row + 1))
            ws.column_dimensions[get_column_letter(ci)].width = min(max(12, longest + 2), 60)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    for title, md in sections:
        n_tables, current = 0, ""
        for kind, p in parse_md(md):
            if kind == "table":
                n_tables += 1
                ws = wb.create_sheet(_sheet_name(title if n_tables == 1 else f"{title} {n_tables}", used))
                write_table(ws, p)
            elif kind == "heading":
                current = _plain(p[1])
            elif kind in ("para", "number"):
                text_rows.append([title, current, _plain(p)])
            elif kind == "bullet":
                text_rows.append([title, current, "- " + _plain(p[1])])
            elif kind == "code":
                text_rows.append([title, current, p])
    if text_rows or not wb.sheetnames:
        ws = wb.create_sheet(_sheet_name("Text", used))
        write_table(ws, [["Artifact", "Section", "Content"]] + (text_rows or [["", "", ""]]))
        ws.column_dimensions["C"].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- Markdown + helpers
def build_md(title, subtitle, sections):
    out = [f"# {title}", f"_{subtitle}_" if subtitle else ""]
    for t, md in sections:
        out.append(f"\n## {t}\n{md}")
    return "\n".join(out).encode("utf-8")


def build(fmt_label, title, subtitle, sections):
    """Returns (bytes, extension, mime)."""
    ext, mime = FORMATS[fmt_label]
    if ext == "docx":
        return build_docx(title, subtitle, sections), ext, mime
    if ext == "xlsx":
        return build_xlsx(sections), ext, mime
    return build_md(title, subtitle, sections), ext, mime


def rows_to_md(headers, rows):
    esc = lambda v: str(v if v is not None else "").replace("|", "/").replace("\n", " <br> ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)
