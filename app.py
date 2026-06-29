from flask import Flask, render_template, request, redirect
from pypdf import PdfReader
from docx import Document
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
import os
import re
import sqlite3
import urllib.parse
import requests
from datetime import date

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE = "job_alerts.db"


def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_title TEXT NOT NULL,
            location TEXT NOT NULL,
            work_type TEXT,
            keywords TEXT,
            email TEXT NOT NULL,
            linkedin_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            job_id TEXT NOT NULL,
            job_title TEXT,
            employer_name TEXT,
            job_apply_link TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(alert_id, job_id)
        )
    """)

    conn.commit()
    conn.close()


init_db()

def years_of_experience():
    start_year = 2018
    start_month = 1

    today = date.today()
    years = today.year - start_year

    if today.month < start_month:
        years -= 1

    return years

def read_pdf(path):
    text = ""
    try:
        reader = PdfReader(path)
        for page in reader.pages:
            text += (page.extract_text() or "") + " "
    except Exception as e:
        print("PDF error:", e)
    return text.lower()


def read_docx(path):
    try:
        doc = Document(path)
        return " ".join(p.text for p in doc.paragraphs).lower()
    except Exception as e:
        print("DOCX error:", e)
        return ""


def read_file(path):
    if path.endswith(".pdf"):
        return read_pdf(path)
    elif path.endswith(".docx"):
        return read_docx(path)
    return ""

def extract_email(text):
    match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    return match.group(0) if match else "Not found"


def extract_phone(text):
    match = re.search(
        r'(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}',
        text
    )
    return match.group(0) if match else "Not found"


def extract_candidate_name(filename):
    name = os.path.splitext(filename)[0]
    name = name.replace("_", " ").replace("-", " ")
    return name.title()


def generate_summary(candidate_score):
    score = candidate_score["score"]
    required = candidate_score["required_matches"]
    preferred = candidate_score["preferred_matches"]
    missing = candidate_score["missing_required"]

    if score >= 80:
        fit = "This candidate appears to be a strong match based on the required and preferred criteria."
    elif score >= 55:
        fit = "This candidate appears to be a potential match, but may require additional screening."
    else:
        fit = "This candidate appears to be a lower match based on the current criteria."

    strengths = []

    if required:
        strengths.append("Matches key required qualifications")
    if preferred:
        strengths.append("Also matches preferred qualifications")
    if score >= 80:
        strengths.append("Strong overall alignment with the role")

    risks = []

    if missing:
        risks.append("Missing some required qualifications")
    if score < 55:
        risks.append("Low overall match score")
    if not required:
        risks.append("No required matches identified")

    return {
        "summary": fit,
        "strengths": strengths,
        "risks": risks
    }

def clean_terms(text):
    terms = text.lower().replace(",", "\n").split("\n")
    cleaned = []

    for term in terms:
        term = term.strip()

        if len(term) >= 3 and term not in cleaned:
            cleaned.append(term)

    return cleaned


def score_candidate(resume_text, required_text, preferred_text):
    required_terms = clean_terms(required_text)
    preferred_terms = clean_terms(preferred_text)

    required_matches = []
    preferred_matches = []
    missing_required = []

    for term in required_terms:
        if term in resume_text:
            required_matches.append(term)
        else:
            missing_required.append(term)

    for term in preferred_terms:
        if term in resume_text:
            preferred_matches.append(term)

    required_score = 0
    preferred_score = 0

    if required_terms:
        required_score = (len(required_matches) / len(required_terms)) * 70

    if preferred_terms:
        preferred_score = (len(preferred_matches) / len(preferred_terms)) * 30

    total_score = round(required_score + preferred_score)

    if total_score >= 80:
        recommendation = "Strong Match"
    elif total_score >= 55:
        recommendation = "Potential Match"
    else:
        recommendation = "Lower Match"

    return {
        "score": total_score,
        "recommendation": recommendation,
        "required_matches": required_matches,
        "preferred_matches": preferred_matches,
        "missing_required": missing_required
    }

@app.route("/")
def home():
    return render_template(
        "home.html",
        years_experience=years_of_experience()
    )


@app.route("/candidate-matcher", methods=["GET", "POST"])
def index():
    results = []

    if request.method == "POST":
        required_text = request.form.get("required_skills", "").strip()
        preferred_text = request.form.get("preferred_skills", "").strip()

        jd_file = request.files.get("job_description")
        resume_files = request.files.getlist("resumes")

        jd_text = ""

        if jd_file and jd_file.filename != "":
            jd_path = os.path.join(app.config["UPLOAD_FOLDER"], jd_file.filename)
            jd_file.save(jd_path)
            jd_text = read_file(jd_path)

            if os.path.exists(jd_path):
                os.remove(jd_path)

        combined_required = (required_text + "\n" + jd_text).strip()
        combined_preferred = preferred_text.strip()

        if combined_required == "" and combined_preferred == "":
            return render_template(
                "index.html",
                error="Please provide search criteria or upload a job description.",
                results=[]
            )

        saved_resume_paths = []

        for resume in resume_files:
            if resume.filename == "":
                continue

            path = os.path.join(app.config["UPLOAD_FOLDER"], resume.filename)
            resume.save(path)
            saved_resume_paths.append(path)

        for path in saved_resume_paths:
            resume_text = read_file(path)

            candidate_score = score_candidate(
                resume_text,
                combined_required,
                combined_preferred
            )

            candidate_summary = generate_summary(candidate_score)

            results.append({
                "name": extract_candidate_name(os.path.basename(path)),
                "file_name": os.path.basename(path),
                "email": extract_email(resume_text),
                "phone": extract_phone(resume_text),
                "score": candidate_score["score"],
                "recommendation": candidate_score["recommendation"],
                "required_matches": candidate_score["required_matches"][:10],
                "preferred_matches": candidate_score["preferred_matches"][:10],
                "missing_required": candidate_score["missing_required"][:10],
                "summary": candidate_summary["summary"],
                "strengths": candidate_summary["strengths"],
                "risks": candidate_summary["risks"]
            })

            if os.path.exists(path):
                os.remove(path)

        results.sort(
            key=lambda candidate: candidate["score"],
            reverse=True
        )

    return render_template("index.html", results=results)


@app.route("/resume-writer")
def resume_writer():
    return render_template(
        "resume_writer.html",
        years_experience=years_of_experience()
    )


@app.route("/candidate-sourcing")
def candidate_sourcing():
    return render_template("candidate_sourcing.html")


@app.route("/upwork")
def upwork():
    return redirect("https://www.upwork.com/freelancers/~0187a46036bf325d2a?mp_source=share")

def search_jobs(job_title, location, keywords=""):
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    rapidapi_host = os.environ.get("RAPIDAPI_HOST", "jsearch.p.rapidapi.com")

    query = f"{job_title} {keywords} in {location}".strip()

    url = f"https://{rapidapi_host}/search-v2"

    headers = {
        "x-rapidapi-key": rapidapi_key,
        "x-rapidapi-host": rapidapi_host
    }

    params = {
    "query": query,
    "page": "1",
    "num_pages": "1",
    "country": "us",
    "date_posted": "all"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        print("STATUS:", response.status_code)
       

        response.raise_for_status()

        data = response.json()

        return data.get("data", {}).get("jobs", [])

    except Exception as e:
        print("JSearch error:", e)
        return []

def get_saved_alerts():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM job_alerts")
    alerts = cursor.fetchall()

    conn.close()
    return alerts


def is_new_job(alert_id, job_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM seen_jobs WHERE alert_id = ? AND job_id = ?",
        (alert_id, job_id)
    )

    exists = cursor.fetchone()
    conn.close()

    return exists is None


def save_seen_job(alert_id, job):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO seen_jobs
        (alert_id, job_id, job_title, employer_name, job_apply_link)
        VALUES (?, ?, ?, ?, ?)
    """, (
        alert_id,
        job.get("job_id"),
        job.get("job_title"),
        job.get("employer_name"),
        job.get("job_apply_link")
    ))

    conn.commit()
    conn.close()

