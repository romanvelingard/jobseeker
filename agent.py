import sys
import os
import json
import re
import smtplib
import time
import argparse
import glob
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
import yaml
from dotenv import load_dotenv
from jobspy import scrape_jobs
from google import genai
from google.genai import types

from scorer import score_jobs, extract_skills_summary

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
SEEN_JOBS_FILE = "seen_jobs.json"
QUOTA_EXHAUSTED_FLAG = [False]

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


def load_profile_and_settings(profile_filepath: str, settings_filepath: str = "settings.yaml", cli_email_to: str = None) -> tuple[dict, dict]:
    """
    Loads application settings and job definition search profile.
    Merges profile-specific parameters such as recipient email(s), email subject prefix, and HTML report output filename.
    """
    default_settings = {
        "scraper": {"sites": ["linkedin"], "hours_old": 24, "results_wanted": 20},
        "email": {"smtp_server": "smtp.gmail.com", "smtp_port": 587, "use_tls": True, "email_to": "", "email_from": ""},
        "llm": {"model": "gemini-1.5-flash", "enabled": True},
        "reports": {"save_local_html": True, "local_filename": "jobs_report.html"},
        "history": {"ignore_days": 7}
    }
    app_settings = load_yaml_file(settings_filepath, default_settings)

    default_job_config = {
        "name": "",
        "jobs": [],
        "locations": [],
        "keywords": [],
        "industries": [],
        "exclude": []
    }
    job_config = load_yaml_file(profile_filepath, default_job_config)

    # Recipient email resolution hierarchy:
    # 1. CLI explicit argument (--email-to)
    # 2. Search profile YAML file (email_to / recipients / email.email_to)
    # 3. Environment variable EMAIL_TO
    # 4. settings.yaml email.email_to
    profile_email_to = (
        job_config.get("email_to")
        or job_config.get("recipients")
        or (job_config.get("email", {}).get("email_to") if isinstance(job_config.get("email"), dict) else None)
    )

    resolved_email_to = None
    if cli_email_to:
        resolved_email_to = cli_email_to
    elif profile_email_to:
        resolved_email_to = profile_email_to
    elif os.getenv("EMAIL_TO"):
        resolved_email_to = os.getenv("EMAIL_TO")
    else:
        resolved_email_to = app_settings.get("email", {}).get("email_to", "")

    email_cfg = app_settings.get("email", {}).copy()
    email_cfg["email_to"] = resolved_email_to
    app_settings["email"] = email_cfg

    # Resolve local HTML report filename
    profile_filename = (
        job_config.get("local_filename")
        or (job_config.get("reports", {}).get("local_filename") if isinstance(job_config.get("reports"), dict) else None)
    )
    if not profile_filename:
        base_name = os.path.splitext(os.path.basename(profile_filepath))[0]
        profile_filename = f"jobs_report_{base_name}.html"

    reports_cfg = app_settings.get("reports", {}).copy()
    reports_cfg["local_filename"] = profile_filename
    app_settings["reports"] = reports_cfg

    return job_config, app_settings


def parse_job_entries(job_config: dict) -> list[dict]:
    """
    Parses jobs from job_config, supporting flat lists, structured lists, and jobs_by_location dictionary.
    Returns a list of dicts: [{'title': str, 'locations': list[str] | None}]
    """
    entries = []
    
    # 1. Parse 'jobs_by_location' section if present
    jobs_by_loc = job_config.get("jobs_by_location")
    if isinstance(jobs_by_loc, dict):
        for loc_key, title_list in jobs_by_loc.items():
            if isinstance(title_list, list):
                for t in title_list:
                    if loc_key.lower() in ("global", "all"):
                        entries.append({"title": str(t), "locations": None})
                    else:
                        entries.append({"title": str(t), "locations": [loc_key]})

    # 2. Parse standard 'jobs' list
    raw_jobs = job_config.get("jobs")
    if isinstance(raw_jobs, list):
        for item in raw_jobs:
            if isinstance(item, dict):
                t = item.get("title")
                locs = item.get("locations")
                if t:
                    entries.append({"title": str(t), "locations": locs if isinstance(locs, list) else None})
            elif isinstance(item, str):
                entries.append({"title": item, "locations": None})

    return entries


