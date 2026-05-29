"""Export dei Fogli collaborativi in CSV e XLSX.

- CSV: protetto da CSV formula injection (piano §Sicurezza). Le celle che
  iniziano con = + - @ (e tab/CR) vengono prefissate con un apostrofo, TRANNE
  i numeri puri (per non rovinare i negativi tipo -5).
- XLSX: writer minimale con sola stdlib (zipfile + XML). Niente openpyxl ->
  zero dipendenze native (importante su Windows). I valori testuali sono scritti
  come inline string (Excel non li valuta MAI come formule -> immune a injection);
  i numeri puri come celle numeriche per preservare la fedelta'. L'XLSX si apre
  nativamente in Excel e si importa in Google Sheets ("o simile").

Nota privacy: l'export e' l'unica via in cui il contenuto del foglio puo' uscire
verso un file. Qui restituiamo uno stream di download (non scriviamo su disco):
resta coerente col principio "i fogli sono asset online".
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _is_number(v: str) -> bool:
    return bool(_NUMERIC_RE.match(v.strip())) if v else False


def csv_safe(value: str) -> str:
    """Neutralizza una cella per il CSV contro la formula injection."""
    if not value:
        return value
    if value[0] in _DANGEROUS and not _is_number(value):
        return "'" + value
    return value


def _grid_bounds(cells: list[dict]) -> tuple[int, int]:
    """(n_rows, n_cols) effettivi usati, minimo 1x1."""
    max_r = max_c = 0
    for c in cells:
        if (c.get("value") or c.get("formula")):
            max_r = max(max_r, int(c["row"]))
            max_c = max(max_c, int(c["col"]))
    return max_r + 1, max_c + 1


def _cell_value(c: dict) -> str:
    v = c.get("value")
    if v is None or v == "":
        # MVP: la formula e' testo; se non c'e' value mostriamo l'eventuale formula
        v = c.get("formula") or ""
    return str(v)


def to_csv(cells: list[dict]) -> bytes:
    """CSV UTF-8 con BOM (Excel-friendly), celle anti-injection."""
    n_rows, n_cols = _grid_bounds(cells)
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for c in cells:
        r, col = int(c["row"]), int(c["col"])
        if 0 <= r < n_rows and 0 <= col < n_cols:
            grid[r][col] = csv_safe(_cell_value(c))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in grid:
        writer.writerow(row)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _col_ref(n: int) -> str:
    s = ""
    n += 1
    while n > 0:
        n, m = divmod(n - 1, 26)
        s = chr(65 + m) + s
    return s


def to_xlsx(cells: list[dict]) -> bytes:
    """XLSX minimale (OOXML) con sola stdlib. Inline string per il testo
    (immune a formula injection), celle numeriche per i numeri puri."""
    n_rows, n_cols = _grid_bounds(cells)
    grid: dict[tuple[int, int], str] = {}
    for c in cells:
        grid[(int(c["row"]), int(c["col"]))] = _cell_value(c)

    rows_xml = []
    for r in range(n_rows):
        cells_xml = []
        for col in range(n_cols):
            v = grid.get((r, col))
            if v is None or v == "":
                continue
            ref = _col_ref(col) + str(r + 1)
            if _is_number(v):
                cells_xml.append(f'<c r="{ref}"><v>{escape(v.strip())}</v></c>')
            else:
                cells_xml.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
                )
        if cells_xml:
            rows_xml.append(f'<row r="{r + 1}">' + "".join(cells_xml) + "</row>")
    sheet_data = "".join(rows_xml)

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet_data}</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Foglio1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return out.getvalue()


def to_prompt_text(cells: list[dict], max_chars: int = 4000) -> str:
    """Rende le celle come tabella testuale (intestazioni colonna A/B/C + numero
    riga) per darle in pasto all'LLM della chat sul fascicolo. Troncata a
    max_chars. Ritorna '(foglio vuoto)' se non ci sono celle."""
    n_rows, n_cols = _grid_bounds(cells)
    if not any((c.get("value") or c.get("formula")) for c in cells):
        return "(foglio vuoto)"
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for c in cells:
        r, col = int(c["row"]), int(c["col"])
        if 0 <= r < n_rows and 0 <= col < n_cols:
            grid[r][col] = _cell_value(c).replace("\t", " ").replace("\n", " ")
    lines = ["\t".join(_col_ref(i) for i in range(n_cols))]
    for ri, row in enumerate(grid, start=1):
        # salta le righe completamente vuote per compattezza
        if any(v for v in row):
            lines.append(str(ri) + "\t" + "\t".join(row))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(troncato)"
    return text


def safe_filename(title: str, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 _.-]", "_", (title or "foglio").strip()) or "foglio"
    return f"{base[:80]}.{ext}"
