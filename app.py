from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, redirect, session
from pypdf import PdfReader
from docx import Document
from openai import OpenAI
from flask import make_response
from xhtml2pdf import pisa
from io import BytesIO
import os
import re
import sqlite3
import urllib.parse
import requests
import stripe
from datetime import date

app = Flask(__name__)
app.secret_key = "change-this-later"

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE = "job_alerts.db"

PLAN_LIMITS = {
    "free": {
        "resumes": 1,
        "job_alerts": 1,
        "consultation": "None"
    },
    "pro": {
        "resumes": 5,
        "job_alerts": 5,
        "consultation": "15-minute interview coaching consultation"
    },
    "accelerator": {
        "resumes": None,
        "job_alerts": None,
        "consultation": "30-minute interview coaching consultation"
    }
}

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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            stripe_customer_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            resumes_used INTEGER DEFAULT 0,
            job_alerts_used INTEGER DEFAULT 0,
            billing_month TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, billing_month)
        )
    """)

    try:
        cursor.execute(
            "ALTER TABLE job_alerts ADD COLUMN is_premium INTEGER DEFAULT 0"
        )
    except:
        pass

    try:
        cursor.execute(
            "ALTER TABLE job_alerts ADD COLUMN stripe_customer_id TEXT"
        )
    except:
        pass

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

def get_current_billing_month():
    today = date.today()
    return f"{today.year}-{today.month:02d}"

def get_usage(user_id):
    billing_month = get_current_billing_month()

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM usage_tracking
        WHERE user_id = ? AND billing_month = ?
    """, (user_id, billing_month))

    usage = cursor.fetchone()

    if not usage:
        cursor.execute("""
            INSERT INTO usage_tracking
            (user_id, resumes_used, job_alerts_used, billing_month)
            VALUES (?, 0, 0, ?)
        """, (user_id, billing_month))
        conn.commit()

        cursor.execute("""
            SELECT * FROM usage_tracking
            WHERE user_id = ? AND billing_month = ?
        """, (user_id, billing_month))

        usage = cursor.fetchone()

    conn.close()
    return usage

def can_use_resume(user_id, plan):
    if plan == "accelerator":
        return True

    usage = get_usage(user_id)
    limit = PLAN_LIMITS[plan]["resumes"]

    return usage["resumes_used"] < limit

def can_use_job_alert(user_id, plan):
    if plan == "accelerator":
        return True

    usage = get_usage(user_id)
    limit = PLAN_LIMITS[plan]["job_alerts"]

    return usage["job_alerts_used"] < limit

def increment_resume_usage(user_id):
    get_usage(user_id)   # ensure row exists
    billing_month = get_current_billing_month()

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE usage_tracking
        SET resumes_used = resumes_used + 1
        WHERE user_id = ? AND billing_month = ?
    """, (user_id, billing_month))

    conn.commit()
    conn.close()

def increment_job_alert_usage(user_id):
    get_usage(user_id)   # ensure row exists
    billing_month = get_current_billing_month()

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE usage_tracking
        SET job_alerts_used = job_alerts_used + 1
        WHERE user_id = ? AND billing_month = ?
    """, (user_id, billing_month))

    conn.commit()
    conn.close()

client = OpenAI()

