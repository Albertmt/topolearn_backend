from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from conftest import tool_doc

FIXTURE = tool_doc("python", ["basics", "lists"], edges=["lists>basics"], name="Python")


@pytest.fixture()
def client(make_client):
    return make_client(FIXTURE)


def test_first_sign_in_creates_the_account(client, fake_firebase):
    from conftest import TOKENS, sign_in

    u = sign_in(client, "  Ada   Lovelace ", "ADA@example.com", uid="fb-ada")
    assert u["name"] == "Ada Lovelace" and u["email"] == "ada@example.com"
    assert u["onboarded"] is False and u["experience"] is None and u["daily_goal"] == 2

    # Signing in again (any time the app starts) returns the same account, unchanged.
    client.patch(f"/api/v1/users/{u['id']}", json={"name": "Countess"})
    again = client.post("/api/v1/session", headers={"Authorization": f"Bearer {TOKENS[u['id']]}"})
    assert again.status_code == 200 and again.json()["id"] == u["id"] and again.json()["name"] == "Countess"

    r = client.patch(f"/api/v1/users/{u['id']}", json={"experience": "some", "daily_goal": 3, "onboarded": True})
    assert r.status_code == 200, r.text
    u2 = r.json()
    assert u2["experience"] == "some" and u2["daily_goal"] == 3 and u2["onboarded"] is True
    assert client.get(f"/api/v1/users/{u['id']}").json()["daily_goal"] == 3

    # Deleting removes the account and its Firebase sign-in record.
    assert client.delete(f"/api/v1/users/{u['id']}").status_code == 204
    assert fake_firebase == ["fb-ada"]
    assert client.get(f"/api/v1/users/{u['id']}").status_code == 401  # the old token has no account now
    # Signing in again later starts a fresh account.
    fresh = sign_in(client, "Ada", uid="fb-ada")
    assert fresh["id"] != u["id"] and fresh["onboarded"] is False


def test_names_and_emails_from_the_provider(client):
    from conftest import sign_in

    assert sign_in(client, None, "grace.hopper@navy.mil")["name"] == "grace.hopper"
    assert sign_in(client, None, None)["name"] == "Learner"
    assert sign_in(client, "   ", None)["name"] == "Learner"
    # An email another account already has isn't copied (accounts can't share one).
    a = sign_in(client, "A", "same@example.com")
    b = sign_in(client, "B", "SAME@example.com")
    assert a["email"] == "same@example.com" and b["email"] is None


def test_session_needs_a_valid_sign_in(client):
    assert client.post("/api/v1/session").status_code == 401
    r = client.post("/api/v1/session", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.post("/api/v1/session", headers={"Authorization": "Basic abc"}).status_code == 401
    # The old open endpoints are gone.
    assert client.get("/api/v1/users").status_code in (404, 405)
    assert client.post("/api/v1/users", json={"name": "X"}).status_code in (404, 405)


def test_validation(client):
    from conftest import sign_in

    uid = sign_in(client, "A", "a@b.co")["id"]
    other = sign_in(client, "B")["id"]
    assert client.patch(f"/api/v1/users/{other}", json={"email": "A@b.co"}).status_code == 409
    assert client.patch(f"/api/v1/users/{uid}", json={"email": "nope"}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"email": ""}).json()["email"] is None
    assert client.patch(f"/api/v1/users/{uid}", json={"name": "   "}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"experience": "wizard"}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"daily_goal": 0}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"daily_goal": 21}).status_code == 422


def test_one_account_per_person_even_when_signing_in_twice_at_once(client):
    """Two first sign-ins racing (app start + a retry) must not make two accounts."""
    from concurrent.futures import ThreadPoolExecutor

    from conftest import bearer, fake_token

    token = fake_token("racer", "Racer", "racer@example.com")
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: client.post("/api/v1/session", headers=bearer(token)).json()["id"], range(8)))
    assert len(set(ids)) == 1
