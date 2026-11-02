import os
import secrets
import hashlib
import base64, requests
import click
from io import BytesIO
from datetime import datetime, timezone
from dotenv import load_dotenv

import qrcode
from flask import Flask, render_template, request, jsonify, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.utils import secure_filename
from PIL import Image
from vercel.blob import sync as blob 

load_dotenv()  # Load environment variables from .env file

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

from sqlalchemy.dialects.postgresql import ENUM
import enum

# 1. Define the Enum for Status
class ParticipantStatus(enum.Enum):
    active = "active"
    revoked = "revoked"
    pending = "pending"

# --- Models ---
class Institute(db.Model):
    __tablename__ = 'institutes'
    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    city = db.Column(db.String(100), nullable=True) # <--- ADDED THIS
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())
    
    participants = db.relationship('Participant', backref='institute')

class Participant(db.Model):
    __tablename__ = 'participants'
    id = db.Column(db.BigInteger, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    roll_no = db.Column(db.String(50), nullable=False, unique=True, index=True) # <--- ADDED
    institute_id = db.Column(db.BigInteger, db.ForeignKey('institutes.id'), nullable=False)
    photo_blob_url = db.Column(db.String(500), nullable=True)
    verification_token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    
    status = db.Column(
        ENUM(ParticipantStatus, name='participant_status', create_type=False), 
        default=ParticipantStatus.active, 
        nullable=False
    )
    
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())
    
    # REMOVED the __table_args__ unique constraint on (full_name, institute_id)
