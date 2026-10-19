# Local dev: `tilt up`, then open the Tilt UI at http://localhost:10350

docker_compose('./docker-compose.yml')
dc_resource('postgres', labels=['infra'])
dc_resource('redis', labels=['infra'])
dc_resource('elasticmq', labels=['infra'])
dc_resource('pgweb', labels=['infra'], links=[link('http://localhost:8081', 'pgweb')])
dc_resource('prometheus', labels=['metrics'], links=[link('http://localhost:9091/alerts', 'Alerts')])
dc_resource('grafana', labels=['metrics'], links=[link('http://localhost:3001/d/chatbot', 'Dashboard')])

dc_resource('langfuse-web', labels=['langfuse'], links=[link('http://localhost:3000', 'Langfuse')])
for name in ['langfuse-worker', 'langfuse-postgres', 'langfuse-clickhouse', 'langfuse-minio', 'langfuse-redis']:
    dc_resource(name, labels=['langfuse'])

local_resource(
    'migrate',
    cmd='uv run python -m chatbot.migrate',
    deps=['migrations'],
    resource_deps=['postgres'],
    labels=['infra'],
)

local_resource(
    'ingest-sample',
    cmd='uv run python -m chatbot.ingestion data/sample --prune',
    deps=['data/sample'],
    resource_deps=['migrate'],
    labels=['infra'],
)

local_resource(
    'reranker-model',
    cmd='uv run python -m chatbot.tools.rerank',
    labels=['infra'],
)

local_resource(
    'api',
    serve_cmd='uv run uvicorn chatbot.api.main:app --reload --port 8000',
    deps=['pyproject.toml', 'uv.lock'],
    resource_deps=['migrate', 'reranker-model', 'redis'],
    readiness_probe=probe(http_get=http_get_action(port=8000, path='/healthz')),
    links=[link('http://localhost:8000/docs', 'API docs')],
    labels=['app'],
)

local_resource(
    'ui',
    serve_cmd='uv run --group ui streamlit run ui/app.py --server.port 8501 --server.headless true',
    serve_env={'CHATBOT_API_URL': 'http://localhost:8000'},
    deps=['pyproject.toml', 'uv.lock'],
    resource_deps=['api'],
    links=[link('http://localhost:8501', 'Chat UI')],
    labels=['app'],
)

local_resource(
    'retention',
    cmd='uv run python -m chatbot.memory.retention',
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    resource_deps=['migrate'],
    labels=['dev'],
)

local_resource(
    'checks',
    cmd='make check',
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    labels=['dev'],
)

local_resource(
    'evals',
    cmd='make eval',
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    resource_deps=['ingest-sample'],
    labels=['dev'],
)
