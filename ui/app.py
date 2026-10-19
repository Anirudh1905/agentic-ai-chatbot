import itertools
import json
import os
import time
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
    "remember": "Saving to memory",
}


class StreamError(Exception):
    pass


def api() -> httpx.Client:
    """The API, as the signed-in user (the token from POST /auth/login), with their own model
    endpoint and key if they logged in with one (checked at login, kept only in this session)."""
    headers = {"Authorization": f"Bearer {st.session_state.token}"}
    if key := st.session_state.get("openai_api_key"):
        headers["X-OpenAI-Key"] = key
        if base_url := st.session_state.get("openai_base_url"):
            headers["X-OpenAI-Base-URL"] = base_url
    return httpx.Client(
        base_url=API_URL,
        headers=headers,
        timeout=httpx.Timeout(10.0, read=300.0),
        event_hooks={"response": [log_out_on_401]},
    )


def log_out_on_401(response: httpx.Response) -> None:
    # The token expired (JWT_TTL_MINUTES) or was refused: back to the login screen.
    if response.status_code == 401:
        log_out()
        st.rerun()  # stops this run here, so nothing reads the cleared session


def log_out() -> None:
    for key in list(st.session_state):
        del st.session_state[key]
    st.query_params.clear()


def login_screen() -> None:
    """Log in, or create an account (POST /auth/register, then log in). Optionally with the
    user's own OpenAI-compatible endpoint and key, which the API checks before logging in."""
    with st.expander("Use your own OpenAI credentials (optional)"):
        st.text_input("OpenAI base URL", placeholder="https://api.openai.com/v1", key="base-url-in")
        st.text_input("OpenAI API key", type="password", key="api-key-in")
        st.caption(
            "Your chats then run on your key. It's checked at login and kept only in this session."
        )
    log_in_tab, register_tab = st.tabs(["Log in", "Create account"])
    with log_in_tab, st.form("log-in"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary"):
            log_in(email, password)
    with register_tab, st.form("register"):
        email = st.text_input("Email", key="new-email")
        password = st.text_input("Password (8 characters or more)", type="password", key="new-pw")
        if st.form_submit_button("Create account", type="primary"):
            try:
                created = httpx.post(
                    f"{API_URL}/auth/register", json={"email": email, "password": password}
                )
            except httpx.HTTPError:
                st.error(f"Can't reach the API at {API_URL}. Is it running?")
                return
            if created.status_code == 201:
                log_in(email, password)
            elif created.status_code == 422:
                st.error("Enter a valid email and a password of at least 8 characters.")
            else:
                st.error(created.json().get("detail", "Couldn't create the account."))


def log_in(email: str, password: str) -> None:
    form = {"username": email, "password": password}
    base_url = st.session_state.get("base-url-in", "").strip()
    api_key = st.session_state.get("api-key-in", "").strip()
    if api_key:
        form |= {"openai_api_key": api_key, **({"openai_base_url": base_url} if base_url else {})}
    try:
        with st.spinner("Logging in…" + (" (checking your OpenAI key)" if api_key else "")):
            # Checking a key makes a small model call, so allow for it.
            resp = httpx.post(f"{API_URL}/auth/login", data=form, timeout=60)
    except httpx.HTTPError:
        st.error(f"Can't reach the API at {API_URL}. Is it running?")
        return
    if resp.status_code != 200:
        st.error(resp.json().get("detail", "Couldn't log in."))
        return
    st.session_state.token = resp.json()["access_token"]
    st.session_state.token_expires = time.time() + resp.json()["expires_in"]
    st.session_state.email = email.lower()
    st.session_state.openai_api_key = api_key or None
    st.session_state.openai_base_url = base_url or None
    st.rerun()


def refused(resp: httpx.Response) -> bool:
    """True (and a notice, once per page) if the API turned the request down: over a rate
    limit (429), say. The page keeps working with what it has."""
    if resp.status_code == 200:
        return False
    if not st.session_state.get("_notice_shown"):
        st.session_state._notice_shown = True
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after", "a few")
            st.toast(f"Too many requests. Try again in {retry} seconds.", icon=":material/timer:")
        else:
            st.toast(f"The API answered {resp.status_code}.", icon=":material/error:")
    return True


def list_threads() -> list[dict[str, Any]]:
    try:
        with api() as client:
            resp = client.get("/threads")
    except httpx.HTTPError:
        return []
    return [] if refused(resp) else resp.json()


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
    if resp.status_code == 404:
        return  # a new chat: the thread is created with its first message
    if not refused(resp):
        body = resp.json()
        st.session_state.messages = body["messages"]
        st.session_state.summary = body["summary"]
        st.session_state.pending = body["pending"]


def new_thread() -> None:
    # Created on the API side with the first message.
    load_thread(str(uuid.uuid4()))


def stream_events(path: str, body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    with api() as client, connect_sse(client, "POST", path, json=body) as source:
        if source.response.status_code != 200:
            # Refused before the run started: rate limited (429), or not signed in (401).
            source.response.read()
            raise StreamError(source.response.json().get("detail", source.response.text))
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
            resp = client.get("/me/memory")
    except httpx.HTTPError:
        return
    if refused(resp):
        return
    memories = resp.json()["memories"]
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
st.session_state._notice_shown = False  # refused() notices: one per page run

if time.time() > st.session_state.get("token_expires", 0):
    if "token" in st.session_state:
        log_out()
        st.info("Your session expired. Please log in again.")
    login_screen()
    st.stop()

if "thread_id" not in st.session_state:
    if thread := st.query_params.get("thread"):
        load_thread(thread)
    else:
        new_thread()

with st.sidebar:
    signed_in, out = st.columns([3, 1], vertical_alignment="center")
    signed_in.caption(f"Signed in as **{st.session_state.email}**")
    if st.session_state.get("openai_api_key"):
        st.caption(f"Model: your key, {st.session_state.openai_base_url or 'OpenAI'}")
    if out.button("", icon=":material/logout:", help="Log out"):
        log_out()
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
