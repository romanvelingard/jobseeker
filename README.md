# Job Seeker Daily Scanner & Email Reporter

Automated daily job scanner using [JobSpy](https://github.com/clemontina/JobSpy), Google Gemini API, and HTML email table notifications.

## Features
- **Multi-Profile Execution**: Execute the pipeline across multiple search profiles, each with its own search criteria (titles, locations, keywords, exclusions) and recipient email list.
- **Daily Automated Scraping**: Scrapes job listings from LinkedIn (configurable for other portals).
- **LLM Matching & Summarization**: Evaluates jobs against target profiles using Gemini Flash models.
- **HTML Email Table Report**: Sends daily digest formatted as an HTML table containing position details, summary, and direct apply link.
- **Local Preview**: Automatically generates profile-specific HTML reports (e.g., `jobs_report_procurement.html`, `jobs_report_qa_director.html`) for offline viewing.
- **Cross-Platform Execution**: Run locally on Python 3.12+ or automatically via GitHub Actions daily schedule.

---

## Search Profiles System

Search profiles are stored as YAML files in the [`profiles/`](file:///c:/Users/RomanVelingard/projects/jobseeker/profiles/) directory. Each profile defines both the search criteria and the recipient email(s).

### Profile Example (`profiles/qa_director.yaml`)
```yaml
name: "QA Leadership & Director"
email_to: "roman.vel@gmail.com"
email_subject_prefix: "🚀 [QA Leadership]"
local_filename: "jobs_report_qa_director.html"

jobs:
  - "QA Director"
  - "Director of Quality Assurance"
  - "VP Quality Assurance"
  - "Head of QA"
  - "Senior QA Manager"
  - "דירקטור QA"
  - "מנהל QA"
  - "מנהל איכות תוכנה"

locations:
  - "Israel"

keywords:
  - "Quality Assurance"
  - "QA Automation"
  - "Test Strategy"
  - "Leadership"
  - "Python"
  - "Playwright"
  - "CI/CD"

exclude:
  - "Pure manual QA"
  - "Junior QA"
  - "Unpaid internship"
```

---

## Local Usage

### 1. Create Virtual Environment & Install Dependencies
```bash
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your details:
```bash
cp .env.example .env
```

### 3. Run Pipeline with Profiles

#### Execute all search profiles in `profiles/` sequentially:
```bash
python agent.py --all-profiles
```

#### Execute a specific search profile:
```bash
python agent.py --profile profiles/qa_director.yaml
```

#### Execute multiple specific profiles:
```bash
python agent.py --profile profiles/procurement.yaml profiles/qa_director.yaml
```

#### Override recipient email for any run:
```bash
python agent.py --profile profiles/qa_director.yaml --email-to custom_recipient@example.com
```

---

## GitHub Actions (Daily Automated Run)

1. Push this repository to GitHub.
2. Under **Settings > Secrets and variables > Actions**, add your secrets (`GEMINI_API_KEY`, `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, etc.).
3. The workflow runs daily and executes all search profiles in `profiles/`.
4. You can also trigger the workflow manually via **Actions > Run workflow**, selecting either `"all"` or a specific profile path (e.g. `profiles/qa_director.yaml`).
