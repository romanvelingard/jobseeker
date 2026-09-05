import sys
import os
import json
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
import yaml
from dotenv import load_dotenv
from jobspy import scrape_jobs
from google import genai

# Ensure UTF-8 output encoding for Windows stdout/stderr to support multi-language characters
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load environment variables from .env file if available
load_dotenv()

# ==========================================
# 1. LOAD CONFIGURATION FILES & HISTORY
# ==========================================
JOB_DEF_FILE = "job_definition.yaml"
SETTINGS_FILE = "settings.yaml"
SEEN_JOBS_FILE = "seen_jobs.json"

def load_seen_jobs(ignore_days: int = 7) -> tuple[set, dict]:
    """Loads historical seen job URLs with timestamps and prunes entries older than ignore_days."""
    seen_map = {}
    if os.path.exists(SEEN_JOBS_FILE):
        try:
            with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    seen_map = data
                elif isinstance(data, list):
                    # Migration from list format
                    now_str = datetime.now(timezone.utc).isoformat()
                    seen_map = {url: now_str for url in data}
        except Exception as e:
            print(f"[!] Error loading {SEEN_JOBS_FILE}: {e}", flush=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=ignore_days) if ignore_days > 0 else None
    active_urls = set()
    pruned_map = {}

    for url, ts_str in seen_map.items():
        try:
            ts = datetime.fromisoformat(ts_str)
            if cutoff is None or ts >= cutoff:
                active_urls.add(url)
                pruned_map[url] = ts_str
        except Exception:
            active_urls.add(url)
            pruned_map[url] = ts_str

    return active_urls, pruned_map

def save_seen_jobs(seen_map: dict):
    """Saves timestamped seen job URLs to seen_jobs.json."""
    try:
        with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_map, f, indent=2, sort_keys=True)
        print(f"[+] Saved {len(seen_map)} total historical job URLs to {SEEN_JOBS_FILE}", flush=True)
    except Exception as e:
        print(f"[!] Error saving {SEEN_JOBS_FILE}: {e}", flush=True)



def load_yaml_file(filepath: str, default_dict: dict) -> dict:
    """Helper to load a YAML file with a fallback dictionary."""
    data = default_dict.copy()
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
            print(f"[+] Loaded configuration from {filepath}", flush=True)
        except Exception as e:
            print(f"[!] Error reading {filepath}: {e}", flush=True)
    else:
        print(f"[!] Warning: Configuration file {filepath} not found. Using defaults.", flush=True)
    return data

# Load Job Definition Criteria
JOB_CONFIG = load_yaml_file(JOB_DEF_FILE, {
    "jobs": ["Senior QA Automation Engineer"],
    "locations": ["Israel"],
    "keywords": ["Python", "Playwright", "Selenium", "CI/CD", "API testing"],
    "exclude": ["Pure manual QA", "Unpaid internship"]
})

# Load Operational & Application Settings
APP_SETTINGS = load_yaml_file(SETTINGS_FILE, {
    "scraper": {"sites": ["linkedin"], "hours_old": 24, "results_wanted": 50},
    "email": {"smtp_server": "smtp.gmail.com", "smtp_port": 587, "use_tls": True, "email_to": "", "email_from": ""},
    "llm": {"model": "gemini-2.5-flash", "enabled": True},
    "reports": {"save_local_html": True, "local_filename": "jobs_report.html"}
})

# Extract Job Criteria
JOBS_LIST = JOB_CONFIG.get("jobs") or ["Senior QA Automation Engineer"]
LOCATIONS_LIST = JOB_CONFIG.get("locations") or ["Israel"]
KEYWORDS_LIST = JOB_CONFIG.get("keywords") or []
EXCLUDE_LIST = JOB_CONFIG.get("exclude") or []

# Extract Operational Settings (Env variables take precedence)
SCRAPER_CFG = APP_SETTINGS.get("scraper", {})
HOURS_OLD = int(os.getenv("HOURS_OLD", SCRAPER_CFG.get("hours_old", 24)))
RESULTS_WANTED = int(os.getenv("RESULTS_WANTED", SCRAPER_CFG.get("results_wanted", 50)))
SCRAPE_SITES = SCRAPER_CFG.get("sites", ["linkedin"])