# --- Helpers ---
def generate_verification_token():
    """Generates a secure token and its hash."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash

def generate_qr_base64(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()

@app.route('/api/admin/update-participant', methods=['POST'])
def update_participant():
    """Admin route to update participant details (Name, Institute, Photo)."""
    uploaded_blob_url = None
    try:
        roll_no = request.form.get('roll_no', '').strip()
        new_name = request.form.get('full_name', '').strip()
        new_institute_id = request.form.get('institute_id', type=int)
        new_status = request.form.get('status', 'active') # e.g., 'active', 'revoked'

        if not roll_no:
            return jsonify({"error": "Roll No is required to identify the participant."}), 400

        # Find participant by unique Roll No
        participant = Participant.query.filter_by(roll_no=roll_no).first()
        if not participant:
            return jsonify({"error": "Participant with this Roll No not found."}), 404

        # Update text fields if provided
        if new_name:
            participant.full_name = new_name
        if new_institute_id:
            # Verify new institute exists
            inst = Institute.query.get(new_institute_id)
            if not inst:
                return jsonify({"error": "New Institute not found."}), 400
            participant.institute_id = new_institute_id
        if new_status in ['active', 'revoked', 'pending']:
            participant.status = new_status

        # Update Photo if a new file is provided
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

            # Upload new photo to Blob
            blob_result = blob.put_blob(
                f"participants/{secrets.token_hex(8)}{ext}",
                file.read(),
                access="public",
                content_type=file.content_type
            )
            uploaded_blob_url = blob_result.url
            
            # Delete old photo from Blob to save space (Optional but recommended)
            old_photo_url = participant.photo_blob_url
            participant.photo_blob_url = uploaded_blob_url
            
            if old_photo_url and "default_avatar" not in old_photo_url:
                try: blob.delete_blob(old_photo_url)
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
        # Cleanup orphaned new blob if DB fails
        if uploaded_blob_url:
            try: blob.delete_blob(uploaded_blob_url)
            except Exception: pass
        print(f"🔥 UPDATE ERROR: {e}")
        return jsonify({"error": "An internal error occurred during update."}), 500


# --- Routes ---
@app.route('/')
def index():
    """Loads the frontend with active institutes."""
    institutes = Institute.query.filter_by(is_active=True).order_by(Institute.name).all()
    return render_template('index.html', institutes=institutes)

@app.route('/api/generate-card', methods=['POST'])
def generate_card():
    """Verifies participant exists, optionally updates photo, and generates card."""
    uploaded_blob_url = None
    try:
        # 1. Get Input
        full_name = request.form.get('full_name', '').strip()
        institute_id = request.form.get('institute_id', type=int)
        roll_no = request.form.get('roll_no', '').strip()
        
        if not full_name or not institute_id:
            return jsonify({"error": "Name and Institute are required."}), 400

        # 2. Check if participant exists in DB (Admin pre-loaded)
        participant = Participant.query.filter(
            db.func.lower(Participant.full_name) == full_name.lower(), 
            Participant.roll_no == roll_no,
            Participant.status == 'active'
        ).first()

        # THE CRITICAL FIX: If not in DB, reject immediately
        if not participant:
            return jsonify({"error": "Participant not registered. Please contact the admin."}), 404

        # 3. Handle Optional Photo Upload (Override existing)
        file = request.files.get('photo')
        if file and file.filename:
            filename = secure_filename(file.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
                return jsonify({"error": "Invalid image format."}), 400

            # Validate image integrity and size
            try:
                img = Image.open(file.stream)
                img.verify()
                file.stream.seek(0, os.SEEK_END)
                if file.stream.tell() > 4.5 * 1024 * 1024:
                    return jsonify({"error": "Image too large (Max 4.5MB)."}), 400
                file.stream.seek(0)
            except Exception:
                return jsonify({"error": "Corrupted image file."}), 400

            # Upload to Blob
            blob_result = blob.put_blob(
                f"participants/{secrets.token_hex(8)}{ext}",
                file.read(),
                access="public",
                content_type=file.content_type
            )
            uploaded_blob_url = blob_result.url
            participant.photo_blob_url = uploaded_blob_url # Update DB record

        # 4. Generate NEW Token & QR for this specific card generation
        # (We regenerate to ensure the QR is fresh and tied to this download)
        raw_token, token_hash = generate_verification_token()
        participant.verification_token_hash = token_hash
        
        db.session.commit()

        # 5. Generate QR URL
        verify_url = url_for('verify_qr', _external=True) + f"?token={raw_token}"
        qr_b64 = generate_qr_base64(verify_url)

        # 6. Return data to frontend
        return jsonify({
            "success": True,
            "redirect_url": url_for('show_card', roll_no=participant.roll_no)
        })

    except Exception as e:
        db.session.rollback()
        # Cleanup orphaned blob if DB fails
        if uploaded_blob_url:
            try: blob.delete_blob(uploaded_blob_url)
            except Exception: pass
        app.logger.error(f"Card Generation Error: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

@app.route('/card/<roll_no>')
def show_card(roll_no):
    participant = Participant.query.filter_by(roll_no=roll_no, status='active').first()
    
    if not participant:
        return "Participant not found or inactive", 404
        
    # Generate a secure random token
    raw_token = secrets.token_urlsafe(32)
    
    # Hash the token and save it to the database
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    participant.verification_token_hash = token_hash
    db.session.commit()
    
    # Create the URL that the mobile app will hit when scanning
    # (Using your production URL later, e.g., https://your-app.vercel.app/api/verify)
    verify_url = url_for('verify_qr', _external=True) + f"?token={raw_token}"
    
    # Generate the QR Code image
    qr_b64 = generate_qr_base64(verify_url)
    
    # Map abbreviation
    inst_name = participant.institute.name
    inst_abbr = "BBS-UTECH" if "Benazir" in inst_name or "BBS" in inst_name else inst_name[:10].upper()
        
    return render_template('card.html', 
        name=participant.full_name,
        institute_name=inst_name,
        institute_abbr=inst_abbr,
        photo_url=participant.photo_blob_url,
        qr_code=qr_b64
    )

@app.route('/api/verify', methods=['GET', 'POST'])
def verify_qr():
    # Accept GET (if scanned by a generic phone camera) or POST (from the scanner app)
    if request.method == 'GET':
        raw_token = request.args.get('token')
    else:
        data = request.get_json()
        raw_token = data.get('token') if data else None

    if not raw_token:
        return jsonify({"success": False, "message": "No token provided"}), 400

    # Hash the incoming token to compare against the DB
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    
    # Find the participant
    participant = Participant.query.filter_by(verification_token_hash=token_hash, status='active').first()

    if not participant:
        return jsonify({"success": False, "message": "Invalid or expired token"}), 404

    # Return verified data
    return jsonify({
        "success": True,
        "message": "Participant Verified",
        "data": {
            "name": participant.full_name,
            "roll_no": participant.roll_no,
            "institute": participant.institute.name
        }
    })

@app.cli.command("add-participant")
@click.argument("name")
@click.argument("roll_no") # <--- ADDED
@click.argument("institute_id", type=int)
def add_participant(name, roll_no, institute_id):
    """Adds a participant: flask add-participant 'John Doe' '2K22/CS/01' 1"""
    inst = Institute.query.get(institute_id)
    if not inst:
        print(f"Institute ID {institute_id} not found.")
        return
    
    _, token_hash = generate_verification_token()
    try:
        p = Participant(
            full_name=name, 
            roll_no=roll_no, 
            institute_id=institute_id, 
            verification_token_hash=token_hash
        )
        db.session.add(p)
        db.session.commit()
        print(f"Successfully added {name} (Roll: {roll_no}) to {inst.name}.")
    except Exception as e:
        db.session.rollback()
        print(f"Error: {e}")
        
if __name__ == '__main__':
    app.run(debug=True)