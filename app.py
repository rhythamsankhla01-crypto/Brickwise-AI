from flask import Flask, render_template, request, jsonify, redirect, session, flash, url_for
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth
from sqlalchemy import or_, text
from dotenv import load_dotenv
import os
import json
import secrets
from datetime import datetime
import ollama
import logging

import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
);



try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PYPDF_AVAILABLE = False

load_dotenv()

# Logging for OAuth / callback debugging
logging.basicConfig(
    filename='oauth_errors.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

app = Flask(__name__)

# DATABASE CONFIG
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///user.db'
app.config['SECRET_KEY'] = 'brickwise1'

# GOOGLE SIGN IN
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
        # Use Google's OpenID Connect discovery document so Authlib can find jwks_uri
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',  # Users are redirected to log in
        access_token_url='https://oauth2.googleapis.com/token',
        authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
        api_base_url='https://www.googleapis.com/oauth2/v1/',       # Used to fetch user information
        userinfo_endpoint='https://openidconnect.googleapis.com/v1/userinfo',  # After login, this endpoint returns user details
        client_kwargs={
            'scope': 'openid email profile',
            'prompt': 'select_account'      # Ask the user to choose the Google Account
        },
        authorize_params={
            'access_type': 'offline'
        }
    )

db = SQLAlchemy(app);

# CHAT HISTORY FOLDER
CHAT_FOLDER = "chat_history"

# CREATE CHAT FOLDER IF NOT EXISTS
os.makedirs(CHAT_FOLDER, exist_ok=True);

# CREATE PDF FOLDER
PDF_FOLDER = "pdf_files"
os.makedirs(PDF_FOLDER, exist_ok=True);

# CREATE IMAGE FOLDER
IMAGE_FOLDER = "image_files"
os.makedirs(IMAGE_FOLDER, exist_ok=True);

# USER DATABASE
class User(db.Model):
    sno = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(30))
    username = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=True)
    image = db.Column(db.LargeBinary, nullable=True)
    google_id = db.Column(db.String(100), unique=True, nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    auth_type = db.Column(db.String(20), nullable=False, default='local')

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20),nullable=False)
    role = db.Column(db.String(20),nullable=False)
    content = db.Column(db.Text,nullable=False)
    timestamp = db.Column(db.DateTime,default=datetime.utcnow);

with app.app_context():
    db.create_all();

# GOOGLE SIGN IN
    def ensure_user_columns():
        with db.engine.begin() as conn:
            existing = [row[1] for row in conn.execute(text("PRAGMA table_info(user);"))]
            if 'google_id' not in existing:
                conn.execute(text("ALTER TABLE user ADD COLUMN google_id VARCHAR(100);"))
            if 'email' not in existing:
                conn.execute(text("ALTER TABLE user ADD COLUMN email VARCHAR(120);"))
            if 'auth_type' not in existing:
                conn.execute(text("ALTER TABLE user ADD COLUMN auth_type VARCHAR(20) DEFAULT 'local';"))

    ensure_user_columns()


# LOGIN REQUIRED DECORATOR
def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if 'user' not in session:
            return redirect(url_for('login'));
        return view(**kwargs);
    return wrapped_view;


# LOAD CHAT HISTORY
def load_chat(username):
    chats = ChatMessage.query.filter_by(
        username=username
    ).order_by(
        ChatMessage.timestamp
    ).all()

    messages = []

    for chat in chats:
        messages.append({
            "role": chat.role,
            "content": chat.content,
            "time": str(chat.timestamp)
        })

    return messages


# SAVE CHAT HISTORY
def save_chat(username, messages):
    file_path = os.path.join(CHAT_FOLDER,f"{username}.json");
    with open(file_path, "w") as file:
        json.dump(messages, file, indent=4);   #Dump saves python data into JSON

# PDF TEXT EXTRACTOR
def extract_pdf_text(pdf_path):
    text = "";
    if not PYPDF_AVAILABLE:
        return "";
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    except Exception as e:
        print(f"PDF Error: {e}")
    return text;


# IMAGE TEXT EXTRACTOR
def extract_image_text(image_path):
    try:
        image = Image.open(image_path)
        text = pytesseract.image_to_string(image);
        return text;
    except Exception as e:
        print("OCR Error:", e)
        return ""

