"""The README graph-query showcase is a runnable command in the format the CLI prints (gh#595).

The showcase used to omit --db/--corpus, double-quote a question whose backticks the shell would
execute, and show an output shape _render_pretty never produces. Pinned against the renderer
itself, so a change to either side is a failure rather than a silent drift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autodoc_query.cli import _render_pretty
from autodoc_query.router import extract_symbol_tokens
from autodoc_query.types import ContextItem, QueryResult, RoutingDecision

README = Path(__file__).resolve().parents[5] / "README.md"


def _showcase() -> tuple[str, list[str]]:
    text = README.read_text(encoding="utf-8")
    i = text.index("$ graph-query ")
    block = text[i:text.index("```", i)]
    cmd_end = block.index("\n\n")
    return block[:cmd_end], block[cmd_end:].strip("\n").split("\n")


def test_readme_graph_query_showcase_is_a_runnable_command() -> None:
    cmd, _ = _showcase()
    for flag in ("--db ", "--corpus ", "--pretty"):
        assert flag in cmd, f"the README showcase omits {flag.strip()}: {cmd}"
    # Single-quoted: inside double quotes the shell would run the backticks as commands.
    question = re.search(r"'([^']*)'", cmd)
    assert question, "the question must be single-quoted so its backticks reach the router"
    tokens = extract_symbol_tokens(question.group(1))
    assert tokens, "no symbol-shaped token in the question, so rule=entity_resolution cannot fire"


def test_readme_graph_query_showcase_is_in_the_format_the_cli_prints() -> None:
    _, out = _showcase()
    routing = re.match(r"routing: (\w+) \(rule=(\w+)\) evidence=(\{.*\})$", out[0])
    assert routing, f"the routing line is not _render_pretty's format: {out[0]}"
    mode, rule, evidence = routing.group(1), routing.group(2), json.loads(routing.group(3))
    assert rule == "entity_resolution"
    cmd, _ = _showcase()
    q = re.search(r"'([^']*)'", cmd).group(1)
    assert evidence["tokens"] == extract_symbol_tokens(q), "evidence tokens disagree with the question"

    item_re = re.compile(
        r"  - (\S+) \((\S+):(\d+)-(\d+)\)  \[tier=(\w+) origin=(\w+) "
        r"score=([\d.]+) min_conf=([\d.]+) depth=(\d+)\]$"
    )
    first = item_re.match(out[3])
    assert first, f"the first item line is not _render_pretty's format: {out[3]}"
    name, f, a, b, tier, origin, score, conf, depth = first.groups()
    item = ContextItem(
        entity_id="x", tier=tier, kind="function", name=name, source_paths=[f],
        span={"file": f, "start_line": int(a), "end_line": int(b)}, origin=origin, path=[],
        min_confidence=float(conf), score=float(score), depth=int(depth),
    )
    res = QueryResult(
        question=q, routing=RoutingDecision(mode=mode, rule=rule, evidence=evidence),
        items=[item], truncated=True, truncation_reasons=[out[1][len("TRUNCATED: "):]],
    )
    rendered = _render_pretty(res).split("\n")
    assert rendered[0] == out[0]
    assert rendered[1] == out[1]
    assert re.fullmatch(r"\d+ context item\(s\):", out[2])
    assert rendered[3] == out[3]
