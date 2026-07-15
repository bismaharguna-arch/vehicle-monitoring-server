from datetime import datetime
from extensions import db
from flask_login import UserMixin

# Tabel User (Untuk Login). Nama tabel sengaja tetap "user" (Inggris).
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # Peran akses: 'admin' (akses penuh) | 'guest' (hanya lihat).
    role = db.Column(db.String(20), nullable=False, default='guest')


# ===== Tabel MASTER kendaraan =====
# Dipisah jadi dua: mobil & motor (sesuai arahan dosen pembimbing).
# Dikunci oleh plat_nomor (identitas kendaraan). Hanya deteksi BERPLAT yang
# membuat baris master; deteksi tanpa plat tidak masuk master.

class Mobil(db.Model):
    __tablename__ = 'mobil'
    id = db.Column(db.Integer, primary_key=True)
    plat_nomor = db.Column(db.String(32), unique=True, nullable=False)
    # Bahan bakar kanonik: 'listrik' | 'bensin' | 'unknown'.
    bahan_bakar = db.Column(db.String(16), nullable=False, default="unknown")


class Motor(db.Model):
    __tablename__ = 'motor'
    id = db.Column(db.Integer, primary_key=True)
    plat_nomor = db.Column(db.String(32), unique=True, nullable=False)
    bahan_bakar = db.Column(db.String(16), nullable=False, default="unknown")


# ===== Tabel TRANSAKSI deteksi (dulu Detection) =====
# Nama tabel = "deteksi". Menyimpan event tiap kali kamera mendeteksi kendaraan.
class Deteksi(db.Model):
    __tablename__ = 'deteksi'
    id = db.Column(db.Integer, primary_key=True)
    # Waktu server menerima data (de-facto received_at). Nama tetap Inggris.
    # index=True: dipakai ORDER BY (riwayat/live feed) & filter tanggal.
    timestamp = db.Column(db.DateTime, default=datetime.now, nullable=False,
                          index=True)
    # Waktu deteksi di sisi detektor (kontrak v1). NULL = legacy / detektor lama.
    # Disimpan naive local supaya comparable dengan kolom timestamp.
    waktu_deteksi = db.Column(db.DateTime, nullable=True)
    # Skor keyakinan YOLO 0.0-1.0. Nama tetap Inggris.
    confidence_score = db.Column(db.Float, nullable=False, default=0.0)
    file_foto = db.Column(db.String(255), nullable=True)

    # Status koreksi (audit trail).
    sudah_dikoreksi = db.Column(db.Boolean, default=False, nullable=False)
    waktu_koreksi = db.Column(db.DateTime, nullable=True)
    # Admin yang melakukan koreksi (NULL = belum dikoreksi).
    dikoreksi_oleh = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    # --- Kolom fallback (dipakai saat deteksi TANPA plat / tipe unknown) ---
    # Untuk baris yang ber-link ke master, tipe & bahan bakar diambil dari master.
    # index=True: kedua kolom ini difilter terus oleh query KPI/statistik
    # (COUNT per tipe & bahan bakar) — tanpa index, COUNT = full table scan.
    tipe_kendaraan = db.Column(db.String(16), nullable=False, default="unknown",
                               index=True)
    bahan_bakar = db.Column(db.String(16), nullable=False, default="unknown",
                            index=True)

    # --- Relasi ke master (tepat satu terisi; dua-duanya NULL = tanpa plat) ---
    id_mobil = db.Column(db.Integer, db.ForeignKey('mobil.id'), nullable=True)
    id_motor = db.Column(db.Integer, db.ForeignKey('motor.id'), nullable=True)

    # Relationship (lazy-joined supaya tidak N+1 saat render tabel).
    mobil = db.relationship('Mobil', foreign_keys=[id_mobil], lazy='joined')
    motor = db.relationship('Motor', foreign_keys=[id_motor], lazy='joined')
    pengoreksi = db.relationship('User', foreign_keys=[dikoreksi_oleh], lazy='joined')

    # ----- Property turunan (memudahkan template & serialisasi) -----
    @property
    def plat_nomor(self):
        """Plat dari master bila ber-link; None bila deteksi tanpa plat."""
        if self.mobil is not None:
            return self.mobil.plat_nomor
        if self.motor is not None:
            return self.motor.plat_nomor
        return None

    @property
    def tipe(self):
        """Tipe kendaraan: dari link master, fallback ke kolom tipe_kendaraan."""
        if self.id_mobil is not None:
            return "mobil"
        if self.id_motor is not None:
            return "motor"
        return self.tipe_kendaraan

    @property
    def jenis_bbm(self):
        """Bahan bakar: dari master bila ber-link, fallback ke kolom deteksi."""
        if self.mobil is not None:
            return self.mobil.bahan_bakar
        if self.motor is not None:
            return self.motor.bahan_bakar
        return self.bahan_bakar
