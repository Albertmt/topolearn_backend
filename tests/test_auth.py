"""Firebase ID token checks, with a locally generated signing key standing in
for Google's (the other tests use fake tokens; these check the real code)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth, config
from conftest import TOKENS, bearer, new_user, sign_in, tool_doc

REAL_VERIFY = auth.verify_token  # captured before the fake_firebase fixture swaps it
PROJECT = "topolearn-test"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    """Stands in for jwt.PyJWKClient: knows one key, "kid-1"."""

    def __init__(self, fail_with=None):
        self.fail_with = fail_with

    def get_signing_key_from_jwt(self, token):
        if self.fail_with:
            raise self.fail_with
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != "kid-1":
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return jwt.PyJWK.from_dict({**jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True),
                                    "kid": "kid-1", "alg": "RS256", "use": "sig"})


@pytest.fixture()
def real_tokens(monkeypatch):
    monkeypatch.setattr(auth, "verify_token", REAL_VERIFY)
    monkeypatch.setattr(auth, "_jwks", _FakeJwks())
    monkeypatch.setattr(config, "FIREBASE_PROJECT_ID", PROJECT)


def make_token(key=_KEY, kid="kid-1", alg="RS256", **overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "firebase-uid-1",
        "iat": now - 10,
        "exp": now + 3600,
        "auth_time": now - 10,
        "name": "Ada Lovelace",
        "email": "Ada@Example.com",
        "email_verified": True,
        "firebase": {"sign_in_provider": "google.com", "identities": {}},
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def test_a_valid_token_gives_the_identity(real_tokens):
    ident = auth.verify_token(make_token())
    assert ident == auth.Identity(uid="firebase-uid-1", name="Ada Lovelace", email="ada@example.com",
                                  email_verified=True, provider="google.com")
    fb = auth.verify_token(make_token(email=None, email_verified=None, name="  ",
                                      firebase={"sign_in_provider": "facebook.com"}))
    assert fb.email is None and fb.name is None and fb.provider == "facebook.com" and not fb.email_verified


@pytest.mark.parametrize("why, token", [
    ("expired", lambda: make_token(exp=int(time.time()) - 120)),
    ("other project", lambda: make_token(aud="someone-elses-project")),
    ("other issuer", lambda: make_token(iss="https://evil.example.com")),
    ("signed by another key", lambda: make_token(key=_OTHER_KEY)),
    ("unknown key id", lambda: make_token(kid="kid-2")),
    ("no subject", lambda: make_token(sub=None)),
    ("empty subject", lambda: make_token(sub="")),
    ("signed in in the future", lambda: make_token(auth_time=int(time.time()) + 3600)),
    ("issued in the future", lambda: make_token(iat=int(time.time()) + 3600)),
    ("no auth_time", lambda: make_token(auth_time=None)),
    ("symmetric algorithm", lambda: jwt.encode({"sub": "x"}, "secret", algorithm="HS256", headers={"kid": "kid-1"})),
    ("not a jwt", lambda: "garbage"),
])
def test_bad_tokens_are_refused(real_tokens, why, token):
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_token(token())
    assert e.value.status_code == 401, why
    assert e.value.headers == {"WWW-Authenticate": "Bearer"}


def test_expired_says_so(real_tokens):
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_token(make_token(exp=int(time.time()) - 120))
    assert e.value.detail == auth.EXPIRED


def test_server_problems_are_503_not_401(real_tokens, monkeypatch):
    """The app signs the learner out on 401, so a hiccup on our side must not look like one."""
    monkeypatch.setattr(auth, "_jwks", _FakeJwks(fail_with=jwt.PyJWKClientConnectionError("down")))
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_token(make_token())
    assert e.value.status_code == 503
    monkeypatch.setattr(auth, "_jwks", _FakeJwks())
    monkeypatch.setattr(config, "FIREBASE_PROJECT_ID", "")
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_token(make_token())
    assert e.value.status_code == 503 and "FIREBASE_PROJECT_ID" in e.value.detail


def test_end_to_end_with_a_real_token(real_tokens, make_client):
    c = make_client(tool_doc("t", ["a", "b"], edges=["a>b"]))
    token = make_token()
    me = c.post("/api/v1/session", headers=bearer(token)).json()
    assert me["name"] == "Ada Lovelace" and me["email"] == "ada@example.com"
    r = c.put(f"/api/v1/users/{me['id']}/progress/t.a", json={"understanding": 50}, headers=bearer(token))
    assert r.status_code == 200
    g = c.get("/api/v1/tools/t/map", params={"user_id": me["id"]}, headers=bearer(token)).json()
    assert {n["id"]: n.get("understanding") for n in g["nodes"]}["t.a"] == 50
    # A token for another project can't touch it.
    stranger = make_token(aud="other")
    r = c.put(f"/api/v1/users/{me['id']}/progress/t.a", json={"understanding": 0}, headers=bearer(stranger))
    assert r.status_code == 401


def test_public_endpoints_need_no_sign_in(make_client):
    c = make_client(tool_doc("t", ["a", "b"], edges=["a>b"]))
    for url in ("/health", "/api/v1/search?q=a", "/api/v1/tools", "/api/v1/tools/t", "/api/v1/tools/t/map",
                "/api/v1/lessons/t.a", "/api/v1/lessons/t.a/comments", "/api/v1/categories", "/t/t"):
        assert c.get(url).status_code == 200, url


def test_learners_cannot_reach_each_others_data(make_client):
    c = make_client(tool_doc("t", ["a", "b"], edges=["a>b"]))
    ada, bob = new_user(c, "Ada"), new_user(c, "Bob")
    as_bob = bearer(TOKENS[bob])
    for method, url, kw in [
        ("get", f"/api/v1/users/{ada}", {}),
        ("patch", f"/api/v1/users/{ada}", {"json": {"name": "Hacked"}}),
        ("delete", f"/api/v1/users/{ada}", {}),
        ("get", f"/api/v1/users/{ada}/progress", {}),
        ("put", f"/api/v1/users/{ada}/progress/t.a", {"json": {"understanding": 1}}),
        ("put", f"/api/v1/users/{ada}/tools/t", {"json": {"depth": 1}}),
        ("put", f"/api/v1/users/{ada}/saved/t.a", {}),
        ("get", f"/api/v1/users/{ada}/feed", {}),
        ("get", f"/api/v1/users/{ada}/stats", {}),
        ("get", "/api/v1/tools/t/map?user_id=" + ada, {}),
        ("get", "/api/v1/lessons/t.a?user_id=" + ada, {}),
        ("post", "/api/v1/lessons/t.a/comments", {"json": {"user_id": ada, "body": "hi"}}),
        ("put", "/api/v1/lessons/t.a/reaction", {"json": {"user_id": ada, "value": 1}}),
    ]:
        r = getattr(c, method)(url, headers=as_bob, **kw)
        assert r.status_code == 403, (method, url, r.text)
    assert c.get(f"/api/v1/users/{ada}").json()["name"] == "Ada"  # untouched


def test_another_server_cached_an_old_account(make_client):
    """Cloud Run runs several copies; one may still remember an account that
    was deleted and re-created elsewhere. The request must still go through."""
    c = make_client(tool_doc("t", ["a"]))
    u = sign_in(c, "Ada", uid="fb-1")
    auth.remember("fb-1", "deleted-long-ago")
    assert c.get(f"/api/v1/users/{u['id']}").status_code == 200
