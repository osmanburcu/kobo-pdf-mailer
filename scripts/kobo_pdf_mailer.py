#!/usr/bin/env python3
"""Render a KoboToolbox submission (Enketo view) to PDF and email it to the
address entered in the form.

The submission id comes from the repository_dispatch event (sent by a Kobo
REST Service) or from SUBMISSION_ID (manual run); the data itself is always
fetched from the Kobo API.
"""
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

KOBO_SERVER = os.environ.get("KOBO_SERVER", "https://kf.kobotoolbox.org").rstrip("/")
KOBO_HEADERS = {"Authorization": f"Token {os.environ['KOBO_TOKEN']}"}


def env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value or default


def kobo_get(path, **params):
    r = requests.get(f"{KOBO_SERVER}{path}", headers=KOBO_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def submission_id():
    # Only the id is taken from the trigger. Everything else (including the
    # recipient) is re-read from Kobo, so a forged trigger can't redirect a
    # submission to a different address.
    event_path = env("GITHUB_EVENT_PATH")
    if env("GITHUB_EVENT_NAME") == "repository_dispatch" and event_path:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        raw = event["client_payload"]["submission"]["_id"]
    else:
        raw = env("SUBMISSION_ID")
    return int(raw)


def load_submission(asset_uid, sub_id):
    return kobo_get(f"/api/v2/assets/{asset_uid}/data/{sub_id}/", format="json")


def find_recipient(submission):
    field = env("EMAIL_FIELD")
    if field:
        # Kobo prefixes grouped questions with their group path, e.g. "contact/email".
        for key, value in submission.items():
            if key == field or key.endswith("/" + field):
                candidate = str(value).strip()
                if EMAIL_RE.match(candidate):
                    return candidate
        return None

    # No field configured: take the first answer that looks like an email address.
    for key, value in submission.items():
        if not key.startswith("_") and isinstance(value, str) and EMAIL_RE.match(value.strip()):
            return value.strip()
    return None


def enketo_view_url(asset_uid, sub_id):
    # Asking the API for the view link also (re)loads the instance into Enketo,
    # so the link is always fresh.
    data = kobo_get(f"/api/v2/assets/{asset_uid}/data/{sub_id}/enketo/view/", return_url="false")
    return data["url"]


def render_pdf(url, out_path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 1600})
        page.goto(url, wait_until="networkidle", timeout=120_000)
        page.wait_for_selector("form.or", state="visible", timeout=120_000)
        page.wait_for_timeout(2_000)  # let Enketo finish populating answers/images
        page.emulate_media(media="print")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
        )
        browser.close()


def send_mail(recipient, pdf_path, submission):
    msg = EmailMessage()
    msg["From"] = env("MAIL_FROM", env("SMTP_USERNAME"))
    msg["To"] = recipient
    if env("MAIL_BCC"):
        msg["Bcc"] = env("MAIL_BCC")
    msg["Subject"] = env("MAIL_SUBJECT", "Your form submission")
    msg.set_content(env(
        "MAIL_BODY",
        "Hello,\n\nThank you for your submission. A copy of the form is attached as a PDF.\n",
    ))
    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=f"submission-{submission['_id']}.pdf",
    )

    host = env("SMTP_HOST")
    port = int(env("SMTP_PORT", "465"))
    user, password = env("SMTP_USERNAME"), env("SMTP_PASSWORD")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls()
    with server:
        if user:
            server.login(user, password)
        server.send_message(msg)


def mask(address):
    name, _, domain = address.partition("@")
    return f"{name[:2]}***@{domain}"


def main():
    asset_uid = env("KOBO_ASSET_UID")
    if not asset_uid:
        sys.exit("KOBO_ASSET_UID is not set.")
    sub_id = submission_id()
    submission = load_submission(asset_uid, sub_id)

    recipient = find_recipient(submission)
    if not recipient:
        print(f"Submission {sub_id}: no valid email address found, nothing sent.")
        return

    pdf_path = Path(env("RUNNER_TEMP", ".")) / f"submission-{sub_id}.pdf"
    render_pdf(enketo_view_url(asset_uid, sub_id), pdf_path)
    send_mail(recipient, pdf_path, submission)
    # Logs may be visible to repo collaborators, so don't print the full address.
    print(f"Submission {sub_id}: PDF sent to {mask(recipient)}")


if __name__ == "__main__":
    main()
