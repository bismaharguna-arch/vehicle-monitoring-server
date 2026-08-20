# Validasi Format Plat & Filter Rentang Tanggal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plat nomor yang masuk selalu dibakukan ke bentuk `XX 1234 ABC` dan plat yang tidak dikenali tidak pernah mencemari tabel master, sementara filter riwayat bisa memilih rentang tanggal.

**Architecture:** Satu helper murni `_normalize_plate()` di `routes/api.py` (sejajar `_normalize_fuel`/`_normalize_vtype` yang sudah ada) dipanggil dari tiga titik masuk HTTP, masing-masing dengan penanganan gagal yang berbeda: detector kehilangan platnya saja, admin mendapat `400`. Filter tanggal murni perubahan template karena kuerinya sudah mendukung rentang.

**Tech Stack:** Python 3 / Flask 3.1, SQLAlchemy 2.0, Jinja2, HTMX. Tanpa pytest — verifikasi lewat skrip mandiri di `tools/` dan pemeriksaan manual, mengikuti kebiasaan repositori.

## Global Constraints

- Semua komentar dan pesan baru ditulis dalam **bahasa Indonesia**, mengikuti isi repositori.
- Kunci kontrak HTTP tetap **bahasa Inggris** (`plate_number`, `vehicle_type`, `is_electric`) — jangan diganti, detector dan JS akan rusak.
- Pola plat sah: `[A-Z]{1,2}` + `[0-9]{1,4}` + `[A-Z]{1,3}`. Huruf belakang **wajib**.
- Bentuk baku keluaran: `"XX 1234 ABC"` (dipisah satu spasi).
- Deteksi **tidak boleh** hilang gara-gara plat tidak terbaca. Plat kosong selalu sah.
- Tanpa migrasi. Data lama tidak disentuh.
- Tidak ada `pytest` di repositori ini. Jangan menambahkannya.
- `POST /api/detections` berhasil membalas `201`; `PATCH` membalas `200`.

---

### Task 1: Helper `_normalize_plate()`

**Files:**
- Modify: `routes/api.py:1` (tambah `import re`)
- Modify: `routes/api.py:36-37` (tambah konstanta pola di dekat `MAX_PLATE_LEN`)
- Create: `tools/test_plat_format.py`

**Interfaces:**
- Consumes: tidak ada.
- Produces: `_normalize_plate(raw) -> tuple[str | None, bool]`. Elemen pertama plat baku atau `None`; elemen kedua `True` bila masukan bisa diterima (termasuk saat memang tanpa plat), `False` bila ada isinya tapi bukan plat yang dikenali. Dipakai Task 2, 3, dan 4.

- [ ] **Step 1: Tulis skrip tes yang gagal**

Buat `tools/test_plat_format.py`:

```python
# Bootstrap: tambah project root ke sys.path supaya import 'routes' bisa.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.api import _normalize_plate

# (masukan, plat yang diharapkan, ok yang diharapkan)
KASUS = [
    ("b1234xyz",     "B 1234 XYZ", True),   # huruf kecil, tanpa spasi
    ("B-1234-XYZ",   "B 1234 XYZ", True),   # pemisah strip
    ("B 1234 XYZ",   "B 1234 XYZ", True),   # sudah baku (idempoten)
    ("  b 1234 xyz ","B 1234 XYZ", True),   # spasi berlebih
    ("D 123 AB",     "D 123 AB",   True),   # nomor & huruf lebih pendek
    ("BE 12 A",      "BE 12 A",    True),   # kode wilayah 2 huruf
    (None,           None,         True),   # memang tanpa plat
    ("",             None,         True),   # string kosong
    ("   ",          None,         True),   # spasi doang
    ("B 1234",       None,         False),  # huruf belakang wajib
    ("B12X#4",       None,         False),  # karakter asing
    ("???",          None,         False),  # bukan plat
    ("BXYZ",         None,         False),  # tanpa angka
    ("1234 B XYZ",   None,         False),  # angka di depan
    ("B 12345 XYZ",  None,         False),  # angka kelebihan
]

gagal = 0
for masukan, plat_harap, ok_harap in KASUS:
    plat, ok = _normalize_plate(masukan)
    status = "OK  " if (plat, ok) == (plat_harap, ok_harap) else "GAGAL"
    if status == "GAGAL":
        gagal += 1
    print(f"{status} | masukan={masukan!r:16} -> ({plat!r}, {ok}) "
          f"| harusnya ({plat_harap!r}, {ok_harap})")

print(f"\n{len(KASUS) - gagal}/{len(KASUS)} kasus lolos.")
sys.exit(1 if gagal else 0)
```

