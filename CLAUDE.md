# CLAUDE.md

Guidance for working in this repository. The README is the overview; `docs/` has the details.

## What this is

An agentic chatbot: FastAPI + LangChain's `create_agent` with middleware, LangGraph checkpoints and store in Postgres (pgvector), a Streamlit UI, Redis for limits, breakers and caches, an SQS queue (ElasticMQ locally) with a worker for memory pushes. It's deployed on a local kind cluster with Tilt; Langfuse runs in Docker Compose. Built in phases; each has a doc, `docs/phase-N-*.md`.

## Where things are

- `src/chatbot/agent/builder.py`: the agent and its middleware chain, in order. Read it first.
- `src/chatbot/api/`: routes, auth (`security.py`), the per-thread lease (`leases.py`), SSE (`streaming.py`).
- `src/chatbot/config.py`: every setting, with its default and why. A new setting goes here, in `.env.example` and in `docs/configuration.md`.
- `deploy/`: Kustomize (`base/`, `local/`, `ci/`), Helm values, and the scripts (`cluster.sh`, `smoke.sh`, `restore*.sh`, `outage-test.sh`).
- `tests/`: `unit/`, `integration/` (need the cluster), `evals/` (golden sets and the gate), `load/`, `outage/`. Shared fakes are in `tests/fakes.py`.

## Commands

```bash
make check         # ruff, ruff format --check, mypy (strict), pytest
make fmt           # fix lint and formatting
make tilt          # the whole stack on kind; make cluster and make secrets first
make eval          # every eval suite against the real model; writes tests/evals/report.json
make eval-check    # what CI runs: report.json must match the code, and not regress against main
make load USERS=10 DURATION=300
make outage-test   # gpt-5.4 fails at a proxy; chats must finish on gpt-4.1
make restore-test  # point-in-time restore of the Postgres backups
```

Integration tests skip when Postgres, ElasticMQ or Redis isn't reachable; CI sets `REQUIRE_DB`, `REQUIRE_SQS` and `REQUIRE_REDIS` so they can't. With Tilt up, they all run locally.

## Rules

- **LangChain and LangGraph built-ins first.** Check `langchain.agents.middleware` (retries, fallbacks, context editing, call limits, HITL, PII, summarization) before writing a middleware. Our own code exists only where no built-in does: the circuit breaker, the thread lease, the web search cache.
- **Any change under `src/chatbot`, `data/sample`, `tests/evals`, `pyproject.toml` or `uv.lock` changes the eval fingerprint** (`tests/evals/report.py`, `INPUTS`), comments included. Run `make eval` and commit the new `tests/evals/report.json`, or CI's eval check fails.
- **Never commit** `.env`, `deploy/local/secrets.env`, `deploy/local/backup-store.env` or `deploy/local/ca-bundle.pem`. They're gitignored; keep it so.
- **Never `tilt trigger` config, postgres, minio, elasticmq or reranker-volume** (nor press their update button): a trigger deletes every object in the resource before re-applying. config holds the namespace, so triggering it deletes everything; the others take their volumes with them. It wiped the local database and its backups once. To re-apply, stop Tilt and run `tilt up` again. Don't delete the `chatbot` namespace or run `tilt down` without asking: both delete every volume.
- **Only the `kind-chatbot` kube context.** Never run kubectl or Helm against any other cluster. The Tiltfile and the scripts pin the context; keep passing `--context kind-chatbot` explicitly.
- **New branches with `--no-track`** (`git checkout --no-track -b <name> main`): a branch that tracks `origin/main` once got pushed straight to main.
- **Designs and as-built notes go in the phase docs** (`docs/phase-N-*.md`), not anywhere else. When a later phase changes something an earlier doc describes, add a short "Since Phase N" note there.
- **Docs and docstrings say what the code does now.** Measured numbers carry their date and setup (the load test's results depend on the Docker VM's size). Mermaid diagrams: no `;` inside a message, since it ends the statement.

## Code style

- Match the surrounding code: its naming, its comment density, and its comments that explain why rather than what.
- Python 3.12, line length 100, `mypy --strict`, ruff rules `E F I B UP SIM ASYNC`.
- Tests describe behaviour in their names (`test_a_repeated_query_is_answered_from_the_cache`), and use the fakes rather than mocks where one exists.
- Commit messages: a short subject, then a body saying what changed and why.

## The local cluster

- **After the Docker VM restarts** (Rancher Desktop), run `make cluster` again: it raises the VM's inotify limits, which the restart resets. Without them kube-proxy fails with "too many open files" and pods crash-loop. Then stop Tilt and run `tilt up` again, which re-applies whatever is left in error. The [runbook](docs/runbook.md#nothing-works-after-docker-restarts) has the steps.
- **If `docker info` fails** after a restart, `/var/run/docker.sock` may point at Docker Desktop instead of Rancher Desktop; use `DOCKER_HOST=unix://$HOME/.rd/docker.sock`.
- **The reranker runs on the API pods' CPU**, one rerank at a time per pod. It's what limits latency under load, and why the API pods ask for 2 Gi.
- The model gateway caches prompt prefixes on its own and rejects `prompt_cache_key` with a 503.
