import os
import io
import json
import base64
import logging
import requests
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, jsonify, redirect, session, flash, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

# Gmail
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# PDF support (Lightweight, safe for Render Free)
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PYPDF_AVAILABLE = False

load_dotenv()

logging.basicConfig(
    filename='oauth_errors.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

app = Flask(__name__)

# =========================
# CONFIG
# =========================
# NOTE: Render uses ephemeral storage. SQLite databases will reset on deploy. 
# For true production, upgrade to a managed PostgreSQL database URL later.
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///user.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'brickwise1')

# GOOGLE AUTH
app.config['GOOGLE_CLIENT_ID'] = (os.getenv('GOOGLE_CLIENT_ID') or '').strip()
app.config['GOOGLE_CLIENT_SECRET'] = (os.getenv('GOOGLE_CLIENT_SECRET') or '').strip()
app.config['GOOGLE_OAUTH_ENABLED'] = bool(app.config['GOOGLE_CLIENT_ID'] and app.config['GOOGLE_CLIENT_SECRET'])
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

oauth = OAuth(app)

if app.config['GOOGLE_OAUTH_ENABLED']:
    oauth.register(
        name="google",
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/gmail.modify"
            )
        }
    )

db = SQLAlchemy(app)

# OPENROUTER API KEY
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


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

class GmailToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_uri = db.Column(db.Text, default="https://oauth2.googleapis.com/token")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
# GMAIL HELPERS
# =========================
def get_gmail_service(username):
    token_record = GmailToken.query.filter_by(username=username).first()
    if not token_record:
        return None

    creds = Credentials(
        token=token_record.access_token,
        refresh_token=token_record.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"]
    )
    return build("gmail", "v1", credentials=creds)

def get_email_body(payload):
    body = ""
    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data")
                if data:
                    body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    else:
        data = payload["body"].get("data")
        if data:
            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return body


# =========================
# AUTH HELPERS
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
# TEXT EXTRACTION (PDF ONLY)
# =========================
def extract_pdf(file_stream):
    if not PYPDF_AVAILABLE:
        return "PDF library missing. Ensure pypdf is installed."
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


# =========================
# APP ROUTES
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
    message = request.form.get("message", "").strip()
    file = request.files.get("file")

    if not message and not file:
        return jsonify({"reply": "Please enter a message or upload a file."})

    extracted = ""
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()
        file_bytes = file.read()
        stream = io.BytesIO(file_bytes)

        if ext == ".pdf":
            extracted = extract_pdf(stream)
            db.session.add(DocumentContext(username=username, filename=filename, extracted_text=extracted))
            db.session.commit()
        else:
            extracted = "Image OCR removed for Cloud Deployment. Please upload PDFs only."

    if message:
        db.session.add(ChatMessage(username=username, role="user", content=message))
        db.session.commit()

    recent_history = ChatMessage.query.filter_by(username=username).order_by(ChatMessage.timestamp.desc()).limit(10).all()
    recent_history = list(reversed(recent_history))

    latest_doc = DocumentContext.query.filter_by(username=username).order_by(DocumentContext.timestamp.desc()).first()
    context_text = latest_doc.extracted_text[:8000] if latest_doc else ""

    system_prompt = f"""
You are BrickWise AI.

DOCUMENT CONTEXT (If any):
{context_text}

RULE:
- Answer questions based on the document context provided above.
- Be helpful and concise.
"""

    messages = [{"role": "system", "content": system_prompt}]
    for m in recent_history:
        messages.append({"role": m.role, "content": m.content})
    
    if extracted and not message:
        messages.append({"role": "user", "content": f"I uploaded a document: {filename}. Please acknowledge."})

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "meta-llama/llama-3.1-8b-instruct", 
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 800
    }

    response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)

    if response.status_code == 200:
        reply = response.json()["choices"][0]["message"]["content"]
    else:
        reply = f"OpenRouter Error: {response.text}"

    db.session.add(ChatMessage(username=username, role="assistant", content=reply))
    db.session.commit()

    return jsonify({"reply": reply})

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


