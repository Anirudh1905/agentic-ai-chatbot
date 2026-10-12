import itertools
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import streamlit as st
from httpx_sse import connect_sse

API_URL = os.environ.get("CHATBOT_API_URL", "http://localhost:8000")

TOOL_LABELS = {
    "search_knowledge_base": "Searching the knowledge base",
    "web_search": "Searching the web",
}


class StreamError(Exception):
    pass


def stream_events(message: str, thread_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
    with (
        httpx.Client(base_url=API_URL, timeout=httpx.Timeout(10.0, read=300.0)) as client,
        connect_sse(
            client, "POST", "/chat/stream", json={"message": message, "thread_id": thread_id}
        ) as source,
    ):
        for sse in source.iter_sse():
            data = json.loads(sse.data)
            if sse.event == "error":
                raise StreamError(data["message"])
            if sse.event == "done":
                return
            yield sse.event, data


def render_sources(sources: list[dict[str, Any]], reply: str) -> None:
    unique = list({s["cite_as"]: s for s in sources}.values())
    # Show what the answer actually cites; fall back to everything retrieved if it cites nothing.
    shown = [s for s in unique if s["cite_as"] in reply] or unique
    if not shown:
        return
    with st.expander(f"Sources ({len(shown)})"):
        for s in shown:
            if s["type"] == "web":
                st.markdown(f"- 🌐 [{s['title']}]({s['url']})")
            else:
                st.markdown(f"- 📄 {s['cite_as']}  \n  `{s['doc_id']}`")


st.set_page_config(page_title="Agentic AI Chatbot", page_icon="💬")
st.title("Agentic AI Chatbot")

if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.messages = []

with st.sidebar:
    st.caption(f"API: `{API_URL}`")
    st.caption(f"Thread: `{st.session_state.thread_id[:8]}`")
    if st.button("New chat", width="stretch"):
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.rerun()
    st.info("The bot doesn't remember earlier messages yet. Memory arrives in Phase 3.")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        render_sources(msg.get("sources", []), msg["content"])

if prompt := st.chat_input("Ask something"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status_slot = st.empty()
        status = status_slot.status("Thinking…")
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = []

        def tokens() -> Iterator[str]:
            for event, data in stream_events(prompt, st.session_state.thread_id):
                if event == "tool_start":
                    tools_used.append(data["name"])
                    label = TOOL_LABELS.get(data["name"], f"Running {data['name']}")
                    query = (data.get("input") or {}).get("query", "")
                    status.update(label=f"{label}…")
                    status.write(f"{label}: “{query}”" if query else label)
                elif event == "tool_end":
                    sources.extend(data["sources"])
                elif event == "token":
                    yield data["token"]

        try:
            stream = tokens()
            # Wait for the first token under the status spinner (tools run before it).
            first = next(stream, "")
            if tools_used:
                status.update(label="Searched", state="complete", expanded=False)
            else:
                status_slot.empty()
            reply = st.write_stream(itertools.chain([first], stream))
        except StreamError as e:
            status_slot.empty()
            st.error(str(e))
        except httpx.HTTPError:
            status_slot.empty()
            st.error(f"Can't reach the API at {API_URL}. Is it running?")
        else:
            render_sources(sources, reply)
            st.session_state.messages.append(
                {"role": "assistant", "content": reply, "sources": sources}
            )
