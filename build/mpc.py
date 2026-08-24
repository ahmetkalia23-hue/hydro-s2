"""
field_indices.py — индексы Sentinel-2 по полям БЕЗ Google Earth Engine / Colab.

Делает то же, что GEE-ноутбук (reduceRegions по сезону), но локально через
Element84 STAC + чтение COG (rasterio). Для каждой даты банды читаются ОДИН раз,
все поля растеризуются и среднее по каждому полю считается векторно
(scipy.ndimage) — поэтому тянет 10-20к полей и больше.

Вход:  шейп / GeoPackage / GeoJSON с полями (любой CRS).
Выход: длинный CSV  id,date,NDVI,EVI,NDTI,STI,NDI5,NDI7,NDSVI,BSI
       Строки, где у поля НЕТ валидных пикселей (облака/вне снимка), отбрасываются.

Маска облаков SCL: убираются классы 3 (тень облака), 8/9 (облака), 10 (cirrus),
11 (снег) — как в ноутбуке (scl==3 or scl>=8).

Запуск (в среде geoflora):
  python tools/field_indices.py --shape fields.shp --id-field ORIG_OID \
      --year 2025 --start-month 4 --end-month 11 --max-cloud 80 --out out.csv

Полезные опции:
  --limit-fields 200   обработать только первые N полей (для теста)
  --max-scenes 0       ограничить число сцен (0 = без лимита)
  --block-megapixels 8 размер блока чтения в мегапикселях (память)
"""
from __future__ import annotations
import os, sys, argparse, time, pathlib, tempfile, ssl, hashlib, threading
from collections import defaultdict

# Консоль Windows (рус. локаль) — cp1251; печать эмодзи/стрелок (→) валит процесс.
# line_buffering=True — каждая строка сразу видна в консоли PyCharm (не копится в буфере).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

# ── TLS для корпоративного прокси РК (как в main.py) ─────────────────
def _setup_tls():
    try:
        import certifi
        parts = [pathlib.Path(certifi.where()).read_text(encoding="utf-8")]
        if sys.platform == "win32" and hasattr(ssl, "enum_certificates"):
            seen = set()
            for store in ("ROOT", "CA"):
                try:
                    for der, _e, _t in ssl.enum_certificates(store):
                        if der in seen:
                            continue
                        seen.add(der)
                        try:
                            parts.append(ssl.DER_cert_to_PEM_cert(der))
                        except Exception:
                            pass
                except Exception:
                    pass
        bundle = os.path.join(tempfile.gettempdir(), "geoflora_cabundle.pem")
        pathlib.Path(bundle).write_text("\n".join(parts), encoding="utf-8")
        for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GDAL_HTTP_CAINFO"):
            os.environ.setdefault(v, bundle)
    except Exception as e:
        print(f"[tls] bundle warn: {e}")
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception as e:
        print(f"[tls] truststore warn: {e}")

# GDAL env для удалённых COG
for _k, _v in {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "VSI_CACHE": "TRUE", "VSI_CACHE_SIZE": "536870912",
    "GDAL_HTTP_MULTIPLEX": "YES", "GDAL_HTTP_VERSION": "2",
    # Таймауты/ретраи: без них одно зависшее соединение через прокси держит
    # всю задачу бесконечно. Теперь застрявшее чтение падает и сцена пропускается.
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_CONNECTTIMEOUT": "20",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    # trickle-стоп: если скорость <1 КБ/с дольше 45 с — оборвать чтение. Ловит
    # зависшие соединения через прокси, которые обычный TIMEOUT не ловит (связь
    # «живая», данные капают) — из-за такого радар-прогон завис на 4.5 ч.
    "GDAL_HTTP_LOW_SPEED_TIME": "45",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "1000",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
}.items():
    os.environ.setdefault(_k, _v)

# GDAL_DATA — иначе pyogrio/GDAL шлёт Warning 3 (gdalvrt.xsd/header.dxf не найдены)
if not os.environ.get("GDAL_DATA"):
    for _gd in (os.path.join(sys.prefix, "Library", "share", "gdal"),
                os.path.join(sys.prefix, "share", "gdal"),
                os.path.join(sys.prefix, "Library", "share", "epsg_csv")):
        if os.path.exists(os.path.join(_gd, "gdalvrt.xsd")):
            os.environ["GDAL_DATA"] = _gd
            break

