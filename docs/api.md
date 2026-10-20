# API

The endpoints, the checks every request passes, and each endpoint's flow. The interactive OpenAPI docs are at http://localhost:8088/api/docs.

[Overview](../README.md) · [How it works](how-it-works.md) · [API](api.md) · [Configuration](configuration.md) · [Development](development.md) · [Runbook](runbook.md)

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /chat` | Full reply, its sources and the `thread_id` as JSON, or `pending` tool calls if it paused for approval. Omit `thread_id` to start a new thread. 409 while the thread waits for approval |
| `POST /chat/stream` | Server-Sent Events: `tool_start` / `tool_end` (with sources) and `token`, then `done` with the `thread_id` (or `error`). An `interrupt` event before `done` means the run is waiting for approval |
| `POST /threads` | Create an empty thread |
| `GET /threads` | The caller's threads, most recent first |
| `GET /threads/{id}/messages` | The thread's messages (with each reply's sources), the summary, and `pending` if it's waiting for approval |
| `GET /threads/{id}/history` | Checkpoints, newest first, for debugging |
| `POST /threads/{id}/resume` | Send approval `decisions`, finish an interrupted run, or replay from `checkpoint_id` |
| `POST /threads/{id}/resume/stream` | The same, streamed like `/chat/stream` |
| `POST /threads/{id}/messages/{index}/remember` | Push one of the bot's replies, with the question before it, to your long-term memory (the "Remember this" button); 202 |
| `DELETE /threads/{id}` | Delete the thread and all its checkpoints |
| `GET /me/memory` | What the bot remembers about you across threads (your long-term memories), newest first |
| `DELETE /me/memory/{id}` | Forget one memory |
| `DELETE /me/memory` | Forget everything |
| `DELETE /me` | Delete your account and all your data: threads, checkpoints, memories (the "Delete my account" button); 204 |
| `POST /auth/register` | Create an account (`{email, password}`); 409 if the email is taken |
| `POST /auth/login` | Log in (form `username` = email, `password`, optionally `openai_api_key` / `openai_base_url`, checked); returns a bearer token |
| `GET /auth/me` | The signed-in account |
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness: 503 when the database doesn't answer |
| `GET /metrics` | Prometheus metrics (404 when `METRICS_PORT` moves them to their own port, as in the cluster) |

All endpoints except `/healthz`, `/readyz`, `/metrics`, `/auth/register` and `/auth/login` need a bearer token (401 without a valid one), and answer 429 with `Retry-After` over a rate limit. The ones that start a model run (`/chat`, `/chat/stream`, `.../resume`, `.../resume/stream`) also answer 429 once the user's daily token budget is spent.


## The checks on every request

Every request passes the same checks before its route runs; the routes that start a model run add the token budget:

```mermaid
flowchart LR
    Req["request"] --> IP{"per-IP limit<br/>(Redis)"}
    IP -->|over| R429["429 + Retry-After"]
    IP --> Public{"/healthz · /readyz · /metrics<br/>/auth/register · /auth/login?"}
    Public -->|yes| Route["the route"]
    Public -->|no| JWT{"bearer JWT valid,<br/>account exists? (Postgres)"}
    JWT -->|no| R401["401"]
    JWT --> User{"per-user limit<br/>(Redis)"}
    User -->|over| R429
    User --> Run{"starts a model run?<br/>/chat · /resume"}
    Run -->|no| Route
    Run --> Budget{"daily token budget left?<br/>(Redis)"}
    Budget -->|spent| R429
    Budget --> Route
```


## A chat, end to end


```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Streamlit UI
    participant API as API pod (FastAPI)
    participant R as Redis
    participant PG as Postgres
    participant A as Agent (create_agent + middleware)
    participant M as Model gateway
    participant T as Tools
    participant W as Queue + worker
    U->>UI: a message (browser → Envoy Gateway → ui pod)
    UI->>API: POST /chat/stream, Bearer JWT
    API->>R: per-IP and per-user limits, token budget
    API->>PG: the account behind the token
    API->>API: redact PII in the message
    API->>PG: claim the thread (404 if someone else's), take its lease (409 if busy)
    API->>A: run it, streamed back as Server-Sent Events
    A->>PG: the thread's checkpoint
    A->>PG: recall long-term memories (by embedding)
    A->>M: system prompt + memories + history (retry → breaker → fallback gpt-4.1)
    M-->>A: a tool call
    alt a sensitive web search
        A-->>UI: interrupt, then done: the run pauses for approval
        Note over UI,API: the user's decision comes back as POST /threads/{id}/resume/stream
    else
        A-->>UI: tool_start
        A->>T: search_knowledge_base (embed, hybrid search, rerank) or web_search (Redis cache, else DuckDuckGo)
        T-->>A: results, PII-redacted
        A-->>UI: tool_end with the sources
        A->>M: the results
        M-->>A: the reply, token by token
        A-->>UI: token events (PII redacted as they stream)
    end
    A->>R: the run's tokens against the daily budget
    A->>PG: a checkpoint after every step
    opt the thread passed CONTEXT_MAX_TOKENS
        A->>M: summarize the older turns (gpt-4.1-mini), after the reply
        A->>W: push the summarized turns to long-term memory (SQS), the worker writes them
    end
    API-->>UI: done, with the thread id
    API->>PG: release the lease
    A--)A: trace to Langfuse
