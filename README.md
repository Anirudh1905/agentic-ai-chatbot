# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, and short-term plus long-term memory.

See the phased build plan in [docs/README.md](docs/README.md). Phases 1 (foundation) and 2 (RAG and web search tools) are implemented.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker and [Tilt](https://docs.tilt.dev/install.html).

```bash
cp .env.example .env        # add OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway)
uv sync --all-groups
tilt up                     # Postgres, Langfuse, migrations, sample docs, API and chat UI
```

Tilt's dashboard is at http://localhost:10350. From there:

| Service | URL |
|---------|-----|
| Chat UI (Streamlit) | http://localhost:8501 |
| API | http://localhost:8000 (OpenAPI docs at `/docs`) |
| Langfuse (tracing) | http://localhost:3000, login `dev@local.test` / `password` |
| Postgres + pgvector | `localhost:5433` |

Postgres is mapped to host port 5433 so it doesn't clash with a local Postgres on 5432. Set `POSTGRES_PORT` to change it.

Without Tilt: `make up`, `make migrate`, `make ingest`, then `make dev` and `make ui` in separate terminals.

Try asking "Can I return a tent I bought at 40% off?" (answered from the sample docs) or "What's the latest news about REI?" (web search).

### Knowledge base

`tilt up` ingests the fictional Acme Outfitters docs in `data/sample/`. To add your own documents (Markdown, text, HTML, PDF, Word):

```bash
make ingest PATH_TO_DOCS=path/to/docs
```

Search combines vector and full-text matches, then a local cross-encoder reranks the top 8 before the best 6 go to the model. The reranker model (~570 MB) is downloaded once by Tilt's `reranker-model` step, or on first use, and runs on CPU (about 0.5 s per search on an M3 Pro).

Ingestion is incremental: unchanged chunks are skipped, and `--prune` (on by default in `make ingest`) removes chunks for files that were deleted. The `search_knowledge_base` tool description in `src/chatbot/tools/rag.py` and the system prompt in `src/chatbot/prompts/system.md` describe the Acme docs; update both when you ingest a different corpus, since the model uses them to decide when to search.

### Web search

The `web_search` tool uses DuckDuckGo through the [`ddgs`](https://pypi.org/project/ddgs/) package, so it needs no API key. It tries DuckDuckGo first and falls back to `ddgs`'s multi-engine search if that returns nothing. Results are short snippets (about 150 characters), so answers that need detail from a page, like exact forecasts, can be thin. Set `WEB_SEARCH_ENABLED=false` to turn it off.

Behind a TLS-inspecting proxy (such as Cloudflare Gateway or Zscaler), Python uses the OS certificate store via `truststore`, so outbound HTTPS works without extra setup.

### Tracing

Langfuse runs locally from `docker-compose.langfuse.yml`, which `docker-compose.yml` includes. On first start it creates a project with fixed API keys, and `.env.example` already contains them, so every chat request is traced with no setup. The first `tilt up` pulls about 2 GB of images, and Langfuse takes a minute or two to become healthy.

The credentials in that file are local-only placeholders. To trace somewhere else, such as Langfuse Cloud, change the `LANGFUSE_*` values in `.env`.

```bash
curl -N localhost:8000/chat/stream \
  -H 'content-type: application/json' \
  -d '{"message": "hi"}'
```

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Full reply and its sources as JSON |
| `POST /chat/stream` | Server-Sent Events: `tool_start` / `tool_end` (with sources) and `token`, then `done` (or `error`) |
| `GET /healthz` | Liveness |

## Configuration

Set in `.env` (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `OPENAI_API_KEY` | unset | Required |
| `OPENAI_BASE_URL` | unset (api.openai.com) | Set to use an OpenAI-compatible gateway |
| `MODEL` | `gpt-5-mini` | Any chat model the endpoint serves. Use `gpt-5.4` for harder questions (slower) |
| `REASONING_EFFORT` | `minimal` | `none` to `high`. Reasoning models only (gpt-5.x, o-series); remove for gpt-4.x |
| `MAX_TOKENS` | unset | Optional cap on output tokens |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must produce 1536-dim vectors, or add a migration and re-ingest |
| `WEB_SEARCH_ENABLED` | `true` | DuckDuckGo web search; no key needed |
| `RETRIEVAL_TOP_K` | `6` | Chunks returned per knowledge base search |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3-int8` | Local cross-encoder that reorders the top `RERANK_CANDIDATES` (8) results. Empty turns it off |
| `MAX_AGENT_STEPS` | `12` | Cap on model/tool round trips per request |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | local dev keys | Tracing turns on when both are set |
| `LANGFUSE_HOST` | `http://localhost:3000` | |

## Development

```bash
make check    # ruff lint + format check, mypy, pytest (integration tests need `make up`)
make fmt      # auto-fix lint and formatting
make eval     # retrieval recall and tool routing on tests/evals/golden.jsonl (calls the model)
```
