import re
import time
from google import genai
from google.genai import types

def extract_skills_summary(description: str, job_title: str, keywords_list: list, industries_list: list, max_words: int = 30) -> str:
    """Extracts matched keywords, target industries, and the first 30 words of job description as a clean summary."""
    if not description or len(description.strip()) == 0:
        return f"Position: {job_title}"
    
    words = description.split()
    first_n_words = " ".join(words[:max_words])
    if len(words) > max_words:
        first_n_words += "..."

    found_skills = []
    seen_lower = set()

    for kw in (keywords_list or []) + (industries_list or []):
        k_lower = kw.lower()
        if k_lower not in seen_lower and re.search(r'\b' + re.escape(kw) + r'\b', description + " " + job_title, re.IGNORECASE):
            found_skills.append(kw)
            seen_lower.add(k_lower)

    if found_skills:
        skills_str = ", ".join(found_skills[:6])
        return f"<strong>Key Skills/Industry: {skills_str}</strong> — {first_n_words}"
    else:
        return f"<strong>Overview:</strong> {first_n_words}"


def calculate_rule_score(job: dict, job_config: dict) -> float:
    """
    Computes numerical rule score (0-100+) based on:
    - Target Industry (+40 pts)
    - Role Match (+30 pts)
    - Location/Country (Israel: +30 pts, Poland: +20 pts, Ukraine: +10 pts)
    """
    title = str(job.get("title", "")).lower()
    country = str(job.get("country", "")).lower()
    desc = str(job.get("desc", "")).lower()

    industries = job_config.get("industries", []) or []
    keywords = job_config.get("keywords", []) or []

    score = 0.0

    # 1. Target Industry Boost (+40 pts)
    for ind in industries:
        if ind.lower() in title or ind.lower() in desc:
            score += 40.0
            break

    # 2. Buyer/Procurement Role Boost (+30 pts)
    buyer_keywords = ["buyer", "purchasing", "procurement", "sourcing", "קניין", "רכש", "zakupów", "kupiec", "закупівель", "постачання"]
    if any(kw in title for kw in buyer_keywords):
        score += 30.0

    # 3. Country Boost (Israel: +30, Poland: +20, Ukraine: +10)
    if "israel" in country:
        score += 30.0
    elif "poland" in country:
        score += 20.0
    elif "ukraine" in country:
        score += 10.0
    else:
        score += 5.0

    # 4. Keyword Match Boost (+3 pts per matching keyword up to 15 pts)
    kw_matches = 0
    for kw in keywords:
        if kw.lower() in desc:
            kw_matches += 1
            if kw_matches >= 5:
                break
    score += (kw_matches * 3.0)

    return score


def parse_batch_response(batch: list[dict], response_text: str, keywords_list: list, industries_list: list):
    """Parses LLM batch evaluation response and assigns eval_result to each job dict."""
    blocks = re.split(r'===\s*EVALUATION FOR JOB\s*\d+\s*===', response_text)
    eval_blocks = [b.strip() for b in blocks if b.strip()]

    for idx, job in enumerate(batch):
        block_text = eval_blocks[idx] if idx < len(eval_blocks) else ""
        if block_text:
            is_match = "MATCH: YES" in block_text
            summary = ""
            ai_score = 70.0 if is_match else 0.0

            for line in block_text.splitlines():
                if line.startswith("SCORE:"):
                    score_str = re.sub(r'[^\d]', '', line)
                    if score_str.isdigit():
                        ai_score = float(score_str)
                elif line.startswith("SUMMARY:"):
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
                summary = extract_skills_summary(job.get("desc", ""), job.get("title", ""), keywords_list, industries_list)

            job["eval_result"] = {
                "match": is_match,
                "verdict": block_text,
                "short_description": summary,
                "ai_score": ai_score
            }
        else:
            job["eval_result"] = {
                "match": True,
                "verdict": "MATCH: YES (Local Fallback)",
                "short_description": extract_skills_summary(job.get("desc", ""), job.get("title", ""), keywords_list, industries_list),
                "ai_score": 50.0
            }


