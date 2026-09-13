import json
import os
import sys
import types


class _Context:
    function_name = "costcare-test-web"
    aws_request_id = "test-request"


def _http_event(path: str = "/", query: str = "") -> dict:
    host = "abc123.execute-api.af-south-1.amazonaws.com"
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"host": host, "accept": "text/html,application/json"},
        "requestContext": {
            "accountId": "123456789012", "apiId": "abc123", "domainName": host, "requestId": "req-1",
            "stage": "$default", "timeEpoch": 0,
            "http": {"method": "GET", "path": path, "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1", "userAgent": "pytest"},
        },
        "isBase64Encoded": False,
    }


def test_lambda_handler_serves_website_and_api(client):
    import lambda_handler

    page = lambda_handler.handler(_http_event("/"), _Context())
    assert page["statusCode"] == 200
    assert "CostCare" in page["body"]

    api = lambda_handler.handler(_http_event("/api/search", "q=mri+scan"), _Context())
    assert api["statusCode"] == 200
    assert json.loads(api["body"])["count"] > 0


def test_sync_handler_skips_without_google_key(client):
    import lambda_handler

    result = lambda_handler.sync_handler({}, _Context())
    assert result["synced"] == 0 and "GOOGLE_MAPS_API_KEY" in result["skipped"]


def test_setup_task_creates_schema_and_is_repeatable(client):
    import lambda_handler

    first = lambda_handler.sync_handler({"task": "setup", "seed_demo_data": True}, _Context())
    second = lambda_handler.sync_handler({"task": "setup", "seed_demo_data": True}, _Context())
    assert first["task"] == "setup" and first["database"] == "sqlite"
    assert first["providers"] > 0 and second["providers"] == first["providers"]


def test_secrets_manager_values_are_loaded(monkeypatch, client):
    import lambda_handler

    secrets = {
        "arn:app": {"CC_TEST_SECRET_KEY": "from-secrets-manager", "CC_TEST_BLANK": ""},
        "arn:admin": {"CC_TEST_ADMIN_PASSWORD": "generated"},
    }

    class FakeSecretsClient:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps(secrets[SecretId])}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: FakeSecretsClient()))
    monkeypatch.setenv("APP_SECRET_ARNS", "arn:app, arn:admin")
    monkeypatch.setenv("CC_TEST_BLANK", "keep-default")
    for key in ("CC_TEST_SECRET_KEY", "CC_TEST_ADMIN_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    lambda_handler._load_secrets()
    try:
        assert os.environ["CC_TEST_SECRET_KEY"] == "from-secrets-manager"
        assert os.environ["CC_TEST_ADMIN_PASSWORD"] == "generated"
        assert os.environ["CC_TEST_BLANK"] == "keep-default"
    finally:
        os.environ.pop("CC_TEST_SECRET_KEY", None)
        os.environ.pop("CC_TEST_ADMIN_PASSWORD", None)