def resolve_target_locations(entry: dict, global_locations: list[str]) -> list[str]:
    """
    Resolves exact target locations for a job entry based on explicit setting or language script detection.
    """
    explicit_locs = entry.get("locations")
    if explicit_locs and isinstance(explicit_locs, list):
        target = explicit_locs
    else:
        title = entry.get("title", "").strip()
        # 1. Hebrew script detection -> Israel
        if re.search(r'[\u0590-\u05FF]', title):
            target = ["Israel"]
        # 2. Ukrainian / Cyrillic script detection -> Ukraine
        elif re.search(r'[\u0400-\u04FF]', title):
            target = ["Ukraine"]
        # 3. Polish script detection (diacritics or Polish-specific keywords) -> Poland
        elif (re.search(r'[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', title) or 
              re.search(r'\b(ds\.|kierownik|kupiec|zakupów|zaopatrzenia|dostawców|sprzedaży|inżynier)\b', title, re.IGNORECASE)):
            target = ["Poland"]
        else:
            target = global_locations

    if global_locations:
        global_lower = [g.lower() for g in global_locations]
        matched = [l for l in target if l.lower() in global_lower]
        return matched
    return target


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


def build_html_report(matched_jobs: list, job_config: dict = None) -> str:
    """Generates an HTML report containing a responsive table tailored to the active profile."""
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

    jobs_list = []
    locations_list = []
    profile_name = ""
    if job_config:
        profile_name = job_config.get("name", "")
        raw_jobs = job_config.get("jobs", []) or []
        for item in raw_jobs:
            jt = item.get("title") if isinstance(item, dict) else str(item)
            if jt:
                jobs_list.append(jt)
        locations_list = job_config.get("locations", []) or []

    jobs_str = ", ".join(jobs_list[:5]) + (f" (+{len(jobs_list)-5} more)" if len(jobs_list) > 5 else "") if jobs_list else "All Configured Roles"
    locations_str = ", ".join(locations_list) if locations_list else "All Active Locations"

    header_title = f"🎯 Daily Job Vacancies Report"
    if profile_name:
        header_title += f" — {profile_name}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{header_title}</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9fafb; margin: 0; padding: 10px;">
        <div style="width: 100%; max-width: 1000px; margin: 0 auto; background: #ffffff; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border: 1px solid #e5e7eb; box-sizing: border-box;">
            <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: #ffffff; padding: 20px; text-align: center; border-top-left-radius: 8px; border-top-right-radius: 8px;">
                <h1 style="margin: 0; font-size: 20px; font-weight: 700;">{header_title}</h1>
                <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.9;">Found {len(matched_jobs)} matching positions for <strong>{jobs_str}</strong> in <strong>{locations_str}</strong></p>
            </div>
            
            <div style="padding: 10px; overflow-x: auto; width: 100%; box-sizing: border-box;">
                <table style="width: 100%; table-layout: fixed; border-collapse: collapse; text-align: left; font-size: 13px;">
                    <thead>
                        <tr style="background-color: #f3f4f6; color: #374151; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em;">
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 5%; text-align: center;">ID</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 20%;">Position Name</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 14%;">Company</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 13%;">Location</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 8%;">Country</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 32%;">Skills & Summary</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; width: 8%; text-align: center;">Link</th>
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


def send_email_report(subject: str, html_body: str, app_settings: dict) -> bool:
    """Sends HTML email report via SMTP using settings & env variable overrides."""
    EMAIL_CFG = app_settings.get("email", {}) if app_settings else {}
    smtp_server = os.getenv("SMTP_SERVER") or EMAIL_CFG.get("smtp_server", "smtp.gmail.com")
    
    smtp_port_raw = os.getenv("SMTP_PORT")
    if smtp_port_raw and str(smtp_port_raw).strip().isdigit():
        smtp_port = int(str(smtp_port_raw).strip())
    else:
        smtp_port = int(EMAIL_CFG.get("smtp_port", 587))

    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    raw_email_to = EMAIL_CFG.get("email_to", "") or os.getenv("EMAIL_TO", "")
    email_from = os.getenv("EMAIL_FROM") or EMAIL_CFG.get("email_from", smtp_username)
    use_tls_val = os.getenv("SMTP_USE_TLS") or str(EMAIL_CFG.get("use_tls", True))
    use_tls = str(use_tls_val).lower() in ("true", "1", "yes")

    if isinstance(raw_email_to, list):
        recipients = [str(addr).strip() for addr in raw_email_to if str(addr).strip()]
    elif isinstance(raw_email_to, str):
        recipients = [addr.strip() for addr in raw_email_to.split(",") if addr.strip()]
    else:
        recipients = []

    if not smtp_server or not smtp_username or not smtp_password or not recipients:
        print("[Email] SMTP configuration incomplete or recipients missing. Skipping email send.", flush=True)
        print(f"[Email] Current configuration -> SMTP_SERVER: {smtp_server}, USERNAME: {smtp_username}, Recipients: {recipients}", flush=True)
        return False

    to_header = ", ".join(recipients)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = to_header

    # Attach HTML part
    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_html)

    try:
        print(f"[*] Connecting to SMTP server {smtp_server}:{smtp_port}...", flush=True)
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
        if use_tls:
            server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(email_from, recipients, msg.as_string())
        server.quit()
        print(f"[+] Email report sent successfully to {to_header}!", flush=True)
        return True
    except Exception as e:
        print(f"[!] Failed to send email: {e}", flush=True)
        return False