def score_jobs(client: genai.Client, jobs_list: list[dict], job_config: dict, app_settings: dict, last_request_time: list[float], quota_exhausted_flag: list[bool]) -> list[dict]:
    """
    Scores all candidate jobs using rule-based scoring and AI batch evaluation.
    Attaches final composite score and sorts jobs descending by score.
    """
    if not jobs_list:
        return []

    keywords_list = job_config.get("keywords", []) or []
    industries_list = job_config.get("industries", []) or []
    llm_cfg = app_settings.get("llm", {})

    provider = str(llm_cfg.get("provider", "gemini")).lower()
    llm_model = llm_cfg.get("model", "gemini-1.5-flash")
    llm_rpm = int(llm_cfg.get("requests_per_minute", 15))
    batch_size = int(llm_cfg.get("batch_size", 3))
    max_retries = int(llm_cfg.get("max_retries", 3))
    retry_delay = float(llm_cfg.get("retry_delay", 12))

    # Step 1: Calculate rule-based score for all jobs
    for job in jobs_list:
        job["rule_score"] = calculate_rule_score(job, job_config)

    # Step 2: AI evaluation for pending jobs (if LLM is enabled and quota not exhausted)
    pending_jobs = [j for j in jobs_list if "eval_result" not in j]

    if not pending_jobs or quota_exhausted_flag[0] or (provider == "gemini" and not client) or not llm_cfg.get("enabled", True):
        for j in pending_jobs:
            j["eval_result"] = {
                "match": True,
                "verdict": "MATCH: YES (Local Fallback)",
                "short_description": extract_skills_summary(j.get("desc", ""), j.get("title", ""), keywords_list, industries_list),
                "ai_score": 50.0
            }
    else:
        chunk_size = max(1, batch_size)
        for i in range(0, len(pending_jobs), chunk_size):
            batch = pending_jobs[i:i + chunk_size]

            if quota_exhausted_flag[0]:
                for j in batch:
                    j["eval_result"] = {
                        "match": True,
                        "verdict": "MATCH: YES (Local Fallback)",
                        "short_description": extract_skills_summary(j.get("desc", ""), j.get("title", ""), keywords_list, industries_list),
                        "ai_score": 50.0
                    }
                continue

            # Build multi-job prompt
            keywords_formatted = "\n".join([f"- {k}" for k in keywords_list])
            industries_formatted = "\n".join([f"- {ind}" for ind in industries_list])
            exclude_formatted = "\n".join([f"- Exclude: {e}" for e in job_config.get("exclude", []) or []])

            prompt_parts = [
                "You are an expert universal career agent. Read and analyze these job postings carefully.",
                "\nUser Criteria:",
                keywords_formatted,
                "\nPreferred Target Industries:",
                industries_formatted,
                "\nExclusion Rules:",
                exclude_formatted,
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

            # Rate limit delay pacing
            min_interval = 60.0 / max(1, llm_rpm)
            elapsed = time.time() - last_request_time[0]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

            response_text = None
            for attempt in range(max_retries + 1):
                try:
                    last_request_time[0] = time.time()
                    if provider == "gemini" and client:
                        gen_config = types.GenerateContentConfig(
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                        )
                        res = client.models.generate_content(
                            model=llm_model,
                            contents=prompt,
                            config=gen_config
                        )
                        response_text = res.text
                        break
                    else:
                        break
                except Exception as e:
                    err_msg = str(e)
                    is_quota = ("429" in err_msg or "ResourceExhausted" in err_msg or "quota" in err_msg.lower() or "rate" in err_msg.lower())
                    if is_quota:
                        quota_exhausted_flag[0] = True
                        print(f"\n[LLM Quota Exceeded] Quota limit reached ({err_msg[:80]}...).", flush=True)
                        print("[LLM Circuit Breaker] Instantly switching to fast local key skills & 30-word summary mode (0s delay)...\n", flush=True)
                        break
                    else:
                        print(f"[LLM Notice] Error (attempt {attempt+1}/{max_retries}): {err_msg[:80]}...", flush=True)

            if response_text:
                parse_batch_response(batch, response_text, keywords_list, industries_list)
            else:
                for job in batch:
                    job["eval_result"] = {
                        "match": True,
                        "verdict": "MATCH: YES (Local Fallback)",
                        "short_description": extract_skills_summary(job.get("desc", ""), job.get("title", ""), keywords_list, industries_list),
                        "ai_score": 50.0
                    }

    # Step 3: Compute final composite score and sort descending
    scored_jobs = []
    for job in jobs_list:
        eval_res = job.get("eval_result", {})
        if eval_res.get("match", True):
            ai_score = eval_res.get("ai_score", 50.0)
            rule_score = job.get("rule_score", 0.0)
            job["score"] = rule_score + (ai_score * 0.5)
            scored_jobs.append(job)

    # Sort descending by composite score
    scored_jobs.sort(key=lambda j: j.get("score", 0.0), reverse=True)
    return scored_jobs
