"""
AI Job Agent - single-file minimal implementation.

Capabilities:
- Scrape job listings (Indeed/Naukri-ish public pages) for today's postings in India for a role.
- Call local LLM adapter to optimize resume/cover.
- Attempt automated apply (Selenium) for simple forms, or queue for manual approval.
- Track applications in local SQLite DB.
- Check Gmail via IMAP and update status when company replies.
- Exposes a tiny Flask dashboard and endpoints to trigger runs.

Customize:
- LOCAL_LLM_CALL: adapt to your local model API / function.
- apply_to_job(): add site-specific selectors for structured apply flows.
- Use APPLY_MODE=manual if you want to review before clicking apply.

IMPORTANT: Respect job board TOS and captcha systems. Use only on accounts you control.
"""

import os
import sqlite3
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template_string
from dotenv import load_dotenv
from datetime import datetime, timedelta
from imapclient import IMAPClient
import email
from email.header import decode_header
from apscheduler.schedulers.background import BackgroundScheduler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "ai_jobs.db")
GMAIL_IMAP = os.getenv("GMAIL_IMAP", "imap.gmail.com")
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
RESUME_PATH = os.getenv("RESUME_PATH", "./resume/my_resume.pdf")
JOB_LOCATION = os.getenv("JOB_LOCATION", "India")
JOB_ROLE = os.getenv("JOB_ROLE", "software engineer")
SELENIUM_DRIVER_PATH = os.getenv("SELENIUM_DRIVER_PATH", "")  # if empty, require chromedriver in PATH
APPLY_MODE = os.getenv("APPLY_MODE", "manual")  # "auto" or "manual"
LOCAL_LLM_ENDPOINT = os.getenv("LOCAL_LLM_ENDPOINT", "")  # optional

# ---------- DB helpers ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS jobs (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 job_id TEXT UNIQUE,
                 title TEXT,
                 company TEXT,
                 location TEXT,
                 url TEXT,
                 posted_date TEXT,
                 status TEXT,
                 applied_at TEXT,
                 notes TEXT
                 )""")
    c.execute("""CREATE TABLE IF NOT EXISTS applications (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 job_id TEXT,
                 cover TEXT,
                 resume_path TEXT,
                 attempt_time TEXT,
                 result TEXT
                 )""")
    conn.commit()
    conn.close()

def add_or_update_job(job):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO jobs (job_id,title,company,location,url,posted_date,status,notes)
            VALUES (?,?,?,?,?,?,?,?)
            """, (job['job_id'], job['title'], job['company'], job['location'], job['url'], job['posted_date'], job.get('status','new'), job.get('notes','')))
        conn.commit()
    except sqlite3.IntegrityError:
        # already exists; update
        c.execute("""UPDATE jobs SET title=?, company=?, location=?, url=?, posted_date=? WHERE job_id=?""",
                  (job['title'], job['company'], job['location'], job['url'], job['posted_date'], job['job_id']))
        conn.commit()
    conn.close()

def set_job_status(job_id, status, notes=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE jobs SET status=?, notes=? WHERE job_id=?", (status, notes, job_id))
    conn.commit()
    conn.close()

def record_application(job_id, cover_text, resume_path, result):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO applications (job_id,cover,resume_path,attempt_time,result) VALUES (?,?,?,?,?)",
              (job_id, cover_text, resume_path, datetime.utcnow().isoformat(), result))
    conn.commit()
    conn.close()

# ---------- Local LLM adapter ----------
def local_llm_generate(prompt: str) -> str:
    """
    Hook to call your local model.
    By default: calls LOCAL_LLM_ENDPOINT if provided (expects JSON {"prompt":...} -> {"text":...})
    Otherwise returns a simple stub.
    Replace this function to call your local model (e.g., local Flask model endpoint, or python function).
    """
    if LOCAL_LLM_ENDPOINT:
        try:
            r = requests.post(LOCAL_LLM_ENDPOINT, json={"prompt": prompt, "max_tokens": 400}, timeout=30)
            r.raise_for_status()
            j = r.json()
            return j.get("text") or j.get("generated") or str(j)
        except Exception as e:
            print("LLM endpoint error:", e)
            return f"[LLM failure] Generated fallback for prompt: {prompt[:200]}"
    # fallback stub
    return f"Optimized cover for prompt (stub): {prompt[:300]}"