```


## Each endpoint's flow


<details>
<summary><code>POST /auth/register</code>, <code>POST /auth/login</code>, <code>GET /auth/me</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant R as Redis
    participant PG as Postgres
    participant M as The user's model endpoint
    C->>API: POST /auth/register {email, password}
    API->>R: register and log-in attempts per IP
    API->>PG: create the user (password hashed)
    API-->>C: 201, or 409 if the email is taken
    C->>API: POST /auth/login (form: username, password, optional openai_api_key / base_url)
    API->>R: register and log-in attempts per IP
    opt their own key
        API->>M: GET /models, then a 16-token chat call
        M-->>API: works, or 400 "Incorrect OpenAI base URL or API key"
    end
    API->>PG: check the password
    API-->>C: a signed JWT (JWT_TTL_MINUTES), or 401
    Note over C,API: the key is never stored: the client sends it with each run (X-OpenAI-Key)
    C->>API: GET /auth/me
    API-->>C: the account behind the token
```

</details>

<details>
<summary><code>POST /chat</code>, <code>POST /chat/stream</code></summary>

The flow above. `/chat` is the same run, answered as one JSON body (the reply, its sources, the thread id, or `pending` tool calls) instead of a stream. Either answers 409 if the thread is waiting for approval or is already answering.

</details>

<details>
<summary><code>POST /threads/{id}/resume</code>, <code>POST /threads/{id}/resume/stream</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant PG as Postgres
    participant A as Agent
    C->>API: POST /threads/{id}/resume {decisions?, checkpoint_id?}
    API->>PG: own the thread (404), take its lease (409 if busy)
    API->>PG: its state: the latest checkpoint, or checkpoint_id's
    alt waiting for approval
        API->>API: one decision per pending call (422 otherwise), redact edits and reasons
        API->>A: Command(resume=decisions): approve runs the call, edit runs the edited one, reject tells the model
    else stopped partway (a crash, a disconnect)
        API->>A: continue from the checkpoint
    else checkpoint_id
        API->>A: replay from it: a fork that becomes the thread's latest state
    else finished
        API-->>C: 409 "Nothing to resume"
    end
    A-->>C: the reply (JSON, or streamed like /chat/stream)
    API->>PG: release the lease
```

</details>

<details>
<summary><code>POST /threads</code>, <code>GET /threads</code>, <code>GET /threads/{id}/messages</code>, <code>GET /threads/{id}/history</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant PG as Postgres
    C->>API: POST /threads {title?}
    API->>PG: insert a thread for the user
    API-->>C: 201 {id, title, created_at}
    C->>API: GET /threads?limit=
    API->>PG: the user's threads, most recent first
    C->>API: GET /threads/{id}/messages
    API->>PG: own the thread (404), then its latest checkpoint
    API-->>C: messages with each reply's sources, the summary, pending approval if any
    C->>API: GET /threads/{id}/history?limit=
    API->>PG: its checkpoints, newest first
    API-->>C: id, parent, step, next node, last message, for debugging and time travel
```

</details>

<details>
<summary><code>POST /threads/{id}/messages/{index}/remember</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant PG as Postgres
    participant Q as SQS queue
    participant W as Worker
    participant M as Memory model
    C->>API: POST /threads/{id}/messages/3/remember
    API->>PG: own the thread, read its messages
    API->>API: 404 no such message, 422 not a reply, 409 memory off
    API->>Q: a remember push: the reply and the question before it
    API-->>C: 202
    Q->>W: the push (one user's in order)
    W->>M: the facts in it, and what each changes
    W->>PG: add, update or delete the user's memories
```

</details>

<details>
<summary><code>DELETE /threads/{id}</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant PG as Postgres
    C->>API: DELETE /threads/{id}
    API->>PG: own the thread (404), take its lease (409 while a run is going)
    API->>PG: every checkpoint, then its memory cursor, then the thread
    API-->>C: 204 (memories already pushed from it stay)
```

</details>

<details>
<summary><code>GET /me/memory</code>, <code>DELETE /me/memory/{id}</code>, <code>DELETE /me/memory</code>, <code>DELETE /me</code></summary>

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API
    participant PG as Postgres (memory store, threads, users)
    C->>API: GET /me/memory
    API->>PG: the user's memories, newest first
    C->>API: DELETE /me/memory/{id} or DELETE /me/memory
    API->>PG: delete one (404 if not theirs), or all, no turn recalls them after
    C->>API: DELETE /me
    API->>API: wait for a memory push in progress for the user, and hold new ones
    API->>PG: each thread's checkpoints and the thread, the memories and cursors, then the account
    API-->>C: 204: the token stops working
```

</details>

<details>
<summary><code>GET /healthz</code>, <code>GET /readyz</code>, <code>GET /metrics</code></summary>

```mermaid
sequenceDiagram
    participant K as kubelet / Prometheus
    participant API as API
    participant PG as Postgres
    K->>API: GET /healthz (liveness)
    API-->>K: 200 while the process runs
    K->>API: GET /readyz (readiness)
    API->>PG: SELECT 1
    API-->>K: 200, or 503: the pod leaves the Service until Postgres answers
    K->>API: GET :9100/metrics (METRICS_PORT, /metrics on the app port is 404 then)
    API-->>K: Prometheus metrics
```

</details>
