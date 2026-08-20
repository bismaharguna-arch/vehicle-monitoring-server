import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify, make_response, request
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
from extensions import db
from models import Deteksi, Mobil, Motor

# Membuat Blueprint dengan nama 'dashboard'
bp = Blueprint('dashboard', __name__)

# Tuple error koneksi/akses DB — dipakai untuk graceful degradation.
DB_ERRORS = (OperationalError, InterfaceError, DBAPIError)

# Jumlah baris "Aktivitas Terbaru" — dibikin banyak biar panel kanan terisi
# penuh sampai bawah (area tabelnya scroll internal).
RECENT_LIMIT = 30


def _offline_partial(template, **ctx):
    """Render partial dengan data kosong + header X-DB-Offline.

    Header dipakai JS di dashboard untuk toggle banner 'database tidak tersedia'
    tanpa harus inject halaman 503 penuh ke dalam fragment kecil.
    """
    resp = make_response(render_template(template, **ctx))
    resp.headers["X-DB-Offline"] = "1"
    return resp


def _empty_tipe():
    """Rincian tipe kosong (dipakai saat DB offline)."""
    return {"mobil": 0, "motor": 0, "unknown": 0}


def _empty_rincian():
    return {"total": _empty_tipe(), "ev": _empty_tipe(), "bbm": _empty_tipe()}


def _empty_kendaraan():
    return {"total": 0, "mobil": 0, "motor": 0}


def _compute_stats():
    """Hitung total + rincian tipe (mobil/motor/unknown) per bahan bakar.

    Semua angka diturunkan dari SATU query silang tipe_kendaraan x bahan_bakar,
    jadi kartu KPI bisa menampilkan rincian tipe di bawahnya tanpa query tambahan.
    `rincian` = rincian tipe untuk: keseluruhan (`total`), khusus listrik (`ev`),
    khusus bensin (`bbm`). Kartu EV/BBM cuma pakai bagiannya masing-masing.
    """
    xt = {}
    for tipe, fuel, n in (
        db.session.query(Deteksi.tipe_kendaraan, Deteksi.bahan_bakar, func.count())
        .group_by(Deteksi.tipe_kendaraan, Deteksi.bahan_bakar)
        .all()
    ):
        xt[(tipe, fuel)] = n

    def tipe_breakdown(fuel=None):
        # fuel=None -> semua bahan bakar; else difilter satu kategori.
        d = _empty_tipe()
        for (tipe, f), n in xt.items():
            if fuel is not None and f != fuel:
                continue
            # tipe non-kanonik (harusnya tak ada) dihitung sebagai unknown.
            d[tipe if tipe in d else "unknown"] += n
        return d

    total = sum(xt.values())
    total_ev = sum(n for (t, f), n in xt.items() if f == "listrik")
    total_gas = sum(n for (t, f), n in xt.items() if f == "bensin")
    total_unknown = sum(n for (t, f), n in xt.items() if f == "unknown")

    rincian = {
        "total": tipe_breakdown(),          # semua deteksi, dipecah per tipe
        "ev": tipe_breakdown("listrik"),    # tipe di dalam kelompok listrik
        "bbm": tipe_breakdown("bensin"),    # tipe di dalam kelompok bensin
    }
    # Kendaraan BERBEDA (bukan peristiwa deteksi) diambil dari tabel master.
    # Satu kendaraan yang lewat 15 kali tetap dihitung satu di sini.
    kendaraan = {"mobil": db.session.query(Mobil).count(),
                 "motor": db.session.query(Motor).count()}
    kendaraan["total"] = kendaraan["mobil"] + kendaraan["motor"]

    return total, total_ev, total_gas, total_unknown, rincian, kendaraan


@bp.route("/")
@bp.route("/dashboard")
@login_required
def index():
    # DB mati JANGAN bikin halaman kelempar ke 503/login. Render dashboard apa
    # adanya (angka 0, tabel kosong) + flag db_offline untuk tampilkan banner.
    db_offline = False
    try:
        total, total_ev, total_gas, total_unknown, rincian, kendaraan = _compute_stats()
        latest_log = (db.session.query(Deteksi)
                      .order_by(Deteksi.timestamp.desc(), Deteksi.id.desc())
                      .limit(RECENT_LIMIT).all())
    except DB_ERRORS:
        db.session.rollback()
        db_offline = True
        total = total_ev = total_gas = total_unknown = 0
        rincian = _empty_rincian()
        kendaraan = _empty_kendaraan()
        latest_log = []

    # URL MJPEG stream dari detector. Detector serve endpoint /preview pada port
    # terpisah. Kalau env tidak di-set / detector mati -> browser fire `error`
    # event -> placeholder muncul. Kalau env kosong, fitur live preview disable.
    # Prioritas URL preview:
    #   1. URL yang didaftarkan detector (Opsi A, ikut IP detector terkini)
    #   2. env DETECTOR_PREVIEW_URL (override manual, mis. detector beda mesin)
    #   3. default: IP LAN mesin web sendiri (detector satu mesin dgn web)
    from routes.api import get_detector_preview_url, default_preview_url
    detector_preview_url = (get_detector_preview_url()
                            or os.getenv("DETECTOR_PREVIEW_URL")
                            or default_preview_url())

    return render_template(
        "dashboard.html",
        total=total,
        total_ev=total_ev,
        total_gas=total_gas,
        total_unknown=total_unknown,
        rincian=rincian,
        kendaraan=kendaraan,
        latest=latest_log,
        detector_preview_url=detector_preview_url,
        db_offline=db_offline,
    )


