import sys
import os
import smtplib
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
# 1. LOAD CONFIGURATION FROM YAML & ENV
# ==========================================
CONFIG_FILE = "job_definition.yaml"

def load_job_config():
    """Loads configuration from job_definition.yaml with fallback defaults."""
    config = {
        "jobs": ["Senior QA Automation Engineer"],
        "locations": ["Israel"],
        "hours_old": 24,
        "results_wanted": 0,
        "keywords": ["Python", "Playwright", "Selenium", "CI/CD", "API testing"],
        "exclude": ["Pure manual QA", "Unpaid internship"]
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f)
                if isinstance(yaml_cfg, dict):
                    config.update(yaml_cfg)
            print(f"[+] Loaded job definitions from {CONFIG_FILE}")
        except Exception as e:
            print(f"[!] Error reading {CONFIG_FILE}: {e}")
    
    return config

JOB_CONFIG = load_job_config()

JOBS_LIST = JOB_CONFIG.get("jobs") or ["Senior QA Automation Engineer"]
LOCATIONS_LIST = JOB_CONFIG.get("locations") or ["Israel"]
HOURS_OLD = int(JOB_CONFIG.get("hours_old") or 24)

# Support unlimited/max results (if 0, None, or omitted, default to 50 per search query to prevent LinkedIn rate limits)
raw_results_wanted = JOB_CONFIG.get("results_wanted")
RESULTS_WANTED = 50 if (raw_results_wanted is None or raw_results_wanted == 0) else int(raw_results_wanted)

KEYWORDS_LIST = JOB_CONFIG.get("keywords") or []
EXCLUDE_LIST = JOB_CONFIG.get("exclude") or []

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
        print("[Telegram] Missing token or chat ID. Skipping alert.")
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
        print("[Telegram] Alert sent successfully.")
    except Exception as e:
        print(f"[Telegram] Error sending message: {e}")

def evaluate_job(client: genai.Client, job_title: str, company: str, description: str) -> dict:
    """Uses LLM to evaluate fit against keywords/exclude rules and generate a short description."""
    if not client:
        # Fallback keyword filter if no Gemini API key provided
        desc_lower = description.lower()
        title_lower = job_title.lower()
        
        # Check exclusion rules
        for ex in EXCLUDE_LIST:
            if ex.lower() in desc_lower or ex.lower() in title_lower:
                return {"match": False, "verdict": f"Excluded due to keyword: {ex}", "short_description": ""}

        summary_snip = description[:180].strip().replace("\n", " ") + "..." if len(description) > 180 else description
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
            model="gemini-2.5-flash",
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
            summary = description[:180].strip().replace("\n", " ") + "..." if len(description) > 180 else description

        return {
            "match": is_match,
            "verdict": text,
            "short_description": summary
        }
    except Exception as e:
        print(f"[LLM] Parsing error: {e}")
        return {
            "match": True,
            "verdict": "MATCH: YES (Evaluation error fallback)",
            "short_description": description[:180].strip().replace("\n", " ") + "..." if len(description) > 180 else description
        }

