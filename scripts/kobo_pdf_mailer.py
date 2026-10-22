#!/usr/bin/env python3
"""Turn a KoboToolbox submission into a PDF (question labels + answers) and
email it to the address entered in the form.

The submission id comes from the repository_dispatch event (sent by a Kobo
REST Service) or from SUBMISSION_ID (manual run); the form and the answers
are always fetched from the Kobo API.
"""
import base64
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

KOBO_SERVER = os.environ.get("KOBO_SERVER", "https://kf.kobotoolbox.org").rstrip("/")
KOBO_HEADERS = {"Authorization": f"Token {os.environ.get('KOBO_TOKEN', '')}"}

# Question types that hold no answer worth printing.
SKIP_TYPES = {
    "calculate", "start", "end", "today", "deviceid", "username", "phonenumber",
    "simserial", "subscriberid", "audit", "hidden", "note", "start-geopoint",
    "background-audio", "xml-external", "csv-external",
}

CSS = """
body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; color: #222; }
h1 { font-size: 18pt; margin: 0 0 4px; }
.meta { color: #666; font-size: 9pt; margin-bottom: 18px; }
h2 { font-size: 12.5pt; background: #eef1f5; padding: 6px 8px; margin: 18px 0 6px; }
section section h2 { background: none; border-bottom: 1px solid #ccd; padding: 4px 0; }
.q { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #eee;
     page-break-inside: avoid; }
.label { flex: 0 0 42%; font-weight: bold; }
.answer { flex: 1; white-space: pre-wrap; word-break: break-word; }
.answer img { max-width: 100%; max-height: 90mm; }
.repeat-item { border-left: 3px solid #ccd; padding-left: 10px; margin: 8px 0; }
.repeat-no { color: #666; font-size: 9pt; }
ul { margin: 0; padding-left: 18px; }
"""


def env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value or default


