import csv
from io import StringIO
from math import ceil
from datetime import datetime
from flask import Blueprint, render_template, request, make_response
from flask_login import login_required
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
from sqlalchemy import or_
from extensions import db
from models import Deteksi, Mobil, Motor

bp = Blueprint('riwayat', __name__)

PER_PAGE = 25

# Tuple error koneksi/akses DB — dipakai untuk graceful degradation.
DB_ERRORS = (OperationalError, InterfaceError, DBAPIError)

# Kolom yang boleh diurutkan (sorting ala Task Manager Windows: klik header).
# Key = nilai param `sort` dari URL; value = kolom SQLAlchemy yang diurutkan.
# Foto, Plat, Tipe, Jenis, & Aksi sengaja tidak masuk: Tipe & Jenis pakai
# dropdown filter di header, sisanya tidak punya makna urutan.
SORT_COLUMNS = {
    "waktu": Deteksi.timestamp,
    "conf": Deteksi.confidence_score,
}
DEFAULT_SORT = "waktu"
DEFAULT_DIR = "desc"

# Batas baris yang ditampilkan di panel expand (riwayat 1 kendaraan). Totalnya
# tetap dihitung penuh; kalau lebih dari ini panel memberi catatan terpotong.
EXPAND_LIMIT = 5


def _empty_page():
    """Context pagination kosong — dipakai saat DB tidak tersedia supaya tabel
    riwayat tetap tampil (kosong) alih-alih halaman 503 penuh."""
    return {
        "latest": [],
        "page": 1,
        "per_page": PER_PAGE,
        "total": 0,
        "total_pages": 1,
        "items_count": 0,
        "sort": DEFAULT_SORT,
        "sort_dir": DEFAULT_DIR,
        "f_type": "all",
        "f_fuel": "all",
        "expanded_id": None,
        "expanded_group": [],
        "expanded_total": 0,
    }


def _filtered_query(p_date=None, p_plate=None, p_type=None, p_fuel=None,
                    p_jam_start=None, p_jam_end=None,
                    p_date_from=None, p_date_to=None, p_plat_status=None):
    query = db.session.query(Deteksi)
    if p_date:
        query = query.filter(db.func.date(Deteksi.timestamp) == p_date)
    # Rentang tanggal (dipakai dialog Cetak, mis. tanggal 1–5).
    if p_date_from:
        query = query.filter(db.func.date(Deteksi.timestamp) >= p_date_from)
    if p_date_to:
        query = query.filter(db.func.date(Deteksi.timestamp) <= p_date_to)
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
    # Filter rentang jam (mis. jam 07:00–09:00). Bandingkan bagian TIME saja
    # supaya lepas dari tanggal. Input <input type="time"> mengirim "HH:MM".
    if p_jam_start:
        query = query.filter(db.func.time(Deteksi.timestamp) >= p_jam_start)
    if p_jam_end:
        query = query.filter(db.func.time(Deteksi.timestamp) <= p_jam_end)
    # Status plat: "ada" = ter-link ke master (punya nomor); "kosong" = kedua
    # FK NULL (plat-less / unknown). Plat kini di master, jadi cek FK-nya.
    if p_plat_status == "ada":
        query = query.filter(or_(Deteksi.id_mobil.isnot(None),
                                 Deteksi.id_motor.isnot(None)))
    elif p_plat_status == "kosong":
        query = query.filter(Deteksi.id_mobil.is_(None),
                             Deteksi.id_motor.is_(None))
    return query


def _get_sort(args):
    """Ambil & validasi (sort, dir) dari query string. Default: waktu desc."""
    sort = args.get("sort") or DEFAULT_SORT
    if sort not in SORT_COLUMNS:
        sort = DEFAULT_SORT
    sort_dir = "asc" if args.get("dir") == "asc" else "desc"
    return sort, sort_dir