# ---------- Scraper (basic) ----------
def search_indeed(role=JOB_ROLE, location=JOB_LOCATION):
    """
    Minimal Indeed-like scraping for demonstration:
    - Builds a search URL and parses job cards.
    - Filters for 'today' in posted date.
    NOTE: selectors may break; update per site. This is a best-effort example.
    """
    results = []
    q = role.replace(" ", "+")
    loc = location.replace(" ", "+")
    url = f"https://www.indeed.com/jobs?q={q}&l={loc}"
    print("Searching:", url)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        print("Search failed", resp.status_code)
        return results
    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.jobsearch-SerpJobCard, div.slider_container, a.tapItem")
    for c in cards:
        # flexible extraction
        title_tag = c.select_one("h2.title, h2.jobTitle")
        title = title_tag.get_text(strip=True) if title_tag else (c.select_one("a.jobtitle").get_text(strip=True) if c.select_one("a.jobtitle") else None)
        company_tag = c.select_one("span.company, span.companyName")
        company = company_tag.get_text(strip=True) if company_tag else "Unknown"
        loc_tag = c.select_one("div.recJobLoc, div.companyLocation, span.location")
        loc_text = loc_tag.get_text(strip=True) if loc_tag else location
        link_tag = c.select_one("a")
        job_url = "https://www.indeed.com" + link_tag.get("href") if link_tag and link_tag.get("href", "").startswith("/") else (link_tag.get("href") if link_tag else None)
        posted = ""
        posted_tag = c.select_one("span.date, span.postedDate")
        if posted_tag:
            posted = posted_tag.get_text(strip=True)
        # filter "today" or "just posted"
        if posted and ("today" in posted.lower() or "just posted" in posted.lower() or "1 day" in posted.lower()):
            job_id = (company + "|" + (title or "") + "|" + (job_url or "") )[:200]
            results.append({
                "job_id": job_id,
                "title": title or "Unknown",
                "company": company,
                "location": loc_text,
                "url": job_url,
                "posted_date": datetime.utcnow().date().isoformat(),
                "status": "new",
                "notes": ""
            })
    return results

def search_naukri(role=JOB_ROLE, location=JOB_LOCATION):
    """
    Minimal Naukri-like scraping example (India). Update as needed.
    """
    results = []
    q = role.replace(" ", "-")
    loc = location.replace(" ", "-")
    url = f"https://www.naukri.com/{q}-jobs-in-{loc}"
    print("Searching:", url)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("article.jobTuple, .jobTuple")
        for c in cards:
            title_tag = c.select_one("a.title")
            title = title_tag.get_text(strip=True) if title_tag else None
            company_tag = c.select_one("a.subTitle")
            company = company_tag.get_text(strip=True) if company_tag else "Unknown"
            loc_tag = c.select_one(".job-search-location span")
            loc_text = loc_tag.get_text(strip=True) if loc_tag else location
            link_tag = c.select_one("a.title")
            job_url = link_tag.get("href") if link_tag else None
            posted_tag = c.select_one(".jobTuple .type")
            posted = posted_tag.get_text(strip=True) if posted_tag else ""
            if posted and ("today" in posted.lower() or "just posted" in posted.lower() or "1 day" in posted.lower()):
                job_id = (company + "|" + (title or "") + "|" + (job_url or ""))[:200]
                results.append({
                    "job_id": job_id,
                    "title": title or "Unknown",
                    "company": company,
                    "location": loc_text,
                    "url": job_url,
                    "posted_date": datetime.utcnow().date().isoformat(),
                    "status": "new",
                    "notes": ""
                })
    except Exception as e:
        print("Naukri search error:", e)
    return results

