#!/usr/bin/env python3
"""Deterministic LaTeX compiler stand-in for the offline resume benchmark."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 36 756 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("deterministic benchmark compiler 1.0")
        return 0

    if "-output-directory" in arguments:
        output_dir = Path(arguments[arguments.index("-output-directory") + 1])
    elif "--outdir" in arguments:
        output_dir = Path(arguments[arguments.index("--outdir") + 1])
    else:
        output_dir = Path(arguments[-1]).parent

    tex_path = Path(arguments[-1])
    source = tex_path.read_text(encoding="utf-8")
    visible = re.sub(r"\\[A-Za-z*]+", " ", source)
    visible = re.sub(r"[^A-Za-z0-9+#./-]+", " ", visible)
    rendered = " ".join(visible.split())

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resume.pdf").write_bytes(pdf_bytes(rendered))
    for name in ("resume.aux", "resume.log", "resume.synctex.gz"):
        (output_dir / name).write_bytes(b"deterministic compiler by-product")

    counter = os.environ.get("BENCHMARK_COMPILER_COUNTER")
    if counter:
        counter_path = Path(counter)
        count = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
        counter_path.write_text(str(count + 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
