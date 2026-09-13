def test_healthz_reports_database_and_counts(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"
    assert body["providers"] > 0 and body["prices"] > 0
