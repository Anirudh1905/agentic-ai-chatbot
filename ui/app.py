import itertools
import json
import os
import uuid
from collections.abc import Iterator

import httpx
import streamlit as st
from httpx_sse import connect_sse

API_URL = os.environ.get("CHATBOT_API_URL", "http://localhost:8000")


class StreamError(Exception):
    pass


def stream_reply(message: str, thread_id: str) -> Iterator[str]:
    with (
        httpx.Client(base_url=API_URL, timeout=httpx.Timeout(10.0, read=300.0)) as client,
        connect_sse(
            client, "POST", "/chat/stream", json={"message": message, "thread_id": thread_id}
        ) as source,
    ):
        for sse in source.iter_sse():
            data = json.loads(sse.data)
            if sse.event == "token":
                yield data["token"]
            elif sse.event == "error":
                raise StreamError(data["message"])
            elif sse.event == "done":
                return


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
    st.info("Phase 1: the bot doesn't remember earlier messages yet. Memory arrives in Phase 3.")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("Ask something"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            tokens = stream_reply(prompt, st.session_state.thread_id)
            # Reasoning models can take a few seconds before the first token.
            with st.spinner("Thinking…"):
                first = next(tokens, "")
            reply = st.write_stream(itertools.chain([first], tokens))
        except StreamError as e:
            st.error(str(e))
        except httpx.HTTPError:
            st.error(f"Can't reach the API at {API_URL}. Is it running?")
        else:
            st.session_state.messages.append({"role": "assistant", "content": reply})
