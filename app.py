import os
import secrets
import base64
import click, tempfile, zipfile, io
from io import BytesIO
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
from fpdf import FPDF
import qrcode, requests
from flask import Flask, render_template, request, jsonify, url_for, send_file, Response, session, redirect, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM
from playwright.sync_api import sync_playwright
import enum
from functools import wraps
from werkzeug.utils import secure_filename
from PIL import Image
import vercel_blob

load_dotenv()

# --- Configuration ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')

db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_size": 2, "max_overflow": 0, "pool_pre_ping": True, "pool_recycle": 300,
}

db = SQLAlchemy(app)

# --- Enums ---
class ParticipantStatus(enum.Enum):
    active = "active"
    revoked = "revoked"
    pending = "pending"

# --- Models ---
class Institute(db.Model):
    __tablename__ = 'institutes'
    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    city = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())
    
    participants = db.relationship('Participant', backref='institute')

class Participant(db.Model):
    __tablename__ = 'participants'
    id = db.Column(db.BigInteger, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    roll_no = db.Column(db.String(50), nullable=False, unique=True, index=True) 
    institute_id = db.Column(db.BigInteger, db.ForeignKey('institutes.id'), nullable=False)
    photo_blob_url = db.Column(db.String(500), nullable=True)
    
    # --- NEW COLUMNS ---
    project_title = db.Column(db.String(255), nullable=True)
    project_category = db.Column(db.String(100), nullable=True)
    
    status = db.Column(
        ENUM(ParticipantStatus, name='participant_status', create_type=False), 
        default=ParticipantStatus.active, 
        nullable=False
    )
    
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())

# --- Helpers ---
def generate_qr_base64(data):
    """Generates a QR code containing the data and returns it as a base64 string."""
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()

# --- Routes ---
@app.route('/')
def index():
    """Loads the frontend with active institutes."""
    institutes = Institute.query.filter_by(is_active=True).order_by(Institute.name).all()
    return render_template('index.html', institutes=institutes)

@app.route('/api/generate-card', methods=['POST'])
def generate_card():
    """
    Handles verification, optional photo update/removal, and card generation.
    """
    uploaded_blob_url = None
    try:
        # 1. Get inputs
        name = request.form.get('full_name', '').strip()
        roll_no = request.form.get('roll_no', '').strip()
        institute_id = request.form.get('institute_id', type=int)
        remove_photo = request.form.get('remove_photo') == 'true' # <--- NEW
        
        if not name or not roll_no or not institute_id:
            return jsonify({"error": "Name, Roll Number, and Institute are required."}), 400

        # 2. Secure Database Query
        participant = Participant.query.filter(
            db.func.lower(Participant.full_name) == name.lower(),
            Participant.roll_no == roll_no,
            Participant.institute_id == institute_id,
            Participant.status == 'active'
        ).first()

        if not participant:
            return jsonify({"error": "No record found matching the provided Name, Roll No, and Institute."}), 404

        # 3. Handle Photo Removal (If user explicitly wants to remove it)
        if remove_photo and participant.photo_blob_url:
            old_url = participant.photo_blob_url
            participant.photo_blob_url = None
            db.session.commit()
            try: 
                vercel_blob.delete_blob(old_url)
            except Exception as e:
                print(f"Blob deletion failed: {e}")

        # 4. Handle New Photo Upload (Max 1MB)
        file = request.files.get('photo')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
                return jsonify({"error": "Invalid image format."}), 400

            try:
                img = Image.open(file.stream)
                img.verify()
                file.stream.seek(0, os.SEEK_END)

                # CHANGED TO 2MB
                if file.stream.tell() > 2 * 1024 * 1024: 
                    return jsonify({"error": "Image too large (Max 2MB)."}), 400

                file.stream.seek(0)
            except Exception:
                return jsonify({"error": "Corrupted image file."}), 400

            # Upload to Vercel Blob
            blob_result = vercel_blob.put_blob(
                f"participants/{secrets.token_hex(8)}{ext}",
                file.read(),
                access="public",
                content_type=file.content_type
            )
            uploaded_blob_url = blob_result.url
            
            # Delete old photo if it exists
            old_url = participant.photo_blob_url
            participant.photo_blob_url = uploaded_blob_url 
            db.session.commit()
            
            if old_url and "default_avatar" not in old_url:
                try: vercel_blob.delete_blob(old_url)
                except Exception: pass

        # 5. Generate QR Code
        qr_b64 = generate_qr_base64(participant.roll_no)
        
        # 6. Render Card
        inst_name = participant.institute.name
        inst_abbr = "BBS-UTECH" if "Benazir" in inst_name or "BBS" in inst_name else inst_name[:10].upper()
            
        card_html = render_template('card.html', 
            name=participant.full_name,
            institute_name=inst_name,
            institute_abbr=inst_abbr,
            photo_url=participant.photo_blob_url, # Will be None if removed
            qr_code=qr_b64,
            roll_no=participant.roll_no
        )

        return jsonify({"success": True, "html": card_html})

    except Exception as e:
        db.session.rollback()
        if uploaded_blob_url:
            try: vercel_blob.delete_blob(uploaded_blob_url)
            except Exception: pass
        print(f"🔥 GENERATION ERROR: {e}")
        return jsonify({"error": "An internal error occurred."}), 500
    

