"""PDF, Word and Excel. Edits are backed up first. PDF text itself cannot be rewritten
reliably, so PDFs support reading plus extracting and combining pages."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from openpyxl import Workbook, load_workbook
from pypdf import PdfReader, PdfWriter

from .. import policy
from .base import ToolResult, tool
from .files import backup

LIMIT = 60000


def _risk_read(args, ctx):
    return policy.check_read(args["path"])


def _risk_write(args, ctx):
    return policy.check_write(args["path"])


def _risk_assemble(args, ctx):
    for source in args["inputs"]:
        decision = policy.check_read(str(source.get("path", "")))
        if decision.level == policy.BLOCK:
            return decision
    return policy.check_write(args["output"])


def _page_list(spec: str | None, total: int) -> list[int]:
    """'1-3,7' to zero-based indexes. Empty means every page."""
    if not spec:
        return list(range(total))
    pages: list[int] = []
    for part in spec.split(","):
        first, _, last = part.strip().partition("-")
        for number in range(int(first), int(last or first) + 1):
            if not 1 <= number <= total:
                raise ValueError(f"Page {number} does not exist; the document has {total}.")
            pages.append(number - 1)
    return pages


def _read_pdf(path: Path, pages: str | None) -> ToolResult:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        return ToolResult("This PDF is password-protected.", ok=False)
    total = len(reader.pages)
    parts = [f"--- page {i + 1} of {total} ---\n{reader.pages[i].extract_text() or ''}" for i in _page_list(pages, total)]
    text = "\n".join(parts)
    if not any(part.split("---\n", 1)[1].strip() for part in parts):
        return ToolResult("This PDF has no text layer, so it is probably scanned. Open it and read it with a screenshot.", ok=False)
    return ToolResult(text[:LIMIT] + (f"\n[truncated: {len(text)} characters]" if len(text) > LIMIT else ""))


def _doc_paragraphs(document):
    yield from document.paragraphs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _read_docx(path: Path) -> ToolResult:
    document = Document(str(path))
    lines = [p.text for p in document.paragraphs]
    for number, table in enumerate(document.tables, 1):
        lines.append(f"[table {number}]")
        lines += [" | ".join(cell.text for cell in row.cells) for row in table.rows]
    text = "\n".join(lines)
    return ToolResult(text[:LIMIT] + (f"\n[truncated: {len(text)} characters]" if len(text) > LIMIT else ""))


def _read_xlsx(path: Path, sheet: str | None) -> ToolResult:
    book = load_workbook(str(path), data_only=True, read_only=True)
    names = [sheet] if sheet else book.sheetnames
    output = [f"Sheets: {', '.join(book.sheetnames)}"]
    for name in names:
        if name not in book.sheetnames:
            return ToolResult(f"No sheet named {name!r}.", ok=False)
        output.append(f"--- {name} ---")
        for row in book[name].iter_rows(values_only=True):
            if any(value is not None for value in row):
                output.append("\t".join("" if value is None else str(value) for value in row))
    book.close()
    text = "\n".join(output)
    return ToolResult(text[:LIMIT] + (f"\n[truncated: {len(text)} characters]" if len(text) > LIMIT else ""))


@tool("document_read", "Read a PDF, Word (.docx) or Excel (.xlsx) file as text. pages is for PDFs ('1-3,7'), sheet for Excel.",
      {"path": {"type": "string"}, "pages": {"type": "string"}, "sheet": {"type": "string"}}, ["path"],
      group="files", risk=_risk_read, phase="OBSERVE", timeout=60, untrusted=True)
def document_read(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        return ToolResult(f"No such file: {path}", ok=False)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path, args.get("pages"))
    if suffix == ".docx":
        return _read_docx(path)
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(path, args.get("sheet"))
    return ToolResult("Supported here: .pdf, .docx, .xlsx. Use file_read for text files.", ok=False)


@tool("document_create", "Create a Word or Excel file. Word: give paragraphs. Excel: give sheets as {name: [[cell, ...], ...]}. Formulas start with =. Refuses to overwrite.",
      {"path": {"type": "string"}, "paragraphs": {"type": "array", "items": {"type": "string"}}, "sheets": {"type": "object"}},
      ["path"], group="files", risk=_risk_write, timeout=60)
def document_create(args, ctx):
    path = Path(args["path"]).expanduser()
    if path.exists():
        return ToolResult(f"{path} already exists. Use document_edit, or choose another name.", ok=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        document = Document()
        for paragraph in args.get("paragraphs", []):
            document.add_paragraph(paragraph)
        document.save(str(path))
    elif suffix == ".xlsx":
        book = Workbook()
        book.remove(book.active)
        for name, rows in (args.get("sheets") or {"Sheet1": []}).items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
        book.save(str(path))
    else:
        return ToolResult("Supported here: .docx and .xlsx.", ok=False)
    return ToolResult(f"Created {path} ({path.stat().st_size} bytes).")


def _replace_in_paragraph(paragraph, find: str, replace: str) -> int:
    """Replace text even when Word split it across formatting runs."""
    count = 0
    while find in paragraph.text:
        runs = paragraph.runs
        start = paragraph.text.index(find)
        end = start + len(find)
        position = 0
        first = True
        for run in runs:
            run_start, run_end = position, position + len(run.text)
            position = run_end
            if run_end <= start or run_start >= end:
                continue
            before = run.text[: max(start - run_start, 0)]
            after = run.text[max(end - run_start, 0):] if run_end > end else ""
            run.text = before + (replace if first else "") + after
            first = False
        if first:  # The text lives outside ordinary runs, as in a hyperlink.
            return count
        count += 1
        if find in replace:
            break
    return count


@tool("document_edit", "Edit a Word or Excel file in place, after backing it up. Word: replacements [{find, replace}]. Excel: cells [{sheet, cell, value}].",
      {"path": {"type": "string"}, "replacements": {"type": "array", "items": {"type": "object"}},
       "cells": {"type": "array", "items": {"type": "object"}}},
      ["path"], group="files", risk=_risk_write, timeout=60)
def document_edit(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        return ToolResult(f"No such file: {path}", ok=False)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        document = Document(str(path))
        counts = []
        for change in args.get("replacements", []):
            counts.append(sum(_replace_in_paragraph(p, change["find"], change["replace"]) for p in _doc_paragraphs(document)))
        if not counts or not all(counts):
            return ToolResult(f"Some text was not found, so nothing was saved. Replacements per item: {counts}", ok=False)
        saved = backup(path)
        document.save(str(path))
        return ToolResult(f"Edited {path}: {sum(counts)} replacement(s). Backup: {saved}")
    if suffix == ".xlsx":
        book = load_workbook(str(path))
        changes = args.get("cells", [])
        for change in changes:
            if change.get("sheet", book.sheetnames[0]) not in book.sheetnames:
                return ToolResult(f"No sheet named {change['sheet']!r}, so nothing was saved.", ok=False)
            book[change.get("sheet", book.sheetnames[0])][change["cell"]] = change["value"]
        if not changes:
            return ToolResult("No cells were given.", ok=False)
        saved = backup(path)
        book.save(str(path))
        return ToolResult(f"Edited {path}: {len(changes)} cell(s). Backup: {saved}")
    return ToolResult("Supported here: .docx and .xlsx. PDFs support reading and pdf_assemble.", ok=False)


@tool("pdf_assemble", "Extract or combine PDF pages into a new PDF. inputs: [{path, pages}] where pages is like '1-3,7' or omitted for all.",
      {"inputs": {"type": "array", "items": {"type": "object"}}, "output": {"type": "string"}}, ["inputs", "output"],
      group="files", risk=_risk_assemble, timeout=120)
def pdf_assemble(args, ctx):
    output = Path(args["output"]).expanduser()
    if output.exists():
        return ToolResult(f"{output} already exists.", ok=False)
    writer = PdfWriter()
    for source in args["inputs"]:
        reader = PdfReader(str(Path(source["path"]).expanduser()))
        for index in _page_list(source.get("pages"), len(reader.pages)):
            writer.add_page(reader.pages[index])
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as fh:
        writer.write(fh)
    return ToolResult(f"Wrote {output} with {len(writer.pages)} page(s).")
