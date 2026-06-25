from flask import Flask, render_template, request, jsonify, redirect, session, flash, url_for
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth
from sqlalchemy import or_, text
from dotenv import load_dotenv
import os
import secrets
import io
from datetime import datetime
import ollama
import logging

import pytesseract
from PIL import Image

# PDF support
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PYPDF_AVAILABLE = False

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

load_dotenv()

logging.basicConfig(
    filename='oauth_errors.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

app = Flask(__name__)

# CONFIG
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///user.db'
app.config['SECRET_KEY'] = 'brickwise1'

# GOOGLE AUTH
app.config['GOOGLE_CLIENT_ID'] = (os.getenv('GOOGLE_CLIENT_ID') or '').strip()
app.config['GOOGLE_CLIENT_SECRET'] = (os.getenv('GOOGLE_CLIENT_SECRET') or '').strip()
app.config['GOOGLE_OAUTH_ENABLED'] = bool(app.config['GOOGLE_CLIENT_ID'] and app.config['GOOGLE_CLIENT_SECRET'])
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

oauth = OAuth(app)

if app.config['GOOGLE_OAUTH_ENABLED']:
    oauth.register(
        name='google',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        access_token_url='https://oauth2.googleapis.com/token',
        authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
        api_base_url='https://www.googleapis.com/oauth2/v1/',
        userinfo_endpoint='https://openidconnect.googleapis.com/v1/userinfo',
        client_kwargs={'scope': 'openid email profile', 'prompt': 'select_account'},
        authorize_params={'access_type': 'offline'}
    )

db = SQLAlchemy(app)

# =========================
# MODELS
# =========================
class User(db.Model):
    sno = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(30))
    username = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128))
    image = db.Column(db.LargeBinary)
    google_id = db.Column(db.String(100), unique=True)
    email = db.Column(db.String(120), unique=True)
    auth_type = db.Column(db.String(20), default='local')

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class DocumentContext(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    filename = db.Column(db.String(255))
    extracted_text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# =========================
# HELPERS
# =========================
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def load_chat(username):
    chats = ChatMessage.query.filter_by(username=username).order_by(ChatMessage.timestamp).all()
    return [{"role": c.role, "content": c.content, "time": str(c.timestamp)} for c in chats]


# =========================
# TEXT EXTRACTION
# =========================
def extract_pdf(file_stream):
    if not PYPDF_AVAILABLE:
        return "PDF library missing. Run: pip install pypdf"

    text = ""
    try:
        reader = PdfReader(file_stream)
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"

        if not text.strip():
            return "Scanned PDF detected. No readable text found."
        return text

    except Exception as e:
        return f"PDF error: {e}"


def extract_image(file_stream):
    try:
        img = Image.open(file_stream)
        text = pytesseract.image_to_string(img)

        if not text.strip():
            return "No text detected in image."
        return text

    except Exception as e:
        return f"OCR error: {e}"


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/registration", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not password:
            flash("Required fields missing", "error")
            return render_template("registration.html")

        if password != confirm:
            flash("Passwords do not match", "error")
            return render_template("registration.html")

        if User.query.filter_by(username=username).first():
            flash("User exists", "error")
            return render_template("registration.html")

        db.session.add(User(username=username, password=password))
        db.session.commit()

        flash("Registered successfully")
        return redirect("/login")

    return render_template("registration.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username")
        p = request.form.get("password")

        user = User.query.filter_by(username=u).first()

        if user and user.password == p:
            session['user'] = user.username
            return redirect("/brickwise")

        flash("Invalid login")

    return render_template("login.html")


@app.route("/brickwise")
@login_required
def brickwise():
    return render_template("index.html", recent_messages=load_chat(session['user']))


# =========================
# CHAT + FILE UPLOAD
# =========================
@app.route("/chats", methods=["POST"])
@login_required
def chat():
    username = session['user']
    message = request.form.get("message", "")
    file = request.files.get("file")

    # ======================
    # SAVE DOCUMENT (NO OVERWRITE)
    # ======================
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()

        file_bytes = file.read()
        stream = io.BytesIO(file_bytes)

        if ext == ".pdf":
            extracted = extract_pdf(stream)
        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
            extracted = extract_image(stream)
        else:
            extracted = "Unsupported file format"

        db.session.add(DocumentContext(
            username=username,
            filename=filename,
            extracted_text=extracted
        ))
        db.session.commit()

    # ======================
    # SAVE USER MESSAGE
    # ======================
    db.session.add(ChatMessage(username=username, role="user", content=message))
    db.session.commit()

    db.session.expire_all()

    # ======================
    # FULL CHAT MEMORY (NO LIMIT)
    # ======================
    full_history = load_chat(username)

    # ======================
    # MEMORY ANCHOR (EARLY CONVERSATION)
    # ======================
    anchor_msgs = ChatMessage.query.filter_by(username=username)\
        .order_by(ChatMessage.timestamp.asc()).limit(5).all()

    memory_text = "\n".join([m.content for m in anchor_msgs])

    # ======================
    # DOCUMENT CONTEXT
    # ======================
    docs = DocumentContext.query.filter_by(username=username).all()
    context_text = "\n\n".join([d.extracted_text for d in docs])[:80000]

    # ======================
    # SYSTEM PROMPT
    # ======================
    system_prompt = f"""
You are BrickWise AI.

LONG TERM MEMORY (EARLY CONVERSATION):
{memory_text}

DOCUMENT CONTEXT:
{context_text}

RULE:
- Remember user preferences across chat
- Use conversation history properly
"""


    # BUILD OLLAMA INPUT
    
    messages = [{"role": "system", "content": system_prompt}]

    for m in full_history:
        messages.append({"role": m["role"], "content": m["content"]})

    # ======================
    # AI RESPONSE
    # ======================
    try:
        res = ollama.chat(model="llama3.2", messages=messages)
        reply = res["message"]["content"]
    except Exception as e:
        reply = str(e)

    db.session.add(ChatMessage(username=username, role="assistant", content=reply))
    db.session.commit()

    return jsonify({"reply": reply})


# =========================
# CLEAR CHAT + DOCS
# =========================
@app.route("/clear_chat")
@login_required
def clear_chat():
    username = session['user']

    ChatMessage.query.filter_by(username=username).delete()
    DocumentContext.query.filter_by(username=username).delete()

    db.session.commit()
    flash("Cleared successfully")
    return redirect("/brickwise")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True)