_setup_tls()

import numpy as np
import httpx
import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio import features as rfeatures
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from scipy import ndimage as ndi

STAC = "https://earth-search.aws.element84.com/v1/search"
INDICES = ["NDVI", "EVI", "NDTI", "STI", "NDI5", "NDI7", "NDSVI", "BSI"]
NEEDED = ["red", "blue", "nir", "nir08", "swir16", "swir22"]   # банды для всех индексов
TARGET_RES = 10.0  # м, рабочая сетка
SCALE = 10000.0    # DN -> reflectance

# Гармонизация Sentinel-2 L2A. С baseline процессинга 04.00 (снимки С 2022-01-25)
# в DN добавлен сдвиг +1000 (BOA_ADD_OFFSET). Коллекция GEE S2_SR_HARMONIZED,
# которую использует ноутбук, этот сдвиг вычитает. Чтобы индексы совпадали 1-в-1
# с GEE, вычитаем 1000 из сырого DN для снимков с 2022-01-25 (раньше — 0).
# true_reflectance = (DN - offset) / 10000.
S2_BOA_OFFSET = 1000.0


def dn_offset(date_str):
    return S2_BOA_OFFSET if (date_str or "") >= "2022-01-25" else 0.0


def search_scenes(bbox, dt_from, dt_to, max_cloud, hard_limit=2000):
    """Поиск всех сцен Sentinel-2 L2A за период (с пагинацией STAC)."""
    feats = []
    body = {
        "collections": ["sentinel-2-l2a"], "bbox": bbox,
        "datetime": f"{dt_from}T00:00:00Z/{dt_to}T23:59:59Z", "limit": 100,
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "asc"}],
    }
    with httpx.Client(timeout=60) as c:
        while True:
            r = c.post(STAC, json=body)
            r.raise_for_status()
            j = r.json()
            feats += j.get("features", [])
            nxt = next((l for l in j.get("links", []) if l.get("rel") == "next"), None)
            if not nxt or not nxt.get("body") or len(feats) >= hard_limit:
                break
            body = nxt["body"]
    return feats


def compute_indices(b):
    eps = 1e-10
    out = {}
    out["NDVI"] = (b["nir"] - b["red"]) / (b["nir"] + b["red"] + eps)
    out["EVI"] = 2.5 * (b["nir"] - b["red"]) / (b["nir"] + 6 * b["red"] - 7.5 * b["blue"] + 1 + eps)
    out["NDTI"] = (b["swir16"] - b["swir22"]) / (b["swir16"] + b["swir22"] + eps)
    out["STI"] = b["swir16"] / (b["swir22"] + eps)
    out["NDI5"] = (b["nir08"] - b["swir16"]) / (b["nir08"] + b["swir16"] + eps)
    out["NDI7"] = (b["nir08"] - b["swir22"]) / (b["nir08"] + b["swir22"] + eps)
    out["NDSVI"] = (b["swir16"] - b["red"]) / (b["swir16"] + b["red"] + eps)
    out["BSI"] = (((b["swir16"] + b["red"]) - (b["nir"] + b["blue"]))
                  / ((b["swir16"] + b["red"]) + (b["nir"] + b["blue"]) + eps))
    return out


def _read_block(href, bounds, h, w, resampling):
    """Читает банд по геогр. границам (в CRS сцены) с ресемплом к сетке h×w."""
    with rasterio.open(href) as ds:
        win = from_bounds(*bounds, transform=ds.transform)
        arr = ds.read(1, window=win, out_shape=(h, w), boundless=True, fill_value=0,
                      resampling=resampling).astype("float32")
    return arr