# ---------- Apply logic (selenium) ----------
def apply_to_job(job, resume_path=RESUME_PATH):
    """
    Try to perform a simple apply:
    - If the job posting has a simple form with <input type='file'>, upload resume and submit.
    - Otherwise, return 'queued' or 'manual required'.
    This is intentionally conservative.
    """
    url = job.get("url")
    if not url:
        return {"result": "no_url"}
    # Setup selenium
    opts = Options()
    if APPLY_MODE == "auto":
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    driver = None
    try:
        if SELENIUM_DRIVER_PATH:
            driver = webdriver.Chrome(executable_path=SELENIUM_DRIVER_PATH, options=opts)  # selenium 4 may warn
        else:
            driver = webdriver.Chrome(options=opts)
        driver.get(url)
        time.sleep(2)
        # Try to find file input
        try:
            file_input = driver.find_element(By.XPATH, "//input[@type='file']")
            # upload
            file_input.send_keys(os.path.abspath(resume_path))
            time.sleep(1)
            # Attempt to find submit/apply button
            btn = None
            for xpath in ["//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'apply')]", "//input[@type='submit']"]:
                try:
                    btn = driver.find_element(By.XPATH, xpath)
                    if btn:
                        btn.click()
                        break
                except:
                    pass
            time.sleep(2)
            driver.quit()
            return {"result": "applied", "detail": "file-uploaded-and-clicked"}
        except Exception as e:
            driver.quit()
            return {"result": "manual_required", "detail": f"no file input or error: {e}"}
    except Exception as e:
        if driver:
            try:
                driver.quit()
            except:
                pass
        print("Selenium error:", e)
        return {"result": "selenium_error", "detail": str(e)}

# ---------- Gmail checker (IMAP) ----------
def check_gmail_and_update():
    """
    Connects to Gmail IMAP, checks inbox for emails received today,
    matches sender or subject to companies in our jobs table, updates status to 'replied' if matched.
    """
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        print("Gmail credentials not set; skipping email check.")
        return
    print("Checking Gmail for replies...")
    with IMAPClient(GMAIL_IMAP) as client:
        client.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        client.select_folder("INBOX")
        since = (datetime.utcnow() - timedelta(days=7)).date().isoformat()  # check recent week
        messages = client.search(['SINCE', since])
        # fetch headers
        if not messages:
            print("No messages found.")
            return
        resp = client.fetch(messages, ['ENVELOPE', 'BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]'])
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for msgid, data in resp.items():
            envelope = data.get(b'ENVELOPE')
            subject_raw = b""
            frm = ""
            try:
                hdr = data.get(b'BODY[HEADER.FIELDS (FROM SUBJECT)]').decode('utf-8', errors='ignore')
                # parse simple
                for line in hdr.splitlines():
                    if line.lower().startswith("subject:"):
                        subject_raw = line[len("subject:"):].strip()
                    if line.lower().startswith("from:"):
                        frm = line[len("from:"):].strip()
                subj = subject_raw
            except Exception:
                subj = ""
            # match against companies in jobs table
            c2 = conn.cursor()
            c2.execute("SELECT job_id, company, status FROM jobs WHERE status NOT IN ('replied','hired','closed')")
            rows = c2.fetchall()
            for job_id, company, status in rows:
                if company and (company.lower() in (frm.lower() + " " + subj.lower())):
                    print("Matched email -> updating job:", job_id, company)
                    set_job_status(job_id, "replied", notes=f"Email matched: {subj} from {frm}")
        conn.close()
        client.logout()