# ---------------------------------------------------------------------------
# Partial routes (HTMX) — return HTML fragment, bukan JSON.
# Endpoint /api/* yang lama tidak disentuh; itu kontrak untuk device IoT.
# ---------------------------------------------------------------------------

@bp.route("/partials/stats")
@login_required
def partial_stats():
    try:
        total, total_ev, total_gas, total_unknown, rincian, kendaraan = _compute_stats()
    except DB_ERRORS:
        db.session.rollback()
        return _offline_partial(
            "partials/_stats_cards.html",
            total=0, total_ev=0, total_gas=0, total_unknown=0,
            rincian=_empty_rincian(), kendaraan=_empty_kendaraan(),
        )
    return render_template(
        "partials/_stats_cards.html",
        total=total,
        total_ev=total_ev,
        total_gas=total_gas,
        total_unknown=total_unknown,
        rincian=rincian,
        kendaraan=kendaraan,
    )


@bp.route("/partials/detections/recent")
@login_required
def partial_recent():
    try:
        latest_log = db.session.query(Deteksi).order_by(Deteksi.timestamp.desc()).limit(RECENT_LIMIT).all()
    except DB_ERRORS:
        db.session.rollback()
        return _offline_partial("partials/_detection_rows_dashboard.html", latest=[])
    return render_template("partials/_detection_rows_dashboard.html", latest=latest_log)


def _parse_date(raw):
    """Parse 'YYYY-MM-DD' -> date, atau None kalau kosong/invalid."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except (ValueError, TypeError):
        return None


def _timeseries_response(day_from, day_to):
    """JSON tren EV vs BBM untuk rentang [day_from, day_to] inklusif.
    Satu hari -> per JAM (24 titik); lebih dari satu hari -> per HARI."""
    start = datetime.combine(day_from, datetime.min.time())
    end = datetime.combine(day_to, datetime.min.time()) + timedelta(days=1)

    if day_from == day_to:
        labels = [f"{h:02d}:00" for h in range(24)]
        buckets_ev = [0] * 24
        buckets_bbm = [0] * 24
        if day_from == datetime.now().date():
            period_label = "Hari ini · per jam"
        else:
            period_label = f"{day_from.day:02d}/{day_from.month:02d}/{day_from.year} · per jam"
        bucket_of = lambda ts: ts.hour  # noqa: E731
    else:
        n = (day_to - day_from).days + 1
        days = [day_from + timedelta(days=i) for i in range(n)]
        day_index = {d: i for i, d in enumerate(days)}
        labels = [f"{d.day:02d}/{d.month:02d}" for d in days]
        buckets_ev = [0] * n
        buckets_bbm = [0] * n
        period_label = (f"{day_from.day:02d}/{day_from.month:02d} – "
                        f"{day_to.day:02d}/{day_to.month:02d} · per hari")
        bucket_of = lambda ts: day_index.get(ts.date())  # noqa: E731

    try:
        rows = (
            db.session.query(Deteksi)
            .filter(Deteksi.timestamp >= start, Deteksi.timestamp < end)
            .all()
        )
    except DB_ERRORS:
        db.session.rollback()
        resp = jsonify({"labels": labels, "ev": buckets_ev, "bbm": buckets_bbm,
                        "period_label": period_label})
        resp.headers["X-DB-Offline"] = "1"
        return resp

    for r in rows:
        i = bucket_of(r.timestamp)
        if i is None:
            continue
        if r.bahan_bakar == "listrik":
            buckets_ev[i] += 1
        elif r.bahan_bakar == "bensin":
            buckets_bbm[i] += 1

    return jsonify({"labels": labels, "ev": buckets_ev, "bbm": buckets_bbm,
                    "period_label": period_label})


@bp.route("/partials/timeseries")
@login_required
def partial_timeseries():
    """Tren deteksi EV vs BBM untuk Chart.js.
      ?range=1|7|30  -> preset (1 = per jam hari ini; 7/30 = per hari N hari terakhir)
      ?from=YYYY-MM-DD&to=YYYY-MM-DD -> rentang kustom (per hari; per jam kalau 1 hari)
    Rentang kustom menang atas range kalau keduanya diisi."""
    day_from = _parse_date(request.args.get("from"))
    day_to = _parse_date(request.args.get("to"))

    if day_from and day_to:
        if day_from > day_to:  # tukar kalau kebalik
            day_from, day_to = day_to, day_from
        if (day_to - day_from).days > 366:  # cap biar titik tak meledak
            day_from = day_to - timedelta(days=366)
        return _timeseries_response(day_from, day_to)

    rng = request.args.get("range", "1")
    if rng not in ("1", "7", "30"):
        rng = "1"
    today = datetime.now().date()
    if rng == "1":
        return _timeseries_response(today, today)
    n = int(rng)
    return _timeseries_response(today - timedelta(days=n - 1), today)
