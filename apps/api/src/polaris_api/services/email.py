"""Postmark email delivery for verification codes."""

from __future__ import annotations

import logging

import httpx

from polaris_api.config import Settings

logger = logging.getLogger(__name__)

POSTMARK_API_URL = "https://api.postmarkapp.com/email"


async def send_verification_email(email: str, code: str, settings: Settings) -> None:
    html_body = _render_verification_email(code)
    text_body = f"Your Polaris verification code is: {code}\n\nThis code expires in 5 minutes."

    payload = {
        "From": settings.postmark_from_email,
        "To": email,
        "Subject": f"Your Polaris login code: {code}",
        "HtmlBody": html_body,
        "TextBody": text_body,
        "MessageStream": settings.postmark_message_stream,
    }

    if not settings.postmark_server_token:
        logger.warning("POSTMARK_SERVER_TOKEN not set — skipping email to %s (code: %s)", email, code)
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            POSTMARK_API_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": settings.postmark_server_token,
            },
            timeout=10.0,
        )

    if resp.status_code != 200:
        logger.error("Postmark API error: %s %s", resp.status_code, resp.text)
        raise RuntimeError(f"Email delivery failed: {resp.status_code}")


def _render_verification_email(code: str) -> str:
    # Inject the 6 digits contiguously — visual spacing is done via the
    # `.code-box span { letter-spacing: 6px }` CSS rule below.  Never put
    # whitespace between digits: users copy-paste the code, and our OTP
    # input strips non-digits but caps at maxLength=6, so a pasted
    # "1 2 3 4 5 6" lands as "123456"... only if CSS didn't fool them
    # into retyping it.  Keep the rendered text a single token.
    return _VERIFICATION_HTML.replace("{{CODE}}", code)


# Compiled from MJML — minimal, clean verification code email.
_VERIFICATION_HTML = """\
<!doctype html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Polaris Verification Code</title>
<style>
  body { margin: 0; padding: 0; background-color: #f4f6f8; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .wrapper { width: 100%; background-color: #f4f6f8; padding: 40px 0; }
  .card { max-width: 440px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; }
  .header { padding: 32px 32px 0; text-align: center; }
  .header h1 { margin: 0; font-size: 22px; font-weight: 700; color: #1a1f2b; letter-spacing: -0.5px; }
  .body { padding: 24px 32px 32px; text-align: center; }
  .body p { margin: 0 0 20px; font-size: 15px; color: #516174; line-height: 1.5; }
  .code-box { display: inline-block; background: #f0f2f5; border-radius: 8px; padding: 16px 32px; margin: 0 0 20px; }
  .code-box span { font-size: 32px; font-weight: 700; font-family: 'SF Mono', SFMono-Regular, Consolas, 'Liberation Mono', Menlo, monospace; color: #1a1f2b; letter-spacing: 6px; }
  .note { font-size: 13px !important; color: #8b95a5 !important; }
  .footer { padding: 20px 32px; text-align: center; font-size: 12px; color: #b0b8c4; }
</style>
</head>
<body>
<div class="wrapper">
  <div class="card">
    <div class="header">
      <h1>Polaris</h1>
    </div>
    <div class="body">
      <p>Your verification code</p>
      <div class="code-box">
        <span>{{CODE}}</span>
      </div>
      <p class="note">This code expires in 5 minutes.<br/>If you didn't request this, you can safely ignore this email.</p>
    </div>
  </div>
  <div class="footer">
    &copy; Polaris
  </div>
</div>
</body>
</html>
"""
