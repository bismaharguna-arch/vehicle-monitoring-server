import os
import logging
import socket
import hmac
import cv2
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, url_for, Response, current_app
from flask_login import login_required
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
from extensions import db, socketio
from models import Deteksi, Mobil, Motor
from flask_login import login_required, current_user

bp = Blueprint('api', __name__)
log = logging.getLogger(__name__)

# Konfigurasi Folder Upload (Mengarah ke folder uploads di root project)
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads", "detections")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===== Enum normalisasi (kontrak v1) =====
# Kanonik: bensin / listrik (Indonesia). Legacy English di-coerce + log warning.
FUEL_CANONICAL = {"bensin", "listrik", "unknown"}
FUEL_LEGACY_MAP = {"gasoline": "bensin", "electric": "listrik"}

VTYPE_CANONICAL = {"mobil", "motor", "unknown"}
VTYPE_LEGACY_MAP = {"car": "mobil", "motorcycle": "motor"}

# Toleransi clock skew antara detector & server. Selisih lebih dari ini di-log
# tapi tidak menolak baris (deteksi tetap masuk).
CLOCK_SKEW_TOLERANCE = timedelta(hours=1)

# Batas panjang plat (samakan dengan kolom plat_nomor VARCHAR(32) di master).
MAX_PLATE_LEN = 32
# Ekstensi foto yang diterima (detektor kirim JPG). Selain ini ditolak 415.
ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# ===== Auto-registrasi detector (Opsi A) =====
# Detector kirim heartbeat berkala -> web simpan URL preview terkini di memori.
# Dashboard baca dari sini (fallback ke env DETECTOR_PREVIEW_URL). Menghapus
# kebutuhan hardcode IP detector di .env saat IP-nya ganti-ganti.
DEFAULT_PREVIEW_PORT = 5001
DEFAULT_PREVIEW_PATH = "/preview"
# Dianggap offline kalau tak ada heartbeat melebihi ini (heartbeat ~15 dtk).
DETECTOR_STALE_SECONDS = 60

_detector_state = {"preview_url": None, "last_seen": None, "source": None}


def _remember_detector(url, source):
    """Catat URL preview detector + waktu. 'register' (heartbeat eksplisit)
    selalu menang; 'detection' cuma mengisi kalau belum ada register segar."""
    now = datetime.now()
    cur = _detector_state
    if source == "detection" and cur["source"] == "register" and cur["last_seen"] \
            and (now - cur["last_seen"]).total_seconds() < DETECTOR_STALE_SECONDS:
        return
    cur["preview_url"] = url
    cur["last_seen"] = now
    cur["source"] = source


def get_detector_preview_url():
    """URL preview detector terkini kalau masih segar; None kalau stale/kosong
    (caller fallback ke env / default)."""
    cur = _detector_state
    if cur["preview_url"] and cur["last_seen"] \
            and (datetime.now() - cur["last_seen"]).total_seconds() < DETECTOR_STALE_SECONDS:
        return cur["preview_url"]
    return None