# =========================
# GMAIL ROUTES
# =========================
@app.route("/gmail")
@login_required
def gmail_dashboard():
    return render_template("gmail.html")

@app.route("/gmail/inbox")
@login_required
def gmail_inbox():
    service = get_gmail_service(session["user"])
    if not service:
        return jsonify({"error": "Gmail not connected"})

    results = service.users().messages().list(userId="me", maxResults=30).execute()
    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        email = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = email["payload"].get("headers", [])
        sender = ""
        subject = ""

        for h in headers:
            if h["name"] == "From": sender = h["value"]
            if h["name"] == "Subject": subject = h["value"]

        body = get_email_body(email["payload"])
        emails.append({"id": msg["id"], "sender": sender, "subject": subject, "body": body[:500]})

    return jsonify(emails)

@app.route("/gmail/read/<message_id>")
@login_required
def read_email(message_id):
    service = get_gmail_service(session["user"])
    email = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = email["payload"].get("headers", [])
    
    sender = ""
    subject = ""
    for h in headers:
        if h["name"] == "From": sender = h["value"]
        if h["name"] == "Subject": subject = h["value"]

    body = get_email_body(email["payload"])
    return jsonify({"id": message_id, "sender": sender, "subject": subject, "body": body})

@app.route("/gmail/send", methods=["POST"])
@login_required
def send_email():
    service = get_gmail_service(session["user"])
    recipient = request.form.get("to")
    subject = request.form.get("subject")
    body = request.form.get("body")

    message = MIMEText(body)
    message["to"] = recipient
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return jsonify({"status": "Email sent"})

@app.route("/gmail/reply", methods=["POST"])
@login_required
def gmail_reply():
    service = get_gmail_service(session["user"])
    recipient = request.form.get("to")
    subject = request.form.get("subject")
    body = request.form.get("body")

    message = MIMEText(body)
    message["to"] = recipient
    message["subject"] = "Re: " + subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return jsonify({"success": True})

@app.route("/gmail/generate_reply", methods=["POST"])
@login_required
def generate_reply():
    email_content = request.form.get("content")
    prompt = f"Generate a professional email reply.\n\nEmail:\n{email_content}"

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "meta-llama/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": prompt}]
    }

    response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
    reply = response.json()["choices"][0]["message"]["content"]
    return jsonify({"reply": reply})


# =========================
# OAUTH ROUTES
# =========================
@app.route("/login/google")
def google_login():
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
        session["gmail_token"] = token

        user_info = token.get("userinfo")
        if not user_info:
            user_info = oauth.google.userinfo()

        google_id = user_info.get("sub")
        email = user_info.get("email")
        fullname = user_info.get("name")

        user = User.query.filter_by(google_id=google_id).first()
        if not user and email:
            user = User.query.filter_by(email=email).first()

        if not user:
            base_username = email.split("@")[0]
            username = base_username
            count = 1
            while User.query.filter_by(username=username).first():
                username = f"{base_username}{count}"
                count += 1

            user = User(fullname=fullname, username=username, email=email, google_id=google_id, auth_type="google")
            db.session.add(user)
            db.session.commit()
        else:
            if not user.google_id: user.google_id = google_id
            if not user.email: user.email = email
            user.auth_type = "google"
            db.session.commit()
            
        gmail_token = GmailToken.query.filter_by(username=user.username).first()
        if not gmail_token:
            gmail_token = GmailToken(username=user.username)
            db.session.add(gmail_token)

        gmail_token.access_token = token.get("access_token")
        gmail_token.refresh_token = token.get("refresh_token")
        db.session.commit()

        session["user"] = user.username
        flash("Logged in successfully", "success")
        return redirect(url_for("brickwise"))

    except Exception as e:
        logging.exception("Google OAuth Error")
        flash(f"Google Login Failed: {str(e)}", "error")
        return redirect(url_for("login"))
    
# =========================
# RUN SERVER (RENDER SAFE)
# =========================
if __name__ == "__main__":
    # Render assigns a dynamic port. Default to 5000 if running locally.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
