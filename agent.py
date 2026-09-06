import sys
import os
import json
import re
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
import yaml
from dotenv import load_dotenv
from jobspy import scrape_jobs
from google import genai
from google.genai import types

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

# Load Job Definition Criteria from job_definition.yaml
JOB_CONFIG = load_yaml_file(JOB_DEF_FILE, {
    "jobs": [],
    "locations": [],
    "keywords": [],
    "exclude": []
})

# Load Operational & Application Settings
APP_SETTINGS = load_yaml_file(SETTINGS_FILE, {
    "scraper": {"sites": ["linkedin"], "hours_old": 24, "results_wanted": 20},
    "email": {"smtp_server": "smtp.gmail.com", "smtp_port": 587, "use_tls": True, "email_to": "", "email_from": ""},
    "llm": {"model": "gemini-3.6-flash", "enabled": True},
    "reports": {"save_local_html": True, "local_filename": "jobs_report.html"}
})

# Extract Job Criteria
JOBS_LIST = JOB_CONFIG.get("jobs") if JOB_CONFIG.get("jobs") is not None else []
LOCATIONS_LIST = JOB_CONFIG.get("locations") if JOB_CONFIG.get("locations") is not None else []
KEYWORDS_LIST = JOB_CONFIG.get("keywords") if JOB_CONFIG.get("keywords") is not None else []
EXCLUDE_LIST = JOB_CONFIG.get("exclude") if JOB_CONFIG.get("exclude") is not None else []

# Extract Operational Settings (Env variables take precedence)
SCRAPER_CFG = APP_SETTINGS.get("scraper", {})
HOURS_OLD = int(os.getenv("HOURS_OLD", SCRAPER_CFG.get("hours_old", 24)))
RESULTS_WANTED = int(os.getenv("RESULTS_WANTED", SCRAPER_CFG.get("results_wanted", 20)))
SCRAPE_SITES = SCRAPER_CFG.get("sites", ["linkedin"])

LLM_CFG = APP_SETTINGS.get("llm", {})
LLM_PROVIDER = str(LLM_CFG.get("provider", "gemini")).lower()
LLM_MODEL = LLM_CFG.get("model", "gemini-2.5-flash")
LLM_RPM = int(LLM_CFG.get("requests_per_minute", 15))
LLM_BATCH_SIZE = int(LLM_CFG.get("batch_size", 3))
LLM_MAX_RETRIES = int(LLM_CFG.get("max_retries", 3))
LLM_RETRY_DELAY = float(LLM_CFG.get("retry_delay", 12))
LLM_OLLAMA_HOST = LLM_CFG.get("ollama_host", "http://localhost:11434")

# Global flag to immediately switch to 0s local mode if quota is reached
QUOTA_EXHAUSTED_FLAG = [False]

# Construct AI profile criteria dynamically from keywords and exclusions
keywords_formatted = "\n".join([f"- {k}" for k in KEYWORDS_LIST])
exclude_formatted = "\n".join([f"- Exclude: {e}" for e in EXCLUDE_LIST])
MY_PROFILE_CRITERIA = f"""
Target Criteria & Keywords:
{keywords_formatted}

Exclusion Rules:
{exclude_formatted}
"""


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
    Resolves the exact target locations for a job entry.
    If explicit locations are defined in entry, uses those.
    Otherwise, uses script/language detection:
      - Hebrew script -> Israel
      - Cyrillic script -> Ukraine
      - Polish script/keywords -> Poland
      - Global/English -> global_locations
    Filters resolved locations against global_locations if global_locations is specified.
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

