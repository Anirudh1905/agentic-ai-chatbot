"""Find what each citation in an answer vouches for, and which results it names.

The system prompt asks for `[cite_as]` after every claim from a knowledge base result, and the
`cite_as` markdown link as-is for a web result. A citation covers the text since the previous
citation on the same line (a paragraph or list item): "Yes. Orders can be canceled within 30
minutes. [Order Changes]" vouches for both sentences, as a reader would take it. Text after a
line's last citation isn't cited. No model calls.
"""

import re
from dataclasses import dataclass

from evals.agent_runs import Result

KB_CITATION = re.compile(r"\[([^\[\]]+)\](?!\()")
# A page title can hold brackets of its own ("REI Return Policy [Latest 2026 Guide]").
WEB_CITATION = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\((https?://[^)\s]+)\)")
# Between two citations, only punctuation or a wrapping bracket: they're one group.
NO_TEXT = re.compile(r"[\s.,;:!?()\[\]*_-]*")


@dataclass(frozen=True)
class Cited:
    text: str  # what the citations vouch for, without them
    sources: list[Result]  # the results they name
    unknown: list[str]  # citations naming no result the model saw


def citations_in(line: str) -> list[tuple[int, int, str, bool]]:
    """(start, end, name or url, is_web) for each citation, in order."""
    found = [(m.start(), m.end(), m[2], True) for m in WEB_CITATION.finditer(line)]
    # Blank the web citations out, so their titles aren't read as knowledge base citations.
    rest = WEB_CITATION.sub(lambda m: " " * len(m[0]), line)
    found += [
        (m.start(), m.end(), m[1], False)
        for m in KB_CITATION.finditer(rest)
        # Not a PII placeholder, or the brackets left around a blanked web citation.
        if m[1].strip() and not m[1].startswith("REDACTED_")
    ]
    return sorted(found)


def cited_spans(answer: str, results: list[Result]) -> list[Cited]:
    kb = {r.cite_as: r for r in results if r.tool == "search_knowledge_base"}
    web = {m[2]: r for r in results if (m := WEB_CITATION.fullmatch(r.cite_as))}
    out: list[Cited] = []
    for line in answer.splitlines():
        text, group, pos = "", [], 0
        for start, end, name, is_web in citations_in(line):
            between = line[pos:start]
            if group and not NO_TEXT.fullmatch(between):
                out.append(span(text, group, kb, web))
                text, group = "", []
            text += between
            group.append((name, is_web))
            pos = end
        if group:
            out.append(span(text, group, kb, web))
    return out


def span(
    text: str, group: list[tuple[str, bool]], kb: dict[str, Result], web: dict[str, Result]
) -> Cited:
    found = [(name, (web if is_web else kb).get(name)) for name, is_web in group]
    return Cited(
        text.strip(" []*-").strip(),
        [s for _, s in found if s],
        [name for name, s in found if not s],
    )