- [ ] **Step 2: Jalankan, pastikan GAGAL**

Run: `venv\Scripts\python tools\test_plat_format.py`
Expected: `ImportError: cannot import name '_normalize_plate' from 'routes.api'`

- [ ] **Step 3: Tambah `import re`**

Di `routes/api.py` baris 1, ubah:

```python
import os
```

menjadi:

```python
import os
import re
```

- [ ] **Step 4: Tambah konstanta pola**

Di `routes/api.py`, tepat SETELAH baris `MAX_PLATE_LEN = 32` (baris 37), sisipkan:

```python
# Pola plat Indonesia: kode wilayah (1-2 huruf) + nomor (1-4 angka) +
# huruf belakang (1-3 huruf). Dicocokkan SETELAH semua pemisah dibuang,
# jadi "b1234xyz", "B-1234-XYZ", dan "B 1234 XYZ" sama-sama cocok.
PLATE_RE = re.compile(r"^([A-Z]{1,2})([0-9]{1,4})([A-Z]{1,3})$")
```

- [ ] **Step 5: Tulis helper**

Di `routes/api.py`, tepat SETELAH fungsi `_normalize_vtype()` (yang mulai di baris 126), tambahkan:

```python
def _normalize_plate(raw):
    """Rapikan & validasi plat Indonesia ke bentuk baku "XX 1234 ABC".

    Return (plat, ok):
      - (None, True)  -> memang tanpa plat (None/kosong/spasi). Sah.
      - (plat, True)  -> cocok pola, sudah dibakukan.
      - (None, False) -> ada isinya, tapi bukan plat yang dikenali.

    Idempoten: plat yang sudah baku diproses ulang hasilnya sama, jadi aman
    walau detector sudah menormalkan duluan di sisi sana.
    """
    if raw is None:
        return None, True
    s = str(raw).strip()
    if not s:
        return None, True
    # Buang semua pemisah (spasi, strip, titik) supaya variasi penulisan
    # yang sama jatuh ke satu bentuk -> master tidak kembar.
    bersih = re.sub(r"[^A-Z0-9]", "", s.upper())
    m = PLATE_RE.match(bersih)
    if not m:
        return None, False
    return f"{m.group(1)} {m.group(2)} {m.group(3)}", True
```

- [ ] **Step 6: Jalankan, pastikan LOLOS**

Run: `venv\Scripts\python tools\test_plat_format.py`
Expected: `15/15 kasus lolos.` dan exit code 0.

- [ ] **Step 7: Commit**

```bash
git add routes/api.py tools/test_plat_format.py
git commit -m "feat(plat): helper _normalize_plate untuk format plat Indonesia"
```

---

### Task 2: Pakai di `POST /api/detections`

**Files:**
- Modify: `routes/api.py:294-296` (ganti pemeriksaan panjang)
- Modify: `routes/api.py:324` (pastikan plat baku yang dikirim ke `_link_master`)

**Interfaces:**
- Consumes: `_normalize_plate(raw) -> (str | None, bool)` dari Task 1.
- Produces: tidak ada yang dipakai task lain.