def fetch_cog(href, cache_dir):
    """Скачать COG целиком в cache_dir одним потоковым GET и вернуть локальный путь.

    Зачем: через прокси с высокой задержкой оконное чтение делает ТЫСЯЧИ мелких
    range-запросов (по одному на внутренний тайл × банд) — каждый платит задержку
    прокси, и большой район читается десятки минут. Один последовательный GET на
    банд (~100 МБ) гораздо быстрее. Дальше окна читаются из локального файла мгновенно.
    """
    if not (isinstance(href, str) and href.startswith(("http://", "https://"))):
        return href
    os.makedirs(cache_dir, exist_ok=True)
    # ключ кэша — по пути БЕЗ query (SAS-токен MPC меняется при обновлении,
    # но сам блоб тот же), иначе кэш всё время мимо.
    h = hashlib.md5(href.split("?")[0].encode()).hexdigest()[:20]
    local = os.path.join(cache_dir, f"{h}.tif")
    if os.path.exists(local) and os.path.getsize(local) > 1000:
        return local
    tmp = local + ".part"
    with httpx.Client(timeout=600, follow_redirects=True) as c:
        with c.stream("GET", href) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(4 * 1024 * 1024):
                    f.write(chunk)
    os.replace(tmp, local)
    return local


# ════════════════════════════════════════════════════════════════
# Microsoft Planetary Computer (Azure, EU) — в РАЗЫ быстрее AWS-Орегон
# через прокси РК (замеры: AWS ~0.18 МБ/с, MPC ~5 МБ/с при параллели).
# Тот же Sentinel-2 L2A; нужен SAS-токен (анонимно, бесплатно).
# ════════════════════════════════════════════════════════════════
MPC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a"
_MPC_BANDMAP = {"red": "B04", "green": "B03", "blue": "B02", "nir": "B08",
                "nir08": "B8A", "swir16": "B11", "swir22": "B12", "scl": "SCL"}
_mpc_tok = {"token": None, "exp": 0.0}
_mpc_lock = threading.Lock()


def mpc_token():
    """Кэшируемый SAS-токен коллекции. Перевыпуск каждые 15 мин: реальный SAS MPC
    живёт меньше часа и на длинном прогоне (десятки районов) старый токен начинал
    отдавать HTTP 403 → целые районы выходили пустыми. Потокобезопасно."""
    with _mpc_lock:
        if _mpc_tok["token"] and time.time() < _mpc_tok["exp"]:
            return _mpc_tok["token"]
        r = httpx.get(MPC_TOKEN_URL, timeout=30)
        r.raise_for_status()
        _mpc_tok["token"] = r.json()["token"]
        _mpc_tok["exp"] = time.time() + 15 * 60
        return _mpc_tok["token"]


def mpc_force_refresh():
    """Сбросить кэш токена → следующий mpc_token() перевыпустит (вызывается при 403)."""
    with _mpc_lock:
        _mpc_tok["token"] = None


def mpc_sign(href):
    """Подписать blob-URL MPC SAS-токеном (если ещё не подписан)."""
    if not isinstance(href, str) or "?" in href:
        return href
    return f"{href}?{mpc_token()}"


def search_scenes_mpc(bbox, dt_from, dt_to, max_cloud, hard_limit=4000):
    """Поиск Sentinel-2 L2A через MPC STAC. Ассеты нормализуются под имена
    red/blue/nir/nir08/swir16/swir22/scl; href БЕЗ подписи (подписываем при
    чтении через mpc_sign, чтобы токен не протух за долгий прогон)."""
    feats = []
    body = {
        "collections": ["sentinel-2-l2a"], "bbox": bbox,
        "datetime": f"{dt_from}T00:00:00Z/{dt_to}T23:59:59Z", "limit": 100,
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "asc"}],
    }
    with httpx.Client(timeout=60) as c:
        while True:
            r = c.post(MPC_STAC, json=body)
            r.raise_for_status()
            j = r.json()
            feats += j.get("features", [])
            nxt = next((l for l in j.get("links", []) if l.get("rel") == "next"), None)
            if not nxt or not nxt.get("body") or len(feats) >= hard_limit:
                break
            body = nxt["body"]
    out = []
    for f in feats:
        a = f.get("assets", {})
        na = {}
        for key, mb in _MPC_BANDMAP.items():
            asset = a.get(mb)
            if asset and asset.get("href"):
                na[key] = {"href": asset["href"]}
        f2 = dict(f)
        f2["assets"] = na
        out.append(f2)
    return out


