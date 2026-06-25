from flask import Flask, render_template, request, redirect
from pypdf import PdfReader
from docx import Document
import os

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


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


def score_candidate(resume_text, criteria_text):
    criteria_words = criteria_text.lower().split()
    score = 0
    matched_words = []

    for word in criteria_words:
        if len(word) < 3:
            continue

        occurrences = resume_text.count(word)

        if occurrences > 0:
            score += occurrences
            matched_words.append(word)

    return score, matched_words


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/candidate-matcher", methods=["GET", "POST"])
def index():
    results = []

    if request.method == "POST":
        criteria_text = request.form.get("criteria", "").strip()

        jd_file = request.files.get("job_description")
        resume_files = request.files.getlist("resumes")

        jd_text = ""

        if jd_file and jd_file.filename != "":
            jd_path = os.path.join(app.config["UPLOAD_FOLDER"], jd_file.filename)
            jd_file.save(jd_path)
            jd_text = read_file(jd_path)

            if os.path.exists(jd_path):
                os.remove(jd_path)

        combined_criteria = (criteria_text + " " + jd_text).strip()

        if combined_criteria == "":
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

            score, matches = score_candidate(resume_text, combined_criteria)

            results.append({
                "name": os.path.basename(path),
                "score": score,
                "matches": matches[:15]
            })

            if os.path.exists(path):
                os.remove(path)

        results.sort(key=lambda candidate: candidate["score"], reverse=True)

    return render_template("index.html", results=results)


@app.route("/resume-writer")
def resume_writer():
    return render_template("resume_writer.html")


@app.route("/candidate-sourcing")
def candidate_sourcing():
    return render_template("candidate_sourcing.html")


@app.route("/upwork")
def upwork():
    return redirect("https://www.upwork.com/freelancers/~0187a46036bf325d2a?mp_source=share")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)