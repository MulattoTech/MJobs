"""MJ-REQ-108: one production consumer across persisted provider/model choices."""
import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from packages.persistence.store import Store
from services.ai.consumer import AIJobInput, consume_ai_candidates
from services.ai.cost_controls import AICostPolicy
from services.ai.providers import AIProviderDisabled
from services.ai.tasks import AITaskConfiguration, AITaskRouter
from tests.integration.test_spine import PROFILE


def test_saved_provider_switches_reach_one_consumer_without_model_coupling(tmp_path, monkeypatch):
    """Exercise real loopback HTTP; no provider class/router/generation is mocked."""
    requests = []
    result = {"category": "REVIEW", "rationale": "Synthetic advisory result.", "job_evidence": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            response = ({"response": json.dumps(result), "done": True}
                        if self.path == "/api/generate" else
                        {"choices": [{"message": {"content": json.dumps(result)}}]})
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    for name in ("MJOBS_OPENAI_COMPATIBLE_BASE_URL", "MJOBS_OPENAI_BASE_URL",
                 "AZURE_OPENAI_ENDPOINT", "MJOBS_LOCAL_AI_BASE_URL"):
        monkeypatch.setenv(name, origin + "/v1")
    monkeypatch.setenv("MJOBS_OLLAMA_BASE_URL", origin)
    # The entire acceptance journey is synthetic and never loads real credentials.
    for name in ("MJOBS_OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY", "MJOBS_OPENAI_API_KEY",
                 "AZURE_OPENAI_API_KEY", "MJOBS_AZURE_OPENAI_API_KEY", "MJOBS_LOCAL_AI_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-02-01")

    url = "sqlite:///" + (tmp_path / "provider-abstraction.sqlite").as_posix()
    store = Store(url)
    candidate = AIJobInput("synthetic-job", 91, "Synthetic Systems Engineer",
                           "Synthetic Employer", "Maintain synthetic Linux services.")
    policy = AICostPolicy(max_ai_candidates=1, max_document_candidates=0)
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(deepcopy(PROFILE))
        for provider in ("openai-compatible", "openai", "azure-openai", "ollama", "local"):
            for model in ("fixture-model-a", "fixture-model-b"):
                saved = {"job_scoring": {"provider": provider, "model": model}}
                store.save_ai_task_settings("fixture", saved)
                store.engine.dispose()
                store = Store(url)
                configuration = AITaskConfiguration.from_mapping(store.ai_task_settings("fixture"))
                assert configuration.for_task("job_scoring").to_dict() == saved["job_scoring"]
                count_before = len(requests)
                output = consume_ai_candidates([candidate], policy, AITaskRouter(configuration),
                                               timeout_seconds=3, total_timeout_seconds=5)
                assert len(requests) == count_before + 1
                assert len(output.evaluations) == 1
                job_id, evaluation = output.evaluations[0]
                assert job_id == "synthetic-job"
                assert evaluation.model_dump() == result
                assert output.document_candidate_ids == ()
                path, body = requests[-1]
                assert body["stream"] is False
                if provider == "ollama":
                    assert path == "/api/generate"
                    assert body["model"] == model
                    assert body["format"]["additionalProperties"] is False
                    prompt = body["prompt"]
                else:
                    if provider == "azure-openai":
                        assert urlsplit(path).path == f"/v1/openai/deployments/{model}/chat/completions"
                        assert urlsplit(path).query == "api-version=2024-02-01"
                    else:
                        assert path == "/v1/chat/completions"
                        assert body["model"] == model
                    assert body["response_format"] == {"type": "json_object"}
                    prompt = body["messages"][0]["content"]
                assert candidate.description in prompt
                assert PROFILE["email"] not in prompt

        store.save_ai_task_settings("fixture", {"job_scoring": {"provider": "disabled", "model": None}})
        store.engine.dispose()
        store = Store(url)
        router = AITaskRouter(AITaskConfiguration.from_mapping(store.ai_task_settings("fixture")))
        count_before = len(requests)
        with pytest.raises(AIProviderDisabled, match="^AI_PROVIDER_DISABLED$"):
            consume_ai_candidates([candidate], policy, router)
        assert len(requests) == count_before == 10
        assert store.count("documents") == store.count("applications") == 0
    finally:
        store.engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()