# ==========================================
# REFACTORED PIPELINE STEPS
# ==========================================

def step1_scrape_all_jobs(job_config: dict, app_settings: dict) -> list[dict]:
    """Step 1: Performs search according to job list and location scoping."""
    job_entries = parse_job_entries(job_config)
    locations_list = job_config.get("locations", []) or []
    scraper_cfg = app_settings.get("scraper", {})
    sites = scraper_cfg.get("sites", ["linkedin"])
    hours_old = int(os.getenv("HOURS_OLD", scraper_cfg.get("hours_old", 24)))
    results_wanted = int(os.getenv("RESULTS_WANTED", scraper_cfg.get("results_wanted", 20)))

    print(f"[*] STEP 1: Scrape & Search positions across locations: {locations_list}", flush=True)
    raw_found_jobs = []

    for entry in job_entries:
        search_term = entry.get("title", "")
        if not search_term:
            continue

        target_locs = resolve_target_locations(entry, locations_list)
        if not target_locs:
            print(f"[*] Skipping '{search_term}' (no target location match in active list {locations_list}).", flush=True)
            continue

        for loc in target_locs:
            print(f"[*] Scanning {sites} for '{search_term}' in '{loc}' (target: {loc}, wanted: {results_wanted}, hours_old: {hours_old})...", flush=True)
            try:
                jobs = scrape_jobs(
                    site_name=sites,
                    search_term=search_term,
                    location=loc,
                    results_wanted=results_wanted,
                    hours_old=hours_old,
                    country_override=loc.lower(),
                    linkedin_fetch_description=True
                )
            except Exception as e:
                print(f"[!] Scraping failed for '{search_term}' in '{loc}': {e}", flush=True)
                continue

            if jobs is None or jobs.empty:
                print(f"[-] No new jobs found for '{search_term}' in '{loc}'.", flush=True)
                continue

            print(f"[+] Scraped {len(jobs)} jobs for '{search_term}' in '{loc}'.", flush=True)

            for _, row in jobs.iterrows():
                job_url = str(row.get("job_url", "")).strip()
                title = str(row.get("title", "Unknown Title"))
                company = str(row.get("company", "Unknown Company"))
                job_loc = str(row.get("location", loc))
                desc = str(row.get("description", ""))

                raw_found_jobs.append({
                    "title": title,
                    "company": company,
                    "location": job_loc if job_loc and job_loc != "nan" else loc,
                    "country": loc,
                    "desc": desc,
                    "url": job_url
                })

    print(f"[+] STEP 1 Complete: Total raw jobs collected = {len(raw_found_jobs)}", flush=True)
    return raw_found_jobs


def step2_filter_seen_jobs(found_jobs: list[dict], ignore_days: int) -> tuple[list[dict], set, dict]:
    """Step 2 & 3: Creates list of found jobs and removes jobs sent already in the last 7 days."""
    seen_urls, seen_map = load_seen_jobs(ignore_days=ignore_days)
    print(f"[*] STEP 2 & 3: Loaded {len(seen_urls)} job URLs seen within the last {ignore_days} days.", flush=True)

    unseen_jobs = []
    run_urls = set()

    for job in found_jobs:
        u = job.get("url", "")
        if u and (u in seen_urls or u in run_urls):
            continue
        if u:
            run_urls.add(u)
        unseen_jobs.append(job)

    print(f"[+] STEP 2 & 3 Complete: {len(unseen_jobs)} unseen jobs remaining for evaluation.", flush=True)
    return unseen_jobs, seen_urls, seen_map


