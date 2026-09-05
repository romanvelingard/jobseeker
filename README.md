# Job Seeker Daily Scanner & Email Reporter

Automated daily job scanner using [JobSpy](https://github.com/clemontina/JobSpy), Google Gemini API, and HTML email table notifications.

## Features
- **Daily Automated Scraping**: Scrapes job listings from LinkedIn (configurable for other portals).
- **LLM Matching & Summarization**: Evaluates jobs against user profile using Gemini 2.5 Flash and produces short 1-2 sentence descriptions.
- **HTML Email Table Report**: Sends daily digest formatted as an HTML table containing:
  `ID | Position Name | Company | Location | Short Description | Link`
- **Local Preview**: Automatically generates a local HTML report `jobs_report.html` for offline viewing or local testing.
- **Cross-Platform Execution**: Run locally on Python 3.12+ or automatically on GitHub Actions daily schedule.

---

## Local Setup

### 1. Create Virtual Environment & Install Dependencies
```bash
# Create venv
python -m venv venv

# Activate venv (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# Upgrade pip & install requirements
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your details:
```bash
cp .env.example .env
```

Configuration variables:
- `GEMINI_API_KEY`: API Key for Google Gemini LLM job evaluation.
- `SMTP_SERVER`: (e.g. `smtp.gmail.com`)
- `SMTP_PORT`: (e.g. `587`)
- `SMTP_USERNAME`: Sender email address
- `SMTP_PASSWORD`: Sender email app-specific password
- `EMAIL_TO`: Recipient email address
- `EMAIL_FROM`: Sender address displayed on email

### 3. Run Locally
```bash
python agent.py
```
After running, view `jobs_report.html` in your browser for a local preview of the generated vacancy table.

---

## GitHub Actions (Daily Automated Run)

1. Push this repository to GitHub.
2. Under **Settings > Secrets and variables > Actions**, add the following repository secrets:
   - `GEMINI_API_KEY`
   - `SMTP_SERVER`
   - `SMTP_PORT`
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
   - `EMAIL_TO`
   - `EMAIL_FROM`
   - *(Optional)* `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
3. Trigger manually from the **Actions** tab or let it run automatically on the daily cron schedule (07:00 UTC).