LLM_CFG = APP_SETTINGS.get("llm", {})
LLM_MODEL = LLM_CFG.get("model", "gemini-2.5-flash")

# Construct AI profile criteria dynamically from keywords and exclusions
keywords_formatted = "\n".join([f"- {k}" for k in KEYWORDS_LIST])
exclude_formatted = "\n".join([f"- Exclude: {e}" for e in EXCLUDE_LIST])
MY_PROFILE_CRITERIA = f"""
Target Criteria & Keywords:
{keywords_formatted}

Exclusion Rules:
{exclude_formatted}
"""

def send_telegram_message(bot_token: str, chat_id: str, message: str):
    """Sends notification message to your Telegram channel/DM."""
    if not bot_token or not chat_id:
        print("[Telegram] Missing token or chat ID. Skipping alert.", flush=True)
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=15)
        print("[Telegram] Alert sent successfully.", flush=True)
    except Exception as e:
        print(f"[Telegram] Error sending message: {e}", flush=True)

def evaluate_job(client: genai.Client, job_title: str, company: str, description: str) -> dict:
    """Uses LLM to evaluate fit against keywords/exclude rules and generate a short description."""
    desc_lower = description.lower()
    title_lower = job_title.lower()
    
    # 1. Pre-filter local exclusion rules to save API quota
    for ex in EXCLUDE_LIST:
        if ex.lower() in desc_lower or ex.lower() in title_lower:
            return {"match": False, "verdict": f"Excluded due to keyword: {ex}", "short_description": ""}

    summary_snip = description[:180].strip().replace("\n", " ") + "..." if len(description) > 180 else description

    if not client:
        return {
            "match": True,
            "verdict": "MATCH: YES\nREASON: Scraped job matches criteria (LLM evaluation skipped).",
            "short_description": summary_snip
        }

    prompt = f"""
You are an expert career agent. Evaluate this job vacancy against the user profile criteria.

User Criteria & Exclusions:
{MY_PROFILE_CRITERIA}

Job Details:
Title: {job_title}
Company: {company}
Description snippet: {description[:2000]}

Respond ONLY in this format:
MATCH: [YES / NO]
SCORE: [0-100]%
SUMMARY: [1-2 sentences concise summary of the position and key requirements]
REASON: [1 sentence summarizing why it fits or fails]
"""
    try:
        response = client.models.generate_content(
            model=LLM_MODEL,
            contents=prompt
        )
        text = response.text
        is_match = "MATCH: YES" in text
        
        summary = ""
        for line in text.splitlines():
            if line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()
                break
            elif line.startswith("REASON:"):
                summary = line.replace("REASON:", "").strip()

        if not summary:
            summary = summary_snip

        return {
            "match": is_match,
            "verdict": text,
            "short_description": summary
        }
    except Exception as e:
        print(f"[LLM] Notice: Using fallback summary (API limit/notice: {str(e)[:80]}...)", flush=True)
        return {
            "match": True,
            "verdict": "MATCH: YES (Fallback)",
            "short_description": summary_snip
        }


