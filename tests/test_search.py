from app.services.search import Catalog, parse_rules


def _catalog(db):
    return Catalog.load(db)


def test_parses_procedure_location_and_budget(db):
    intent = parse_rules("cheap root canal in cape town under R8 000", _catalog(db))
    assert intent.service_names[0] == "Root canal treatment"
    assert intent.location == "Cape Town"
    assert intent.max_price == 8000
    assert intent.sort == "price_asc"


def test_handles_typos_aliases_and_k_amounts(db):
    intent = parse_rules("tooth extration jhb max 2.5k", _catalog(db))
    assert "Tooth extraction" in intent.service_names
    assert intent.location == "Johannesburg"
    assert intent.max_price == 2500


def test_broad_request_maps_to_category(db):
    intent = parse_rules("dentist in durban", _catalog(db))
    assert intent.categories == ["Dental"]
    assert intent.location == "Durban"


def test_rating_and_sort_words(db):
    intent = parse_rules("top rated MRI scan 4.5 stars", _catalog(db))
    assert intent.service_names[0] == "MRI scan"
    assert intent.min_rating == 4.5
    assert intent.sort == "rating"


def test_search_api_filters_by_service_city_and_budget(client):
    data = client.get("/api/search", params={"q": "root canal in Cape Town under R8000"}).json()
    assert data["count"] > 0
    for item in data["results"]:
        assert item["service"]["name"] == "Root canal treatment"
        assert item["provider"]["city"] == "Cape Town"
        assert item["price_min"] <= 8000
    assert data["intent"]["parser"] == "rules"


def test_location_without_matches_falls_back_with_notice(client):
    data = client.get("/api/search", params={"q": "MRI scan in Bloemfontein"}).json()
    assert data["notice"]
    assert data["count"] > 0
    assert all(i["service"]["name"] == "MRI scan" for i in data["results"])


def test_sort_and_rating_overrides(client):
    data = client.get("/api/search", params={"q": "GP consultation", "sort": "price_asc", "min_rating": 4}).json()
    prices = [i["price_min"] for i in data["results"]]
    assert prices == sorted(prices)
    assert all(i["provider"]["rating"] >= 4 for i in data["results"])


def test_empty_query_lists_everything(client):
    data = client.get("/api/search").json()
    assert data["count"] > 50
    assert data["price_bounds"][0] < data["price_bounds"][1]


def test_ai_pauses_after_account_error_and_falls_back_to_rules(monkeypatch, db):
    import dataclasses
    import sys
    import types

    from app.services import search

    calls = []

    class QuotaError(Exception):
        status_code = 429
        code = "insufficient_quota"

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise QuotaError("You have no credits remaining")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setattr(search, "settings", dataclasses.replace(search.settings, openai_api_key="test-key"))
    monkeypatch.setattr(search, "_ai_paused_until", 0.0)
    search._ai_cache.clear()
    catalog = _catalog(db)

    assert search.parse_with_ai("mri scan in durban", catalog) is None
    assert search.parse_with_ai("root canal in cape town", catalog) is None
    assert len(calls) == 1  # the second search skipped OpenAI while paused

    intent = search.parse_query("MRI scan in Durban", catalog)
    assert intent.parser == "rules" and intent.service_names[0] == "MRI scan"