def extract_skills_summary(description: str, job_title: str, max_words: int = 30) -> str:
    """Extracts matched keywords and the first 30 words of job description as a clean fallback summary."""
    if not description or len(description.strip()) == 0:
        return f"Position: {job_title}"
    
    # Clean whitespace and extract first max_words
    words = description.split()
    first_n_words = " ".join(words[:max_words])
    if len(words) > max_words:
        first_n_words += "..."

    # Extract matched keywords from job_definition.yaml
    found_skills = []
    seen_lower = set()

    for kw in KEYWORDS_LIST:
        k_lower = kw.lower()
        if k_lower not in seen_lower and re.search(r'\b' + re.escape(kw) + r'\b', description, re.IGNORECASE):
            found_skills.append(kw)
            seen_lower.add(k_lower)

    if found_skills:
        skills_str = ", ".join(found_skills[:6])
        return f"<strong>Key Skills: {skills_str}</strong> — {first_n_words}"
    else:
        return f"<strong>Overview:</strong> {first_n_words}"


def get_job_priority(job: dict) -> tuple[int, int]:
    """Returns priority sorting tuple (rank, original_id):
    Rank 1: Purchasing / Buyer / Procurement roles in Israel
    Rank 2: Purchasing / Buyer / Procurement roles in Poland
    Rank 3: Purchasing / Buyer / Procurement roles in Ukraine
    Rank 4: All other matched roles
    """
    title = job.get("title", "").lower()
    country = job.get("country", "").lower()

    buyer_keywords = ["buyer", "purchasing", "procurement", "sourcing", "קניין", "רכש", "zakupów", "kupiec", "закупівель", "постачання"]
    is_buyer_role = any(kw in title for kw in buyer_keywords)

    if is_buyer_role and "israel" in country:
        rank = 1
    elif is_buyer_role and "poland" in country:
        rank = 2
    elif is_buyer_role and "ukraine" in country:
        rank = 3
    else:
        rank = 4

    return (rank, job.get("id", 0))


def parse_batch_response(batch: list[dict], response_text: str):
    """Parses LLM batch evaluation response and assigns eval_result to each job dict."""
    blocks = re.split(r'===\s*EVALUATION FOR JOB\s*\d+\s*===', response_text)
    eval_blocks = [b.strip() for b in blocks if b.strip()]

    for idx, job in enumerate(batch):
        block_text = eval_blocks[idx] if idx < len(eval_blocks) else ""
        if block_text:
            is_match = "MATCH: YES" in block_text
            summary = ""
            for line in block_text.splitlines():
                if line.startswith("SUMMARY:"):
                    summary = line.replace("SUMMARY:", "").strip()
                    if summary.startswith("Key Skills:"):
                        parts = summary.split(" — ", 1)
                        if len(parts) == 2:
                            summary = f"<strong>{parts[0]}</strong> — {parts[1]}"
                        else:
                            summary = f"<strong>{summary}</strong>"
                    break
                elif line.startswith("REASON:") and not summary:
                    summary = line.replace("REASON:", "").strip()
            
            if not summary:
                summary = extract_skills_summary(job.get("desc", ""), job.get("title", ""))
            
            job["eval_result"] = {
                "match": is_match,
                "verdict": block_text,
                "short_description": summary
            }
        else:
            job["eval_result"] = {
                "match": True,
                "verdict": "MATCH: YES (Fallback)",
                "short_description": extract_skills_summary(job.get("desc", ""), job.get("title", ""))
            }


def evaluate_jobs_batch(client: genai.Client, unparsed_jobs: list[dict], last_request_time: list[float]):
    """Evaluates a list of scraped jobs using LLM in batches with request delay pacing and exponential retries."""
    if not unparsed_jobs:
        return

    # 1. Local keyword pre-filtering to eliminate obvious non-matches without API calls
    for j in unparsed_jobs:
        desc_lower = j.get("desc", "").lower()
        title_lower = j.get("title", "").lower()
        for ex in EXCLUDE_LIST:
            if ex.lower() in desc_lower or ex.lower() in title_lower:
                j["eval_result"] = {"match": False, "verdict": f"Excluded due to keyword: {ex}", "short_description": ""}
                break