def kobo_request(path_or_url, **params):
    url = path_or_url if path_or_url.startswith("http") else f"{KOBO_SERVER}{path_or_url}"
    r = requests.get(url, headers=KOBO_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r


def kobo_get(path, **params):
    return kobo_request(path, **params).json()


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


# ---------------------------------------------------------------- PDF content

def item_name(item):
    return item.get("name") or item.get("$autoname") or ""


class Form:
    """The parts of the Kobo form definition needed to label answers."""

    def __init__(self, asset):
        content = asset.get("content", {})
        self.title = asset.get("name") or "Form submission"
        self.lang = self._language_index(content.get("translations") or [None])
        self.tree = self._build_tree(content.get("survey", []))
        self.choices = {
            (c.get("list_name"), item_name(c)): self.label(c)
            for c in content.get("choices", [])
        }

    @staticmethod
    def _language_index(translations):
        wanted = (env("FORM_LANGUAGE") or "").lower()
        for i, name in enumerate(translations):
            if wanted and name and wanted in name.lower():
                return i
        return 0

    @staticmethod
    def _build_tree(rows):
        # Kobo stores the survey flat with begin/end markers; nest it.
        root = {"children": []}
        stack = [root]
        for row in rows:
            kind = row.get("type", "").replace(" ", "_")
            if kind in ("begin_group", "begin_repeat"):
                node = {"row": row, "repeat": kind == "begin_repeat", "children": []}
                stack[-1]["children"].append(node)
                stack.append(node)
            elif kind in ("end_group", "end_repeat"):
                if len(stack) > 1:
                    stack.pop()
            else:
                stack[-1]["children"].append({"row": row})
        return root["children"]

    def label(self, item):
        label = item.get("label")
        if isinstance(label, list):
            picked = label[self.lang] if self.lang < len(label) else None
            label = picked or next((l for l in label if l), None)
        return label or item_name(item)


class Renderer:
    def __init__(self, form, submission):
        self.form = form
        self.submission = submission
        self.attachments = submission.get("_attachments", [])

    def render(self):
        submitted = self.submission.get("_submission_time", "")
        try:
            submitted = datetime.fromisoformat(submitted).strftime("%d %B %Y, %H:%M UTC")
        except ValueError:
            pass
        body = self.nodes(self.form.tree, self.submission, "")
        return (
            f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<h1>{html.escape(self.form.title)}</h1>"
            f"<div class='meta'>Submitted: {html.escape(submitted)} &nbsp;·&nbsp; "
            f"Submission #{self.submission['_id']}</div>{body}</body></html>"
        )

    def nodes(self, nodes, data, prefix):
        out = []
        for node in nodes:
            row = node["row"]
            path = prefix + item_name(row)
            if "children" in node:
                if node["repeat"]:
                    items = data.get(path) or []
                    inner = "".join(
                        f"<div class='repeat-item'><div class='repeat-no'>#{i}</div>"
                        f"{self.nodes(node['children'], item, path + '/')}</div>"
                        for i, item in enumerate(items, 1)
                    )
                else:
                    inner = self.nodes(node["children"], data, path + "/")
                if inner:  # skip groups with no answered questions
                    out.append(f"<section><h2>{html.escape(self.form.label(row))}</h2>{inner}</section>")
                continue

            kind = row.get("type", "")
            value = data.get(path)
            if kind in SKIP_TYPES or value in (None, ""):
                continue
            out.append(
                f"<div class='q'><div class='label'>{html.escape(self.form.label(row))}</div>"
                f"<div class='answer'>{self.answer(row, kind, value)}</div></div>"
            )
        return "".join(out)

    def answer(self, row, kind, value):
        list_name = row.get("select_from_list_name")
        if kind == "select_one":
            return html.escape(self.form.choices.get((list_name, str(value)), str(value)))
        if kind == "select_multiple":
            labels = [self.form.choices.get((list_name, v), v) for v in str(value).split()]
            return "<ul>" + "".join(f"<li>{html.escape(l)}</li>" for l in labels) + "</ul>"
        if kind == "image":
            img = self.image(str(value))
            if img:
                return img
        return html.escape(str(value))

    def image(self, filename):
        for att in self.attachments:
            stored = att.get("filename", "")
            if stored == filename or stored.endswith("/" + filename) or att.get("media_file_basename") == filename:
                url = att.get("download_large_url") or att.get("download_url")
                if not url:
                    return None
                data = kobo_request(url).content
                mime = att.get("mimetype") or "image/jpeg"
                return f"<img src='data:{mime};base64,{base64.b64encode(data).decode()}'>"
        return None


def render_pdf(page_html, out_path):
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")  # preinstalled on GitHub runners
        page = browser.new_page()
        page.set_content(page_html, wait_until="load")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "14mm", "right": "14mm"},
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=(
                "<div style='font-size:8pt;color:#888;width:100%;text-align:center'>"
                "Page <span class='pageNumber'></span> of <span class='totalPages'></span></div>"
            ),
        )
        browser.close()


# ---------------------------------------------------------------- email

def send_mail(recipient, pdf_path, submission):
    msg = EmailMessage()
    sender = env("MAIL_FROM", env("SMTP_USERNAME"))
    if env("MAIL_FROM_NAME"):
        sender = formataddr((env("MAIL_FROM_NAME"), parseaddr(sender)[1]))
    msg["From"] = sender
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
    submission = kobo_get(f"/api/v2/assets/{asset_uid}/data/{sub_id}/", format="json")

    recipient = find_recipient(submission)
    if not recipient:
        print(f"Submission {sub_id}: no valid email address found, nothing sent.")
        return

    form = Form(kobo_get(f"/api/v2/assets/{asset_uid}/", format="json"))
    pdf_path = Path(env("RUNNER_TEMP", ".")) / f"submission-{sub_id}.pdf"
    render_pdf(Renderer(form, submission).render(), pdf_path)
    send_mail(recipient, pdf_path, submission)
    # Logs may be visible to repo collaborators, so don't print the full address.
    print(f"Submission {sub_id}: PDF sent to {mask(recipient)}")


if __name__ == "__main__":
    main()