def generate_ai_resume_content(candidate_info, job_description):
    prompt = f"""
You are an expert resume writer and recruiter.

Create ATS-optimized resume content using the candidate information and target job description.

Return ONLY plain text.

Each section MUST start on its own line.

SUMMARY:
[text]

SKILLS:
[text]

EXPERIENCE:
[Use ONLY <p>, <strong>, <ul>, and <li>. Do NOT include <html>, <body>, markdown, dashes, or bullet symbols.]

ACHIEVEMENTS:
[text]

LEADERSHIP:
[text]

Candidate Information:
{candidate_info}

Target Job Description:
{job_description}
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
        )
        return response.output_text

    except Exception as e:
        print("OpenAI error:", e)
        return ""

def parse_ai_resume_sections(ai_output):
    sections = {
        "summary": "",
        "skills": "",
        "experience": "",
        "achievements": "",
        "leadership": ""
    }

    current_section = None

    for raw_line in ai_output.splitlines():
        line = raw_line.strip()
        upper_line = line.upper()

        if upper_line.startswith("SUMMARY:"):
            current_section = "summary"
            line = line.split(":", 1)[1].strip()

        elif upper_line.startswith("SKILLS:"):
            current_section = "skills"
            line = line.split(":", 1)[1].strip()

        elif upper_line.startswith("EXPERIENCE:"):
            current_section = "experience"
            line = line.split(":", 1)[1].strip()

        elif upper_line.startswith("ACHIEVEMENTS:"):
            current_section = "achievements"
            line = line.split(":", 1)[1].strip()

        elif upper_line.startswith("LEADERSHIP:"):
            current_section = "leadership"
            line = line.split(":", 1)[1].strip()

        if current_section and line:
            sections[current_section] += line + "\n"

        sections["experience"] = (
            sections["experience"]
            .replace("<html>", "")
            .replace("</html>", "")
            .replace("<body>", "")
            .replace("</body>", "")
            .strip()
        )

    return sections

@app.route("/")
def home():
    return render_template(
        "home.html",
        years_experience=years_of_experience()
    )

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            return render_template("signup.html", error="Email and password are required.")

        password_hash = generate_password_hash(password)

        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO users (email, password_hash, plan)
                VALUES (?, ?, ?)
            """, (email, password_hash, "free"))

            conn.commit()
            conn.close()

            return redirect("/login")

        except sqlite3.IntegrityError:
            return render_template("signup.html", error="An account with this email already exists.")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_email"] = user["email"]
            session["plan"] = user["plan"]
            return redirect("/dashboard")

        return render_template("login.html", error="Invalid email or password.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/dashboard")
