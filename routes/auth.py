from flask import Blueprint, render_template, redirect, url_for, request, jsonify, session
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash
from extensions import db
from models import User

# Membuat Blueprint dengan nama 'auth'
bp = Blueprint('auth', __name__)


@bp.after_request
def _no_store_halaman_auth(response):
    """Larang browser menyimpan cache halaman login & register (no-store).

    Tanpa ini, tombol Back setelah login menampilkan halaman login BASI dari
    cache browser — padahal session masih aktif — lalu klik "Daftar" terasa
    "nyasar" ke dashboard (redirect is_authenticated). Dengan no-store, Back
    memaksa browser minta ulang ke server sehingga langsung ke-redirect ke
    dashboard dan halaman login palsu tak pernah terlihat."""
    if request.endpoint in ("auth.login", "auth.register"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _do_login(user):
    """Blok sukses login yang dipakai login manual dan registrasi.

    session.permanent=True mengaktifkan absolute timeout PERMANENT_SESSION_LIFETIME
    (8 jam, di app.py) — user otomatis login ulang setelahnya, apa pun role-nya.
    vm_username/vm_role di-cache di session supaya load_user() tetap bisa
    merekonstruksi user walau MySQL sekejap mati (lihat app.py).
    """
    login_user(user)
    session.permanent = True
    session["vm_username"] = user.username
    session["vm_role"] = user.role
    return redirect(url_for("dashboard.index"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    # Jika user sudah login, langsung lempar ke dashboard
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index")) # Perhatikan: 'dashboard.index'

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Cari user di database
        user = db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()

        # Cek password
        if user and check_password_hash(user.password_hash, password):
            return _do_login(user)

        return render_template("login.html", error="Username atau password salah")

    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Registrasi mandiri: siapa pun bisa membuat akun sendiri. Role DIPAKSA
    'guest' di server (tidak ada input role dari form) — naik ke admin tetap
    hanya lewat manajemen user oleh admin."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        error = None
        if not (3 <= len(username) <= 60):  # kolom username = String(60)
            error = "Username harus 3-60 karakter."
        elif len(password) < 4:  # samakan aturan minimal dengan /api/profile
            error = "Password minimal 4 karakter."
        elif password != password2:
            error = "Konfirmasi password tidak sama."
        elif db.session.execute(
                db.select(User).filter_by(username=username)).scalar_one_or_none():
            error = "Username sudah dipakai."
        if error:
            return render_template("register.html", error=error, username=username)

        user = User(username=username,
                    password_hash=generate_password_hash(password),
                    role="guest")
        db.session.add(user)
        db.session.commit()
        return _do_login(user)

    return render_template("register.html")

@bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("vm_username", None)
    session.pop("vm_role", None)
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Self-service profile edit — semua user bisa ubah username + password sendiri.
# Tidak bisa ubah role (itu tetap hak admin via /api/users).
# Wajib verifikasi current_password kalau mau ganti password — biar tidak
# bisa dibajak via session yang ditinggal terbuka.
# ---------------------------------------------------------------------------
@bp.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    return jsonify({
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
    })


@bp.route("/api/profile", methods=["PATCH"])
@login_required
def update_profile():
    data = request.get_json(silent=True) or {}

    user = db.session.get(User, current_user.id)
    if not user:
        return jsonify({"status": "ERROR", "message": "User tidak ditemukan"}), 404

    new_username = (data.get("username") or "").strip()
    new_password = data.get("new_password") or ""
    current_password = data.get("current_password") or ""

    # Wajib current_password kalau ada perubahan
    wants_username_change = new_username and new_username != user.username
    wants_password_change = bool(new_password)

    if not (wants_username_change or wants_password_change):
        return jsonify({"status": "ERROR", "message": "Tidak ada perubahan."}), 400

    if not check_password_hash(user.password_hash, current_password):
        return jsonify({"status": "ERROR", "message": "Password saat ini salah."}), 403

    if wants_username_change:
        existing = db.session.execute(
            db.select(User).filter_by(username=new_username)
        ).scalar_one_or_none()
        if existing and existing.id != user.id:
            return jsonify({"status": "ERROR", "message": "Username sudah dipakai."}), 400
        user.username = new_username
        session["vm_username"] = new_username

    if wants_password_change:
        if len(new_password) < 4:
            return jsonify({"status": "ERROR", "message": "Password minimal 4 karakter."}), 400
        user.password_hash = generate_password_hash(new_password)

    db.session.commit()
    return jsonify({
        "status": "SUCCESS",
        "username": user.username,
        "role": user.role,
    }), 200