def is_valid_role_title(title: str, job_config: dict) -> bool:
    """Filters out irrelevant roles whose description text merely mentions target terms."""
    if not title:
        return False

    t_lower = title.lower()

    # 1. Direct match with any target job title in job_config
    raw_jobs = job_config.get("jobs", []) or []
    for item in raw_jobs:
        jt = item.get("title") if isinstance(item, dict) else str(item)
        if jt and jt.lower() in t_lower:
            return True

    # 2. Check for keywords from KEYWORDS_LIST or INDUSTRIES_LIST in title
    keywords = job_config.get("keywords", []) or []
    industries = job_config.get("industries", []) or []
    for kw in keywords + industries:
        if kw and len(kw) > 3 and kw.lower() in t_lower:
            return True

    # 3. Comprehensive domain role keywords in TITLE
    role_keywords = [
        "procurement", "purchasing", "sourcing", "buyer", "kupiec", "zakupów", "zaopatrzenia",
        "קניין", "רכש", "שרשרת אספקה", "закупівель", "постачання",
        "supply chain", "vendor", "supplier", "category", "materials", "material", "inventory", "demand",
        "operations", "logistics", "planning", "planner", "commercial", "subcontracting", "tendering",
        "logistyka", "planowanie", "analityk", "rekrutacji",
        "תפעול", "לוגיסטיקה", "מלאי", "ספקים",
        "operations coordinator", "supplier coordinator", "supply coordinator", "sales coordinator",
        "רכז תפעול", "מתאם תפעול", "מתאם ספקים", "מתאם מכירות", "רכז מכירות",
        "koordynator", "specjalista", "kierownik",
        "qa", "automation", "test", "testing", "developer", "engineer", "software", "devops", "cloud", "backend"
    ]

    for rk in role_keywords:
        if rk in t_lower:
            return True

    return False


def step3_filter_exclusions(jobs_list: list[dict], exclude_list: list, job_config: dict = None) -> list[dict]:
    """Step 4: Removes jobs matching exclusion criteria or having non-matching position titles."""
    print(f"[*] STEP 4: Applying {len(exclude_list)} exclusion rules & position title validation...", flush=True)
    filtered_jobs = []

    for job in jobs_list:
        title = job.get("title", "")
        title_lower = title.lower()
        desc_lower = job.get("desc", "").lower()
        loc_lower = job.get("location", "").lower()

        # Title role validation check
        if job_config and not is_valid_role_title(title, job_config):
            continue

        excluded = False
        for ex in exclude_list:
            ex_lower = ex.lower()
            if not ex_lower:
                continue

            # Short exclusion terms (<=3 chars e.g. "vp") use regex word boundary matching
            if len(ex_lower) <= 3:
                pattern = r'\b' + re.escape(ex_lower) + r'\b'
                if re.search(pattern, title_lower) or re.search(pattern, desc_lower) or re.search(pattern, loc_lower):
                    excluded = True
                    break
            else:
                if ex_lower in desc_lower or ex_lower in title_lower or ex_lower in loc_lower:
                    excluded = True
                    break

        if not excluded:
            filtered_jobs.append(job)

    print(f"[+] STEP 4 Complete: Filtered out {len(jobs_list) - len(filtered_jobs)} invalid/excluded jobs. Remaining = {len(filtered_jobs)}", flush=True)
    return filtered_jobs


def step4_score_jobs(jobs_list: list[dict], client: genai.Client, job_config: dict, app_settings: dict, last_request_time: list[float], quota_exhausted_flag: list[bool]) -> list[dict]:
    """Step 5: Gives score for each job left by country and by industry via dedicated scorer module."""
    print(f"[*] STEP 5: Scoring {len(jobs_list)} candidate jobs by country & industry...", flush=True)
    scored = score_jobs(client, jobs_list, job_config, app_settings, last_request_time, quota_exhausted_flag)
    print(f"[+] STEP 5 Complete: Scored {len(scored)} jobs.", flush=True)
    return scored