def build_html_report(matched_jobs: list) -> str:
    """Generates an HTML report containing a responsive table: ID, Position Name, Company, Location, Country, Short Description, Link."""
    table_rows = []
    for job in matched_jobs:
        job_id = job.get("id", "")
        title = job.get("title", "N/A")
        company = job.get("company", "N/A")
        location = job.get("location", "N/A")
        country = job.get("country", "N/A")
        desc = job.get("short_description", "")
        url = job.get("url", "#")
        
        link_html = f'<a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none; font-weight: bold; display: inline-block;">View Job</a>' if url and url != "#" else 'N/A'
        
        row_html = f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
            <td style="padding: 10px; text-align: center; font-weight: bold; color: #4b5563; word-break: break-word;">{job_id}</td>
            <td style="padding: 10px; font-weight: 600; color: #111827; word-break: break-word;">{title}</td>
            <td style="padding: 10px; color: #374151; word-break: break-word;">{company}</td>
            <td style="padding: 10px; color: #6b7280; font-size: 13px; word-break: break-word;">{location}</td>
            <td style="padding: 10px; color: #4b5563; font-size: 13px; font-weight: 500; word-break: break-word;">{country}</td>
            <td style="padding: 10px; color: #4b5563; font-size: 13px; word-break: break-word; line-height: 1.4;">{desc}</td>
            <td style="padding: 10px; text-align: center; word-break: break-word;">{link_html}</td>
        </tr>
        """
        table_rows.append(row_html)
        
    rows_content = "\n".join(table_rows) if table_rows else """
    <tr>
        <td colspan="7" style="padding: 20px; text-align: center; color: #6b7280;">No matching job vacancies found for today.</td>
    </tr>
    """

    jobs_str = ", ".join(JOBS_LIST)
    locations_str = ", ".join(LOCATIONS_LIST)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Job Vacancies Report</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9fafb; margin: 0; padding: 10px;">
        <div style="width: 100%; max-width: 1000px; margin: 0 auto; background: #ffffff; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border: 1px solid #e5e7eb; box-sizing: border-box;">
            <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: #ffffff; padding: 20px; text-align: center; border-top-left-radius: 8px; border-top-right-radius: 8px;">
                <h1 style="margin: 0; font-size: 20px; font-weight: 700;">🎯 Daily Job Vacancies Report</h1>
                <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.9;">Found {len(matched_jobs)} matching positions for <strong>{jobs_str}</strong> in <strong>{locations_str}</strong></p>
            </div>
            
            <div style="padding: 10px; overflow-x: auto; width: 100%; box-sizing: border-box;">
                <table style="width: 100%; table-layout: fixed; border-collapse: collapse; text-align: left; font-size: 13px;">
                    <thead>
                        <tr style="background-color: #f3f4f6; color: #374151; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em;">
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 5%; text-align: center;">ID</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 22%;">Position Name</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 15%;">Company</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 18%;">Location</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 10%;">Country</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 20%;">Short Description</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 10%; text-align: center;">Link</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_content}
                    </tbody>
                </table>
            </div>
            
            <div style="background-color: #f9fafb; padding: 12px; border-top: 1px solid #e5e7eb; text-align: center; font-size: 12px; color: #9ca3af; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px;">
                Automated Job Finder • Running Daily
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_email_report(subject: str, html_body: str) -> bool:
    """Sends HTML email report via SMTP using settings.yaml defaults & env variable overrides."""
    EMAIL_CFG = APP_SETTINGS.get("email", {})
    smtp_server = os.getenv("SMTP_SERVER") or EMAIL_CFG.get("smtp_server", "smtp.gmail.com")
    
    smtp_port_raw = os.getenv("SMTP_PORT")
    if smtp_port_raw and str(smtp_port_raw).strip().isdigit():
        smtp_port = int(str(smtp_port_raw).strip())
    else:
        smtp_port = int(EMAIL_CFG.get("smtp_port", 587))

    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO") or EMAIL_CFG.get("email_to", "")
    email_from = os.getenv("EMAIL_FROM") or EMAIL_CFG.get("email_from", smtp_username)
    use_tls_val = os.getenv("SMTP_USE_TLS") or str(EMAIL_CFG.get("use_tls", True))
    use_tls = str(use_tls_val).lower() in ("true", "1", "yes")

    if not all([smtp_server, smtp_username, smtp_password, email_to]):

        print("[Email] SMTP configuration incomplete. Skipping email send.", flush=True)
        print("[Email] Set SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_TO in your .env file to enable email dispatch.", flush=True)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    # Attach HTML part
    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_html)

    try:
        print(f"[*] Connecting to SMTP server {smtp_server}:{smtp_port}...", flush=True)
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
        if use_tls:
            server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(email_from, [email_to], msg.as_string())
        server.quit()
        print(f"[+] Email report sent successfully to {email_to}!", flush=True)
        return True
    except Exception as e:
        print(f"[!] Failed to send email: {e}", flush=True)
        return False

def main():
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    client = None
    if gemini_api_key:
        client = genai.Client(api_key=gemini_api_key)
    else:
        print("[!] GEMINI_API_KEY not set. Job evaluation will run in fallback mode without LLM filtering.", flush=True)

    HISTORY_CFG = APP_SETTINGS.get("history", {})
    IGNORE_DAYS = int(os.getenv("IGNORE_DAYS", HISTORY_CFG.get("ignore_days", 7)))

    matched_jobs = []
    seen_urls, seen_map = load_seen_jobs(ignore_days=IGNORE_DAYS)
    print(f"[*] Loaded {len(seen_urls)} active job URLs seen within the last {IGNORE_DAYS} days.", flush=True)
    item_id = 1

    for search_term in JOBS_LIST:
        for loc in LOCATIONS_LIST:
            print(f"[*] Scanning {SCRAPE_SITES} for '{search_term}' in '{loc}' (wanted: {RESULTS_WANTED}, hours_old: {HOURS_OLD})...", flush=True)
            try:
                jobs = scrape_jobs(
                    site_name=SCRAPE_SITES,
                    search_term=search_term,
                    location=loc,
                    results_wanted=RESULTS_WANTED,
                    hours_old=HOURS_OLD,
                    country_override=loc.lower(),
                    linkedin_fetch_description=True
                )
            except Exception as e:
                print(f"[!] Scraping failed for '{search_term}' in '{loc}': {e}", flush=True)
                continue

            if jobs is None or jobs.empty:
                print(f"[-] No new jobs found for '{search_term}' in '{loc}'.", flush=True)
                continue

            print(f"[+] Scraped {len(jobs)} jobs for '{search_term}' in '{loc}'. Evaluating matches...", flush=True)

            for _, row in jobs.iterrows():
                job_url = str(row.get("job_url", "")).strip()
                if job_url and job_url in seen_urls:
                    continue
                if job_url:
                    seen_urls.add(job_url)
                    seen_map[job_url] = datetime.now(timezone.utc).isoformat()

                title = str(row.get("title", "Unknown Title"))
                company = str(row.get("company", "Unknown Company"))
                job_loc = str(row.get("location", loc))
                desc = str(row.get("description", ""))

                eval_result = evaluate_job(client, title, company, desc)
                if eval_result["match"]:
                    matched_jobs.append({
                        "id": item_id,
                        "title": title,
                        "company": company,
                        "location": job_loc if job_loc and job_loc != "nan" else loc,
                        "country": loc,
                        "short_description": eval_result["short_description"],
                        "url": job_url,
                        "verdict": eval_result["verdict"]
                    })
                    item_id += 1

    print(f"[+] Total new unique matched vacancies today: {len(matched_jobs)}", flush=True)

    # Save updated seen_map history
    save_seen_jobs(seen_map)


    # For debugging/testing, cap report to top 10 matched jobs
    if len(matched_jobs) > 10:
        print(f"[*] Capping report to top 10 matches for debug testing (out of {len(matched_jobs)} total).", flush=True)
        matched_jobs = matched_jobs[:10]

    # 1. Build HTML Report
    html_report = build_html_report(matched_jobs)

    # 2. Save HTML report locally if configured
    REPORTS_CFG = APP_SETTINGS.get("reports", {})
    if REPORTS_CFG.get("save_local_html", True):
        filename = REPORTS_CFG.get("local_filename", "jobs_report.html")
        report_filepath = os.path.abspath(filename)
        with open(report_filepath, "w", encoding="utf-8") as f:
            f.write(html_report)
        print(f"[+] Local HTML report saved to: file:///{report_filepath.replace('\\', '/')}", flush=True)

    # 3. Send Email
    jobs_summary = ", ".join(JOBS_LIST)
    locs_summary = ", ".join(LOCATIONS_LIST)
    if matched_jobs:
        subject = f"🎯 Daily Job Vacancies: {len(matched_jobs)} new matches for {jobs_summary} ({locs_summary})"
    else:
        subject = f"ℹ️ Daily Job Vacancies: No new matches ({locs_summary})"
    
    send_email_report(subject, html_report)

    # 4. Optional Telegram Notification
    if tg_token and tg_chat_id and matched_jobs:
        print("[*] Sending Telegram notifications...", flush=True)
        for match in matched_jobs:
            msg = (
                f"🎯 *New Job Match #{match['id']}!*\n\n"
                f"*{match['title']}* at *{match['company']}*\n"
                f"📍 Location: {match['location']} ({match['country']})\n"
                f"🔗 [Job Link]({match['url']})\n\n"
                f"📝 {match['short_description']}"
            )
            send_telegram_message(tg_token, tg_chat_id, msg)


if __name__ == "__main__":
    main()




