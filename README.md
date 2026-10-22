# Kobo submission → PDF → email

When a form is submitted in KoboToolbox, a Kobo **REST Service** calls the GitHub
API, which starts the workflow in `.github/workflows/kobo-pdf-email.yml`. The workflow:

1. downloads the submission and the form definition from the Kobo API,
2. lays out every answered question (label + answer, grouped by section,
   choice labels instead of codes, photos embedded) and prints it to PDF
   with headless Chromium (Playwright),
3. emails the PDF to the address entered in the form.

The Enketo "view" page is not used: it only loads for someone logged in to
Kobo, which the GitHub runner is not.

```
Kobo submission ──REST Service──▶ api.github.com/repos/<owner>/<repo>/dispatches
                                        │
                                        ▼
                           GitHub Action (repository_dispatch)
              Kobo API (form + answers) → HTML → Chromium → PDF → SMTP
```

> Use a **private** repository: submission data passes through the Actions run.

## 1. Repository settings

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Value |
|---|---|
| `KOBO_TOKEN` | Kobo API token (Kobo → Account settings → Security → API key) |
| `SMTP_USERNAME` | SMTP login, e.g. your Gmail address |
| `SMTP_PASSWORD` | SMTP password; for Gmail an [App Password](https://myaccount.google.com/apppasswords) |

**Settings → Secrets and variables → Actions → Variables**

| Variable | Example | Notes |
|---|---|---|
| `KOBO_SERVER` | `https://kf.kobotoolbox.org` | `https://eu.kobotoolbox.org` for the EU server |
| `KOBO_ASSET_UID` | `aBcD123...` | **Required.** From the project URL: `.../#/forms/<ASSET_UID>/...` |
| `EMAIL_FIELD` | `email` | Name of the email question in XLSForm (group prefix not needed). If empty, the first answer that looks like an email is used. |
| `FORM_LANGUAGE` | `English` | Optional, for multi-language forms: which label language to print |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `465` | `465` = SSL, anything else = STARTTLS (e.g. `587`) |
| `MAIL_FROM` | `Survey Team <you@gmail.com>` | Optional, defaults to `SMTP_USERNAME` |
| `MAIL_SUBJECT` | `Your form submission` | Optional |
| `MAIL_BODY` | | Optional plain-text body |
| `MAIL_BCC` | `archive@example.org` | Optional, get a copy of every email |

## 2. GitHub token for Kobo

Create a **fine-grained personal access token**
(GitHub → Settings → Developer settings → Fine-grained tokens):

- Repository access: only this repository
- Permissions: **Contents → Read and write** (required for `repository_dispatch`)

## 3. Kobo REST Service

In your Kobo project: **Settings → REST Services → Register a new service**

- **Endpoint URL:** `https://api.github.com/repos/<owner>/<repo>/dispatches`
- **Type:** JSON
- **Custom HTTP Headers:**
  - `Authorization` → `Bearer <your fine-grained token>`
  - `Accept` → `application/vnd.github+json`
  - `X-GitHub-Api-Version` → `2022-11-28`
- **Add custom wrapper around JSON submission:**

  ```
  {"event_type": "kobo_submission", "client_payload": {"submission": %SUBMISSION%}}
  ```

  The wrapper is required: GitHub expects this shape and allows at most 10
  top-level keys in `client_payload`, so the submission is nested under one key.

GitHub answers `204 No Content` on success; Kobo shows the status per submission
in the REST Service log, where failed calls can be retried.

## Privacy: who can see what

| Place | Who can see it | Protection |
|---|---|---|
| Emailed PDF | Only the address typed in the form (+ `MAIL_BCC`) | Recipient is always re-read from Kobo, never taken from the trigger |
| Actions run logs | People with access to the GitHub repo | Logs contain only the submission id and a masked email (`os***@gmail.com`); no answers, no view link |
| The PDF file | Nobody | Created on a throwaway GitHub runner, deleted when the job ends; not uploaded as an artifact |
| `KOBO_TOKEN` | Nobody (masked secret) | But anyone with **write** access to the repo could change the workflow and use it, so give write access only to people allowed to see the data |
| GitHub token in Kobo | Kobo project admins | They can already see all submissions. Someone holding it can only trigger an email of a submission to *its own* recipient |

Recommended:
- Keep the repo **private** with as few collaborators as possible.
- Settings → Actions → General → set **log retention** to a few days.
- If you use `MAIL_BCC`, that mailbox receives every submission.

## 4. Test

- Manually: **Actions → Kobo submission PDF email → Run workflow**, enter a
  submission `_id` (the number shown in Kobo's data table / API).
- End to end: submit the form with your own email address.
