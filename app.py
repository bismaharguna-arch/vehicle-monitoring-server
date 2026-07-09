import os
import logging
from datetime import timedelta
from flask import Flask, send_from_directory, jsonify, render_template, request, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from werkzeug.exceptions import RequestEntityTooLarge
from sqlalchemy.exc import OperationalError, DBAPIError, InterfaceError
from dotenv import load_dotenv

# 1. Import Komponen yang sudah kita pisah
from extensions import db, login_manager, socketio
from models import User

# 2. Import Blueprints (Logika Website) dari folder routes
from routes import dashboard, riwayat, auth, api, user_mgt

# Load file .env
load_dotenv()

log = logging.getLogger(__name__)

def create_app():
    # Setup Aplikasi Flask
    app = Flask(__name__)
    CORS(app) # Agar bisa diakses dari beda domain/port (Laptop A)

    # Konfigurasi Dasar
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-123")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "mysql+pymysql://root:@127.0.0.1:3306/vehicle_monitoring")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # API key detector: kalau di-set, endpoint deteksi & heartbeat wajib bawa
    # header X-API-Key yang cocok. Kalau kosong -> pengecekan dimatikan.
    app.config["DETECTOR_API_KEY"] = os.getenv("DETECTOR_API_KEY", "")
    # Cap upload 5 MB (foto detector resize ke ~50-300 KB, ini headroom 10x).
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    # Cookie sesi: SameSite=Lax mencegah cookie ikut terkirim dari situs lain
    # (mitigasi CSRF). Secure sengaja TIDAK dipaksa True karena app jalan via
    # HTTP (bukan HTTPS) — kalau True, cookie tak terkirim & login gagal.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # Sesi rolling 8 jam sebagai backstop server-side. Idle timeout yang
    # sebenarnya (auto-logout kalau user ditinggal pergi) ditangani di sisi
    # browser lewat deteksi aktivitas asli di base.html — karena polling
    # dashboard tiap 10 detik bikin idle timeout server tak pernah kepicu.
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True

    # Werkzeug default return HTML page utk 413; IoT client butuh JSON.
    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_e):
        return jsonify({
            "status": "ERROR",
            "message": "Payload terlalu besar (maks 5 MB). Resize foto dulu.",
        }), 413
    
    # Inisialisasi Database, Login Manager, & SocketIO
    db.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(app)
    login_manager.login_view = "auth.login" # Kalau belum login, lempar ke sini

    # User Loader (Syarat Flask-Login)
    @login_manager.user_loader
    def load_user(user_id):
        # Kalau MySQL mati, JANGAN paksa logout (yang bikin user kelempar ke
        # halaman login). Rekonstruksi user minimal dari cache session supaya
        # halaman tetap bisa render dalam mode read-only + banner "DB offline".
        try:
            return db.session.get(User, int(user_id))
        except (OperationalError, InterfaceError, DBAPIError) as e:
            log.warning("load_user gagal (DB offline?): %s", e)
            db.session.rollback()
            cached_name = session.get("vm_username")
            if cached_name:
                ghost = User(
                    id=int(user_id),
                    username=cached_name,
                    role=session.get("vm_role", "guest"),
                )
                return ghost
            return None

    # ===== Graceful degradation kalau MySQL mati =====
    # SQLAlchemy lempar OperationalError/InterfaceError saat konek/query gagal.
    # API routes balas 503 JSON; HTML routes render halaman "DB offline".
    @app.errorhandler(OperationalError)
    @app.errorhandler(InterfaceError)
    @app.errorhandler(DBAPIError)
    def _db_offline(e):
        log.warning("DB error untuk %s %s: %s", request.method, request.path, e)
        try:
            db.session.rollback()
        except Exception:
            pass
        if request.path.startswith("/api/"):
            return jsonify({
                "status": "ERROR",
                "code": "DB_UNAVAILABLE",
                "message": "Database sedang tidak tersedia. Coba lagi sebentar lagi.",
            }), 503
        return render_template("db_offline.html"), 503
    
    # Route Khusus untuk Membuka File Foto (Uploads)
    @app.route("/uploads/detections/<path:filename>")
    def uploaded_file(filename):
        path = os.path.join(os.getcwd(), "uploads", "detections")
        return send_from_directory(path, filename)

    # 3. DAFTARKAN BLUEPRINTS (Pasang fitur-fiturnya)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(riwayat.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(user_mgt.bp)

    # 4. Setup Database & User Default
    # Bungkus dengan try/except: kalau MySQL mati saat boot, server tetap jalan
    # (halaman akan render 503 "DB offline" sampai DB hidup lagi).
    with app.app_context():
        try:
            db.create_all()

            # Cek dan Buat User: ADMIN (Full Akses)
            if not db.session.execute(db.select(User).filter_by(username="admin")).scalar_one_or_none():
                db.session.add(User(username="admin", password_hash=generate_password_hash("admin123"), role="admin"))
                print(">>> Akun 'admin' dibuat (Pass: admin123)")

            db.session.commit()
        except (OperationalError, InterfaceError, DBAPIError) as e:
            log.warning(">>> DB tidak tersedia saat boot — skip create_all & seeding: %s", e)
            print(">>> [WARNING] MySQL belum jalan. Server tetap up, halaman akan 503 sampai DB hidup.")
            try:
                db.session.rollback()
            except Exception:
                pass

    return app

# Jalankan Aplikasi
if __name__ == "__main__":
    app = create_app()
    # Host 0.0.0.0 agar bisa diakses Laptop A lewat Wi-Fi
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)