# Agentic AI Chatbot: Build Plan

An agentic chatbot that can answer from private documents (RAG), search the web, pause for human approval, protect personal data, and remember things both within a conversation and across conversations.

The work is split into phases. Each phase ends with something that runs and can be tested, and each one builds on the previous phase.

## Phases

| # | Phase | Delivers | Depends on | Status |
|---|-------|----------|------------|--------|
| 1 | [Foundation](phase-1-foundation.md) | API, agent, streaming, config, tracing | None | Built |
| 2 | [Tools: RAG + Web Search](phase-2-tools-rag-web-search.md) | Document ingestion, vector retrieval tool, web search tool, tool routing | 1 | Built |
| 3 | [Short-Term Memory & Checkpointing](phase-3-short-term-memory-checkpointing.md) | Per-thread state saved to Postgres, resume, summarization of old turns | 1 | Built |
| 4 | [Human-in-the-Loop](phase-4-human-in-the-loop.md) | Approval on risky tool calls, interrupt and resume, edit or reject flows | 3 | Built (audit table deferred) |
| 5 | [PII Detection & Masking](phase-5-pii-detection-masking.md) | Input/output guardrails, reversible masking, PII-safe tools, logs and checkpoints | 2, 3 | Planned |
| 6 | [Long-Term Memory](phase-6-long-term-memory.md) | Cross-thread user memory, extraction, semantic recall | 3, 5 | Planned |
| 7 | [Production Hardening](phase-7-production-hardening.md) | Evals, observability, auth, rate limits, deployment | All | Planned |

The agent is LangChain's `create_agent` (a LangGraph graph) with middleware. Each feature is a middleware hook around the model call, built-in wherever LangChain has one: `SummarizationMiddleware` (Phase 3), `HumanInTheLoopMiddleware` (Phase 4), `ModelCallLimitMiddleware` and `ToolRetryMiddleware`, and later `PIIMiddleware` (Phase 5).

### Why this order

- **Checkpointing comes before HITL.** Pausing a graph for a human means saving its state somewhere and loading it back later. The checkpointer from Phase 3 is what makes Phase 4 possible.
- **PII comes before long-term memory.** Long-term memory saves facts about the user for good. If masking isn't in place first, raw emails, phone numbers and IDs end up in the memory store and are hard to remove.
- **Tools come early.** RAG and web search are the core value of the bot, and the later phases (approval gates, PII-safe tool calls) need real tools to wrap.

```mermaid
flowchart LR
    P1[Phase 1<br/>Foundation] --> P2[Phase 2<br/>RAG + Web Search]
    P1 --> P3[Phase 3<br/>Short-Term Memory<br/>& Checkpointing]
    P3 --> P4[Phase 4<br/>Human-in-the-Loop]
    P2 --> P5[Phase 5<br/>PII Detection<br/>& Masking]
    P3 --> P5
    P3 --> P6[Phase 6<br/>Long-Term Memory]
    P5 --> P6
    P4 --> P7[Phase 7<br/>Production Hardening]
    P6 --> P7
```

## Target architecture (end state)

Solid boxes exist today; dashed ones (PII guards, long-term memory) arrive in Phases 5 and 6.

```mermaid
flowchart TB
    User([User]) <--> UI[Chat UI]
    UI <-->|HTTP / SSE| API[FastAPI service]

    subgraph Agent["create_agent + middleware"]
        direction TB
        PIIin[PII guard: input<br/>before_agent] -.-> LoadMem[Recall long-term memory<br/>before_agent]
        LoadMem -.-> Sum[Summarization<br/>before_model]
        Sum --> LLM[model<br/>system prompt, call limit]
        LLM -->|tool calls| HITL{HumanInTheLoopMiddleware<br/>needs approval?}
        HITL -->|yes| Interrupt[[interrupt: wait for human]]
        Interrupt --> Tools
        HITL -->|no| Tools[tools<br/>retried once on failure]
        Tools --> Sum
        LLM -->|final answer| PIIout[PII guard: output<br/>after_model]
        PIIout -.-> SaveMem[Extract + save memories<br/>after_agent]
    end

    API <--> Agent

    Tools --> RAG[Knowledge base search<br/>hybrid + rerank]
    Tools --> Web[Web search<br/>DuckDuckGo]
    RAG --> VDB[(pgvector<br/>chunks)]
    Agent <-->|checkpoints| CP[(Postgres<br/>checkpointer)]
    LoadMem -.- LTM[(Postgres store<br/>long-term memory)]
    SaveMem -.- LTM
    PIIin -.- Vault[(PII vault<br/>encrypted)]
    PIIout -.- Vault
    Agent -.traces.-> Obs[Langfuse]
    API --> Threads[(threads table)]

    classDef planned stroke-dasharray: 5 5
    class PIIin,LoadMem,PIIout,SaveMem,LTM,Vault planned
```

## Default tech stack

These are defaults, not hard requirements. Each phase doc notes where a swap is easy.

| Concern | Choice | Notes |
|---------|--------|-------|
| Language | Python 3.12 | |
| Agent framework | LangChain `create_agent` + middleware, on LangGraph | LangGraph's checkpointers, `interrupt()` and `Store` cover persistence, HITL and long-term memory; LangChain's middleware covers summarization, approvals, limits, retries and PII. Check `langchain.agents.middleware` before writing a custom hook |
| LLM | OpenAI (`gpt-5.4` for chat; `gpt-5-mini` when cost matters more), a small model such as `gpt-5-nano` for cheap side tasks | Model, effort and an optional gateway URL are set in `.env`. Summarization, memory extraction and query rewriting can run on the small model |
| API | FastAPI + Server-Sent Events | |
| Vector DB | Postgres + pgvector | One database for vectors, checkpoints and memory. Qdrant is a straightforward swap |
| Embeddings | OpenAI `text-embedding-3-small` (1536 dims), through the same endpoint as the chat model | Voyage or `bge-m3` are swaps, but need a migration and a re-ingest |
| Reranker | `BAAI/bge-reranker-v2-m3-int8`, local via fastembed (ONNX) | Top-1 retrieval 96% → 100% on the golden set |
| Web search | DuckDuckGo via `ddgs` (no API key) | Tavily, Brave Search and Exa return richer snippets but need a key |
| PII | Microsoft Presidio + spaCy | Add custom recognizers for domain-specific IDs |
| Tracing | Langfuse (self-hosted) or LangSmith | |
| Packaging | `uv`, Docker Compose for local infra | |

## Repository layout

```
agentic-ai-chatbot/
├── docs/                      # these documents
├── src/chatbot/
│   ├── agent/                 # builder.py (create_agent), middleware.py, approvals.py (HITL rules)
│   ├── api/                   # routes/, runs.py, streaming.py (SSE), schemas.py, request_logging.py
│   ├── tools/                 # rag.py, web_search.py, rerank.py, registry.py
│   ├── ingestion/             # loaders, chunking, indexing CLI
│   ├── memory/                # threads.py (thread metadata), retention.py; Phase 6: long-term memory
│   ├── pii/                   # Phase 5: detector, vault, recognizers
│   ├── prompts/               # system.md, summarize.md
│   ├── config.py
│   └── observability.py
├── migrations/                # Alembic: chunks, threads
├── tests/
│   ├── unit/
│   ├── integration/           # need Postgres (make up)
│   └── evals/                 # golden set, make eval
├── ui/app.py                  # Streamlit chat UI
├── docker-compose.yml         # postgres+pgvector, pgweb, langfuse
├── Tiltfile
├── pyproject.toml
└── .env.example
```
