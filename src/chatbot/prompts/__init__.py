import re
from functools import cache
from importlib.resources import files

# Tags that mark where untrusted text (documents, web pages, memories) starts and ends in what
# the model reads. The system prompt tells it never to follow instructions in search results;
# memories are framed as what the user said, never as permissions.
DATA_TAGS = ("kb_results", "web_results", "result", "user_memories")
_DATA_TAG = re.compile(rf"<(/?)({'|'.join(DATA_TAGS)})\b", re.IGNORECASE)


@cache
def load_prompt(name: str) -> str:
    return files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


def as_data(text: str) -> str:
    """`text` with our data tags defused, so a page or document that contains, say,
    `</web_results>` can't end the results early and pass what follows off as ours."""
    return _DATA_TAG.sub(r"&lt;\1\2", text)
