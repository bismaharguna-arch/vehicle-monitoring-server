# Vehicle Monitoring Server

Aplikasi web monitoring deteksi kendaraan — Flask + Flask-SocketIO + SQLAlchemy (MySQL). Menerima hasil deteksi dari detector AI (POST ke `/api/detections`) dan menampilkannya realtime di dashboard.

> **Stack:** Flask 3.1, Flask-SocketIO, Flask-Login, SQLAlchemy 2.0 (MySQL via PyMySQL), Jinja + Tabler/Bootstrap 5, HTMX, Alpine.js, Chart.js. Windows-first.

---

## Setup di laptop baru

### 1. Prasyarat
- **Python 3.12+** (cek: `python --version`)
- **MySQL 8+** (mis. lewat [Laragon](https://laragon.org/))
- **Git**

### 2. Clone repo
```powershell
git clone https://github.com/bismaharguna-arch/vehicle-monitoring-server.git
cd vehicle-monitoring-server
```

### 3. Virtual environment + dependency
```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```
> `venv/` sengaja **tidak** ikut git (tidak portable). Selalu dibangun ulang dari `requirements.txt` — versinya sudah dipin, jadi hasilnya identik.

### 4. Siapkan database MySQL
Buat database (dan user) sesuai `DATABASE_URL` di `.env`. Contoh via MySQL client:
```sql
CREATE DATABASE vehicle_monitoring CHARACTER SET utf8mb4;
CREATE USER 'vm_app'@'127.0.0.1' IDENTIFIED BY 'ganti_password';
GRANT ALL PRIVILEGES ON vehicle_monitoring.* TO 'vm_app'@'127.0.0.1';
FLUSH PRIVILEGES;
```
> Tabel dibuat otomatis oleh `db.create_all()` saat server pertama kali boot — **tidak perlu migrasi manual** untuk setup fresh.

### 5. Buat file `.env`
`.env` berisi rahasia jadi **tidak ikut git** — salin manual dari laptop lama, atau buat baru dengan isi:
```env
SECRET_KEY=<string_acak_panjang>
DATABASE_URL=mysql+pymysql://vm_app:<password>@127.0.0.1:3306/vehicle_monitoring
DETECTOR_API_KEY=<kunci_sama_dengan_detector>
# DETECTOR_PREVIEW_URL sengaja DIKOSONGKAN — URL preview otomatis dari heartbeat detector.
# Isi HANYA kalau mau pin manual: http://<ip-detector>:5001/preview
```

### 6. Jalankan
```powershell
start.bat
```
Server jalan di `http://0.0.0.0:5000` (buka `http://localhost:5000`).

**Akun default** (dibuat otomatis saat boot):
| Username | Password | Peran |
|---|---|---|
| `admin` | `admin123` | admin (akses penuh) |

> Akun guest read-only dibuat lewat halaman **Daftar** (registrasi mandiri) — role otomatis dipaksa `guest`.

---

## Update setelah setup

Di mesin sumber (tempat ngoding):
```powershell
git add -A
git commit -m "deskripsi perubahan"
git push
```
Di laptop web:
```powershell
git pull
venv\Scripts\pip install -r requirements.txt   # kalau requirements.txt berubah
```

---

## Catatan

- **Detector terpisah** ada di project `deteksi_kendaraan` (laptop lain). Agar preview live muncul otomatis di dashboard: di GUI detector nyalakan **"Aktifkan streaming" + "Kirim Data"** supaya heartbeat terkirim — web akan tahu IP detector sendiri, `.env` tidak perlu disentuh.
- Yang **tidak** ikut git (lihat `.gitignore`): `venv/`, `.env`, `backup/`, `uploads/`, folder laporan (`test_report/`, `tabel_report/`, `postman/`), `tools/`, dan `catatan/`.
- Detail arsitektur ada di `CLAUDE.md`.
