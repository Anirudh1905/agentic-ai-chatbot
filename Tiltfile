# Local dev on Kubernetes (Phase 9): `make cluster` once, then `make tilt` (tilt up on the
# kind-chatbot context). Tilt UI: http://localhost:10350. The app: http://localhost:8088.
#
# Only the kind cluster is allowed: Tilt refuses any other kubectl context, so nothing here
# can reach a real cluster.
allow_k8s_contexts('kind-chatbot')
if k8s_context() != 'kind-chatbot':
    fail("Run `make cluster`, then `make tilt` (tilt up --context kind-chatbot); current context: " + k8s_context())

# The app image. A code change rebuilds only its last layers (dependencies are cached).
docker_build(
    'chatbot',
    '.',
    ignore=['data/*', '!data/sample', 'docs', 'tests', 'deploy', '.archify'],
)

# make secrets writes these; they're gitignored, which Tilt doesn't watch on its own.
watch_file('deploy/local/secrets.env')
watch_file('deploy/local/ca-bundle.pem')
manifests = kustomize('deploy/local')
k8s_yaml(manifests)


def generated(kind, name):
    """A generated ConfigMap's or Secret's object selector (its name ends in a hash of its content)."""
    return [o['metadata']['name'] + ':' + kind for o in decode_yaml_stream(manifests)
            if o['kind'] == kind and o['metadata']['name'].startswith(name + '-')][0]


# Every object belongs to a resource below: Tilt groups whatever isn't claimed as "uncategorized".
# Moving an object between resources while Tilt runs deletes it, so change these with Tilt stopped.
k8s_resource(
    new_name='config',
    objects=['chatbot:namespace', generated('ConfigMap', 'app-config'), generated('ConfigMap', 'ca-bundle'),
             generated('Secret', 'app-secrets')],
    labels=['app'],
)

# Data services, first.
k8s_kind('Cluster', api_version='postgresql.cnpg.io/v1', pod_readiness='wait')
k8s_resource('postgres', objects=['postgres-dev:secret', 'postgres-host:service'], labels=['data'])
# localhost:5433, through a NodePort and a socat container (deploy/local/postgres-host.sh).
local_resource('postgres-host', cmd='./deploy/local/postgres-host.sh', resource_deps=['postgres'], labels=['data'])
k8s_resource('redis', labels=['data'], port_forwards=['6380:6379'])
k8s_resource('elasticmq', objects=[generated('ConfigMap', 'elasticmq-config')], labels=['data'],
             port_forwards=['9324:9324'])
k8s_resource('pgweb', labels=['data'], resource_deps=['migrate'], port_forwards=['8081:8081'],
             links=[link('http://localhost:8081', 'pgweb')])

# One-off work, in order: the schema, the reranker model, the sample knowledge base.
k8s_resource('migrate', labels=['jobs'], resource_deps=['postgres'])
k8s_resource('reranker-model', objects=['reranker-model:persistentvolumeclaim'], labels=['jobs'])
k8s_resource('ingest', labels=['jobs'], resource_deps=['migrate'])
k8s_resource('retention', labels=['jobs'], trigger_mode=TRIGGER_MODE_MANUAL, auto_init=False)

# The app.
k8s_resource(
    'api',
    objects=['api:poddisruptionbudget'],
    labels=['app'],
    resource_deps=['migrate', 'reranker-model', 'redis', 'elasticmq'],
    links=[link('http://localhost:8088/api/docs', 'API docs')],
)
k8s_resource('worker', labels=['app'], resource_deps=['migrate', 'elasticmq'])
k8s_resource('ui', labels=['app'], resource_deps=['api'], links=[link('http://localhost:8088', 'Chat UI')])
k8s_resource(
    new_name='gateway',
    objects=['chatbot:gateway', 'envoy:gatewayclass', 'kind-nodeport:envoyproxy', 'api:httproute', 'ui:httproute'],
    labels=['app'],
)

# Our side of monitoring: scrape targets, alert rules, the dashboard and the routes. Prometheus,
# Grafana and Alertmanager themselves are kube-prometheus-stack, installed by `make cluster`.
k8s_resource(
    new_name='monitoring',
    objects=['api:servicemonitor', 'worker:servicemonitor', 'chatbot:prometheusrule', generated('ConfigMap', 'chatbot-dashboard'),
             'grafana:httproute', 'prometheus:httproute', 'alertmanager:httproute'],
    labels=['monitoring'],
    links=[link('http://localhost:8088/grafana/d/chatbot', 'Grafana'), link('http://localhost:8088/prometheus/alerts', 'Alerts'),
           link('http://localhost:8088/alertmanager', 'Alertmanager')],
)

# Langfuse stays in Docker Compose; this links it into the cluster.
docker_compose('./docker-compose.yml')
dc_resource('langfuse-web', labels=['langfuse'], links=[link('http://localhost:3000', 'Langfuse')])
for name in ['langfuse-worker', 'langfuse-postgres', 'langfuse-clickhouse', 'langfuse-minio', 'langfuse-redis']:
    dc_resource(name, labels=['langfuse'])
local_resource('langfuse-link', cmd='./deploy/local/langfuse-link.sh', resource_deps=['langfuse-web'], labels=['langfuse'])

# On demand.
local_resource('checks', cmd='make check', auto_init=False, trigger_mode=TRIGGER_MODE_MANUAL, labels=['dev'])
local_resource('evals', cmd='make eval', auto_init=False, trigger_mode=TRIGGER_MODE_MANUAL,
               resource_deps=['ingest'], labels=['dev'])