def _apply_sort(query, sort, sort_dir):
    col = SORT_COLUMNS.get(sort, Deteksi.timestamp)
    # id sebagai pemecah seri WAJIB: tanpa ini baris dengan nilai kembar
    # (mis. 193 baris ber-confidence 0.9) bisa berpindah urutan antar
    # permintaan, sehingga saat dipaginasi ada baris tampil dua kali di
    # halaman berbeda dan baris lain tidak pernah muncul sama sekali.
    return query.order_by(col.asc() if sort_dir == "asc" else col.desc(),
                          Deteksi.id.desc())


def _filtered_from_args(args):
    """Query terfilter (belum diurutkan) dari query string."""
    return _filtered_query(
        p_date=args.get("date") or None,
        p_plate=args.get("plate") or None,
        p_type=args.get("type") or None,
        p_fuel=args.get("fuel") or None,
        p_jam_start=args.get("jam_start") or None,
        p_jam_end=args.get("jam_end") or None,
        p_date_from=args.get("date_from") or None,
        p_date_to=args.get("date_to") or None,
        p_plat_status=args.get("plat_status") or None,
    )


def _query_from_args(args):
    """Bangun query terfilter + terurut dari query string (dipakai bersama oleh
    pagination, export CSV, dan cetak)."""
    query = _filtered_from_args(args)
    sort, sort_dir = _get_sort(args)
    return _apply_sort(query, sort, sort_dir), sort, sort_dir


def _expanded_group(args, items):
    """Isi panel expand: semua deteksi dari kendaraan yang sama.

    `expanded` (query string) = id deteksi yang barisnya sedang dibuka. State-nya
    ikut terkirim di tiap refresh/polling (hidden input #vm-expanded-input), jadi
    panel dirender server dan selamat dari swap `morph` tiap 10 detik.

    Kendaraan diidentifikasi lewat FK master (id_mobil / id_motor). Deteksi tanpa
    plat (kedua FK NULL) tidak punya identitas untuk dikelompokkan -> grupnya
    berisi baris itu sendiri saja. Isi panel menghormati filter yang aktif.

    Returns (expanded_id, items_grup, total_grup).
    """
    expanded_id = args.get("expanded", type=int)
    if not expanded_id:
        return None, [], 0

    # Barisnya harus ada di halaman ini; kalau tidak (pindah halaman / kena
    # filter) panel tidak dirender dan query grup tidak perlu dijalankan.
    row = next((i for i in items if i.id == expanded_id), None)
    if row is None:
        return None, [], 0

    if not row.id_mobil and not row.id_motor:
        return expanded_id, [row], 1

    query = _filtered_from_args(args)
    if row.id_mobil:
        query = query.filter(Deteksi.id_mobil == row.id_mobil)
    else:
        query = query.filter(Deteksi.id_motor == row.id_motor)

    total = query.count()
    grup = (query.order_by(Deteksi.timestamp.desc(), Deteksi.id.desc())
                 .limit(EXPAND_LIMIT).all())
    return expanded_id, grup, total


def _paginate(args):
    """Ambil satu halaman deteksi + metadata pagination dari query string filter."""
    query, sort, sort_dir = _query_from_args(args)
    total = query.count()
    total_pages = max(1, ceil(total / PER_PAGE))
    # Clamp page ke rentang valid (mis. setelah filter jumlah halaman menyusut).
    page = min(max(1, args.get("page", 1, type=int) or 1), total_pages)
    items = (query.offset((page - 1) * PER_PAGE)
                  .limit(PER_PAGE)
                  .all())
    expanded_id, expanded_group, expanded_total = _expanded_group(args, items)
    return {
        "latest": items,
        "page": page,
        "per_page": PER_PAGE,
        "total": total,
        "total_pages": total_pages,
        "items_count": len(items),
        "sort": sort,
        "sort_dir": sort_dir,
        # Nilai filter aktif untuk dropdown di header kolom (Tipe & Jenis).
        "f_type": args.get("type") or "all",
        "f_fuel": args.get("fuel") or "all",
        # Baris yang sedang dibuka + isi panelnya (lihat _expanded_group).
        "expanded_id": expanded_id,
        "expanded_group": expanded_group,
        "expanded_total": expanded_total,
    }