def _web_lan_ip():
    """IP LAN mesin web ini. Trik UDP (tidak benar-benar kirim data) supaya dapat
    IP interface aktif, bukan 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_preview_url():
    """Fallback saat belum ada registrasi & env tak di-set: pakai IP LAN mesin
    web sendiri + port preview default. Cocok saat detector satu mesin dengan
    web (kasus umum) — ikut IP terkini otomatis, tanpa hardcode di .env."""
    return f"http://{_web_lan_ip()}:{DEFAULT_PREVIEW_PORT}{DEFAULT_PREVIEW_PATH}"


def _check_api_key():
    """Verifikasi header X-API-Key untuk endpoint detector.
    Return None kalau lolos; (response, 401) kalau ditolak.
    Kalau DETECTOR_API_KEY tidak di-set (kosong) -> pengecekan dimatikan."""
    expected = current_app.config.get("DETECTOR_API_KEY") or ""
    if not expected:
        return None  # tidak dikonfigurasi -> endpoint terbuka (mode lama)
    got = request.headers.get("X-API-Key", "")
    # compare_digest -> banding waktu-konstan (hindari timing attack)
    if not hmac.compare_digest(got, expected):
        log.warning("Tolak POST detector: X-API-Key salah/absen (dari %s)",
                    request.remote_addr)
        return jsonify({"status": "ERROR", "code": "UNAUTHORIZED",
                        "message": "API key tidak valid"}), 401
    return None


def _normalize_fuel(raw):
    if raw is None:
        return "unknown"
    v = str(raw).strip().lower()
    if v in FUEL_LEGACY_MAP:
        log.warning("legacy is_electric value %r dipetakan ke %r", v, FUEL_LEGACY_MAP[v])
        return FUEL_LEGACY_MAP[v]
    if v in FUEL_CANONICAL:
        return v
    log.warning("is_electric value %r tidak dikenal, coerce ke 'unknown'", v)
    return "unknown"


def _normalize_vtype(raw):
    if raw is None:
        return "unknown"
    v = str(raw).strip().lower()
    if v in VTYPE_LEGACY_MAP:
        log.warning("legacy vehicle_type value %r dipetakan ke %r", v, VTYPE_LEGACY_MAP[v])
        return VTYPE_LEGACY_MAP[v]
    if v in VTYPE_CANONICAL:
        return v
    log.warning("vehicle_type value %r tidak dikenal, coerce ke 'unknown'", v)
    return "unknown"


def _parse_detected_at(raw):
    """Parse ISO 8601 string -> naive local datetime.

    Detector kirim TZ-aware ISO (mis. '2026-05-08T02:06:08+07:00'). Kita konversi
    ke local timezone lalu strip tzinfo supaya comparable dengan kolom `timestamp`
    (yang juga naive local). Kalau invalid/empty -> None.
    """
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError) as e:
        log.warning("detected_at invalid (%r): %s", raw, e)
        return None
    if dt.tzinfo is not None:
        # Convert ke local TZ system, lalu jadikan naive
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _link_master(plate, vtype, fuel):
    """Find-or-create master kendaraan untuk deteksi BERPLAT.

    Return (id_mobil, id_motor). Dua-duanya None kalau tidak bisa dilink
    (plat kosong, atau tipe unknown). Aturan bahan bakar: nilai non-unknown
    terbaru menang.
    """
    if not plate:
        return None, None
    plate = str(plate).strip()
    if not plate:
        return None, None

    if vtype == "mobil":
        m = db.session.execute(
            db.select(Mobil).filter_by(plat_nomor=plate)
        ).scalar_one_or_none()
        if m is None:
            m = Mobil(plat_nomor=plate, bahan_bakar=fuel)
            db.session.add(m)
            db.session.flush()  # supaya m.id terisi
        elif fuel != "unknown":
            m.bahan_bakar = fuel
        return m.id, None

    if vtype == "motor":
        m = db.session.execute(
            db.select(Motor).filter_by(plat_nomor=plate)
        ).scalar_one_or_none()
        if m is None:
            m = Motor(plat_nomor=plate, bahan_bakar=fuel)
            db.session.add(m)
            db.session.flush()
        elif fuel != "unknown":
            m.bahan_bakar = fuel
        return None, m.id

    # tipe unknown walau berplat -> tidak dilink (pakai kolom fallback deteksi)
    return None, None


def _stats():
    """Hitung KPI bahan bakar dari tabel deteksi (kolom fallback selalu terisi)."""
    total = db.session.query(Deteksi).count()
    total_ev = db.session.query(Deteksi).filter_by(bahan_bakar="listrik").count()
    total_gas = db.session.query(Deteksi).filter_by(bahan_bakar="bensin").count()
    total_unknown = db.session.query(Deteksi).filter_by(bahan_bakar="unknown").count()
    return total, total_ev, total_gas, total_unknown

# =====================
# 1. CAMERA STREAMING
# =====================
def generate_frames():
    # 0 = Webcam Default. Kalau dipegang proses lain (detector), open() gagal
    # dan generator langsung exit -> browser dapat error event.
    camera = cv2.VideoCapture(0)
    try:
        if not camera.isOpened():
            log.warning("video_feed: cv2.VideoCapture(0) gagal open (kamera busy?)")
            return
        while True:
            success, frame = camera.read()
            if not success:
                break
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    finally:
        camera.release()


@bp.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# =====================
# 2. CREATE (Terima Data dari Laptop A)
# =====================
@bp.route("/api/detections", methods=["POST"])
def api_create_detection():
    denied = _check_api_key()
    if denied:
        return denied
    try:
        photo_filename = None

        # Ambil input (key kontrak tetap Inggris). Foto hanya lewat form-data.
        if request.is_json:
            data = request.get_json(silent=True) or {}
            plate = data.get("plate_number")
            fuel_raw = data.get("is_electric")
            vtype_raw = data.get("vehicle_type")
            conf_raw = data.get("confidence_score")
            detected_at_raw = data.get("detected_at")
            file = None
        else:
            plate = request.form.get("plate_number")
            fuel_raw = request.form.get("is_electric")
            vtype_raw = request.form.get("vehicle_type")
            conf_raw = request.form.get("confidence_score")
            detected_at_raw = request.form.get("detected_at")
            file = request.files.get("photo")

        # --- Validasi input (balas 400, bukan 500/503) ---
        # Field wajib harus ADA (nilai tak dikenal tetap boleh -> di-coerce 'unknown').
        if vtype_raw in (None, "") or fuel_raw in (None, ""):
            return jsonify({"status": "ERROR",
                            "message": "vehicle_type & is_electric wajib diisi"}), 400
        try:
            conf = float(conf_raw)
        except (TypeError, ValueError):
            return jsonify({"status": "ERROR",
                            "message": "confidence_score harus angka 0.0-1.0"}), 400
        conf = max(0.0, min(1.0, conf))  # clamp ke rentang wajar
        if plate and len(str(plate).strip()) > MAX_PLATE_LEN:
            return jsonify({"status": "ERROR",
                            "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400

        # --- Simpan foto (opsional) ---
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ALLOWED_PHOTO_EXT:
                return jsonify({"status": "ERROR",
                                "message": "Foto harus jpg/jpeg/png/gif/webp"}), 415
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            # Potong nama biar muat kolom (255) & tidak lewat batas path Windows.
            photo_filename = f"{ts}_{secure_filename(file.filename)}"[:150]
            file.save(os.path.join(UPLOAD_DIR, photo_filename))

        # Coerce ke enum kanonik (terima legacy English dari detector lama)
        is_electric = _normalize_fuel(fuel_raw)
        vtype = _normalize_vtype(vtype_raw)

        # Parse detected_at (kontrak v1, opsional)
        detected_at = _parse_detected_at(detected_at_raw)
        if detected_at is not None:
            skew = abs(datetime.now() - detected_at)
            if skew > CLOCK_SKEW_TOLERANCE:
                log.warning(
                    "Clock skew: detected_at=%s vs server now=%s (delta=%s)",
                    detected_at, datetime.now(), skew,
                )

        # Upsert master kendaraan (hanya untuk deteksi berplat)
        id_mobil, id_motor = _link_master(plate, vtype, is_electric)

        # Simpan transaksi deteksi. tipe_kendaraan & bahan_bakar tetap diisi
        # sebagai fallback (penting untuk deteksi tanpa plat) + jaga KPI/stats.
        det = Deteksi(
            confidence_score=conf,
            file_foto=photo_filename,
            waktu_deteksi=detected_at,
            tipe_kendaraan=vtype,
            bahan_bakar=is_electric,
            id_mobil=id_mobil,
            id_motor=id_motor,
        )
        db.session.add(det)
        db.session.commit()

        # Opsi A (fallback): catat IP detector dari sumber POST ini (port default)
        # supaya dashboard tetap dapat URL preview walau heartbeat belum dipasang.
        _remember_detector(
            f"http://{request.remote_addr}:{DEFAULT_PREVIEW_PORT}{DEFAULT_PREVIEW_PATH}",
            source="detection")

        # Broadcast to web clients via WebSocket (payload pakai key Inggris)
        total, total_ev, total_gas, total_unknown = _stats()

        # Display ts: pakai detected_at kalau ada, fallback ke server timestamp
        display_ts = det.waktu_deteksi or det.timestamp
        socketio.emit('new_detection', {
            'stats': {
                'total': total,
                'total_ev': total_ev,
                'total_gas': total_gas,
                'total_unknown': total_unknown
            },
            'new_detection': {
                'id': det.id,
                'timestamp': display_ts.strftime("%Y-%m-%d %H:%M:%S"),
                'detected_at': det.waktu_deteksi.strftime("%Y-%m-%d %H:%M:%S") if det.waktu_deteksi else None,
                'received_at': det.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                'plate_number': det.plat_nomor,
                'is_electric': det.bahan_bakar,
                'vehicle_type': det.tipe_kendaraan,
                'photo_url': f"/uploads/detections/{det.file_foto}" if det.file_foto else None,
                'is_corrected': False
            }
        })

        return jsonify({"status": "SUCCESS", "id": det.id}), 201

    except (OperationalError, InterfaceError, DBAPIError):
        # Biarkan handler global di app.py yang balas 503 DB_UNAVAILABLE
        raise
    except RequestEntityTooLarge:
        # Foto > MAX_CONTENT_LENGTH: biarkan handler global balas 413 (bukan 500)
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "ERROR", "message": str(e)}), 500


# =====================
# 2b. Detector auto-registrasi (Opsi A): heartbeat URL preview
# =====================
@bp.route("/api/detector/register", methods=["POST"])
def api_detector_register():
    """Detector kirim heartbeat berkala berisi PORT preview-nya. Host diambil
    dari IP sumber request (remote_addr) — jadi detector tak perlu tahu IP-nya
    sendiri, dan tak bisa daftarin URL milik host lain (mitigasi abuse)."""
    denied = _check_api_key()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        port = int(data.get("port", DEFAULT_PREVIEW_PORT))
    except (TypeError, ValueError):
        return jsonify({"status": "ERROR", "message": "port harus angka"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"status": "ERROR", "message": "port di luar rentang"}), 400
    path = str(data.get("path") or DEFAULT_PREVIEW_PATH)
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{request.remote_addr}:{port}{path}"
    _remember_detector(url, source="register")
    return jsonify({"status": "SUCCESS", "preview_url": url,
                    "stale_after_s": DETECTOR_STALE_SECONDS}), 200


# =====================
# 3. READ (List Data & Search untuk Tabel)
# =====================
@bp.route("/api/detections", methods=["GET"])
@login_required
def api_list_detections():
    # Ambil parameter filter dari URL (Search bar)
    p_date = request.args.get("date")
    p_plate = request.args.get("plate")
    p_type = request.args.get("type")
    p_fuel = request.args.get("fuel")

    query = db.session.query(Deteksi)

    # Terapkan Filter
    if p_date:
        query = query.filter(db.func.date(Deteksi.timestamp) == p_date)
    if p_plate:
        # Plat kini di master -> outerjoin ke mobil & motor lalu cari di keduanya.
        query = (query.outerjoin(Mobil, Deteksi.id_mobil == Mobil.id)
                      .outerjoin(Motor, Deteksi.id_motor == Motor.id)
                      .filter(or_(Mobil.plat_nomor.ilike(f"%{p_plate}%"),
                                  Motor.plat_nomor.ilike(f"%{p_plate}%"))))
    if p_type and p_type != "all":
        query = query.filter_by(tipe_kendaraan=p_type)
    if p_fuel and p_fuel != "all":
        query = query.filter_by(bahan_bakar=p_fuel)

    items = query.order_by(Deteksi.timestamp.desc()).limit(100).all()

    out = []
    for d in items:
        p_url = None
        if d.file_foto:
            p_url = f"/uploads/detections/{d.file_foto}"

        display_ts = d.waktu_deteksi or d.timestamp
        out.append({
            "id": d.id,
            "timestamp": display_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "detected_at": d.waktu_deteksi.strftime("%Y-%m-%d %H:%M:%S") if d.waktu_deteksi else None,
            "received_at": d.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "plate_number": d.plat_nomor,
            "is_electric": d.bahan_bakar,
            "vehicle_type": d.tipe_kendaraan,
            "confidence_score": d.confidence_score,
            "photo_url": p_url,
            "is_corrected": d.sudah_dikoreksi,
            "corrected_at": d.waktu_koreksi.strftime("%Y-%m-%d %H:%M:%S") if d.waktu_koreksi else None,
            "corrected_by": d.pengoreksi.username if d.pengoreksi else None,
        })
    return jsonify(out)

# =====================
# 4. STATS (Untuk Realtime Chart & Angka)
# =====================
@bp.route("/api/stats", methods=["GET"])
def api_stats():
    total, total_ev, total_gas, total_unknown = _stats()
    return jsonify({
        "total": total,
        "total_ev": total_ev,
        "total_gas": total_gas,
        "total_unknown": total_unknown
    })

# =====================
# 5. UPDATE (Edit Data) - ADMIN ONLY
# =====================
@bp.route("/api/detections/<int:det_id>", methods=["PATCH"])
@login_required
def api_edit_detection(det_id):
    # CEK ROLE: Hanya Admin yang boleh Edit
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak: Hanya Admin yang boleh mengedit data!"}), 403

    data = request.get_json(silent=True) or {}
    d = db.session.get(Deteksi, det_id)
    if not d:
        return jsonify({"status": "ERROR", "message": "Not found"}), 404

    # Hitung nilai baru (pakai nilai lama bila field tidak dikirim).
    new_plate = data["plate_number"] if "plate_number" in data else d.plat_nomor
    new_type = _normalize_vtype(data["vehicle_type"]) if "vehicle_type" in data else d.tipe_kendaraan
    new_fuel = _normalize_fuel(data["is_electric"]) if "is_electric" in data else d.bahan_bakar

    if new_plate and len(str(new_plate).strip()) > MAX_PLATE_LEN:
        return jsonify({"status": "ERROR",
                        "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400

    # Update kolom fallback (sumber stats & tampilan untuk baris tanpa plat).
    d.tipe_kendaraan = new_type
    d.bahan_bakar = new_fuel

    # Hitung ulang link master: ganti plat -> master baru/lama; ganti tipe ->
    # pindah FK antar tabel; ganti bbm -> master ikut ter-update via _link_master.
    d.id_mobil, d.id_motor = _link_master(new_plate, new_type, new_fuel)

    d.sudah_dikoreksi = True
    d.waktu_koreksi = datetime.now()
    d.dikoreksi_oleh = current_user.id
    db.session.commit()
    return jsonify({"status": "SUCCESS"}), 200


# =====================
# 5b. UPDATE PLAT (late-OCR detector) - X-API-Key, BUKAN session admin
# =====================
@bp.route("/api/detections/<int:det_id>/plate", methods=["PATCH"])
def api_detector_update_plate(det_id):
    """Koreksi otomatis dari detector saat plat terbaca SETELAH deteksi
    ter-push (late OCR). Terpisah dari PATCH admin: auth pakai X-API-Key,
    dan TIDAK menyentuh audit trail koreksi manusia (sudah_dikoreksi dkk.).
    Body JSON: plate_number dan/atau is_electric (minimal satu)."""
    denied = _check_api_key()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    plate_raw = data.get("plate_number")
    fuel_raw = data.get("is_electric")

    # Minimal satu field bermakna (plat kosong/whitespace dianggap absen).
    plate = str(plate_raw).strip() if plate_raw is not None else ""
    if not plate and not fuel_raw:
        return jsonify({"status": "ERROR",
                        "message": "kirim plate_number dan/atau is_electric"}), 400
    if plate and len(plate) > MAX_PLATE_LEN:
        return jsonify({"status": "ERROR",
                        "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400

    d = db.session.get(Deteksi, det_id)
    if not d:
        return jsonify({"status": "ERROR", "message": "Not found"}), 404

    # Koreksi manusia menang atas update otomatis: record yang sudah
    # dikoreksi admin tidak diubah. 200 (bukan 4xx) supaya detector
    # menganggap selesai dan tidak retry.
    if d.sudah_dikoreksi:
        return jsonify({"status": "SKIPPED", "reason": "already corrected"}), 200

    new_plate = plate or d.plat_nomor
    new_fuel = _normalize_fuel(fuel_raw) if fuel_raw else d.bahan_bakar

    # Update kolom fallback + relink master (plat baru -> find-or-create).
    d.bahan_bakar = new_fuel
    d.id_mobil, d.id_motor = _link_master(new_plate, d.tipe_kendaraan, new_fuel)
    db.session.commit()
    return jsonify({"status": "SUCCESS"}), 200


# =====================
# 6. DELETE (Hapus Data) - ADMIN ONLY
# =====================
@bp.route("/api/detections/<int:det_id>", methods=["DELETE"])
@login_required
def api_delete_detection(det_id):
    # CEK ROLE: Hanya Admin yang boleh Hapus
    if current_user.role != 'admin':
        return jsonify({"status": "ERROR", "message": "Akses Ditolak: Hanya Admin yang boleh menghapus data!"}), 403

    d = db.session.get(Deteksi, det_id)
    if not d:
        return jsonify({"status": "ERROR", "message": "Not found"}), 404
    if d.file_foto:
        path = os.path.join(UPLOAD_DIR, d.file_foto)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    # Master kendaraan dibiarkan (registry tetap utuh meski satu event dihapus).
    db.session.delete(d)
    db.session.commit()
    return jsonify({"status": "SUCCESS"}), 200