def call_llm_api(prompt: str, client: genai.Client, last_request_time: list[float]) -> str:
    """Dispatches prompt to configured LLM provider (gemini, groq, openrouter, ollama)."""
    if QUOTA_EXHAUSTED_FLAG[0]:
        return None

    # Rate limit delay pacing: enforce requests_per_minute quota
    min_interval = 60.0 / max(1, LLM_RPM)
    elapsed = time.time() - last_request_time[0]
    if elapsed < min_interval:
        sleep_needed = min_interval - elapsed
        time.sleep(sleep_needed)

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            last_request_time[0] = time.time()
            if LLM_PROVIDER == "groq":
                groq_key = os.getenv("GROQ_API_KEY") or LLM_CFG.get("api_key", "")
                if not groq_key:
                    raise Exception("GROQ_API_KEY not found in environment or settings.yaml")
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                model_name = LLM_MODEL if LLM_MODEL and "gemini" not in LLM_MODEL else "llama-3.1-8b-instant"
                payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
                res = requests.post(url, headers=headers, json=payload, timeout=20)
                if res.status_code == 200:
                    return res.json()["choices"][0]["message"]["content"]
                else:
                    raise Exception(f"Groq API Error {res.status_code}: {res.text}")

            elif LLM_PROVIDER == "openrouter":
                or_key = os.getenv("OPENROUTER_API_KEY") or LLM_CFG.get("api_key", "")
                if not or_key:
                    raise Exception("OPENROUTER_API_KEY not found in environment or settings.yaml")
                url = "https://openrouter.ai/api/v1/chat/completions"
                headers = {"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"}
                model_name = LLM_MODEL if LLM_MODEL and "gemini" not in LLM_MODEL else "meta-llama/llama-3.1-8b-instruct:free"
                payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
                res = requests.post(url, headers=headers, json=payload, timeout=20)
                if res.status_code == 200:
                    return res.json()["choices"][0]["message"]["content"]
                else:
                    raise Exception(f"OpenRouter API Error {res.status_code}: {res.text}")

            elif LLM_PROVIDER == "ollama":
                url = f"{LLM_OLLAMA_HOST.rstrip('/')}/api/generate"
                model_name = LLM_MODEL if LLM_MODEL and "gemini" not in LLM_MODEL else "llama3.2"
                payload = {"model": model_name, "prompt": prompt, "stream": False}
                res = requests.post(url, json=payload, timeout=30)
                if res.status_code == 200:
                    return res.json().get("response", "")
                else:
                    raise Exception(f"Ollama API Error {res.status_code}: {res.text}")

            else:
                # Default: Gemini API
                if not client:
                    return None
                gen_config = types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                )
                res = client.models.generate_content(
                    model=LLM_MODEL,
                    contents=prompt,
                    config=gen_config
                )
                return res.text

        except Exception as e:
            err_msg = str(e)
            is_quota_limit = ("429" in err_msg or "ResourceExhausted" in err_msg or "quota" in err_msg.lower() or "rate" in err_msg.lower())
            
            if is_quota_limit:
                QUOTA_EXHAUSTED_FLAG[0] = True
                print(f"\n[LLM Quota Exceeded] Quota limit reached ({err_msg[:80]}...).", flush=True)
                print("[LLM Circuit Breaker] Instantly switching to fast local key skills & 30-word summary mode (0s delay for remaining jobs)...\n", flush=True)
                return None
            else:
                print(f"[LLM] Notice: Provider '{LLM_PROVIDER}' notice/error (attempt {attempt + 1}/{LLM_MAX_RETRIES}): {err_msg[:100]}...", flush=True)

    return None