def build_html_report(matched_jobs: list) -> str:
    """Generates an HTML report containing a table with schema: ID, Position Name, Company, Location, Country, Short Description, Link."""
    table_rows = []
    for job in matched_jobs:
        job_id = job.get("id", "")
        title = job.get("title", "N/A")
        company = job.get("company", "N/A")
        location = job.get("location", "N/A")
        country = job.get("country", "N/A")
        desc = job.get("short_description", "")
        url = job.get("url", "#")
        
        link_html = f'<a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none; font-weight: bold;">View Job</a>' if url and url != "#" else 'N/A'
        
        row_html = f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
            <td style="padding: 12px 15px; text-align: center; font-weight: bold; color: #4b5563;">{job_id}</td>
            <td style="padding: 12px 15px; font-weight: 600; color: #111827;">{title}</td>
            <td style="padding: 12px 15px; color: #374151;">{company}</td>
            <td style="padding: 12px 15px; color: #6b7280; font-size: 13px;">{location}</td>
            <td style="padding: 12px 15px; color: #4b5563; font-size: 13px; font-weight: 500;">{country}</td>
            <td style="padding: 12px 15px; color: #4b5563; font-size: 13px; max-width: 300px;">{desc}</td>
            <td style="padding: 12px 15px; text-align: center;">{link_html}</td>
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
        <title>Daily Job Vacancies Report</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9fafb; margin: 0; padding: 20px;">
        <div style="max-width: 980px; margin: 0 auto; background: #ffffff; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); overflow: hidden; border: 1px solid #e5e7eb;">
            <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: #ffffff; padding: 24px; text-align: center;">
                <h1 style="margin: 0; font-size: 22px; font-weight: 700;">🎯 Daily Job Vacancies Report</h1>
                <p style="margin: 6px 0 0 0; font-size: 14px; opacity: 0.9;">Found {len(matched_jobs)} matching positions for <strong>{jobs_str}</strong> in <strong>{locations_str}</strong></p>
            </div>
            
            <div style="padding: 20px; overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 14px;">
                    <thead>
                        <tr style="background-color: #f3f4f6; color: #374151; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.05em;">
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb; width: 40px; text-align: center;">ID</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb;">Position Name</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb;">Company</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb;">Location</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb;">Country</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb;">Short Description</th>
                            <th style="padding: 12px 15px; border-bottom: 2px solid #e5e7eb; text-align: center;">Link</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_content}
                    </tbody>
                </table>
            </div>
            
            <div style="background-color: #f9fafb; padding: 15px 24px; border-top: 1px solid #e5e7eb; text-align: center; font-size: 12px; color: #9ca3af;">
                Automated Job Finder • Running Daily
            </div>
        </div>
    </body>
    </html>
    """
    return html

def send_email_report(subject: str, html_body: str) -> bool:
    """Sends HTML email report via SMTP."""
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")
    email_from = os.getenv("EMAIL_FROM", smtp_username)
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")

    if not all([smtp_server, smtp_username, smtp_password, email_to]):
        print("[Email] SMTP configuration incomplete. Skipping email send.")
        print("[Email] Set SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_TO in your .env file to enable email dispatch.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    # Attach HTML part
    part_html = MIMEText(html_body, "utf-8")
    msg.attach(part_html)

    try:
        print(f"[*] Connecting to SMTP server {smtp_server}:{smtp_port}...")
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
        if use_tls:
            server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(email_from, [email_to], msg.as_string())
        server.quit()
        print(f"[+] Email report sent successfully to {email_to}!")
        return True
    except Exception as e:
        print(f"[!] Failed to send email: {e}")
        return False

def main():
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    client = None
    if gemini_api_key:
        client = genai.Client(api_key=gemini_api_key)
    else:
        print("[!] GEMINI_API_KEY not set. Job evaluation will run in fallback mode without LLM filtering.")

    matched_jobs = []
    seen_urls = set()
    item_id = 1

    for search_term in JOBS_LIST:
        for loc in LOCATIONS_LIST:
            print(f"[*] Scanning LinkedIn for '{search_term}' in '{loc}' (wanted: {RESULTS_WANTED}, hours_old: {HOURS_OLD})...", flush=True)
            try:
                jobs = scrape_jobs(
                    site_name=["linkedin"],
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

    print(f"[+] Total unique matched vacancies: {len(matched_jobs)}")

    # 1. Build HTML Report
    html_report = build_html_report(matched_jobs)

    # 2. Save HTML report locally for inspection / offline testing
    report_filepath = os.path.abspath("jobs_report.html")
    with open(report_filepath, "w", encoding="utf-8") as f:
        f.write(html_report)
    print(f"[+] Local HTML report saved to: file:///{report_filepath.replace('\\', '/')}")

    # 3. Send Email
    jobs_summary = ", ".join(JOBS_LIST)
    locs_summary = ", ".join(LOCATIONS_LIST)
    if matched_jobs:
        subject = f"🎯 Daily Job Vacancies: {len(matched_jobs)} matches for {jobs_summary} ({locs_summary})"
    else:
        subject = f"ℹ️ Daily Job Vacancies: No new matches ({locs_summary})"
    
    send_email_report(subject, html_report)

    # 4. Optional Telegram Notification
    if tg_token and tg_chat_id and matched_jobs:
        print("[*] Sending Telegram notifications...")
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