@app.route('/api/verify', methods=['GET', 'POST'])
def verify_qr():
    """Endpoint for mobile app scanner to verify participant."""
    if request.method == 'GET':
        scanned_roll_no = request.args.get('roll_no')
    else:
        data = request.get_json(silent=True) or {}
        scanned_roll_no = data.get('roll_no')

    if not scanned_roll_no:
        return jsonify({"success": False, "message": "No Roll Number provided"}), 400

    # Query directly by unique roll_no
    participant = Participant.query.filter_by(roll_no=scanned_roll_no, status='active').first()

    if not participant:
        return jsonify({"success": False, "message": "Invalid or inactive registration."}), 404

    return jsonify({
        "success": True,
        "message": "Participant Verified",
        "data": {
            "name": participant.full_name,
            "roll_no": participant.roll_no,
            "institute": participant.institute.name
        }
    })

# --- Admin Route ---
@app.route('/api/admin/update-participant', methods=['POST'])
def update_participant():
    """Admin route to update participant details (Name, Institute, Photo)."""
    uploaded_blob_url = None
    try:
        roll_no = request.form.get('roll_no', '').strip()
        new_name = request.form.get('full_name', '').strip()
        new_institute_id = request.form.get('institute_id', type=int)
        new_status = request.form.get('status', 'active')

        if not roll_no:
            return jsonify({"error": "Roll No is required to identify the participant."}), 400

        participant = Participant.query.filter_by(roll_no=roll_no).first()
        if not participant:
            return jsonify({"error": "Participant with this Roll No not found."}), 404

        if new_name:
            participant.full_name = new_name
        if new_institute_id:
            inst = Institute.query.get(new_institute_id)
            if not inst:
                return jsonify({"error": "New Institute not found."}), 400
            participant.institute_id = new_institute_id
        if new_status in ['active', 'revoked', 'pending']:
            participant.status = new_status

        file = request.files.get('photo')
        if file and file.filename:
            ext = os.path.splitext(secure_filename(file.filename))[1].lower()
            if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
                return jsonify({"error": "Invalid image format."}), 400

            try:
                img = Image.open(file.stream)
                img.verify()
                file.stream.seek(0, os.SEEK_END)
                if file.stream.tell() > 4.5 * 1024 * 1024:
                    return jsonify({"error": "Image too large (Max 4.5MB)."}), 400
                file.stream.seek(0)
            except Exception:
                return jsonify({"error": "Corrupted image file."}), 400

            blob_result = vercel_blob.put_blob(
                f"participants/{secrets.token_hex(8)}{ext}",
                file.read(),
                access="public",
                content_type=file.content_type
            )
            uploaded_blob_url = blob_result.url
            
            old_photo_url = participant.photo_blob_url
            participant.photo_blob_url = uploaded_blob_url
            
            if old_photo_url and "default_avatar" not in old_photo_url:
                try: vercel_blob.delete_blob(old_photo_url)
                except Exception: pass

        db.session.commit()

        return jsonify({
            "success": True,
            "message": f"Participant {roll_no} updated successfully.",
            "participant": {
                "roll_no": participant.roll_no,
                "name": participant.full_name,
                "institute": participant.institute.name,
                "status": participant.status
            }
        })

    except Exception as e:
        db.session.rollback()
        if uploaded_blob_url:
            try: vercel_blob.delete_blob(uploaded_blob_url)
            except Exception: pass
        print(f"🔥 UPDATE ERROR: {e}")
        return jsonify({"error": "An internal error occurred during update."}), 500

# --- CLI Commands ---
@app.cli.command("add-participant")
@click.argument("name")
@click.argument("roll_no")
@click.argument("institute_id", type=int)
def add_participant(name, roll_no, institute_id):
    """Adds a participant: flask add-participant 'John Doe' '2K22/CS/01' 1"""
    inst = Institute.query.get(institute_id)
    if not inst:
        print(f"Institute ID {institute_id} not found.")
        return
    
    try:
        p = Participant(
            full_name=name, 
            roll_no=roll_no, 
            institute_id=institute_id
        )
        db.session.add(p)
        db.session.commit()
        print(f"Successfully added {name} (Roll: {roll_no}) to {inst.name}.")
    except Exception as e:
        db.session.rollback()
        print(f"Error adding participant (check for duplicate roll_no): {e}")

app.config['SESSION_PERMANENT'] = False
app.config['SESSION_COOKIE_SECURE'] = False # Set to True if using HTTPS (Vercel)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'expo2026')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            # Session dies when browser closes (because SESSION_PERMANENT = False)
            return redirect(url_for('admin_portal'))
        else:
            flash('Invalid password', 'error')
    return render_template('login.html') # We will create this simple form

