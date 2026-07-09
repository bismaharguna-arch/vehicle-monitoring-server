from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from extensions import db
from models import User

bp = Blueprint('user_mgt', __name__)

@bp.route("/users")
@login_required
def index():
    if current_user.role != 'admin':
        return "Akses Ditolak: Hanya Admin yang dapat mengakses halaman ini.", 403
    users = db.session.query(User).order_by(User.id.asc()).all()
    return render_template("user_mgt.html", users=users)


@bp.route("/partials/users")
@login_required
def partial_users():
    """HTMX target: return tbody fragment user list."""
    if current_user.role != 'admin':
        return "Forbidden", 403
    users = db.session.query(User).order_by(User.id.asc()).all()
    return render_template("partials/_user_rows.html", users=users)

@bp.route("/api/users", methods=["GET"])
@login_required
def get_users():
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak"}), 403
        
    users = db.session.query(User).all()
    out = []
    for u in users:
        out.append({
            "id": u.id,
            "username": u.username,
            "role": u.role
        })
    return jsonify(out)

@bp.route("/api/users", methods=["POST"])
@login_required
def add_user():
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak"}), 403

    data = request.get_json()
    if not data or not data.get("username") or not data.get("password") or not data.get("role"):
        return jsonify({"status": "ERROR", "message": "Data tidak lengkap"}), 400

    username = data["username"].strip()
    
    # Check if username exists
    existing = db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()
    if existing:
        return jsonify({"status": "ERROR", "message": "Username sudah terdaftar"}), 400

    hashed_pw = generate_password_hash(data["password"])
    
    new_user = User(
        username=username,
        password_hash=hashed_pw,
        role=data["role"]
    )
    
    db.session.add(new_user)
    db.session.commit()
    
    return jsonify({"status": "SUCCESS"}), 201

@bp.route("/api/users/<int:user_id>", methods=["PATCH"])
@login_required
def update_user(user_id):
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak"}), 403

    data = request.get_json()
    user = db.session.get(User, user_id)
    
    if not user:
        return jsonify({"status": "ERROR", "message": "User tidak ditemukan"}), 404

    # Pastikan admin tidak bisa mengubah perannya sendiri menjadi non-admin sembarangan, 
    #   atau kalau mau dibiarkan juga bisa.
    
    if "username" in data and data["username"].strip():
        new_username = data["username"].strip()
        if new_username != user.username:
            existing = db.session.execute(db.select(User).filter_by(username=new_username)).scalar_one_or_none()
            if existing:
                return jsonify({"status": "ERROR", "message": "Username sudah dipakai"}), 400
        user.username = new_username
        
    if "password" in data and data["password"].strip():
        user.password_hash = generate_password_hash(data["password"])
        
    if "role" in data and data["role"].strip():
        # Jangan sampai admin mengubah role dirinya sendiri menjadi guest dan kehilangan akses
        if user.id == current_user.id and data["role"] != 'admin':
            return jsonify({"status": "ERROR", "message": "Anda tidak bisa menghapus status admin Anda sendiri"}), 400
        user.role = data["role"]
        
    db.session.commit()
    return jsonify({"status": "SUCCESS"}), 200

@bp.route("/api/users/<int:user_id>", methods=["DELETE"])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"status": "ERROR", "message": "User tidak ditemukan"}), 404
        
    # Cegah admin menghapus dirinya sendiri
    if user.id == current_user.id:
        return jsonify({"status": "ERROR", "message": "Anda tidak bisa menghapus akun Anda sendiri"}), 400

    db.session.delete(user)
    db.session.commit()
    
    return jsonify({"status": "SUCCESS"}), 200