@bp.route("/riwayat")
@login_required
def index():
    # DB mati JANGAN render 503/login. Tampilkan tabel riwayat kosong + banner.
    try:
        ctx = _paginate(request.args)
        db_offline = False
    except DB_ERRORS:
        db.session.rollback()
        ctx = _empty_page()
        db_offline = True
    return render_template("history.html", db_offline=db_offline, **ctx)


@bp.route("/partials/detections/list")
@login_required
def partial_list():
    """HTMX target: tabel + bar pagination (satu wadah div), pakai filter & page dari query string."""
    try:
        ctx = _paginate(request.args)
    except DB_ERRORS:
        db.session.rollback()
        resp = make_response(render_template("partials/_history_result.html", **_empty_page()))
        resp.headers["X-DB-Offline"] = "1"
        return resp
    return render_template("partials/_history_result.html", **ctx)


# ===== Export / Cetak laporan (menghormati filter + sort yang aktif) =====

# Label bahan bakar untuk output laporan (kanonik -> tampilan).
_FUEL_LABEL = {"listrik": "Listrik (EV)", "bensin": "Bensin (BBM)"}


def _filters_summary(args):
    """Ringkasan filter aktif untuk dicetak di header laporan."""
    return {
        "date": args.get("date") or "",
        "date_from": args.get("date_from") or "",
        "date_to": args.get("date_to") or "",
        "jam_start": args.get("jam_start") or "",
        "jam_end": args.get("jam_end") or "",
        "plate": args.get("plate") or "",
        "type": args.get("type") if args.get("type") not in (None, "", "all") else "",
        "fuel": args.get("fuel") if args.get("fuel") not in (None, "", "all") else "",
    }


def _csv_safe(value):
    """Cegah CSV/formula injection. Sel yang diawali = + - @ (atau tab/CR)
    bisa dieksekusi Excel sebagai rumus. Beri prefix petik satu supaya
    dibaca sebagai teks biasa."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


@bp.route("/riwayat/export.csv")
@login_required
def export_csv():
    query, _sort, _dir = _query_from_args(request.args)
    items = query.all()

    buf = StringIO()
    # Delimiter ';' — Excel (locale Indonesia) pakai titik-koma sbagai pemisah
    # kolom; kalau pakai koma, semua data numpuk di 1 kolom saat dibuka di Excel.
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Waktu", "Plat Nomor", "Tipe", "Bahan Bakar",
                     "Confidence", "Dikoreksi", "Dikoreksi Oleh"])
    for d in items:
        ts = d.waktu_deteksi or d.timestamp
        writer.writerow([
            ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            _csv_safe(d.plat_nomor or ""),
            _csv_safe((d.tipe_kendaraan or "").capitalize()),
            _FUEL_LABEL.get(d.bahan_bakar, "Unknown"),
            f"{d.confidence_score:.2f}" if d.confidence_score else "",
            "Ya" if d.sudah_dikoreksi else "Tidak",
            _csv_safe(d.pengoreksi.username if d.pengoreksi else ""),
        ])

    # BOM (﻿) supaya Excel buka UTF-8 dengan benar.
    resp = make_response("﻿" + buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    fname = "riwayat_deteksi_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@bp.route("/riwayat/cetak")
@login_required
def cetak():
    """Halaman print-friendly: semua baris terfilter (tanpa pagination), auto window.print()."""
    query, sort, sort_dir = _query_from_args(request.args)
    items = query.all()
    return render_template(
        "cetak_riwayat.html",
        items=items,
        total=len(items),
        filters=_filters_summary(request.args),
        fuel_label=_FUEL_LABEL,
        generated_at=datetime.now(),
    )