- [ ] **Step 1: Ganti pemeriksaan panjang dengan validasi format**

Di `routes/api.py`, HAPUS blok ini (baris 294-296):

```python
        if plate and len(str(plate).strip()) > MAX_PLATE_LEN:
            return jsonify({"status": "ERROR",
                            "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400
```

GANTI dengan (mentahnya disimpan dulu supaya bisa dicatat di log apa adanya):

```python
        # Plat: dibakukan. Kalau tidak dikenali, plat DIBUANG tapi deteksinya
        # tetap disimpan — OCR wajar salah baca, dan membuang barisnya bikin
        # jumlah deteksi lebih kecil dari kenyataan.
        plate_raw = plate
        plate, plate_ok = _normalize_plate(plate)
        if not plate_ok:
            log.info("Plat ditolak (format tak dikenali), deteksi tetap "
                     "disimpan tanpa plat: %r", plate_raw)
```

- [ ] **Step 2: Nyalakan server**

Run: `start.bat`
Expected: server hidup di `0.0.0.0:5000` tanpa traceback.

- [ ] **Step 3: Uji tiga kasus lewat HTTP**

Jalankan di terminal terpisah (Git Bash):

```bash
# 1. Plat wajar -> 201, tersimpan baku
curl -s -X POST http://localhost:5000/api/detections \
  -F "plate_number=b1234xyz" -F "vehicle_type=mobil" \
  -F "is_electric=bensin" -F "confidence_score=0.9"

# 2. Plat ngaco -> 201, plat kosong
curl -s -X POST http://localhost:5000/api/detections \
  -F "plate_number=B12X#4" -F "vehicle_type=mobil" \
  -F "is_electric=bensin" -F "confidence_score=0.9"

# 3. Tanpa plat -> 201, plat kosong
curl -s -X POST http://localhost:5000/api/detections \
  -F "plate_number=" -F "vehicle_type=motor" \
  -F "is_electric=listrik" -F "confidence_score=0.8"
```

Expected: ketiganya membalas `{"status":"SUCCESS","id":...}`. Di log server,
kasus 2 memunculkan baris `Plat ditolak (format tak dikenali)`.

- [ ] **Step 4: Pastikan master tidak tercemar**

Buka `/riwayat` di browser. Baris kasus 1 menampilkan `B 1234 XYZ`; baris kasus
2 dan 3 menampilkan `—`. Lalu di MySQL:

```sql
SELECT plat_nomor FROM mobil ORDER BY id DESC LIMIT 5;
```

Expected: `B 1234 XYZ` ada; tidak ada baris `B12X#4`.

- [ ] **Step 5: Commit**

```bash
git add routes/api.py
git commit -m "feat(plat): bakukan plat di POST /api/detections, plat ngaco dikosongkan"
```

---

### Task 3: Pakai di `PATCH /api/detections/<id>` (admin)

**Files:**
- Modify: `routes/api.py:493-499`

**Interfaces:**
- Consumes: `_normalize_plate(raw) -> (str | None, bool)` dari Task 1.
- Produces: tidak ada yang dipakai task lain.

Berbeda dari Task 2: di sini yang mengetik manusia, jadi kesalahan format
**ditolak** dengan pesan supaya admin bisa membetulkan, bukan dikosongkan diam-diam.

- [ ] **Step 1: Tambah validasi, ganti pemeriksaan panjang**

Di `routes/api.py`, HAPUS blok ini (baris 497-499):

```python
    if new_plate and len(str(new_plate).strip()) > MAX_PLATE_LEN:
        return jsonify({"status": "ERROR",
                        "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400
```

GANTI dengan:

```python
    # Admin mengetik manual: format salah DITOLAK dengan pesan, bukan
    # dikosongkan diam-diam seperti jalur detector — biar bisa dibetulkan.
    if new_plate and len(str(new_plate).strip()) > MAX_PLATE_LEN:
        return jsonify({"status": "ERROR",
                        "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400
    new_plate, plate_ok = _normalize_plate(new_plate)
    if not plate_ok:
        return jsonify({"status": "ERROR",
                        "message": "Format plat harus seperti B 1234 XYZ"}), 400
```