def dashboard():
    if not session.get("user_id"):
        return redirect("/login")

    return render_template(
        "dashboard.html",
        email=session.get("user_email"),
        plan=session.get("plan")
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

@app.route("/resume-builder", methods=["GET", "POST"])
def resume_builder():
    if not session.get("user_id"):
        return redirect("/login")

    user_id = session.get("user_id")
    plan = session.get("plan", "free")

    if request.method == "POST":
        if not can_use_resume(user_id, plan):
            return render_template(
                "resume_builder.html",
                limit_reached=True,
                plan=plan
            )

        template_choice = request.form.get("template_choice")
        resume_source = request.form.get("resume_source")

        existing_resume_text = ""
        manual_info = {}

        if resume_source == "upload":
            resume_file = request.files.get("resume_file")

            if resume_file and resume_file.filename != "":
                resume_path = os.path.join(app.config["UPLOAD_FOLDER"], resume_file.filename)
                resume_file.save(resume_path)
                existing_resume_text = read_file(resume_path)

                if os.path.exists(resume_path):
                    os.remove(resume_path)

        else:
            manual_info = {
                "full_name": request.form.get("full_name", "").strip(),
                "email": request.form.get("email", "").strip(),
                "phone": request.form.get("phone", "").strip(),
                "linkedin": request.form.get("linkedin", "").strip(),
                "location": request.form.get("location", "").strip(),
                "education": request.form.get("education", "").strip(),
                "work_history": request.form.get("work_history", "").strip(),
                "certifications": request.form.get("certifications", "").strip(),
                "tools": request.form.get("tools", "").strip(),
                "languages": request.form.get("languages", "").strip(),
            }

        job_description = request.form.get("job_description", "").strip()

        jd_file = request.files.get("job_description_file")
        if jd_file and jd_file.filename != "":
            jd_path = os.path.join(app.config["UPLOAD_FOLDER"], jd_file.filename)
            jd_file.save(jd_path)
            job_description += "\n" + read_file(jd_path)

            if os.path.exists(jd_path):
                os.remove(jd_path)

        candidate_info = (
            existing_resume_text
            if resume_source == "upload"
            else str(manual_info)
        )

        ai_output = generate_ai_resume_content(
            candidate_info,
            job_description
        )

        print("AI OUTPUT:")
        print(ai_output)
        print("----------------")

        ai_sections = parse_ai_resume_sections(ai_output)

        preview_data = {
            "template_choice": template_choice,
            "full_name": manual_info.get("full_name", ""),
            "email": manual_info.get("email", session.get("user_email")),
            "phone": manual_info.get("phone", ""),
            "linkedin": manual_info.get("linkedin", ""),
            "location": manual_info.get("location", ""),
            "education": manual_info.get("education", ""),
            "certifications": manual_info.get("certifications", ""),
            "tools": manual_info.get("tools", ""),
            "languages": manual_info.get("languages", ""),
            "summary": ai_sections["summary"],
            "skills": ai_sections["skills"],
            "experience": ai_sections["experience"],
            "achievements": ai_sections["achievements"],
            "leadership_summary": ai_sections["leadership"]
        }

        session["resume_preview_data"] = preview_data
        increment_resume_usage(user_id)
        return redirect("/resume-preview")  

    usage = get_usage(user_id)

    return render_template(
        "resume_builder.html",
        plan=plan,
        usage=usage
    )

@app.route("/resume-preview")
def resume_preview():
    if not session.get("resume_preview_data"):
        return redirect("/resume-builder")

    data = session["resume_preview_data"]

    template_choice = data.get("template_choice", "professional")

    template_map = {
        "professional": "resume_templates/professional.html",
        "modern": "resume_templates/modern.html",
        "executive": "resume_templates/executive.html"
    }

    template_file = template_map.get(
        template_choice,
        "resume_templates/professional.html"
    )

    return render_template(
        "resume_preview.html",
        selected_template=template_file,
        full_name=data.get("full_name", "John Doe"),
        email=data.get("email", ""),
        phone=data.get("phone", ""),
        linkedin=data.get("linkedin", ""),
        location=data.get("location", ""),
        summary=data.get("summary", "AI-generated executive summary will appear here."),
        leadership_summary=data.get("leadership_summary", "Leadership summary placeholder."),
        skills=data.get("skills", "Sales, SaaS, Leadership, Strategy"),
        experience=data.get("experience", "<p><strong>Sample Employer</strong> — Example Role</p><ul><li>Sample accomplishment</li></ul>"),
        achievements=data.get("achievements", "Revenue growth, pipeline expansion, strategic leadership"),
        education=data.get("education", ""),
        certifications=data.get("certifications", ""),
        tools=data.get("tools", ""),
        languages=data.get("languages", "")
    )

@app.route("/download-resume-pdf")
def download_resume_pdf():
    if not session.get("resume_preview_data"):
        return redirect("/resume-builder")

    data = session["resume_preview_data"]
    template_choice = data.get("template_choice", "professional")

    template_map = {
        "professional": "resume_templates/professional.html",
        "modern": "resume_templates/modern.html",
        "executive": "resume_templates/executive.html"
    }

    template_file = template_map.get(
        template_choice,
        "resume_templates/professional.html"
    )

    html = render_template(
        "resume_preview.html",
        selected_template=template_file,
        full_name=data.get("full_name", "Resume"),
        email=data.get("email", ""),
        phone=data.get("phone", ""),
        linkedin=data.get("linkedin", ""),
        location=data.get("location", ""),
        summary=data.get("summary", ""),
        leadership_summary=data.get("leadership_summary", ""),
        skills=data.get("skills", ""),
        experience=data.get("experience", ""),
        achievements=data.get("achievements", ""),
        education=data.get("education", ""),
        certifications=data.get("certifications", ""),
        tools=data.get("tools", ""),
        languages=data.get("languages", ""),
        pdf_mode=True
    )

    pdf = BytesIO()
    pisa_status = pisa.CreatePDF(html, dest=pdf)

    if pisa_status.err:
        return "PDF generation failed", 500

    pdf.seek(0)

    response = make_response(pdf.read())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "attachment; filename=resume.pdf"

    return response

@app.route("/reset-usage")
def reset_usage():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE usage_tracking
        SET resumes_used = 0
    """)

    conn.commit()
    conn.close()

    return "Usage reset"

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
    print("Job alert email sending is currently disabled.")
    return False

@app.route("/job-alerts", methods=["GET", "POST"])
def job_alerts():
    if not session.get("user_id"):
        return redirect("/login")

    user_id = session.get("user_id")
    email = session.get("user_email")
    plan = session.get("plan", "free")

    if request.method == "POST":
        if not can_use_job_alert(user_id, plan):
            return render_template(
                "job_alerts.html",
                limit_reached=True,
                plan=plan
            )

        job_title = request.form.get("job_title", "").strip()
        location = request.form.get("location", "").strip()
        work_type = request.form.get("work_type", "").strip()
        keywords = request.form.get("keywords", "").strip()

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
            (job_title, location, work_type, keywords, email, linkedin_url, is_premium)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            job_title,
            location,
            work_type,
            keywords,
            email,
            linkedin_url,
            1 if plan in ["pro", "accelerator"] else 0
        ))

        conn.commit()
        conn.close()

        increment_job_alert_usage(user_id)

        return render_template(
            "job_alerts.html",
            success=True,
            linkedin_url=linkedin_url,
            plan=plan
        )

    usage = get_usage(user_id)

    return render_template(
        "job_alerts.html",
        plan=plan,
        usage=usage
    )

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

@app.route("/create-checkout-session")
def create_checkout_session():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": os.environ.get("STRIPE_PRICE_ID"),
                    "quantity": 1,
                }
            ],
            success_url=os.environ.get("DOMAIN_URL", "http://127.0.0.1:5000") + "/premium-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=os.environ.get("DOMAIN_URL", "http://127.0.0.1:5000") + "/job-alerts",
        )

        return redirect(checkout_session.url, code=303)

    except Exception as e:
        print("Stripe error:", e)
        return "Stripe checkout error", 500

@app.route("/premium-success")
def premium_success():
    session_id = request.args.get("session_id")

    if not session_id:
        return "Missing Stripe session ID", 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        customer_email = None
        stripe_customer_id = None

        if session.customer_details:
            customer_email = session.customer_details.email

        stripe_customer_id = session.customer

        if not customer_email:
            return "Could not find customer email", 400

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE job_alerts
            SET is_premium = 1,
                stripe_customer_id = ?
            WHERE email = ?
        """, (stripe_customer_id, customer_email))

        conn.commit()
        conn.close()

        return render_template(
            "job_alerts.html",
            premium_success=True
        )

    except Exception as e:
        print("Premium success error:", repr(e))
        return f"Premium success error: {repr(e)}", 500

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password")
        admin_password = os.environ.get("ADMIN_PASSWORD", "changeme")

        if password == admin_password:
            session["admin_authenticated"] = True
            return redirect("/admin")

        return render_template("admin_login.html", error="Incorrect password")

    return render_template("admin_login.html")

