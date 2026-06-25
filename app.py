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
import logging
import requests   #ADDED

import pytesseract
from PIL import Image

# Gmail
import base64
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials

# =========================
# CHROMADB + EMBEDDINGS
# =========================
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

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
# CHROMADB SETUP (PERSISTENT MEMORY)
# =========================
chroma_client = chromadb.PersistentClient(path="chroma_memory")
chat_collection = chroma_client.get_or_create_collection("chat_memory")
embedder = SentenceTransformer("all-MiniLM-L6-v2")


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
    username = db.Column(db.String(120),nullable=False)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_uri = db.Column(db.Text,default="https://oauth2.googleapis.com/token")
    created_at = db.Column(db.DateTime,default=datetime.utcnow)


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


# Gmail
def get_gmail_service(username):

    token_record = GmailToken.query.filter_by(
        username=username
    ).first()

    if not token_record:
        return None

    creds = Credentials(
        token=token_record.access_token,
        refresh_token=token_record.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"]
    )
 

    service = build(
        "gmail",
        "v1",
        credentials=creds
    )

    return service # Gmail

def get_email_body(payload):

    body = ""

    if "parts" in payload:

        for part in payload["parts"]:

            if part.get("mimeType") == "text/plain":

                data = part["body"].get(
                    "data"
                )

                if data:

                    body += base64.urlsafe_b64decode(
                        data
                    ).decode(
                        "utf-8",
                        errors="ignore"
                    )

    else:

        data = payload["body"].get(
            "data"
        )

        if data:

            body += base64.urlsafe_b64decode(
                data
            ).decode(
                "utf-8",
                errors="ignore"
            )

    return body


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

# Gmail
@app.route("/gmail")
@login_required
def gmail_dashboard():

    return render_template(
        "gmail.html"
    )

@app.route("/gmail/inbox")
@login_required
def gmail_inbox():

    service = get_gmail_service(
        session["user"]
    )

    if not service:
        return jsonify({
            "error": "Gmail not connected"
        })

    results = service.users().messages().list(
        userId="me",
        maxResults=30
    ).execute()

    messages = results.get(
        "messages",
        []
    )

    emails = []

    for msg in messages:

        email = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="full"
        ).execute()

        headers = email["payload"].get(
            "headers",
            []
        )

        sender = ""
        subject = ""

        for h in headers:

            if h["name"] == "From":
                sender = h["value"]

            if h["name"] == "Subject":
                subject = h["value"]

        body = get_email_body(
            email["payload"]
        )

        emails.append({

            "id": msg["id"],
            "sender": sender,
            "subject": subject,
            "body": body[:500]

        })

    return jsonify(emails)

@app.route("/gmail/read/<message_id>")
@login_required
def read_email(message_id):

    service = get_gmail_service(
        session["user"]
    )

    email = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full"
    ).execute()

    headers = email["payload"].get(
        "headers",
        []
    )

    sender = ""
    subject = ""

    for h in headers:

        if h["name"] == "From":
            sender = h["value"]

        if h["name"] == "Subject":
            subject = h["value"]

    body = get_email_body(
        email["payload"]
    )

    return jsonify({

        "id": message_id,
        "sender": sender,
        "subject": subject,
        "body": body

    })

@app.route(
    "/gmail/send",
    methods=["POST"]
)

@login_required
def send_email():

    service = get_gmail_service(
        session["user"]
    )

    recipient = request.form.get(
        "to"
    )

    subject = request.form.get(
        "subject"
    )

    body = request.form.get(
        "body"
    )

    message = MIMEText(body)

    message["to"] = recipient

    message["subject"] = subject

    raw = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()

    return jsonify({
        "status": "Email sent"
    })


@app.route(
    "/gmail/reply",
    methods=["POST"]
)
@login_required
def gmail_reply():

    service = get_gmail_service(
        session["user"]
    )

    recipient = request.form.get(
        "to"
    )

    subject = request.form.get(
        "subject"
    )

    body = request.form.get(
        "body"
    )

    message = MIMEText(body)

    message["to"] = recipient

    message["subject"] = "Re: " + subject

    raw = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    service.users().messages().send(
        userId="me",
        body={
            "raw": raw
        }
    ).execute()

    return jsonify({
        "success": True
    })

@app.route(
    "/gmail/generate_reply",
    methods=["POST"]
)
@login_required
def generate_reply():

    email_content = request.form.get(
        "content"
    )

    prompt = f"""
Generate a professional email reply.

Email:

{email_content}
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {

        "model":
        "meta-llama/llama-3.1-8b-instruct",

        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload
    )

    reply = response.json()[
        "choices"
    ][0]["message"]["content"]

    return jsonify({
        "reply": reply
    })

@app.route("/gmail/analyze")
@login_required
def gmail_analyze():

    service = get_gmail_service(
        session["user"]
    )

    results = service.users().messages().list(
        userId="me",
        maxResults=15
    ).execute()

    messages = results.get(
        "messages",
        []
    )

    email_text = ""

    for msg in messages:

        data = service.users().messages().get(
            userId="me",
            id=msg["id"]
        ).execute()

        headers = data["payload"].get(
            "headers",
            []
        )

        subject = ""

        sender = ""

        for h in headers:

            if h["name"] == "Subject":
                subject = h["value"]

            if h["name"] == "From":
                sender = h["value"]

        email_text += f"""
