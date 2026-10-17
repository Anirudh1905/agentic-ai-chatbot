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
# No login until Phase 8: the API trusts this header to say whose threads these are.
DEFAULT_USER = os.environ.get("CHATBOT_USER_ID", "demo-user")

TOOL_LABELS = {
    "search_knowledge_base": "Searching the knowledge base",
    "web_search": "Searching the web",
    "remember": "Saving to memory",
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
    st.session_state.pending = None
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
        st.session_state.pending = body["pending"]


def new_thread() -> None:
    # Created on the API side with the first message.
    load_thread(str(uuid.uuid4()))


def stream_events(path: str, body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    with api() as client, connect_sse(client, "POST", path, json=body) as source:
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


def memory_panel() -> None:
    """What the bot remembers about this user across conversations (long-term memory)."""
    try:
        with api() as client:
            memories = client.get("/me/memory").json()["memories"]
    except httpx.HTTPError:
        return
    with st.expander(f"What I remember about you ({len(memories)})"):
        if not memories:
            st.caption(
                "Nothing yet. I learn from long chats and idle ones, or press "
                '"Remember this" beside any of my replies.'
            )
            return
        for memory in memories:
            text, delete = st.columns([6, 1], vertical_alignment="center")
            text.markdown(f"{memory['content']}  \n:gray[{memory['topic']}]")
            if delete.button(
                "", icon=":material/close:", key=f"forget-{memory['id']}", help="Forget this"
            ):
                with api() as client:
                    client.delete(f"/me/memory/{memory['id']}")
                st.rerun()
        if st.button("Forget everything", icon=":material/delete_forever:", width="stretch"):
            with api() as client:
                client.delete("/me/memory")
            st.rerun()


st.set_page_config(page_title="Agentic AI Chatbot", page_icon="💬")
# Streamlit pads the right of the user's messages only; matching it on every message keeps the
# "Remember this" buttons in one column.
st.markdown(
    '<style>[data-testid="stChatMessage"] { padding-right: 1rem; }</style>',
    unsafe_allow_html=True,
)
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
    memory_panel()
    st.caption(f"API: `{API_URL}` · thread `{st.session_state.thread_id[:8]}`")

if st.session_state.summary:
    with st.expander("Earlier messages were condensed into a summary"):
        st.markdown(st.session_state.summary)


def run_assistant(path: str, body: dict[str, Any]) -> None:
    """Stream one assistant turn (a new message, or a resume after approval) into the page."""
    with st.chat_message("assistant"):
        status_slot = st.empty()
        status = status_slot.status("Thinking…")
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = []
        pending: dict[str, Any] = {}

        def tokens() -> Iterator[str]:
            for event, data in stream_events(path, body):
                if event == "tool_start":
                    tools_used.append(data["name"])
                    label = TOOL_LABELS.get(data["name"], f"Running {data['name']}")
                    query = (data.get("input") or {}).get("query", "")
                    status.update(label=f"{label}…")
                    status.write(f"{label}: “{query}”" if query else label)
                elif event == "tool_end":
                    sources.extend(data["sources"])
                elif event == "interrupt":
                    pending.update(data)
                elif event == "token":
                    yield data["token"]

        try:
            stream = tokens()
            # Wait for the first token under the status spinner (tools run before it).
            first = next(stream, "")
            if pending:
                # Paused for approval; the card below takes over.
                st.session_state.pending = pending
                st.rerun()
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


def approval_card(pending: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask about each pending tool call. Returns the decisions, in order, once submitted."""
    actions = pending["action_requests"]
    with st.chat_message("assistant"):
        st.markdown(
            "I'd like to run this, but it needs your OK first."
            if len(actions) == 1
            else f"I'd like to run these {len(actions)} steps, but they need your OK first."
        )
        with st.form("approval"):
            for i, action in enumerate(actions):
                st.markdown(f"**{TOOL_LABELS.get(action['name'], action['name'])}**")
                st.warning(action.get("description", ""), icon=":material/shield:")
                if list(action["args"]) == ["query"]:
                    st.text_input(
                        "Query (edit it to search for something else)",
                        value=action["args"]["query"],
                        key=f"query-{i}",
                    )
                else:
                    st.text_area(
                        "Arguments (JSON)",
                        value=json.dumps(action["args"], indent=2),
                        key=f"args-{i}",
                    )
                st.radio("Decision", ["Approve", "Reject"], horizontal=True, key=f"action-{i}")
                st.text_input("Reason, if rejecting (optional; the bot sees it)", key=f"why-{i}")
            if not st.form_submit_button("Submit", type="primary"):
                return None

    decisions: list[dict[str, Any]] = []
    for i, action in enumerate(actions):
        if st.session_state[f"action-{i}"] == "Reject":
            reason = st.session_state[f"why-{i}"]
            decisions.append({"type": "reject", **({"message": reason} if reason else {})})
            continue
        if f"query-{i}" in st.session_state:
            args = {"query": st.session_state[f"query-{i}"]}
        else:
            try:
                args = json.loads(st.session_state[f"args-{i}"])
            except json.JSONDecodeError:
                st.error(f"The arguments for {action['name']} aren't valid JSON.")
                return None
        if args == action["args"]:
            decisions.append({"type": "approve"})
        else:
            decisions.append(
                {"type": "edit", "edited_action": {"name": action["name"], "args": args}}
            )
    return decisions


def remember_button(index: int) -> None:
    """Push this reply, with the question it answered, into long-term memory
    (POST .../messages/{index}/remember)."""
    if st.button("", icon=":material/bookmark_add:", key=f"remember-{index}", help="Remember this"):
        with api() as client:
            r = client.post(f"/threads/{st.session_state.thread_id}/messages/{index}/remember")
        if r.status_code == 202:
            st.toast("Got it, I'll remember that.")
        else:
            st.toast("Couldn't remember that message.")


for index, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        # The message, with a "Remember this" button beside each of the bot's replies.
        text, button = st.columns([20, 1])
        with text:
            st.markdown(msg["content"])
        if msg["role"] == "assistant":
            with button:
                remember_button(index)
        render_sources(msg.get("sources", []), msg["content"])

pending = st.session_state.pending
if pending and (decisions := approval_card(pending)):
    st.session_state.pending = None
    run_assistant(f"/threads/{st.session_state.thread_id}/resume/stream", {"decisions": decisions})

prompt = st.chat_input(
    "Approve or reject the step above first" if pending else "Ask something",
    disabled=bool(pending),
)
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    run_assistant("/chat/stream", {"message": prompt, "thread_id": st.session_state.thread_id})