# HOME PAGE
@app.route("/")
def home():
    return render_template("home.html");

# REGISTRATION
@app.route("/registration", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fullname = request.form.get('fullname','').strip();
        username = request.form.get('username','').strip();
        password = request.form.get('password','');
        confirm_password = request.form.get('confirm_password','');

        # VALIDATION
        if not username or not password:
            flash('Username and password are required.','error')
            return render_template('registration.html')

        if password != confirm_password:
            flash('Passwords do not match.','error')
            return render_template('registration.html')

        # CHECK EXISTING USER
        existing = User.query.filter_by(username=username).first()

        if existing:
            flash('Username already taken.','error')
            return render_template('registration.html');

        # CREATE USER
        new_user = User(fullname=fullname, username=username, password=password)
        db.session.add(new_user)
        db.session.commit();    #commit save database changes permanently

        flash('Registration successful. Please log in.','success')
        return redirect(url_for('login'));
    return render_template('registration.html');

# LOGIN
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip();
        password = request.form.get('password','');
        user = User.query.filter_by(username=username).first();

        # CHECK USER
        if user and password == user.password:
            session['user'] = user.username
            return redirect('/brickwise')

        flash('Invalid username or password.','error')
    return render_template('login.html')


# GOOGLE SIGN IN
@app.route('/login/google')
def google_login():
    if not app.config['GOOGLE_OAUTH_ENABLED']:
        flash(
            'Google OAuth is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to your environment or .env file.',
            'error'
        )
        return redirect(url_for('login'))

    redirect_uri = url_for('google_auth_callback', _external=True);
    return oauth.google.authorize_redirect(redirect_uri);

# 
@app.route('/login/google/callback')
def google_auth_callback():
    if not app.config['GOOGLE_OAUTH_ENABLED']:
        flash('Google OAuth is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to your environment or .env file.','error')
        return redirect(url_for('login'))
    
    try:
        token = oauth.google.authorize_access_token()

        # Try to fetch userinfo from the provider
        resp = oauth.google.get('userinfo')   # Return user informations
        user_info = None;
        if resp and getattr(resp, 'ok', False):
            try:
                user_info = resp.json()
            except Exception:
                user_info = None;

        # Fallback: some providers attach id_token payload to token
        if not user_info:
            user_info = token.get('userinfo') or token.get('id_token') or {}

        email = user_info.get('email')
        google_id = user_info.get('sub') or user_info.get('id')
        fullname = user_info.get('name') or user_info.get('given_name') or (email.split('@')[0] if email else None)

        if not email or not google_id:
            flash('Google login failed. Email access is required.','error')
            logging.error('Google login failed: missing email or id. user_info=%s token=%s', user_info, token)
            return redirect(url_for('login'))

        user = User.query.filter(or_(User.google_id == google_id, User.email == email, User.username == email)).first()

        if user:
            user.google_id = user.google_id or google_id
            user.email = user.email or email
            user.auth_type = 'google'
            db.session.commit()
        else:
            user = User(
                fullname=fullname,
                username=email,
                email=email,
                google_id=google_id,
                auth_type='google',
                password=secrets.token_urlsafe(24)
            )
            db.session.add(user);
            db.session.commit();

        session['user'] = user.username
        flash('Signed in with Google successfully.', 'success')
        return redirect(url_for('brickwise'))

    except Exception as exc:
        # Log full exception for debugging and show a friendly message
        logging.exception('Exception in google_auth_callback')
        flash('An error occurred during Google sign-in. Check server logs for details.','error')
        return redirect(url_for('login'))


# LOGOUT
@app.route('/logout')
@login_required
def logout():
    session.pop('user', None)
    flash("Successfully logged out");
    return redirect('/');


# PROFILE PAGE
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():

    if 'user' not in session:
        return redirect(url_for('login'))
    user = User.query.filter_by(username=session['user']).first()

    # # IMAGE UPLOAD
    # if request.method == 'POST':
    #     if 'image' in request.files:
    #         image = request.files['image'].read()
    #         user.image = image
    #         db.session.commit()
    #         flash('Profile image updated.','success')

    return render_template('profile.html',user=user)



# BRICKWISE PAGE
@app.route("/brickwise")
@login_required
def brickwise():
    # LOAD RECENT CHAT HISTORY ONLY for the recent chats sidebar
    recent_messages = load_chat(session['user'])
    
    # Keep the active chat area empty on reload
    return render_template("index.html",messages=[],recent_messages=recent_messages)


# OLLAMA CHAT API
@app.route("/chats", methods=["POST"])
@login_required
def chat():
    user_message = request.form.get("message", "").strip();
    username = session.get("user");
    
    # PDF UPLOAD
    uploaded_file = request.files.get("file")
    if uploaded_file and uploaded_file.filename:
        filename = secure_filename(uploaded_file.filename)
        ext = os.path.splitext(filename)[1].lower()

        # PDF
        if ext == ".pdf":
            pdf_path = os.path.join(PDF_FOLDER,f"{username}_{filename}");
            uploaded_file.save(pdf_path);
            pdf_text = extract_pdf_text(pdf_path);
            text_file = os.path.join(PDF_FOLDER,f"{username}.txt");

            with open(text_file, "w", encoding="utf-8") as file:
                file.write(pdf_text);

        # IMAGE
        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
            image_path = os.path.join(IMAGE_FOLDER,f"{username}_{filename}")

            uploaded_file.save(image_path)
            image_text = extract_image_text(image_path)
            text_file = os.path.join(IMAGE_FOLDER,f"{username}_ocr.txt")

            with open(text_file, "w", encoding="utf-8") as file:
                file.write(image_text);
    
    # LOAD CHAT HISTORY
    user_chat = ChatMessage(
    username=username,
    role="user",
    content=user_message
);

    db.session.add(user_chat);
    db.session.commit();

    # LOAD PDF CONTENT
    # LOAD PDF & OCR CONTENT
    
    pdf_context = "";
    ocr_context = "";

    # LOAD PDF TEXT
    text_file = os.path.join(PDF_FOLDER,f"{username}.txt")

    if os.path.exists(text_file):
        with open(text_file, "r", encoding="utf-8") as file:
            pdf_context = file.read()[:12000]

    # LOAD OCR TEXT
    ocr_file = os.path.join(IMAGE_FOLDER,f"{username}_ocr.txt")

    if os.path.exists(ocr_file):
        with open(ocr_file, "r", encoding="utf-8") as file:
            ocr_context = file.read()[:12000];
    messages = load_chat(username);

    try:
        ollama_messages = [];

        # PDF Context
        if pdf_context:
            ollama_messages.append({
                "role": "system",
                "content": f"""
Answer questions using the uploaded PDF.

If the answer is not present in the PDF, say:
'I could not find that information in the uploaded PDF.'

PDF CONTENT:
{pdf_context}
"""
        })

        # OCR Context
        if ocr_context:
            ollama_messages.append({
                "role": "system",
                "content": f"""
The user uploaded an image.

Extracted text from image:

{ocr_context}

Use this OCR text to answer questions.
"""
            })

        # Chat History
        for msg in messages[-10:]:
            ollama_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Current User Message
        ollama_messages.append({
            "role": "user",
            "content": user_message
        })

        response = ollama.chat(
            model="llama3.2",
            messages=ollama_messages
        )

        reply = response["message"]["content"]

    except Exception as exc:
        print("Ollama Error:", exc)
        reply = f"Error: {str(exc)}"

    # SAVE BOT MESSAGE
    bot_chat = ChatMessage(
        username=username,
        role="assistant",
        content=reply
    )

    db.session.add(bot_chat)
    db.session.commit()
    messages.append({
        "role": "assistant",
        "content": reply,
        "time": str(datetime.utcnow())
    })
    save_chat(username, messages)
    return jsonify({
        "reply": reply
    })


# =========================
# CLEAR CHAT HISTORY
# =========================

@app.route("/clear_chat")
@login_required
def clear_chat():

    username = session.get("user");
    file_path = os.path.join(CHAT_FOLDER,f"{username}.json");
    ChatMessage.query.filter_by(
        username=username
    ).delete();
    db.session.commit();
    flash("Chat history cleared.");
    return redirect("/brickwise");


# ABOUT PAGE ROUTE
@app.route('/about-brickwise')
def about():
    return render_template('about.html');


# =========================
# RUN APP
# =========================
if __name__ == "__main__":
    app.run(debug=True);