#!/usr/bin/env python3
"""Structural Mermaid validator — deterministic gate before any diagram is embedded.

Not a full grammar: catches the high-frequency failure classes (unknown header,
unbalanced brackets outside quotes, unclosed quotes, dangling arrows, subgraph/end
mismatch, empty body). Philosophy: never let a diagram that obviously won't render
persist to disk. A pass here is necessary, not sufficient.
"""
import re
import sys
from pathlib import Path

DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram-v2",
    "stateDiagram", "erDiagram", "journey", "gantt", "pie", "mindmap", "timeline",
    "quadrantChart", "gitGraph", "requirementDiagram",
    "C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment",
)


def _strip_quoted(text: str) -> str:
    """Remove double-quoted spans so label content never trips bracket checks."""
    return re.sub(r'"[^"]*"', '""', text)


def validate(source: str) -> list[str]:
    errors = []
    lines = [ln for ln in source.splitlines()
             if ln.strip() and not ln.strip().startswith("%%")]
    if not lines:
        return ["empty diagram: no content lines"]

    header = lines[0].strip()
    if not any(header == t or header.startswith(t + " ") for t in DIAGRAM_TYPES):
        errors.append(
            f"unknown diagram type in header: {header!r} "
            f"(expected one of: {', '.join(DIAGRAM_TYPES[:6])}, ...)")

    body = "\n".join(lines)
    if body.count('"') % 2:
        errors.append("odd number of double quotes — unclosed quoted label")

    stripped = _strip_quoted(body)
    is_flow = header.split()[0] in ("flowchart", "graph")

    # Node-shape brackets ([], (), {}) are flowchart/graph syntax; other diagram
    # types (ER cardinality "||--o{", etc.) use these chars with different grammar,
    # so balance-counting only applies to flowchart/graph.
    if is_flow:
        for open_c, close_c, name in (("[", "]", "square"), ("(", ")", "paren"),
                                      ("{", "}", "brace")):
            n_open, n_close = stripped.count(open_c), stripped.count(close_c)
            if n_open != n_close:
                errors.append(
                    f"unbalanced {name} brackets outside quotes: "
                    f"{n_open} '{open_c}' vs {n_close} '{close_c}' "
                    f"(labels with special chars must be double-quoted)")

    if re.search(r"--[->]>?\s*$", stripped, re.MULTILINE):
        errors.append("dangling arrow: line ends with an arrow and no target node")

    if is_flow:
        n_sub = len(re.findall(r"^\s*subgraph\b", stripped, re.MULTILINE))
        n_end = len(re.findall(r"^\s*end\s*$", stripped, re.MULTILINE))
        if n_sub != n_end:
            errors.append(f"subgraph/end mismatch: {n_sub} subgraph vs {n_end} end")

    return errors


def extract_fences(markdown: str) -> list[str]:
    """Return bodies of COMPLETE ```mermaid fences only (unclosed fences skipped)."""
    return [m.group(1).strip("\n") for m in
            re.finditer(r"```mermaid[^\S\n]*\n(.*?)\n```", markdown, re.DOTALL)]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_mermaid.py <file>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8", errors="replace")
    sources = extract_fences(text) if path.suffix.lower() in (".md", ".markdown") \
        else [text]
    failed = False
    for i, src in enumerate(sources, 1):
        for err in validate(src):
            print(f"diagram {i}: {err}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
