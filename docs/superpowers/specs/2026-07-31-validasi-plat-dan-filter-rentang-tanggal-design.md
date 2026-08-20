# Desain: Validasi Format Plat & Filter Rentang Tanggal

Tanggal: 2026-07-31
Status: disetujui, siap dibuatkan rencana implementasi

## Latar Belakang

Dua permintaan perbaikan yang muncul saat persiapan revisi TA:

1. **Plat nomor** masuk apa adanya. Satu-satunya pemeriksaan adalah panjang maksimal
   32 karakter (`routes/api.py:294`). Akibatnya hasil OCR yang salah baca
   (`"B12X#4"`, `"???"`) ikut tersimpan dan ikut membuat baris di tabel master
   `mobil`/`motor`, sehingga daftar kendaraan berbeda tercemar. Plat yang sama
   dengan spasi berbeda (`"B1234XYZ"` vs `"B 1234 XYZ"`) juga terhitung sebagai
   dua kendaraan berbeda.
2. **Filter tanggal** di halaman `/riwayat` hanya menerima satu tanggal
   (`templates/history.html:65`), padahal pengguna perlu memilih rentang
   "tanggal berapa sampai berapa".

## Sasaran

- Plat nomor yang tersimpan selalu dalam satu bentuk baku: `XX 1234 ABC`.
- Plat yang tidak dikenali tidak pernah mencemari tabel master.
- Tidak ada satu pun peristiwa deteksi yang hilang gara-gara plat tidak terbaca.
- Filter riwayat bisa memilih rentang tanggal.

## Bukan Sasaran

- Merapikan plat yang sudah terlanjur tersimpan di basis data (tidak ada migrasi).
- Mengubah tampilan atau perilaku dialog Cetak dan Ekspor CSV.
- Mengubah kontrak HTTP selain yang disebut tegas di bagian "Perubahan Kontrak".

---

## Bagian 1 — Validasi Format Plat

### Keputusan Desain

| Pertanyaan | Keputusan |
|---|---|
| Plat tidak sesuai format dari detector | Deteksi **tetap disimpan**, plat dikosongkan (`NULL`) |
| Plat tidak sesuai format dari admin (UI Koreksi) | Ditolak `400` dengan pesan yang bisa dibaca manusia |
| Pola yang dianggap sah | Ketat — huruf belakang **wajib** ada |
| Deteksi tanpa plat sama sekali | Tetap sah, tetap disimpan (perilaku lama, tidak berubah) |
| Data lama | Tidak disentuh |

Alasan detector dan admin diperlakukan berbeda: detector adalah mesin OCR yang
wajar salah baca, dan menolak barisnya berarti membuang peristiwa deteksi yang
benar-benar terjadi (KPI jadi lebih kecil dari kenyataan). Admin adalah manusia
yang sedang mengetik, jadi lebih berguna diberi tahu kesalahannya daripada
platnya diam-diam dihilangkan.

### Helper Baru

`_normalize_plate(raw)` di `routes/api.py`, sejajar dengan `_normalize_fuel()`
dan `_normalize_vtype()` yang sudah ada. Mengembalikan pasangan `(plat, ok)`.

Langkah kerjanya:

1. `None` atau string kosong → kembalikan `(None, True)`. Tanpa plat itu sah.
2. Huruf dibesarkan, lalu semua pemisah (spasi, strip, titik) dibuang.
3. Cocokkan ke pola: `[A-Z]{1,2}` + `[0-9]{1,4}` + `[A-Z]{1,3}`.
4. Cocok → rakit ulang jadi `"XX 1234 ABC"`, kembalikan `(plat, True)`.
5. Tidak cocok → kembalikan `(None, False)`.

Tabel perilaku:

| Masukan | Keluaran | Keterangan |
|---|---|---|
| `"b1234xyz"` | `("B 1234 XYZ", True)` | dirapikan |
| `"B-1234-XYZ"` | `("B 1234 XYZ", True)` | pemisah dibuang |
| `"B 1234 XYZ"` | `("B 1234 XYZ", True)` | sudah baku |
| `"D 123 AB"` | `("D 123 AB", True)` | nomor & huruf lebih pendek tetap sah |
| `None` / `""` | `(None, True)` | memang tanpa plat |
| `"B 1234"` | `(None, False)` | huruf belakang wajib |
| `"B12X#4"` | `(None, False)` | ada karakter asing |
| `"???"` | `(None, False)` | bukan plat |

Efek sampingan yang diinginkan: karena semua plat dirakit ulang ke satu bentuk,
plat yang sama dari pembacaan OCR yang berbeda spasinya sekarang jatuh ke
**satu** baris master, tidak lagi kembar.

### Titik Pemakaian

| Lokasi | Pemanggil | Perilaku saat `ok=False` |
|---|---|---|
| `POST /api/detections` (`routes/api.py:294`) | detector | plat dikosongkan, deteksi tetap disimpan, dicatat di log |
| `PATCH /api/detections/<id>` (`routes/api.py:493-499`) | admin (UI Koreksi) | `400` + pesan `"Format plat harus seperti B 1234 XYZ"` |
| `PATCH /api/detections/<id>/plate` (`routes/api.py:530-540`) | detector (OCR susulan) | plat diabaikan; bila tidak ada lagi yang bisa diterapkan → `200 SKIPPED` |

