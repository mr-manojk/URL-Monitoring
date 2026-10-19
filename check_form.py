#!/usr/bin/env python3
"""
check_form.py — Monitor one or more URLs and email you if any break.

What it checks, per URL:
  1. The HTTP status code is what you expect (default: 200)
  2. A specific piece of text/HTML you expect to see is actually present
     (e.g. a form field name, a button label, "Sign Up", etc.)
  3. (Optional) A piece of text that should NEVER appear is absent
     (e.g. "500 Internal Server Error", "Something went wrong")
  4. (Optional) If the URL requires login, it logs in first and flags
     it as a failure if it ever ends up back on a login page.

If a URL's checks fail, or the request errors out / times out, it
sends a separate email alert for that URL. Passing URLs stay silent
(no spam emails).

Configuration lives in config.ini (see config.example.ini). Each URL
you want to monitor gets its own [site:somename] section. If a URL
needs login, add a matching [login:somename] section and reference it
from the site section with `login = somename`. Different sites can
point to different login sections, or share one, as needed.
"""

import configparser
import html
import re
import smtplib
import ssl
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.ini"
LOG_PATH = SCRIPT_DIR / "check_form.log"


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clean_value(raw: str) -> str:
    """
    Strip surrounding whitespace, and — if present — a single matching
    pair of leading/trailing quotes (single or double). This lets you
    wrap values like URLs in quotes in config.ini so everything inside
    the quotes is treated literally, e.g.:

        url = "https://example.com/path/wER0SCl3thQ,"

    becomes:  https://example.com/path/wER0SCl3thQ,
    """
    value = raw.strip()
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1]
    return value


def perform_login(login_cfg: dict, timeout: int) -> tuple:
    """
    Log in using a requests.Session so cookies persist for subsequent
    requests. Returns (session, error_message_or_None).

    Handles an optional CSRF/anti-forgery hidden input (common on ASP.NET
    sites using __RequestVerificationToken) by fetching the login page
    first and extracting it before submitting credentials.
    """
    session = requests.Session()
    login_url = login_cfg["login_url"]
    username_field = login_cfg.get("username_field", "UserName")
    password_field = login_cfg.get("password_field", "Password")
    username = login_cfg["username"]
    password = login_cfg["password"]
    csrf_field = login_cfg.get("csrf_field", "__RequestVerificationToken").strip()

    form_data = {
        username_field: username,
        password_field: password,
    }

    try:
        # Step 1: GET the login page to pick up cookies + CSRF token (if any)
        get_resp = session.get(login_url, timeout=timeout)

        if csrf_field:
            match = re.search(
                rf'name="{re.escape(csrf_field)}"[^>]*value="([^"]*)"',
                get_resp.text,
            )
            if not match:
                # Some markup orders value before name — try the reverse too
                match = re.search(
                    rf'value="([^"]*)"[^>]*name="{re.escape(csrf_field)}"',
                    get_resp.text,
                )
            if match:
                form_data[csrf_field] = match.group(1)
                log(f"Login: found CSRF token field '{csrf_field}'")
            else:
                log(
                    f"Login: WARNING - could not find CSRF field '{csrf_field}' "
                    "on login page. Proceeding without it (login may fail if "
                    "the site requires it)."
                )

        # Step 2: POST credentials
        post_resp = session.post(
            login_url, data=form_data, timeout=timeout, allow_redirects=True
        )

        # Heuristic: if we're still on a login/logon page after posting,
        # the login almost certainly failed (wrong credentials, wrong
        # field names, or a missing/incorrect CSRF token).
        final_url_lower = post_resp.url.lower()
        if "logon" in final_url_lower or "login" in final_url_lower:
            return None, (
                f"Login POST still landed on a login page ({post_resp.url}). "
                "Check username_field/password_field/csrf_field names in "
                "config.ini match the real form, and that credentials are correct."
            )

        log(f"Login: appears successful, landed on {post_resp.url}")
        return session, None

    except requests.exceptions.RequestException as e:
        return None, f"Login request failed: {e}"


def load_config() -> configparser.ConfigParser:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"ERROR: {CONFIG_PATH} not found. "
            "Copy config.example.ini to config.ini and fill in your details."
        )
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config


