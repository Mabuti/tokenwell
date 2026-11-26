import pytest

import tokenwell.gateway as gateway_module
import tokenwell.project_attribution as project_attribution
import tokenwell.project_usage as project_usage
from tokenwell.config import Pool, Profile, save_pool
from tokenwell.gateway import Gateway
from tokenwell.upstream import UpstreamResponse


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def fake_response(status, headers=None, body=b"ok"):
    def chunks():
        if body:
            yield body

    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("tokenwell.config.APP_DIR", tmp_path)
    monkeypatch.setattr("tokenwell.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("tokenwell.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("tokenwell.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("tokenwell.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "tok-a"}))
    monkeypatch.setattr(project_usage, "USAGE_FILE", tmp_path / "project_usage.json")

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    monkeypatch.setattr(project_attribution, "PROJECTS_DIR", projects_dir)
    return projects_dir


def _ok_response():
    return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                "anthropic-ratelimit-unified-5h-reset": "1787191800"})


def test_successful_request_with_known_session_records_project_usage(pool_env):
    proj_dir = pool_env / "-Users-a-my-app"
    proj_dir.mkdir()
    (proj_dir / "sess-1.jsonl").write_text("{}")

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _ok_response())
    result = gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "sess-1"}, b"{}")

    assert result.status == 200
    assert project_usage.get_counts() == {"-Users-a-my-app": 1}


def test_successful_request_without_session_header_records_nothing(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _ok_response())
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert project_usage.get_counts() == {}


def test_successful_request_with_unknown_session_records_nothing(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _ok_response())
    gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "no-such-session"}, b"{}")
    assert project_usage.get_counts() == {}


def test_attribution_failure_never_breaks_the_request(pool_env, monkeypatch):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))

    def boom(*a, **kw):
        raise RuntimeError("filesystem exploded")

    monkeypatch.setattr(project_attribution, "resolve_project", boom)
    gw = Gateway(transport=lambda req: _ok_response())
    result = gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "sess-1"}, b"{}")
    assert result.status == 200  # the client still gets a normal successful response


def test_multiple_requests_accumulate_counts(pool_env):
    proj_dir = pool_env / "-Users-a-my-app"
    proj_dir.mkdir()
    (proj_dir / "sess-1.jsonl").write_text("{}")

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _ok_response())
    gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "sess-1"}, b"{}")
    gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "sess-1"}, b"{}")
    assert project_usage.get_counts() == {"-Users-a-my-app": 2}
