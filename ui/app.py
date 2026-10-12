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
# No login until Phase 7: the API trusts this header to say whose threads these are.
DEFAULT_USER = os.environ.get("CHATBOT_USER_ID", "demo-user")

TOOL_LABELS = {
    "search_knowledge_base": "Searching the knowledge base",
    "web_search": "Searching the web",
}


class StreamError(Exception):
    pass


def api() -> httpx.Client:
    return httpx.Client(
        base_url=API_URL,
        headers={"X-User-Id": st.session_state.user_id},
        timeout=httpx.Timeout(10.0, read=300.0),
    )


def list_threads() -> list[dict[str, Any]]:
    try:
        with api() as client:
            resp = client.get("/threads")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        return []


def load_thread(thread_id: str) -> None:
    """Fetch the thread's messages from the API, so a reload or another tab picks up where
    the conversation left off."""
    st.session_state.thread_id = thread_id
    st.session_state.messages = []
    st.session_state.summary = None
    st.query_params["thread"] = thread_id
    try:
        with api() as client:
            resp = client.get(f"/threads/{thread_id}/messages")
    except httpx.HTTPError:
        return
    if resp.status_code == 200:
        body = resp.json()
        st.session_state.messages = body["messages"]
        st.session_state.summary = body["summary"]


def new_thread() -> None:
    # Created on the API side with the first message.
    load_thread(str(uuid.uuid4()))


def stream_events(message: str, thread_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
    with (
        api() as client,
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

if "user_id" not in st.session_state:
    st.session_state.user_id = DEFAULT_USER
if "thread_id" not in st.session_state:
    if thread := st.query_params.get("thread"):
        load_thread(thread)
    else:
        new_thread()

with st.sidebar:
    user = st.text_input("User", value=st.session_state.user_id)
    if user and user != st.session_state.user_id:
        st.session_state.user_id = user
        new_thread()
        st.rerun()
    if st.button("New chat", width="stretch", icon=":material/add:"):
        new_thread()
        st.rerun()

    threads = list_threads()
    if threads:
        st.subheader("Chats")
    for t in threads:
        current = t["id"] == st.session_state.thread_id
        label = t["title"] or "Untitled"
        if st.button(
            label,
            key=f"thread-{t['id']}",
            width="stretch",
            type="primary" if current else "secondary",
        ):
            load_thread(t["id"])
            st.rerun()

    if any(t["id"] == st.session_state.thread_id for t in threads):
        st.divider()
        if st.button("Delete this chat", width="stretch", icon=":material/delete:"):
            with api() as client:
                client.delete(f"/threads/{st.session_state.thread_id}")
            new_thread()
            st.rerun()
    st.caption(f"API: `{API_URL}` · thread `{st.session_state.thread_id[:8]}`")

if st.session_state.summary:
    with st.expander("Earlier messages were condensed into a summary"):
        st.markdown(st.session_state.summary)

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
            # Refresh the sidebar so a new thread shows up in the list.
            st.rerun()