def build_alert_email(
    site_name: str,
    url: str,
    status_code,
    failure_reasons: list,
    checked_at: str,
) -> tuple:
    """Return (plain_text_body, html_body) for a failure alert."""

    status_display = status_code if status_code is not None else "No response"

    # ---- Plain-text fallback (for clients that don't render HTML) ----
    plain_lines = [
        f"MONITORING ALERT: {site_name}",
        "=" * 50,
        "",
        f"URL:            {url}",
        f"Status:         {status_display}",
        f"Checked at:     {checked_at}",
        "",
        "Issues detected:",
    ]
    plain_lines += [f"  - {reason}" for reason in failure_reasons]
    plain_lines += [
        "",
        "This is an automated alert from your URL monitoring script.",
        "No action is needed from this email — please check the site directly.",
    ]
    plain_body = "\n".join(plain_lines)

    # ---- HTML version ----
    reasons_html = "".join(
        f"<li style='margin-bottom:8px;'>{html.escape(reason)}</li>"
        for reason in failure_reasons
    )

    html_body = f"""\
<html>
  <body style="margin:0; padding:0; background-color:#f4f5f7; font-family:Segoe UI, Arial, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <tr>
              <td style="background-color:#c0392b; padding:20px 32px;">
                <span style="color:#ffffff; font-size:18px; font-weight:600;">&#9888; Monitoring Alert</span>
              </td>
            </tr>
            <tr>
              <td style="padding:28px 32px 8px 32px;">
                <p style="margin:0 0 4px 0; font-size:14px; color:#6b7280;">Site</p>
                <p style="margin:0 0 16px 0; font-size:16px; font-weight:600; color:#111827;">{html.escape(site_name)}</p>

                <p style="margin:0 0 4px 0; font-size:14px; color:#6b7280;">URL</p>
                <p style="margin:0 0 16px 0; font-size:14px; color:#111827; word-break:break-all;">
                  <a href="{html.escape(url)}" style="color:#2563eb; text-decoration:none;">{html.escape(url)}</a>
                </p>

                <table role="presentation" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
                  <tr>
                    <td style="padding-right:32px;">
                      <p style="margin:0 0 4px 0; font-size:14px; color:#6b7280;">Status</p>
                      <p style="margin:0; font-size:14px; font-weight:600; color:#c0392b;">{html.escape(str(status_display))}</p>
                    </td>
                    <td>
                      <p style="margin:0 0 4px 0; font-size:14px; color:#6b7280;">Checked at</p>
                      <p style="margin:0; font-size:14px; color:#111827;">{html.escape(checked_at)}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px 28px 32px;">
                <p style="margin:0 0 8px 0; font-size:14px; font-weight:600; color:#111827;">Issues detected</p>
                <ul style="margin:0; padding-left:20px; font-size:14px; color:#374151;">
                  {reasons_html}
                </ul>
              </td>
            </tr>
            <tr>
              <td style="background-color:#f9fafb; padding:16px 32px; border-top:1px solid #e5e7eb;">
                <p style="margin:0; font-size:12px; color:#9ca3af;">
                  This is an automated alert from your URL monitoring script.
                  No reply is needed — please check the site directly.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    return plain_body, html_body


def send_email(
    config: configparser.ConfigParser, subject: str, plain_body: str, html_body: str
) -> None:
    smtp_cfg = config["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_address"]
    msg["To"] = smtp_cfg["to_address"]

    # Attach plain text first, HTML second — email clients render the
    # last part they support, so HTML-capable clients show the HTML
    # version while plain-text clients fall back to the text version.
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(smtp_cfg["smtp_host"], int(smtp_cfg["smtp_port"])) as server:
            server.starttls(context=context)
            server.login(smtp_cfg["smtp_username"], smtp_cfg["smtp_password"])
            server.sendmail(
                smtp_cfg["from_address"], smtp_cfg["to_address"], msg.as_string()
            )
        log(f"Alert email sent: {subject}")
    except Exception as e:
        log(f"FAILED TO SEND EMAIL: {e}")


def check_one_site(
    config: configparser.ConfigParser,
    site_section_name: str,
    login_sessions: dict,
) -> None:
    """
    Run all checks for a single [site:name] section. Sends its own
    email if it fails. `login_sessions` is a cache shared across sites
    in this run, keyed by login section name, so two sites pointing at
    the same [login:name] section only log in once.
    """
    site_cfg = config[site_section_name]
    site_key = site_section_name.split(":", 1)[1] if ":" in site_section_name else site_section_name

    url = clean_value(site_cfg["url"])
    site_name = clean_value(site_cfg.get("site_name", site_key))
    expected_status = int(clean_value(site_cfg.get("expected_status", "200")))
    expected_text = clean_value(site_cfg.get("expected_text", ""))
    forbidden_text = clean_value(site_cfg.get("forbidden_text", ""))
    timeout = int(clean_value(site_cfg.get("timeout_seconds", "15")))
    login_ref = clean_value(site_cfg.get("login", ""))

    log(f"--- Checking '{site_name}' ({url}) ---")

    failure_reasons = []
    response = None
    session_or_requests = requests
    use_login = bool(login_ref)

    if use_login:
        login_section_name = f"login:{login_ref}"
        if login_section_name not in config:
            failure_reasons.append(
                f"Config error: site references login '{login_ref}' but "
                f"[{login_section_name}] section doesn't exist in config.ini"
            )
        else:
            if login_ref in login_sessions:
                # Reuse a session already established for another site
                # that shares this same login section.
                session, login_error = login_sessions[login_ref]
                log(f"Reusing existing login session for '{login_ref}'")
            else:
                login_cfg = {
                    k: clean_value(v) for k, v in config[login_section_name].items()
                }
                session, login_error = perform_login(login_cfg, timeout)
                login_sessions[login_ref] = (session, login_error)

            if login_error:
                failure_reasons.append(login_error)
            else:
                session_or_requests = session

    # Show exactly what URL is being requested, byte for byte.
    prepared_url = requests.Request("GET", url).prepare().url
    log(f"Requesting URL: {prepared_url}")
    if prepared_url != url:
        log(
            f"NOTE: requests library rewrote the URL before sending. "
            f"Original: {url!r} -> Sent: {prepared_url!r}"
        )

    if not failure_reasons:
        try:
            response = session_or_requests.get(
                url, timeout=timeout, allow_redirects=True
            )

            if response.history:
                log(
                    f"NOTE: request was redirected {len(response.history)} time(s). "
                    f"Original: {url!r} -> Final: {response.url!r}"
                )
                for i, hist_resp in enumerate(response.history):
                    log(
                        f"  Redirect {i + 1}: {hist_resp.status_code} "
                        f"{hist_resp.url!r} -> Location: {hist_resp.headers.get('Location', 'N/A')!r}"
                    )
        except requests.exceptions.RequestException as e:
            failure_reasons.append(f"Request failed entirely: {e}")
            response = None

    if response is not None:
        if response.status_code != expected_status:
            failure_reasons.append(
                f"Unexpected status code: got {response.status_code}, "
                f"expected {expected_status}"
            )

        if use_login and (
            "logon" in response.url.lower() or "login" in response.url.lower()
        ):
            failure_reasons.append(
                f"Redirected to a login page instead of the target content: {response.url}"
            )

        page_text = response.text

        if expected_text and expected_text not in page_text:
            failure_reasons.append(
                f"Expected text/element not found on page: {expected_text!r}"
            )

        if forbidden_text and forbidden_text in page_text:
            failure_reasons.append(
                f"Forbidden/error text WAS found on page: {forbidden_text!r}"
            )

    if failure_reasons:
        checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
        status_code = response.status_code if response is not None else None
        subject = f"[ALERT] {site_name} is down or misbehaving"

        plain_body, html_body = build_alert_email(
            site_name=site_name,
            url=url,
            status_code=status_code,
            failure_reasons=failure_reasons,
            checked_at=checked_at,
        )

        log(f"CHECK FAILED for '{site_name}': " + "; ".join(failure_reasons))
        send_email(config, subject, plain_body, html_body)
    else:
        log(
            f"OK: '{site_name}' passed all checks (status {response.status_code}, "
            f"final URL after any redirects: {response.url})"
        )


def main() -> None:
    config = load_config()

    site_sections = [s for s in config.sections() if s.startswith("site:")]

    if not site_sections:
        sys.exit(
            "ERROR: no [site:name] sections found in config.ini. "
            "See config.example.ini for the expected format."
        )

    log(f"Starting check run for {len(site_sections)} site(s)")

    # Cache of (session, error) per login section name, shared across
    # sites in this run so a shared login is only performed once.
    login_sessions: dict = {}

    for site_section_name in site_sections:
        try:
            check_one_site(config, site_section_name, login_sessions)
        except Exception as e:
            # Never let one broken site config crash the whole run —
            # log it and keep checking the rest.
            log(f"UNEXPECTED ERROR checking '{site_section_name}': {e}")

    log("Check run complete")


if __name__ == "__main__":
    main()