def evaluate_jobs_batch(client: genai.Client, unparsed_jobs: list[dict], last_request_time: list[float]):
    """Evaluates a list of scraped jobs using LLM in batches with request delay pacing and exponential retries."""
    if not unparsed_jobs:
        return

    # 1. Local keyword pre-filtering to eliminate obvious non-matches without API calls
    for j in unparsed_jobs:
        desc_lower = j.get("desc", "").lower()
        title_lower = j.get("title", "").lower()
        for ex in EXCLUDE_LIST:
            if ex.lower() in desc_lower or ex.lower() in title_lower:
                j["eval_result"] = {"match": False, "verdict": f"Excluded due to keyword: {ex}", "short_description": ""}
                break

    pending_jobs = [j for j in unparsed_jobs if "eval_result" not in j]

    if not pending_jobs or QUOTA_EXHAUSTED_FLAG[0] or (LLM_PROVIDER == "gemini" and not client):
        for j in pending_jobs:
            j["eval_result"] = {
                "match": True,
                "verdict": "MATCH: YES (Local Fallback)",
                "short_description": extract_skills_summary(j.get("desc", ""), j.get("title", ""))
            }
        return

    # 2. Process pending jobs in chunks of LLM_BATCH_SIZE
    chunk_size = max(1, LLM_BATCH_SIZE)
    for i in range(0, len(pending_jobs), chunk_size):
        batch = pending_jobs[i:i + chunk_size]

        if QUOTA_EXHAUSTED_FLAG[0]:
            for j in batch:
                j["eval_result"] = {
                    "match": True,
                    "verdict": "MATCH: YES (Local Fallback)",
                    "short_description": extract_skills_summary(j.get("desc", ""), j.get("title", ""))
                }
            continue

        prompt_parts = [
            "You are an expert universal career agent. Read and analyze these job postings carefully.",
            "\nUser Criteria & Exclusions:",
            MY_PROFILE_CRITERIA,
            "\nJobs to Evaluate:"
        ]

        for idx, job in enumerate(batch, 1):
            prompt_parts.append(f"\n--- JOB {idx} ---")
            prompt_parts.append(f"Title: {job.get('title', 'N/A')}")
            prompt_parts.append(f"Company: {job.get('company', 'N/A')}")
            prompt_parts.append(f"Description: {job.get('desc', '')[:2000]}")

        prompt_parts.append("""
Instructions:
For EACH job listed above:
1. Determine if it matches the user criteria.
2. Extract key skills/tools required for the position.
3. Generate a 1-sentence summary of the core role responsibilities.

Respond in this EXACT format for each job:
=== EVALUATION FOR JOB 1 ===
MATCH: [YES / NO]
SCORE: [0-100]%
SUMMARY: Key Skills: [3-6 key skills/tools] — [1 concise sentence describing the role]
REASON: [1 sentence summarizing why it fits or fails]
""")

        prompt = "\n".join(prompt_parts)

        response_text = call_llm_api(prompt, client, last_request_time)

        if response_text:
            parse_batch_response(batch, response_text)
        else:
            # Instant fallback if quota reached or provider returned error
            for job in batch:
                job["eval_result"] = {
                    "match": True,
                    "verdict": "MATCH: YES (Local Fallback)",
                    "short_description": extract_skills_summary(job.get("desc", ""), job.get("title", ""))
                }


