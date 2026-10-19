"""Outgoing email through Resend (https://resend.com), from do-not-reply@topolearnapp.com.

The domain's SPF, DKIM and DMARC records are set up in Cloudflare DNS, which
is what keeps these out of spam. Without RESEND_API_KEY nothing is sent and
the link is logged instead (handy locally)."""
import html
import logging

import requests

from . import config

log = logging.getLogger("topolearn.mail")

_BRAND = "#2563eb"


def _layout(title: str, intro: str, button: str, url: str, footer: str) -> str:
    """One simple, inline-styled template that renders the same everywhere."""
    t, i, b, u, f = (html.escape(x) for x in (title, intro, button, url, footer))
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f2f5fb;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#3a4660;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2f5fb;padding:32px 16px;">
<tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:520px;background:#ffffff;border-radius:16px;padding:32px;">
<tr><td style="font:800 22px/1.2 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#13203f;padding-bottom:12px;">Topolearn</td></tr>
<tr><td style="font:700 20px/1.3 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#13203f;padding-bottom:12px;">{t}</td></tr>
<tr><td style="font-size:16px;line-height:1.6;padding-bottom:24px;">{i}</td></tr>
<tr><td style="padding-bottom:24px;"><a href="{u}" style="display:inline-block;background:{_BRAND};color:#ffffff;text-decoration:none;font-weight:700;font-size:16px;padding:12px 22px;border-radius:12px;">{b}</a></td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#66708a;">If the button doesn't work, copy this link into your browser:<br><a href="{u}" style="color:{_BRAND};word-break:break-all;">{u}</a></td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#66708a;padding-top:24px;border-top:1px solid #dde3ee;margin-top:24px;">{f}</td></tr>
</table>
<p style="font-size:12px;color:#8c96ad;margin:16px 0 0;">Armapp LLC · topolearnapp.com · This address doesn't accept replies; write to support@topolearnapp.com.</p>
</td></tr></table></body></html>"""


def send(to: str, subject: str, title: str, intro: str, button: str, url: str, footer: str) -> bool:
    """Send one transactional email. Returns True when Resend accepted it."""
    text = f"{title}\n\n{intro}\n\n{button}: {url}\n\n{footer}\n\n-- Topolearn (Armapp LLC). This address doesn't accept replies; write to support@topolearnapp.com."
    if not config.RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set; not sending %r to %s. Link: %s", subject, to, url)
        return False
    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={
                "from": config.EMAIL_FROM,
                "to": [to],
                "reply_to": config.EMAIL_REPLY_TO,
                "subject": subject,
                "html": _layout(title, intro, button, url, footer),
                "text": text,
                "headers": {"X-Entity-Ref-ID": subject[:40]},
            },
            timeout=10,
        )
        if res.ok:
            return True
        log.warning("Resend refused %r to %s: %s %s", subject, to, res.status_code, res.text[:300])
    except requests.RequestException:
        log.warning("Resend call failed for %r to %s", subject, to, exc_info=True)
    return False