- [ ] **Step 2: Nyalakan server & login sebagai admin**

Run: `start.bat`, lalu buka `http://localhost:5000/login`, masuk dengan
`admin` / `admin123`.

- [ ] **Step 3: Uji lewat UI**

Di `/riwayat`, klik tombol Koreksi pada salah satu baris.

| Yang diketik | Harapan |
|---|---|
| `b 1234 xyz` | tersimpan, tabel menampilkan `B 1234 XYZ` |
| `B12X#4` | muncul pesan `Format plat harus seperti B 1234 XYZ`, data tidak berubah |
| dikosongkan | tersimpan tanpa plat, tabel menampilkan `—` |

- [ ] **Step 4: Commit**

```bash
git add routes/api.py
git commit -m "feat(plat): tolak format plat salah pada koreksi admin"
```

---

### Task 4: Pakai di `PATCH /api/detections/<id>/plate` (detector)

**Files:**
- Modify: `routes/api.py:533-540`

**Interfaces:**
- Consumes: `_normalize_plate(raw) -> (str | None, bool)` dari Task 1.
- Produces: tidak ada yang dipakai task lain.

Aturan yang harus dibedakan dengan teliti:

| Kiriman | Balasan |
|---|---|
| tidak mengirim `plate_number` maupun `is_electric` | `400` (permintaan memang salah) — perilaku lama, dipertahankan |
| `plate_number` ngaco, tanpa `is_electric` | `200 SKIPPED` — tidak ada yang bisa diterapkan, dan 200 supaya detector berhenti retry |
| `plate_number` ngaco, `is_electric` ada | `200 SUCCESS` — plat diabaikan, bahan bakar tetap diterapkan |

- [ ] **Step 1: Sisipkan validasi**

Di `routes/api.py`, HAPUS blok ini (baris 533-540):

```python
    # Minimal satu field bermakna (plat kosong/whitespace dianggap absen).
    plate = str(plate_raw).strip() if plate_raw is not None else ""
    if not plate and not fuel_raw:
        return jsonify({"status": "ERROR",
                        "message": "kirim plate_number dan/atau is_electric"}), 400
    if plate and len(plate) > MAX_PLATE_LEN:
        return jsonify({"status": "ERROR",
                        "message": f"plate_number maksimal {MAX_PLATE_LEN} karakter"}), 400
```

GANTI dengan:

```python
    # Minimal satu field bermakna (plat kosong/whitespace dianggap absen).
    dikirim_plat = plate_raw is not None and str(plate_raw).strip() != ""
    if not dikirim_plat and not fuel_raw:
        return jsonify({"status": "ERROR",
                        "message": "kirim plate_number dan/atau is_electric"}), 400

    # Plat ngaco dari OCR susulan: diabaikan, jangan sampai mencemari master.
    plate, plate_ok = _normalize_plate(plate_raw)
    if not plate_ok:
        log.info("PATCH /plate: plat ditolak (format tak dikenali): %r", plate_raw)
        plate = None
        # Tidak ada lagi yang bisa diterapkan. 200 supaya detector tidak retry.
        if not fuel_raw:
            return jsonify({"status": "SKIPPED", "reason": "invalid plate"}), 200
    plate = plate or ""
```

Baris terakhir menjaga ekspresi `new_plate = plate or d.plat_nomor` di bawahnya
tetap bekerja seperti semula (plat absen → pakai plat lama).

- [ ] **Step 2: Nyalakan server**

Run: `start.bat`

- [ ] **Step 3: Uji empat kasus**

Ganti `<ID>` dengan id deteksi yang ada dan `<KEY>` dengan `X-API-Key` yang
dipakai `.env`. Kalau `API_KEY` tidak diset, hapus header `-H`.

