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