def step5_select_top_jobs(scored_jobs: list[dict], max_results: int = 30) -> list[dict]:
    """Step 6: Prepares final list ordered by country tier and composite score."""
    print(f"[*] STEP 6: Selecting top {max_results} jobs...", flush=True)
    
    def get_country_tier(c_str: str) -> int:
        c = str(c_str or "").lower()
        if "israel" in c:
            return 0
        elif "poland" in c:
            return 1
        else:
            return 2

    sorted_scored = sorted(scored_jobs, key=lambda j: (get_country_tier(j.get("country", "")), -j.get("score", 0.0)))
    top_jobs = sorted_scored[:max_results]

    final_list = []
    for idx, j in enumerate(top_jobs, 1):
        eval_res = j.get("eval_result", {})
        final_list.append({
            "id": idx,
            "title": j.get("title", "Unknown Title"),
            "company": j.get("company", "Unknown Company"),
            "location": j.get("location", j.get("country", "N/A")),
            "country": j.get("country", "N/A"),
            "short_description": eval_res.get("short_description", ""),
            "url": j.get("url", "#"),
            "verdict": eval_res.get("verdict", ""),
            "score": j.get("score", 0.0)
        })

    print(f"[+] STEP 6 Complete: Final report selection contains {len(final_list)} top-scoring positions.", flush=True)
    return final_list


def step6_dispatch_reports(final_jobs: list[dict], seen_urls: set, seen_map: dict, ignore_days: int, app_settings: dict, job_config: dict):
    """Dispatches reports and updates seen_jobs history."""
    print(f"[*] Dispatching final reports...", flush=True)
    
    # 1. Update seen_jobs.json history for top selected jobs
    now_str = datetime.now(timezone.utc).isoformat()
    for j in final_jobs:
        u = j.get("url")
        if u:
            seen_urls.add(u)
            seen_map[u] = now_str

    save_seen_jobs(seen_map)

    # 2. Build HTML report tailored to profile
    html_report = build_html_report(final_jobs, job_config=job_config)

    # 3. Save local HTML report if configured
    reports_cfg = app_settings.get("reports", {})
    if reports_cfg.get("save_local_html", True):
        filename = reports_cfg.get("local_filename", "jobs_report.html")
        report_filepath = os.path.abspath(filename)
        with open(report_filepath, "w", encoding="utf-8") as f:
            f.write(html_report)
        print(f"[+] Local HTML report saved to: file:///{report_filepath.replace('\\', '/')}", flush=True)

    # 4. Dispatch Email Report
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    profile_prefix = job_config.get("email_subject_prefix") or (f"[{job_config.get('name')}]" if job_config.get("name") else "")
    prefix_str = f"{profile_prefix} " if profile_prefix else ""

    if final_jobs:
        subject = f"🎯 {prefix_str}Daily Job Vacancies: {len(final_jobs)} new matches for {today_str}".strip()
    else:
        subject = f"ℹ️ {prefix_str}Daily Job Vacancies: No new matches for {today_str}".strip()

    send_email_report(subject, html_report, app_settings=app_settings)

    # 5. Dispatch Telegram Notifications
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id and final_jobs:
        print("[*] Sending Telegram notifications...", flush=True)
        for match in final_jobs:
            msg = (
                f"🎯 *New Job Match #{match['id']} (Score: {match['score']:.0f})*\n\n"
                f"*{match['title']}* at *{match['company']}*\n"
                f"📍 Location: {match['location']} ({match['country']})\n"
                f"🔗 [Job Link]({match['url']})\n\n"
                f"📝 {match['short_description']}"
            )
            send_telegram_message(tg_token, tg_chat_id, msg)

    print("[+] Pipeline Execution Finished Successfully!", flush=True)