def process_scene_group(date, scenes, gdf_wgs, ids_arr, id_to_int, block_px, accum):
    """Обработать все сцены одной даты: накопить sum/count по индексам на поле."""
    for sc in scenes:
        assets = sc.get("assets", {})
        if not all(assets.get(b, {}).get("href") for b in NEEDED) or not assets.get("scl", {}).get("href"):
            continue
        # CRS/границы сцены — по красному банду
        with rasterio.open(assets["red"]["href"]) as ds:
            scene_crs = ds.crs
            sb = ds.bounds  # left, bottom, right, top (в CRS сцены)
        # поля в CRS сцены, пересекающие сцену
        gdf_s = gdf_wgs.to_crs(scene_crs)
        sel = gdf_s.cx[sb.left:sb.right, sb.bottom:sb.top]
        if sel.empty:
            continue
        minx, miny, maxx, maxy = sel.total_bounds
        minx = max(minx, sb.left); miny = max(miny, sb.bottom)
        maxx = min(maxx, sb.right); maxy = min(maxy, sb.top)
        if maxx <= minx or maxy <= miny:
            continue
        W = max(1, int(round((maxx - minx) / TARGET_RES)))
        H = max(1, int(round((maxy - miny) / TARGET_RES)))
        # геометрии полей -> (geom, int_id) для растеризации
        shapes = [(geom, id_to_int[i]) for i, geom in zip(sel.index, sel.geometry) if geom is not None]
        if not shapes:
            continue
        # поблочно по строкам (ограничение памяти)
        block_rows = max(256, int(block_px // max(1, W)))
        for r0 in range(0, H, block_rows):
            bh = min(block_rows, H - r0)
            top = maxy - r0 * TARGET_RES
            bottom = maxy - (r0 + bh) * TARGET_RES
            bnds = (minx, bottom, maxx, top)
            tr = transform_from_bounds(minx, bottom, maxx, top, W, bh)
            # растеризация полей на сетку блока
            labels = rfeatures.rasterize(
                shapes, out_shape=(bh, W), transform=tr, fill=0,
                dtype="int32", all_touched=False)
            if not labels.any():
                continue
            present = np.unique(labels)
            present = present[present > 0]
            if present.size == 0:
                continue
            # читаем банды + SCL для блока (сырой DN, потом гармонизация)
            try:
                raw = {b: _read_block(assets[b]["href"], bnds, bh, W, Resampling.bilinear)
                       for b in NEEDED}
                scl = _read_block(assets["scl"]["href"], bnds, bh, W, Resampling.nearest)
            except Exception as e:
                print(f"    [warn] чтение блока не удалось ({date}): {str(e)[:80]}")
                continue
            off = dn_offset(date)
            bands = {b: (raw[b] - off) / SCALE for b in NEEDED}  # гармонизация как в GEE
            good = ~((scl == 3) | (scl >= 8)) & (raw["red"] > 0)  # SCL ok + не nodata(DN==0)
            idx = compute_indices(bands)
            for name in INDICES:
                a = idx[name]
                vmask = good & np.isfinite(a) & (a > -50) & (a < 50)
                vals = np.where(vmask, a, 0.0).astype("float64")
                s = ndi.sum(vals, labels, index=present)
                c = ndi.sum(vmask.astype("float64"), labels, index=present)
                S, C = accum[name]
                for k, lbl in enumerate(present):
                    S[lbl] += float(s[k]); C[lbl] += float(c[k])


def main():
    ap = argparse.ArgumentParser(description="Sentinel-2 индексы по полям (без GEE)")
    ap.add_argument("--shape", required=True, help="путь к шейпу/gpkg/geojson с полями")
    ap.add_argument("--id-field", default=None, help="поле уникального id (иначе индекс строки)")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--start-month", type=int, default=4)
    ap.add_argument("--end-month", type=int, default=11)
    ap.add_argument("--max-cloud", type=float, default=80.0)
    ap.add_argument("--out", required=True, help="выходной CSV")
    ap.add_argument("--limit-fields", type=int, default=0, help="взять только первые N полей (тест)")
    ap.add_argument("--max-scenes", type=int, default=0, help="ограничить число сцен (0=все)")
    ap.add_argument("--block-megapixels", type=float, default=8.0, help="размер блока чтения, Мпикс")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[1/4] Читаю поля: {args.shape}")
    gdf = gpd.read_file(args.shape)
    if args.limit_fields and args.limit_fields > 0:
        gdf = gdf.iloc[: args.limit_fields].copy()
    gdf = gdf[gdf.geometry.notna()].reset_index(drop=True)
    n_fields = len(gdf)
    print(f"      полей: {n_fields} · CRS источника: {gdf.crs}")

    # id каждого поля
    if args.id_field and args.id_field in gdf.columns:
        ids_arr = gdf[args.id_field].astype(str).tolist()
        id_col = args.id_field
    else:
        if args.id_field:
            print(f"      [warn] поля '{args.id_field}' нет — использую индекс строки")
        ids_arr = [str(i) for i in range(n_fields)]
        id_col = "id"
    # int-метка 1..N для растеризации
    id_to_int = {i: i + 1 for i in range(n_fields)}
    int_to_id = {i + 1: ids_arr[i] for i in range(n_fields)}

    gdf_wgs = gdf.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = gdf_wgs.total_bounds
    bbox = [float(minx), float(miny), float(maxx), float(maxy)]
    print(f"[2/4] Ищу сцены S2 за {args.year}-{args.start_month:02d}..{args.year}-{args.end_month:02d}, "
          f"облачность < {args.max_cloud}% · bbox={[round(x,3) for x in bbox]}")
    dt_from = f"{args.year}-{args.start_month:02d}-01"
    end_m = args.end_month
    dt_to = (f"{args.year}-12-31" if end_m == 12
             else f"{args.year}-{end_m + 1:02d}-01")
    scenes = search_scenes(bbox, dt_from, dt_to, args.max_cloud)
    if args.max_scenes and args.max_scenes > 0:
        scenes = scenes[: args.max_scenes]
    # группировка по дате
    by_date = defaultdict(list)
    for sc in scenes:
        d = (sc.get("properties", {}).get("datetime", "") or "")[:10]
        if d:
            by_date[d].append(sc)
    dates = sorted(by_date)
    print(f"      сцен: {len(scenes)} · уникальных дат: {len(dates)}")
    if not dates:
        print("Сцены не найдены — расширьте период/облачность.")
        return

    block_px = int(args.block_megapixels * 1_000_000)
    rows = []
    print(f"[3/4] Считаю индексы по {n_fields} полям на {len(dates)} датах...")
    for di, date in enumerate(dates, 1):
        accum = {name: (defaultdict(float), defaultdict(float)) for name in INDICES}
        process_scene_group(date, by_date[date], gdf_wgs, ids_arr, id_to_int, block_px, accum)
        # собрать строки для этой даты (только поля с валидными пикселями)
        emitted = 0
        # множество int-меток, у которых есть хоть какой-то count
        any_count = set()
        for name in INDICES:
            any_count |= set(accum[name][1].keys())
        for lbl in sorted(any_count):
            row = {id_col: int_to_id[lbl], "date": date}
            has = False
            for name in INDICES:
                S, C = accum[name]
                c = C.get(lbl, 0.0)
                if c > 0:
                    row[name] = round(S[lbl] / c, 4); has = True
                else:
                    row[name] = ""
            if has:
                rows.append(row); emitted += 1
        el = time.time() - t0
        print(f"      [{di}/{len(dates)}] {date}: строк с данными {emitted} · "
              f"всего {len(rows)} · {el:.0f}s")

    print(f"[4/4] Пишу CSV: {args.out}")
    cols = [id_col, "date"] + INDICES
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
    print(f"Готово: {len(rows)} строк, {n_fields} полей, {len(dates)} дат · "
          f"{time.time()-t0:.0f}s · → {args.out}")


if __name__ == "__main__":
    main()
