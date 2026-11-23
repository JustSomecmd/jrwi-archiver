"""A small PDF laid out as JRWI's transcripts are: each name in bold on a line of its own, then what they said,
wrapped lines closer together than paragraphs. Standard fonts, so no font files are needed."""

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int = 85) -> list[str]:
    out, line = [], ""
    for w in text.split():
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    return out + ([line] if line else [])


def write_early(path: Path, title: str, cast: list[str], turns: list[tuple[str | None, str]],
                joined: set[int] = frozenset()) -> None:
    """As the early transcripts are laid out: a bold title, the cast a line each with no gap between, then
    "Name : what they said" in plain text, a paragraph each; a turn whose index is in joined starts on the next line
    with no gap, as Google Docs sometimes lays them out."""
    rows = [(title, True, 0.0)] + [(c, False, 22.0 if k == 0 else 14.0) for k, c in enumerate(cast)]
    for k, (speaker, text) in enumerate(turns):
        first = f"{speaker} : {text}" if speaker else text
        for n, line in enumerate(_wrap(first)):
            rows.append((line, False, 14.0 if n or k in joined else 22.0))
    _pdf(path, rows)


def write(path: Path, turns: list[tuple[str | None, str]]) -> None:
    """turns: (speaker, text); a None speaker writes the text as a paragraph of its own (a note)."""
    rows, first = [], True
    for speaker, text in turns:
        if speaker is not None:
            rows.append((speaker, True, 0.0 if first else 38.0))
        for k, line in enumerate(_wrap(text)):
            rows.append((line, False, 22.0 if k == 0 else 14.0))
        first = False
    _pdf(path, rows)


def _pdf(path: Path, rows: list[tuple[str, bool, float]]) -> None:
    """rows: (text, bold, how far below the line before it), a page after another as they fill."""
    pages, ops, y = [], [], 740.0

    def put(font: str, text: str, step: float) -> None:
        nonlocal y, ops
        if y - step < 60:
            pages.append(ops)
            ops, y = [], 740.0
        else:
            y -= step
        ops.append(f"BT /{font} 11 Tf 1 0 0 1 72 {y:.1f} Tm ({_esc(text)}) Tj ET")

    for text, bold, step in rows:
        put("F2" if bold else "F1", text, step)
    pages.append(ops)

    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    kids = []
    for ops in pages:
        stream = "\n".join(ops).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream")
        content = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content} 0 R >>")
        kids.append(len(objects))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        body = obj if isinstance(obj, bytes) else obj.encode("latin-1")
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
