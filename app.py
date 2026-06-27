from flask import Flask, render_template, request, redirect
from pypdf import PdfReader
from docx import Document
import os
import re
from datetime import date

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)