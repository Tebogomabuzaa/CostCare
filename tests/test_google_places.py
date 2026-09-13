import dataclasses
import json

import httpx
import pytest

from app.models import Provider
from app.services import google_places as gp


@pytest.mark.parametrize(
    "link, expected",
    [
        ("ChIJN1t_tDeuEmsRUsoyG83frY4", {"place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4"}),
        ("https://www.google.com/maps/search/?api=1&query=Some+Clinic&query_place_id=ChIJabcdefghijk123",
         {"place_id": "ChIJabcdefghijk123"}),
        ("https://www.google.com/maps/place/Groote+Schuur+Hospital/@-33.9411,18.4640,17z/data=!3m1!4b1!3d-33.9412!4d18.4641",
         {"name": "Groote Schuur Hospital", "latitude": -33.9412, "longitude": 18.4641}),
        ("https://www.google.com/search?q=Sample+Day+Clinic+Cape+Town&kgmid=/g/11abc", {"query": "Sample Day Clinic Cape Town"}),
        ("Sample Day Clinic, Cape Town", {"query": "Sample Day Clinic, Cape Town"}),
        ("https://maps.google.com/?cid=1234567890", {"cid": "1234567890"}),
    ],
)
def test_parse_profile_link(link, expected):
    parsed = gp.parse_profile_link(link)
    for key, value in expected.items():
        assert getattr(parsed, key) == value


PLACE = {
    "id": "ChIJTestPlace000001",
    "displayName": {"text": "Test Hospital Cape Town"},
    "formattedAddress": "1 Test Road, Observatory, Cape Town, 7925, South Africa",
    "addressComponents": [
        {"longText": "Cape Town", "types": ["locality", "political"]},
        {"longText": "Western Cape", "types": ["administrative_area_level_1", "political"]},
    ],
    "location": {"latitude": -33.9412, "longitude": 18.4641},
    "rating": 4.4,
    "userRatingCount": 812,
    "googleMapsUri": "https://maps.google.com/?cid=42",
    "websiteUri": "https://hospital.example",
    "nationalPhoneNumber": "021 000 0000",
    "regularOpeningHours": {"weekdayDescriptions": ["Monday: Open 24 hours", "Tuesday: Open 24 hours"]},
    "reviews": [
        {"rating": 5, "text": {"text": "Excellent emergency care."}, "relativePublishTimeDescription": "2 weeks ago",
         "publishTime": "2026-08-30T10:00:00Z",
         "authorAttribution": {"displayName": "Thandi M.", "uri": "https://www.google.com/maps/contrib/1", "photoUri": ""}},
        {"rating": 3, "text": {"text": "Long wait but good doctors."}, "relativePublishTimeDescription": "a month ago",
         "authorAttribution": {"displayName": "Pieter V."}},
    ],
}


def mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "maps.app.goo.gl":
            return httpx.Response(302, headers={"Location": (
                "https://www.google.com/maps/place/Test+Hospital+Cape+Town/@-33.9,18.4,17z/"
                "data=!4m6!3m5!1s0x1dcc:0x1!8m2!3d-33.9412!4d18.4641")})
        if request.url.host == "www.google.com":
            return httpx.Response(200, text="ok")
        if request.url.path == "/v1/places:searchText":
            assert request.headers["X-Goog-Api-Key"] == "test-key"
            body = json.loads(request.content)
            assert body["textQuery"] == "Test Hospital Cape Town"
            assert body["locationBias"]["circle"]["center"]["latitude"] == -33.9412
            return httpx.Response(200, json={"places": [{"id": "ChIJTestPlace000001"}]})
        if request.url.path == "/v1/places/ChIJTestPlace000001":
            assert "reviews" in request.headers["X-Goog-FieldMask"]
            return httpx.Response(200, json=PLACE)
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture
def google_key(monkeypatch):
    monkeypatch.setattr(gp, "settings", dataclasses.replace(gp.settings, google_maps_api_key="test-key"))


def test_link_profile_captures_reviews_location_and_contact(google_key, db):
    provider = Provider(name="Test Hospital", slug="test-hospital-google")
    db.add(provider)
    db.commit()

    result = gp.link_google_profile(db, provider, "https://maps.app.goo.gl/abc123", client=mock_client())

    assert result["rating"] == 4.4 and result["review_count"] == 812 and result["reviews_stored"] == 2
    assert (provider.latitude, provider.longitude) == (-33.9412, 18.4641)
    assert provider.city == "Cape Town" and provider.province == "Western Cape"
    assert provider.phone == "021 000 0000" and provider.website == "https://hospital.example"
    assert provider.google_place_id == "ChIJTestPlace000001"
    assert provider.google_profile_url == "https://maps.app.goo.gl/abc123"
    assert "Open 24 hours" in provider.opening_hours
    reviews = [r for r in provider.reviews if r.source == "google"]
    assert reviews[0].author_name == "Thandi M." and reviews[0].published_at is not None
    assert provider.rating == 4.4

    # Re-syncing replaces Google reviews instead of duplicating them
    gp.sync_provider(db, provider, client=mock_client())
    assert len([r for r in provider.reviews if r.source == "google"]) == 2


def test_google_error_is_reported(google_key):
    with pytest.raises(gp.GooglePlacesError, match="not found"):
        gp.get_place_details("ChIJDoesNotExist0000", client=mock_client())


def test_cid_only_link_gives_helpful_message():
    with pytest.raises(gp.GooglePlacesError, match="Share"):
        gp.resolve_place_id(gp.parse_profile_link("https://maps.google.com/?cid=123"))


def test_admin_api_requires_login(client):
    r = client.post("/api/admin/providers/1/google-profile", json={"url": "https://maps.app.goo.gl/x"})
    assert r.status_code == 401


def test_api_without_key_explains_setup(admin_client):
    created = admin_client.post("/api/admin/providers", json={"name": "No Key Clinic", "city": "Durban"})
    assert created.status_code == 201
    pid = created.json()["id"]
    r = admin_client.post(f"/api/admin/providers/{pid}/google-profile", json={"url": "ChIJabcdefghijk123"})
    assert r.status_code == 400
    assert "GOOGLE_MAPS_API_KEY" in r.json()["detail"]