def run_pipeline(profile_filepath: str, settings_filepath: str = "settings.yaml", cli_email_to: str = None):
    """Executes full search and report dispatch pipeline for a single profile."""
    print(f"\n========================================================", flush=True)
    print(f"🚀 STARTING PIPELINE RUN FOR PROFILE: {profile_filepath}", flush=True)
    print(f"========================================================\n", flush=True)

    job_config, app_settings = load_profile_and_settings(profile_filepath, settings_filepath, cli_email_to)

    profile_name = job_config.get("name", os.path.basename(profile_filepath))
    recipients = app_settings.get("email", {}).get("email_to", "Not configured")
    print(f"[*] Profile Name: {profile_name}", flush=True)
    print(f"[*] Recipient Email(s): {recipients}", flush=True)
    print(f"[*] Local HTML Output: {app_settings.get('reports', {}).get('local_filename')}\n", flush=True)

    llm_cfg = app_settings.get("llm", {})
    llm_provider = str(llm_cfg.get("provider", "gemini")).lower()
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    client = None
    if llm_provider == "gemini":
        if gemini_api_key:
            client = genai.Client(api_key=gemini_api_key)
        else:
            print("[!] GEMINI_API_KEY not set. Job evaluation running in local fallback mode.", flush=True)

    history_cfg = app_settings.get("history", {})
    ignore_days = int(os.getenv("IGNORE_DAYS", history_cfg.get("ignore_days", 7)))
    scraper_cfg = app_settings.get("scraper", {})
    max_results = int(os.getenv("RESULTS_WANTED", scraper_cfg.get("results_wanted", 30)))

    exclude_list = job_config.get("exclude", []) or []
    last_request_time = [0.0]

    # 1. Scrape jobs matching search profile criteria
    found_jobs = step1_scrape_all_jobs(job_config, app_settings)

    # 2 & 3. Filter jobs already seen
    unseen_jobs, seen_urls, seen_map = step2_filter_seen_jobs(found_jobs, ignore_days=ignore_days)

    # 4. Remove jobs matching exclusions or invalid position titles
    candidate_jobs = step3_filter_exclusions(unseen_jobs, exclude_list, job_config)

    # 5. Score candidate jobs
    scored_jobs = step4_score_jobs(candidate_jobs, client, job_config, app_settings, last_request_time, QUOTA_EXHAUSTED_FLAG)

    # 6. Prepare top selected jobs
    final_jobs = step5_select_top_jobs(scored_jobs, max_results=max_results)

    # Dispatch final reports & persist seen history
    step6_dispatch_reports(final_jobs, seen_urls, seen_map, ignore_days, app_settings, job_config)

    print(f"\n[+] Finished execution for profile: {profile_name}\n", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Job Seeker Multi-Profile Automated Finder")
    parser.add_argument(
        "-p", "--profile", "--job-def", "--config",
        nargs="*",
        dest="profiles",
        help="Path to search profile YAML file(s) or directory. Can pass multiple profiles."
    )
    parser.add_argument(
        "-a", "--all-profiles",
        action="store_true",
        help="Run all search profile YAML files located inside profiles/ directory."
    )
    parser.add_argument(
        "-s", "--settings",
        default="settings.yaml",
        help="Path to global application settings YAML file (default: settings.yaml)."
    )
    parser.add_argument(
        "-e", "--email-to",
        help="Override recipient email address(es) for this run (comma-separated)."
    )
    args = parser.parse_args()

    profiles_to_run = []

    if args.all_profiles:
        if os.path.exists("profiles"):
            profiles_to_run = sorted(glob.glob("profiles/*.yaml") + glob.glob("profiles/*.yml"))
        if not profiles_to_run:
            print("[!] Warning: --all-profiles specified but no YAML files found in profiles/ directory.", flush=True)
    elif args.profiles:
        for item in args.profiles:
            if os.path.isdir(item):
                profiles_to_run.extend(sorted(glob.glob(os.path.join(item, "*.yaml")) + glob.glob(os.path.join(item, "*.yml"))))
            elif os.path.exists(item):
                profiles_to_run.append(item)
            else:
                print(f"[!] Warning: Profile file '{item}' not found.", flush=True)
    else:
        # Default behavior when no profile arguments are specified:
        env_job_def = os.getenv("JOB_DEF_FILE")
        if env_job_def and os.path.exists(env_job_def):
            profiles_to_run.append(env_job_def)
        elif os.path.exists("profiles"):
            found = sorted(glob.glob("profiles/*.yaml") + glob.glob("profiles/*.yml"))
            if found:
                profiles_to_run = found

        if not profiles_to_run:
            if os.path.exists("job_definition.yaml"):
                profiles_to_run.append("job_definition.yaml")
            else:
                print("[!] Error: No search profile configuration files found.", flush=True)
                return

    # Deduplicate profile paths while preserving order
    seen_paths = set()
    unique_profiles = []
    for path in profiles_to_run:
        norm = os.path.abspath(path)
        if norm not in seen_paths:
            seen_paths.add(norm)
            unique_profiles.append(path)

    print(f"[*] Identified {len(unique_profiles)} search profile(s) to execute:", flush=True)
    for p in unique_profiles:
        print(f"    - {p}", flush=True)

    for profile_path in unique_profiles:
        try:
            run_pipeline(profile_path, settings_filepath=args.settings, cli_email_to=args.email_to)
        except Exception as e:
            print(f"[!] Error running profile '{profile_path}': {e}", flush=True)


if __name__ == "__main__":
    main()
