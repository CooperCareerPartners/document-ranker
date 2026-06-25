from flask import Flask, render_template, request
from pathlib import Path
from pypdf import PdfReader
from docx import Document
import os

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# --- Read PDF ---
def read_pdf(path):
    text = ""
    try:
        reader = PdfReader(path)
        for page in reader.pages:
            text += (page.extract_text() or "") + " "
    except Exception as e:
        print("PDF error:", e)
    return text.lower()


# --- Read DOCX ---
def read_docx(path):
    try:
        doc = Document(path)
        return " ".join(p.text for p in doc.paragraphs).lower()
    except Exception as e:
        print("DOCX error:", e)
        return ""


# --- Simple scoring system ---
def score(text, terms):
    return sum(text.count(t.lower()) for t in terms)


# --- Main webpage route ---
@app.route("/", methods=["GET", "POST"])
def index():
    results = []

    if request.method == "POST":
        files = request.files.getlist("files")
        keywords = request.form.get("keywords", "")

        terms = [t.strip() for t in keywords.split(",") if t.strip()]

        saved_paths = []

        # Save uploaded files
        for f in files:
            if f.filename == "":
                continue

            path = os.path.join(app.config["UPLOAD_FOLDER"], f.filename)
            f.save(path)
            saved_paths.append(path)

        # Analyze files
        for path in saved_paths:
            text = ""

            if path.endswith(".pdf"):
                text = read_pdf(path)
            elif path.endswith(".docx"):
                text = read_docx(path)
            else:
                continue

            results.append((os.path.basename(path), score(text, terms)))

        # Sort best match first
        results.sort(key=lambda x: x[1], reverse=True)

    return render_template("index.html", results=results)


# --- Run server ---
if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)