Balasan `200 SKIPPED` pada baris ketiga mengikuti semantik yang sudah dipakai
endpoint itu untuk kasus "sudah dikoreksi admin": statusnya 200 supaya detector
tidak mengulang permintaan terus-menerus.

Plat hasil normalisasi (bukan mentahan) yang diteruskan ke `_link_master()`,
sehingga master hanya pernah menerima plat berformat baku.

### Pencatatan Log

Plat yang ditolak dicatat lewat `log.info` beserta nilai mentahnya. Tujuannya
agar frekuensi salah baca OCR bisa dihitung dari berkas log — berguna sebagai
bahan tulisan pada bab pengujian.

### Perubahan Kontrak

Aturan lama **"`plate_number` > 32 karakter → `400`"** tidak lagi berlaku di
jalur detector: plat kepanjangan sekarang otomatis masuk kategori "tidak
dikenali" sehingga dikosongkan, bukan ditolak. Konstanta `MAX_PLATE_LEN` tetap
dipakai pada jalur admin. `CLAUDE.md` harus diperbarui mengikuti perubahan ini.

Kunci dan bentuk JSON lainnya tidak berubah.

---

## Bagian 2 — Filter Rentang Tanggal di `/riwayat`

### Perubahan

Satu blok di `templates/history.html:63-66`. Input tunggal `name="date"`
diganti dua input `name="date_from"` dan `name="date_to"`, dipisah teks `s/d`,
mengikuti persis pola bidang **"Rentang Jam"** yang sudah ada tepat di bawahnya
(`templates/history.html:68-75`).

### Yang Tidak Perlu Diubah

- **Kueri.** `_query_from_args()` sudah menangani `date_from`/`date_to`
  (`routes/riwayat.py:62-65`).
- **Ketahanan nilai saat polling.** `ctx` sudah menyimpan kedua kunci itu
  (`routes/riwayat.py:235-236`).
- **`resetFilter()`.** Memanggil `f.reset()` yang otomatis mengosongkan input
  ber-`name`; penanganan manual di bawahnya hanya untuk hidden input
  (`templates/history.html:467-474`).
- **Paginasi & sorting.** Ikut lewat form yang sama.

### Perilaku

| Isian | Hasil |
|---|---|
| `date_from` saja | sejak tanggal itu sampai data terbaru |
| `date_to` saja | data terlama sampai tanggal itu |
| keduanya, tanggal sama | satu hari — setara filter lama |
| keduanya, `from` > `to` | hasil kosong; dianggap wajar, tidak diberi penanganan khusus |

### Keputusan Sadar

- Parameter `date` (tunggal) **tetap dipertahankan** di backend meski sudah tidak
  dipakai sidebar. Hanya satu baris, dan URL lama yang memuat `?date=` tetap
  bisa dibuka. Menjelang sidang, menghapusnya menambah risiko tanpa manfaat.
- Tombol **Cetak** dan **Ekspor CSV** tidak ikut berubah. Keduanya membaca
  `printForm` dari dialognya sendiri (`templates/history.html:456-465`), bukan
  form sidebar, jadi rentang di sidebar tidak menular ke sana. Ini perilaku yang
  sudah berlaku sekarang.
- `CLAUDE.md` menyatakan CSV "serializes the live filter form" — keliru, dan
  ikut diperbaiki.

---

## Pengujian

Repositori ini tidak punya `pytest`; verifikasi dilakukan manual terhadap server
yang sedang berjalan, mengikuti kebiasaan skrip di `tools/`.

**Plat** — tiga kasus lewat `curl`/Postman ke `POST /api/detections`:

| Kasus | Kiriman | Harapan |
|---|---|---|
| wajar | `plate_number="b1234xyz"` | `201`, tersimpan `"B 1234 XYZ"`, master bertambah |
| ngaco | `plate_number="B12X#4"` | `201`, tersimpan, plat `NULL`, master **tidak** bertambah |
| tanpa plat | `plate_number=""` | `201`, tersimpan, plat `NULL` |

Ditambah dua kasus koreksi: admin mengetik plat salah lewat UI → muncul pesan
error; `PATCH /plate` dengan plat ngaco → `200 SKIPPED`.

**Filter tanggal** — periksa langsung di `/riwayat`: isi `from` saja, `to` saja,
keduanya sama, lalu diamkan 10 detik untuk memastikan nilainya tidak hilang saat
polling, dan ganti halaman untuk memastikan filter ikut terbawa.

Hasil pemeriksaan manual ini sekaligus menjadi bahan tangkapan layar untuk
subbab pengujian fungsional yang diminta penguji.

## Berkas yang Tersentuh

- `routes/api.py` — helper `_normalize_plate()` + tiga titik pemakaian
- `templates/history.html` — blok filter tanggal
- `CLAUDE.md` — perubahan kontrak plat + pembetulan catatan CSV