From: {sender}
Subject: {subject}

"""

    prompt = f"""
Analyze these emails.

Provide:

1. Summary
2. Important Emails
3. Tasks
4. Deadlines
5. Urgent Actions

Emails:

{email_text}
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model":"meta-llama/llama-3.1-8b-instruct",
        "messages":[
            {
                "role":"user",
                "content":prompt
            }
        ]
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload
    )

    analysis = response.json()["choices"][0]["message"]["content"]

    return jsonify({
        "analysis": analysis
    })


def load_chat(username):
    chats = ChatMessage.query.filter_by(username=username).order_by(ChatMessage.timestamp).all()
    return [{"role": c.role, "content": c.content, "time": str(c.timestamp)} for c in chats]


# =========================
# CHROMA MEMORY FUNCTIONS
# =========================
def add_to_memory(username, text, role):
    embedding = embedder.encode(text).tolist()

    chat_collection.add(
        embeddings=[embedding],
        documents=[text],
        ids=[f"{username}_{datetime.utcnow().timestamp()}"],
        metadatas=[{"username": username, "role": role}]
    )


def get_relevant_memory(username, query, k=5):
    query_embedding = embedder.encode(query).tolist()

    results = chat_collection.query(
        query_embeddings=[query_embedding],
        n_results=k,
        where={"username": username}
    )

    if results and results["documents"]:
        return "\n".join(results["documents"][0])
    return ""


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


# ROUTES

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

    # FILE UPLOAD
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

        add_to_memory(username, extracted, "document")

    # SAVE USER MESSAGE
    db.session.add(ChatMessage(username=username, role="user", content=message))
    db.session.commit()

    add_to_memory(username, message, "user")

    # RAG MEMORY FETCH
    rag_memory = get_relevant_memory(username, message)

    # RECENT HISTORY
    recent_history = ChatMessage.query.filter_by(username=username)\
        .order_by(ChatMessage.timestamp.desc())\
        .limit(10)\
        .all()

    recent_history = list(reversed(recent_history))

    # DOCUMENT CONTEXT
    latest_doc = (
    DocumentContext.query
    .filter_by(username=username)
    .order_by(DocumentContext.timestamp.desc())
    .first()
)

    context_text = latest_doc.extracted_text[:80000] if latest_doc else ""

    # SYSTEM PROMPT
    system_prompt = f"""
You are BrickWise AI.

LONG TERM MEMORY (RAG):
{rag_memory}

DOCUMENT CONTEXT:
{context_text}

RULE:
- Use past memory to answer consistently
- Remember user preferences
"""

    messages = [{"role": "system", "content": system_prompt}]

    for m in recent_history:
        messages.append({
            "role": m.role,
            "content": m.content
        })

    # =========================
    # OPENROUTER FAST MODEL (FREE)
    # =========================
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
    "model": "meta-llama/llama-3.1-8b-instruct",  # meta-llama/llama-3.1-8b-instruct meta-llama/llama-3.1-8b-instruct:free meta-llama/llama-3.2-3b-instruct:free
    "messages": messages,
    "temperature": 0.7,
    "max_tokens": 800
}

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload
    )

    if response.status_code == 200:
        reply = response.json()["choices"][0]["message"]["content"]
    else:
        reply = f"OpenRouter Error: {response.text}"

    db.session.add(ChatMessage(username=username, role="assistant", content=reply))
    db.session.commit()

    add_to_memory(username, reply, "assistant")

    return jsonify({"reply": reply})


# =========================
# CLEAR CHAT
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


@app.route("/login/google")
def google_login():
    redirect_uri = url_for(
        "google_callback",
        _external=True
    )

    print("Redirect URI:", redirect_uri)

    return oauth.google.authorize_redirect(
        redirect_uri
    )

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

        user = User.query.filter_by(
            google_id=google_id
        ).first()

        if not user and email:
            user = User.query.filter_by(
                email=email
            ).first()

        if not user:
            base_username = email.split("@")[0]

            username = base_username
            count = 1

            while User.query.filter_by(
                username=username
            ).first():
                username = f"{base_username}{count}"
                count += 1

            user = User(
                fullname=fullname,
                username=username,
                email=email,
                google_id=google_id,
                auth_type="google"
            )

            db.session.add(user)
            db.session.commit()

        else:
            if not user.google_id:
                user.google_id = google_id

            if not user.email:
                user.email = email

            user.auth_type = "google"

            db.session.commit()
            
        #Gmail
        gmail_token = GmailToken.query.filter_by(username=user.username).first()

        if not gmail_token:

            gmail_token = GmailToken(
                username=user.username
            )

        db.session.add(gmail_token)

        gmail_token.access_token = token.get("access_token")
        gmail_token.refresh_token = token.get("refresh_token")

        db.session.commit()


        session["user"] = user.username

        flash("Logged in successfully", "success")

        return redirect(url_for("brickwise"))

    except Exception as e:
        logging.exception("Google OAuth Error")

        flash(
            f"Google Login Failed: {str(e)}",
            "error"
        )

        return redirect(url_for("login"))
    
if __name__ == "__main__":
    app.run(debug=True)
