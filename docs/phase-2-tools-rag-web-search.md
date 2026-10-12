# Phase 2: Tools (RAG + Web Search)

**Goal:** Turn the single LLM call into a ReAct-style agent that decides when to look up internal documents, when to search the web, and when to answer directly.

[← Phase 1](phase-1-foundation.md) · [Overview](README.md) · [Next: Phase 3 →](phase-3-short-term-memory-checkpointing.md)

## Scope

**In scope**
- Ingestion pipeline: load → clean → chunk → embed → index into pgvector
- `search_knowledge_base` tool (RAG retrieval with metadata filters)
- `web_search` tool (Tavily)
- Agent loop: LLM ↔ tool node until the model returns a final answer
- Citations in answers (doc chunk IDs or URLs)

**Out of scope:** approval gates (Phase 4), PII filtering of tool inputs (Phase 5).

## Architecture

```mermaid
flowchart TB
    subgraph Offline["Offline: ingestion"]
        direction LR
        Src[PDF / DOCX / HTML / MD] --> Load[Loader]
        Load --> Clean[Clean + normalize]
        Clean --> Chunk[Chunker<br/>~500-800 tokens,<br/>10-15% overlap]
        Chunk --> Embed[Embedding model]
        Embed --> Idx[(pgvector<br/>chunks table)]
    end

    subgraph Online["Online: agent loop"]
        direction TB
        START((START)) --> LLM[llm_node<br/>tools bound]
        LLM -->|tool_calls| TN[tool_node]
        TN --> LLM
        LLM -->|no tool_calls| END((END))
    end

    TN --> KB[search_knowledge_base]
    TN --> WS[web_search]
    KB -->|hybrid search| Idx
    KB --> RR[Reranker<br/>optional]
    WS --> Tavily[Tavily API]
```

## Tool-use sequence

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant L as LLM node
    participant T as Tool node
    participant KB as Knowledge base
    participant W as Web search

    U->>L: "What's our refund policy, and how does it compare to Amazon's?"
    L->>T: tool_calls: search_knowledge_base("refund policy")
    T->>KB: hybrid search, top_k=8
    KB-->>T: chunks + source metadata
    T-->>L: ToolMessage(chunks)
    L->>T: tool_calls: web_search("Amazon refund policy 2026")
    T->>W: query
    W-->>T: results + URLs
    T-->>L: ToolMessage(results)
    L-->>U: Answer with citations [doc:policy.pdf p.3] [amazon.com/...]
```

The model can also issue both tool calls in one turn. The tool node runs them in parallel.

## Components

### Ingestion

| Step | Default | Notes |
|------|---------|-------|
| Load | `unstructured` or `pymupdf` for PDFs, `markdownify` for HTML | Keep page numbers and headings as metadata |
| Chunk | Recursive splitter on headings → paragraphs → sentences | Store the heading path in each chunk (`"Policies > Refunds > EU"`) so retrieval results make sense on their own |
| Embed | Voyage `voyage-3` or `bge-m3` | Pin the model version. Changing it means re-embedding everything |
| Index | pgvector `HNSW` index + Postgres full-text `tsvector` | Both are needed for hybrid search |
| Dedup | Hash of normalized chunk text | Re-ingesting a doc should be a no-op |

Table sketch:

```sql
CREATE TABLE chunks (
  id          uuid PRIMARY KEY,
  doc_id      text NOT NULL,
  source_uri  text NOT NULL,
  heading     text,
  page        int,
  content     text NOT NULL,
  content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding   vector(1024) NOT NULL,
  metadata    jsonb DEFAULT '{}',
  content_hash text UNIQUE
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks USING gin (content_tsv);
```

### `search_knowledge_base` tool

```python
@tool
async def search_knowledge_base(query: str, filters: dict | None = None, top_k: int = 6) -> list[dict]:
    """Search internal company documents. Use for questions about our
    policies, products, processes or anything not on the public web."""
```

- **Hybrid retrieval:** vector similarity + BM25-style full-text, merged with Reciprocal Rank Fusion.
- **Optional reranker** (Cohere Rerank, `bge-reranker`) over the top ~30 candidates. Usually the biggest quality win per line of code.
- Return `content`, `source_uri`, `heading`, `page` and a stable `chunk_id` so the model can cite.

### `web_search` tool

```python
@tool
async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the public web for current events, third-party info,
    or anything that changes over time."""
```

- Wrap Tavily (`langchain-tavily`), trim each result to title, URL and a snippet of 500–1000 characters.
- Apply a timeout and retry with backoff. Return a structured error string on failure rather than raising, so the model can recover.

### Tool descriptions matter

The model picks tools based on the docstrings. Write them as routing rules: what the tool is for, what it's *not* for, and an example query. Most "wrong tool" bugs get fixed here rather than in code.

### Graph changes

```python
llm_with_tools = llm.bind_tools([search_knowledge_base, web_search])
g.add_node("llm", llm_node)
g.add_node("tools", ToolNode([search_knowledge_base, web_search]))
g.add_conditional_edges("llm", tools_condition)   # → "tools" or END
g.add_edge("tools", "llm")
```

Set a `recursion_limit` (e.g. 12) on invocation so a confused model can't loop forever.

## Implementation steps

1. Create `chunks` table migration (Alembic).
2. Build `ingestion/` CLI: `python -m chatbot.ingestion ingest ./data/docs`.
3. Implement hybrid search + RRF in `tools/rag.py`; unit-test the fusion logic.
4. Implement `tools/web_search.py` with timeouts and result trimming.
5. Add a `tools/registry.py` so Phase 4 can attach approval policies per tool.
6. Update graph with `ToolNode` and conditional edges.
7. Update the system prompt with citation rules.
8. Emit `tool_start` / `tool_end` SSE events.

## Evaluation

Build a small golden set now (30–50 questions) and keep growing it:

| Metric | How |
|--------|-----|
| Retrieval recall@k | Does the correct chunk appear in the top k? Labelled question → chunk pairs |
| Tool routing accuracy | For each question, the expected tool(s). Did the agent pick them? |
| Answer faithfulness | LLM-as-judge (or Ragas): are claims supported by retrieved context? |
| Citation correctness | Does every cited source actually contain the claim? |

## Exit criteria

- [ ] Ingesting a folder of docs is idempotent
- [ ] Internal questions route to RAG, current-events questions route to web search, small talk uses neither
- [ ] Answers include citations that resolve to real chunks or URLs
- [ ] Recall@6 ≥ 0.8 on the golden set (tune to your data)
- [ ] Tool failures produce a graceful answer rather than a 500

## Risks and notes

- **Prompt injection through retrieved content.** Web pages and documents can contain instructions. Wrap tool output in clear delimiters, tell the model that tool content is data, and never let tool output trigger tools with side effects without the Phase 4 approval gate.
- **Chunking is the main quality lever.** If answers are poor, look at chunk boundaries before swapping models.
- **Cost.** Each tool round trip resends the whole conversation. Prompt caching on the system prompt and tool definitions helps a lot.