def evaluate_job(client: genai.Client, job_title: str, company: str, description: str) -> dict:
    """Single job evaluation wrapper for backward compatibility."""
    single_job = [{"title": job_title, "company": company, "desc": description}]
    evaluate_jobs_batch(client, single_job, [0.0])
    return single_job[0].get("eval_result", {
        "match": True,
        "verdict": "MATCH: YES (Fallback)",
        "short_description": extract_skills_summary(description, job_title)
    })


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

    if not smtp_server or not smtp_username or not smtp_password or not email_to:
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
    if LLM_PROVIDER == "gemini":
        if gemini_api_key:
            client = genai.Client(api_key=gemini_api_key)
        else:
            print("[!] GEMINI_API_KEY not set. Job evaluation running in local fallback mode.", flush=True)
    elif LLM_PROVIDER == "groq":
        print("[*] LLM Provider set to Groq (Free tier: 14,400 requests/day, 30 RPM).", flush=True)
    elif LLM_PROVIDER == "openrouter":
        print("[*] LLM Provider set to OpenRouter.", flush=True)
    elif LLM_PROVIDER == "ollama":
        print(f"[*] LLM Provider set to local Ollama ({LLM_OLLAMA_HOST}).", flush=True)

    HISTORY_CFG = APP_SETTINGS.get("history", {})
    IGNORE_DAYS = int(os.getenv("IGNORE_DAYS", HISTORY_CFG.get("ignore_days", 7)))

    matched_jobs = []
    seen_urls, seen_map = load_seen_jobs(ignore_days=IGNORE_DAYS)
    print(f"[*] Loaded {len(seen_urls)} active job URLs seen within the last {IGNORE_DAYS} days.", flush=True)
    item_id = 1
    last_request_time = [0.0]

    job_entries = parse_job_entries(JOB_CONFIG)
    print(f"[*] Configured {len(job_entries)} position terms across locations: {LOCATIONS_LIST}", flush=True)

    for entry in job_entries:
        search_term = entry.get("title", "")
        if not search_term:
            continue

        target_locs = resolve_target_locations(entry, LOCATIONS_LIST)
        if not target_locs:
            print(f"[*] Skipping '{search_term}' (no active target location match among {LOCATIONS_LIST}).", flush=True)
            continue

        for loc in target_locs:
            print(f"[*] Scanning {SCRAPE_SITES} for '{search_term}' in '{loc}' (target location matched: {loc}, wanted: {RESULTS_WANTED}, hours_old: {HOURS_OLD})...", flush=True)
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

            unprocessed_jobs = []
            for _, row in jobs.iterrows():
                job_url = str(row.get("job_url", "")).strip()
                if job_url and job_url in seen_urls:
                    continue

                # Mark URL seen within current run to avoid duplicate scans
                if job_url:
                    seen_urls.add(job_url)

                title = str(row.get("title", "Unknown Title"))
                company = str(row.get("company", "Unknown Company"))
                job_loc = str(row.get("location", loc))
                desc = str(row.get("description", ""))

                unprocessed_jobs.append({
                    "title": title,
                    "company": company,
                    "location": job_loc if job_loc and job_loc != "nan" else loc,
                    "country": loc,
                    "desc": desc,
                    "url": job_url
                })

            if unprocessed_jobs:
                evaluate_jobs_batch(client, unprocessed_jobs, last_request_time)
                for j in unprocessed_jobs:
                    eval_result = j.get("eval_result", {})
                    if eval_result.get("match"):
                        matched_jobs.append({
                            "id": item_id,
                            "title": j["title"],
                            "company": j["company"],
                            "location": j["location"],
                            "country": j["country"],
                            "short_description": eval_result.get("short_description", ""),
                            "url": j["url"],
                            "verdict": eval_result.get("verdict", "")
                        })
                        item_id += 1

    print(f"[+] Total new unique matched vacancies today: {len(matched_jobs)}", flush=True)

    # 1. Sort matched jobs by user priority:
    #    Priority 1: Purchasing/Buyer in Israel
    #    Priority 2: Purchasing/Buyer in Poland
    #    Priority 3: Purchasing/Buyer in Ukraine
    #    Priority 4: All other matched vacancies
    matched_jobs.sort(key=get_job_priority)

    # 2. Cap total report positions to top 30 based on priority ranking
    if len(matched_jobs) > 30:
        print(f"[*] Prioritizing and capping report to top 30 positions total (out of {len(matched_jobs)} total matches).", flush=True)
        matched_jobs = matched_jobs[:30]

    # 3. Re-index IDs sequentially for the final report (1..N)
    for idx, job in enumerate(matched_jobs, 1):
        job["id"] = idx

    # Mark ONLY the jobs being sent in today's report as seen in seen_jobs.json
    now_str = datetime.now(timezone.utc).isoformat()
    for job in matched_jobs:
        u = job.get("url")
        if u:
            seen_urls.add(u)
            seen_map[u] = now_str

    # Save updated seen_map history
    save_seen_jobs(seen_map)

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