def send_job_alert_email(to_email, job):
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("FROM_EMAIL")

    if not api_key or not from_email:
        print("Missing SendGrid environment variables")
        return False

    subject = f"New Job Alert: {job.get('job_title')}"

    company = job.get("employer_name", "Unknown Company")
    title = job.get("job_title", "Unknown Role")
    link = job.get("job_apply_link", "#")

    html_content = f"""
    <h2>New Job Match Found</h2>
    <p><strong>Role:</strong> {title}</p>
    <p><strong>Company:</strong> {company}</p>
    <p>
        <a href="{link}">
            View Job / Apply Now
        </a>
    </p>
    <p>Apply quickly — early applicants often get more visibility.</p>
    """

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print("EMAIL STATUS:", response.status_code)
        return True

    except Exception as e:
        print("Email error:", e)
        return False

@app.route("/job-alerts", methods=["GET", "POST"])
def job_alerts():
    if request.method == "POST":
        job_title = request.form.get("job_title", "").strip()
        location = request.form.get("location", "").strip()
        work_type = request.form.get("work_type", "").strip()
        keywords = request.form.get("keywords", "").strip()
        email = request.form.get("email", "").strip()

        search_terms = job_title

        if keywords:
            search_terms += " " + keywords

        encoded_keywords = urllib.parse.quote(search_terms)
        encoded_location = urllib.parse.quote(location)

        linkedin_url = (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={encoded_keywords}&location={encoded_location}"
        )

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO job_alerts
            (job_title, location, work_type, keywords, email, linkedin_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (job_title, location, work_type, keywords, email, linkedin_url))

        conn.commit()
        conn.close()

        return render_template(
            "job_alerts.html",
            success=True,
            linkedin_url=linkedin_url
        )

    return render_template("job_alerts.html")

@app.route("/test-job-search")
def test_job_search():
    jobs = search_jobs("Account Executive", "Remote", "SaaS")

    return {
        "count": len(jobs),
        "sample": jobs[:3]
    }

@app.route("/test-check-alerts")
def test_check_alerts():
    alerts = get_saved_alerts()
    new_jobs_found = []

    for alert in alerts:
        jobs = search_jobs(
            alert["job_title"],
            alert["location"],
            alert["keywords"] or ""
        )

        for job in jobs:
            job_id = job.get("job_id")

            if not job_id:
                continue

            if is_new_job(alert["id"], job_id):
                save_seen_job(alert["id"], job)

                send_job_alert_email(alert["email"], job)

                new_jobs_found.append({
                    "alert_email": alert["email"],
                    "job_title": job.get("job_title"),
                    "company": job.get("employer_name"),
                    "apply_link": job.get("job_apply_link")
                })

    return {
        "alerts_checked": len(alerts),
        "new_jobs_found": new_jobs_found
    }

def check_alerts_and_send_emails():
    alerts = get_saved_alerts()
    print(f"Checking {len(alerts)} job alerts...")

    for alert in alerts:
        jobs = search_jobs(
            alert["job_title"],
            alert["location"],
            alert["keywords"] or ""
        )

        for job in jobs:
            job_id = job.get("job_id")

            if not job_id:
                continue

            if is_new_job(alert["id"], job_id):
                save_seen_job(alert["id"], job)
                send_job_alert_email(alert["email"], job)

scheduler = BackgroundScheduler()
scheduler.add_job(
    func=check_alerts_and_send_emails,
    trigger="interval",
    hours=1
)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)