```bash
# 1. Plat wajar -> 200 SUCCESS, tersimpan baku
curl -s -X PATCH http://localhost:5000/api/detections/<ID>/plate \
  -H "Content-Type: application/json" -H "X-API-Key: <KEY>" \
  -d '{"plate_number":"b1234xyz"}'

# 2. Plat ngaco doang -> 200 SKIPPED
curl -s -X PATCH http://localhost:5000/api/detections/<ID>/plate \
  -H "Content-Type: application/json" -H "X-API-Key: <KEY>" \
  -d '{"plate_number":"B12X#4"}'

# 3. Plat ngaco + bahan bakar -> 200 SUCCESS, bbm tetap berubah
curl -s -X PATCH http://localhost:5000/api/detections/<ID>/plate \
  -H "Content-Type: application/json" -H "X-API-Key: <KEY>" \
  -d '{"plate_number":"B12X#4","is_electric":"listrik"}'

# 4. Kosong dua-duanya -> 400
curl -s -X PATCH http://localhost:5000/api/detections/<ID>/plate \
  -H "Content-Type: application/json" -H "X-API-Key: <KEY>" \
  -d '{}'
```

Expected berturut-turut: `SUCCESS`, `SKIPPED` (`reason: invalid plate`),
`SUCCESS`, lalu `ERROR` dengan status `400`.

- [ ] **Step 4: Commit**

```bash
git add routes/api.py
git commit -m "feat(plat): abaikan plat ngaco pada PATCH /plate detector"
```

---

### Task 5: Filter rentang tanggal di `/riwayat`

**Files:**
- Modify: `templates/history.html:63-66`

**Interfaces:**
- Consumes: `date_from` / `date_to` yang sudah ditangani `_query_from_args()` di `routes/riwayat.py:62-65`.
- Produces: tidak ada.

Tidak ada perubahan Python sama sekali. Kueri, penyimpanan nilai saat polling
(`routes/riwayat.py:235-236`), dan `resetFilter()` sudah menangani ini.

- [ ] **Step 1: Ganti input tanggal tunggal jadi rentang**

Di `templates/history.html`, HAPUS blok ini (baris 63-66):

```jinja
        <div class="vm-field">
          <label class="vm-label">Tanggal</label>
          <input type="date" name="date" class="vm-input" onchange="vmResetPage()">
        </div>
```

GANTI dengan (meniru pola bidang "Rentang Jam" tepat di bawahnya):

```jinja
        <div class="vm-field">
          <label class="vm-label">Rentang Tanggal</label>
          <div class="d-flex align-items-center gap-2">
            <input type="date" name="date_from" class="vm-input" onchange="vmResetPage()" title="Dari tanggal" aria-label="Dari tanggal">
            <span class="vm-muted" style="font-size: 0.8125rem;">s/d</span>
            <input type="date" name="date_to" class="vm-input" onchange="vmResetPage()" title="Sampai tanggal" aria-label="Sampai tanggal">
          </div>
        </div>
```

- [ ] **Step 2: Nyalakan server & buka halaman**

Run: `start.bat`, buka `http://localhost:5000/riwayat`.

- [ ] **Step 3: Uji perilaku filter**

| Yang dilakukan | Harapan |
|---|---|
| isi `date_from` saja | tampil data sejak tanggal itu ke terbaru |
| isi `date_to` saja | tampil data terlama sampai tanggal itu |
| isi keduanya, tanggal sama | tampil data satu hari itu saja |
| isi `from` > `to` | tabel kosong (wajar, tidak error) |
| diamkan 10 detik setelah mengisi | nilai filter TIDAK hilang, hasil tetap tersaring |
| klik halaman 2 | filter tetap terbawa |
| klik Reset Filter | kedua tanggal kosong, data kembali penuh |