# ---------- Orchestration ----------
def run_full_cycle():
    print("Starting run:", datetime.utcnow().isoformat())
    # 1. search sites
    jobs_found = []
    try:
        jobs_found += search_indeed(JOB_ROLE, JOB_LOCATION)
    except Exception as e:
        print("indeed error:", e)
    try:
        jobs_found += search_naukri(JOB_ROLE, JOB_LOCATION)
    except Exception as e:
        print("naukri error:", e)

    # 2. upsert jobs
    for j in jobs_found:
        add_or_update_job(j)

    # 3. For each new job, generate optimized cover/resume tweak and attempt apply (depending on APPLY_MODE)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT job_id, title, company, url, status FROM jobs WHERE status='new'")
    rows = c.fetchall()
    for job_id, title, company, url, status in rows:
        job = {"job_id": job_id, "title": title, "company": company, "url": url}
        prompt = f"Optimize applicant resume and generate a short cover letter (3 bullets + 3-line intro) for job: {title} at {company}. Base resume: (local file: {RESUME_PATH})"
        cover_text = local_llm_generate(prompt)
        # store cover + record attempt
        if APPLY_MODE == "auto":
            result = apply_to_job(job, resume_path=RESUME_PATH)
            record_application(job_id, cover_text, RESUME_PATH, str(result))
            if result.get("result") == "applied":
                set_job_status(job_id, "applied", notes=str(result))
            elif result.get("result") == "manual_required":
                set_job_status(job_id, "queued_manual", notes=result.get("detail",""))
            else:
                set_job_status(job_id, "error", notes=str(result))
        else:
            # queue for manual approval
            record_application(job_id, cover_text, RESUME_PATH, "queued_for_manual")
            set_job_status(job_id, "queued_manual", notes="Awaiting manual approval to apply")
    conn.close()

    # 4. Check Gmail for replies and update statuses
    try:
        check_gmail_and_update()
    except Exception as e:
        print("Email check error:", e)

    print("Run finished:", datetime.utcnow().isoformat())

# ---------- Flask Dashboard & Endpoints ----------
app = Flask(__name__)

@app.route("/")
def index():
    # simple HTML dashboard
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT job_id,title,company,location,posted_date,status,notes FROM jobs ORDER BY posted_date DESC LIMIT 200")
    jobs = c.fetchall()
    conn.close()
    html = """
    <html><head><title>AI Job Agent Dashboard</title></head><body>
    <h1>AI Job Agent Dashboard</h1>
    <form action="/run_now" method="post"><button type="submit">Run Now</button></form>
    <table border="1" cellpadding="6" cellspacing="0">
    <tr><th>Job ID</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Status</th><th>Notes</th></tr>
    {% for j in jobs %}
      <tr>
        <td>{{ j[0] }}</td>
        <td>{{ j[1] }}</td>
        <td>{{ j[2] }}</td>
        <td>{{ j[3] }}</td>
        <td>{{ j[4] }}</td>
        <td>{{ j[5] }}</td>
        <td>{{ j[6] }}</td>
      </tr>
    {% endfor %}
    </table>
    <p>Apply mode: <strong>{{ apply_mode }}</strong></p>
    <p>To run daily automatically, set up a cron job: <code>python3 app.py --run-once</code> or use the scheduler below.</p>
    </body></html>
    """
    return render_template_string(html, jobs=jobs, apply_mode=APPLY_MODE)

@app.route("/run_now", methods=["POST"])
def run_now():
    run_full_cycle()
    return jsonify({"ok": True, "message": "Run triggered"})

@app.route("/applications", methods=["GET"])
def list_applications():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT job_id,cover,resume_path,attempt_time,result FROM applications ORDER BY attempt_time DESC LIMIT 200")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"job_id":r[0],"cover":r[1],"resume":r[2],"time":r[3],"result":r[4]} for r in rows])

# ---------- Scheduler (optional background if you run the script persistently) ----------
scheduler = BackgroundScheduler()
scheduler.add_job(run_full_cycle, 'interval', hours=24, next_run_time=datetime.utcnow() + timedelta(seconds=5))
scheduler.start()

# ---------- CLI ----------
if __name__ == "__main__":
    init_db()
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true", help="Run scraping + apply once and exit")
    parser.add_argument("--no-server", action="store_true", help="Do not start the Flask server")
    args = parser.parse_args()
    if args.run_once:
        run_full_cycle()
        print("Completed run-once.")
        exit(0)
    if args.no_server:
        # run scheduler only
        print("Scheduler started (runs every 24h). Press Ctrl+C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Exiting.")
    else:
        init_db()
        # start Flask app (dashboard) and scheduler runs in background
        app.run(host="0.0.0.0", port=5000, debug=False)