@app.route("/admin")
def admin_dashboard():
    if not session.get("admin_authenticated"):
        return redirect("/admin-login")
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) AS total_alerts FROM job_alerts")
    total_alerts = cursor.fetchone()["total_alerts"]

    cursor.execute("SELECT COUNT(DISTINCT email) AS total_users FROM job_alerts")
    total_users = cursor.fetchone()["total_users"]

    cursor.execute("""
        SELECT COUNT(DISTINCT email) AS premium_users
        FROM job_alerts
        WHERE COALESCE(is_premium, 0) = 1
    """)
    premium_users = cursor.fetchone()["premium_users"]

    free_users = total_users - premium_users
    mrr = premium_users * 15

    cursor.execute("""
        SELECT job_title, COUNT(*) AS count
        FROM job_alerts
        GROUP BY job_title
        ORDER BY count DESC
        LIMIT 5
    """)
    top_titles = cursor.fetchall()

    cursor.execute("""
        SELECT location, COUNT(*) AS count
        FROM job_alerts
        GROUP BY location
        ORDER BY count DESC
        LIMIT 5
    """)
    top_locations = cursor.fetchall()

    cursor.execute("""
        SELECT email, job_title, location, keywords, is_premium, created_at
        FROM job_alerts
        ORDER BY created_at DESC
        LIMIT 25
    """)
    recent_alerts = cursor.fetchall()

    conn.close()

    return render_template(
        "admin.html",
        total_alerts=total_alerts,
        total_users=total_users,
        premium_users=premium_users,
        free_users=free_users,
        mrr=mrr,
        top_titles=top_titles,
        top_locations=top_locations,
        recent_alerts=recent_alerts
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)