- [ ] **Step 4: Commit**

```bash
git add templates/history.html
git commit -m "feat(riwayat): filter tanggal jadi rentang dari-sampai"
```

---

### Task 6: Perbarui dokumentasi

**Files:**
- Modify: `CLAUDE.md` (bagian "Input validation on POST" dan "History filters/sort/export")
- Modify: `catatan/CLAUDE.md` (salinan naratif — jaga tetap seiring)

**Interfaces:**
- Consumes: perilaku final dari Task 1-5.
- Produces: tidak ada.

- [ ] **Step 1: Perbaiki aturan plat pada bagian validasi POST**

Di `CLAUDE.md`, cari kalimat:

> `plate_number` > 32 chars → `400` (matches master `plat_nomor` width).

Ganti dengan:

> `plate_number` divalidasi ke format plat Indonesia `XX 1234 ABC` (`[A-Z]{1,2}` + `[0-9]{1,4}` + `[A-Z]{1,3}`, huruf belakang wajib) lewat `_normalize_plate()`. Plat yang tidak dikenali **tidak** menolak request: deteksi tetap tersimpan dengan plat `NULL` + `log.info`. Plat kosong tetap sah. Admin `PATCH /api/detections/<id>` sebaliknya membalas `400 "Format plat harus seperti B 1234 XYZ"` karena yang mengetik manusia. `PATCH /<id>/plate` mengabaikan plat ngaco; bila tak ada lagi yang bisa diterapkan → `200 SKIPPED`.

- [ ] **Step 2: Betulkan catatan CSV yang keliru**

Di `CLAUDE.md`, cari kalimat yang menyebut **CSV export** "serializes the live
filter form". Itu keliru — `doExportCsv()` di `templates/history.html:456-465`
membaca `printForm` dari dialog Cetak, bukan form sidebar. Ganti jadi:

> **CSV export** (`/riwayat/export.csv`) dan **printable report** (`/riwayat/cetak`) sama-sama memakai `printForm` dari dialog Cetak (rentang tanggal + tipe + bahan bakar), BUKAN form filter sidebar.

- [ ] **Step 3: Catat filter tanggal sidebar yang baru**

Di `CLAUDE.md` bagian "History filters/sort/export", pada daftar filter, ubah
penyebutan filter `date` tunggal menjadi:

> filters are **rentang tanggal** (`date_from`/`date_to`, dua `<input type="date">` di sidebar), plate, type, fuel, dan **hour range** (`jam_start`/`jam_end` → `TIME(timestamp)`). Parameter `date` (tanggal tunggal) masih didukung backend untuk URL lama, tapi tidak lagi punya kontrol di UI.

- [ ] **Step 4: Seiringkan salinan naratif**

Terapkan tiga perubahan yang sama, dengan gaya naratif, di `catatan/CLAUDE.md`.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md catatan/CLAUDE.md
git commit -m "docs: catat validasi format plat & filter rentang tanggal"
```

---

## Catatan Eksekusi

**Git.** Repositori sedang berada di branch `main` dengan berkas belum
ter-commit (`models.py`, `routes/api.py`, `routes/dashboard.py`,
`routes/riwayat.py`, beberapa template). `routes/api.py` termasuk yang akan
diubah rencana ini. Sebelum Task 1, pastikan pekerjaan yang sudah ada itu
diamankan dulu (commit sendiri atau `git stash`), dan konfirmasi ke pemilik
repositori apakah pekerjaan ini jalan di branch terpisah.

**Urutan.** Task 1 wajib pertama. Task 2-4 boleh dikerjakan dalam urutan apa
pun setelahnya. Task 5 bebas — tidak menyentuh Python sama sekali. Task 6
terakhir.

**Hasil pengujian manual di Task 2, 3, 4, dan 5 sekaligus jadi bahan tangkapan
layar** untuk subbab pengujian fungsional yang diminta penguji.