@app.route('/logout')
def logout():
    session.clear() # Instantly destroys the session cookie
    return redirect(url_for('login'))

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/certificate')
@login_required
def admin_portal():
    """Shows the admin dashboard to download the ZIP."""
    return render_template('certificate_main.html')

@app.route('/certificate/download-all', methods=['GET'])
@login_required
def download_all_certificates():
    """Generates PDFs using Playwright and returns a ZIP."""
    participants = Participant.query.filter_by(status='active').all()
    
    if not participants:
        flash('No active participants found.', 'error')
        return redirect(url_for('admin_portal'))

    zip_buffer = io.BytesIO()

    with sync_playwright() as p:
        # Launch headless browser
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for p in participants:
                # 1. Render the HTML for this specific participant
                inst_name = p.institute.name
                inst_abbr = "BBS-UTECH" if "Benazir" in inst_name or "BBS" in inst_name else inst_name[:10].upper()
                
                html_content = render_template('certificate.html',
                    name=p.full_name,
                    roll_no=p.roll_no,
                    institute_name=inst_name,
                    institute_abbr=inst_abbr,
                    is_pdf_mode=True # We will use this to hide the download button in the PDF
                )

                # 2. Load HTML into Playwright
                page.set_content(html_content)
                
                # 3. Wait for Google Fonts and images to load
                page.wait_for_timeout(1000) 

                # 4. Find the certificate container and take a screenshot
                cert_element = page.locator('#certificate-node')
                
                # Save to temp file
                temp_pdf_path = tempfile.mktemp(suffix='.pdf')
                
                # Playwright can generate PDFs directly from the page
                # We set the format to A4 Landscape and use the background
                page.pdf(
                    path=temp_pdf_path, 
                    format="A4", 
                    landscape=True, 
                    print_background=True
                )

                # 5. Add to ZIP
                safe_roll = p.roll_no.replace('/', '-')
                with open(temp_pdf_path, 'rb') as f:
                    zip_file.writestr(f"Certificate-{safe_roll}.pdf", f.read())
                
                os.remove(temp_pdf_path)

        browser.close()

    zip_buffer.seek(0)
    return Response(
        zip_buffer.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': 'attachment; filename=All-Certificates.zip'}
    )



def get_base64_image(filepath):
    """Reads a static image and converts it to a Base64 Data URI."""
    try:
        with open(filepath, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            # Determine MIME type based on extension
            ext = os.path.splitext(filepath)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            return f"data:{mime};base64,{encoded_string}"
    except Exception as e:
        print(f"Error loading image {filepath}: {e}")
        return ""

@app.route('/api/download-card/<roll_no>')
def download_card_image(roll_no):
    """Generates a high-res PNG of the card using a headless browser."""
    participant = Participant.query.filter_by(roll_no=roll_no, status='active').first()
    if not participant:
        return "Participant not found", 404

    # 1. Generate QR and Data
    qr_b64 = generate_qr_base64(participant.roll_no)
    inst_name = participant.institute.name
    inst_abbr = "BBS-UTECH" if "Benazir" in inst_name or "BBS" in inst_name else inst_name[:10].upper()

    # 2. Render the HTML template to a string
    html_content = render_template('card.html', 
        name=participant.full_name,
        institute_name=inst_name,
        institute_abbr=inst_abbr,
        photo_url=participant.photo_blob_url,
        qr_code=qr_b64,
        roll_no=participant.roll_no
    )

    # 3. CRITICAL FIX: Convert static logos to Base64 and inject them into the HTML
    # This guarantees Playwright does not need to fetch anything for the logos.
    static_path = os.path.join(app.root_path, 'static')
    
    uni_logo_b64 = get_base64_image(os.path.join(static_path, 'uni_logo.png'))
    event_logo_b64 = get_base64_image(os.path.join(static_path, 'event_logo.png'))

    # Replace the relative URLs with the Base64 data URIs
    html_content = html_content.replace(
        url_for('static', filename='uni_logo.png'), 
        uni_logo_b64
    )
    html_content = html_content.replace(
        url_for('static', filename='event_logo.png'), 
        event_logo_b64
    )

    # 4. Use Playwright to screenshot it
    with sync_playwright() as p:
        browser = p.chromium.launch()
        
        # High resolution settings
        context = browser.new_context(
            viewport={'width': 500, 'height': 800},
            device_scale_factor=3 
        )
        page = context.new_page()
        
        # Load the HTML with the embedded logos
        page.set_content(html_content, wait_until='networkidle')
        
        # Wait for the participant photo (from Vercel Blob) to load
        page.wait_for_timeout(1500) 
        
        # Screenshot the specific card element
        card_element = page.locator('#participant-card')
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        card_element.screenshot(path=temp_file.name, type='png')
        
        browser.close()

    # 5. Send the file to the user
    return send_file(
        temp_file.name, 
        mimetype='image/png', 
        as_attachment=True, 
        download_name=f"Expo-Card-{participant.roll_no}.png"
    )

if __name__ == '__main__':
    app.run(debug=True)
    # app.run(host="0.0.0.0", port=5000, debug=True)