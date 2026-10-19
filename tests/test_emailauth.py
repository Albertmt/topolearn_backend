"""The verification and password-reset emails (Firebase link + Resend are mocked)."""
import pytest
from conftest import bearer, fake_token, tool_doc

from app import emailauth, mail


@pytest.fixture
def client(make_client):
    return make_client(tool_doc("python", lessons=["basics"]))


def _mock_links_and_mail(monkeypatch, link="https://topolearn-gcloud.firebaseapp.com/__/auth/action?oobCode=abc"):
    asked, sent = [], []
    monkeypatch.setattr(emailauth, "_oob_link", lambda kind, email, cont: asked.append((kind, email, cont)) or link)
    monkeypatch.setattr(mail, "send", lambda to, subject, *a, **k: sent.append((to, subject)) or True)
    monkeypatch.setattr(emailauth, "_per_email", emailauth._Limiter(4, 3600))
    monkeypatch.setattr(emailauth, "_per_ip", emailauth._Limiter(20, 3600))
    return asked, sent


def test_verification_email_sends_a_sign_in_link(client, monkeypatch):
    asked, sent = _mock_links_and_mail(monkeypatch)
    tok = fake_token("u-new", email="New@Example.com", email_verified=False, provider="password")
    r = client.post("/api/v1/auth/verification-email", headers=bearer(tok))
    assert r.status_code == 202
    assert asked == [("EMAIL_SIGNIN", "new@example.com", "https://topolearnapp.com/verify?email=new%40example.com")]
    assert sent == [("new@example.com", "Confirm your email for Topolearn")]


def test_verified_accounts_get_nothing(client, monkeypatch):
    asked, sent = _mock_links_and_mail(monkeypatch)
    r = client.post("/api/v1/auth/verification-email", headers=bearer(fake_token("u1", email="a@b.co")))
    assert r.status_code == 204 and not asked and not sent


def test_verification_needs_sign_in(client):
    assert client.post("/api/v1/auth/verification-email").status_code == 401


def test_verification_is_rate_limited(client, monkeypatch):
    _, sent = _mock_links_and_mail(monkeypatch)
    tok = fake_token("u-new", email="x@example.com", email_verified=False, provider="password")
    codes = [client.post("/api/v1/auth/verification-email", headers=bearer(tok)).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 202, 429] and len(sent) == 4


def test_password_reset_never_reveals_accounts(client, monkeypatch):
    asked, sent = _mock_links_and_mail(monkeypatch, link=None)  # Firebase: no such user
    r = client.post("/api/v1/auth/password-reset", json={"email": "nobody@example.com"})
    assert r.status_code == 202 and asked[0][0] == "PASSWORD_RESET" and not sent


def test_password_reset_sends_when_account_exists(client, monkeypatch):
    _, sent = _mock_links_and_mail(monkeypatch)
    r = client.post("/api/v1/auth/password-reset", json={"email": "Someone@Example.com"})
    assert r.status_code == 202 and sent == [("someone@example.com", "Reset your Topolearn password")]


def test_password_reset_rejects_junk(client):
    assert client.post("/api/v1/auth/password-reset", json={"email": "not an email"}).status_code == 400


def test_mail_is_logged_not_sent_without_key(monkeypatch, caplog):
    from app import config

    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    assert mail.send("a@b.co", "Hi", "Hi", "Intro", "Go", "https://x.y/z", "Bye") is False
    assert "https://x.y/z" in caplog.text
