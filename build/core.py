r"""
core.py — общая логика расчёта индексов и гистограмм по полям Hydrosat.

Используется и первичным прогоном сезона, и инкрементальным обновлением
(update.py): формулы, спецификация бинов и обработка одной сцены живут здесь,
чтобы данные, посчитанные вчера и сегодня, были получены одним и тем же кодом.

Методика (см. README): средние — на сетке 10 м; распределения индексов,
использующих 20-метровые каналы (B05/B11/B12), — на нативных 20 м;
внутренний буфер −10 м от границы поля; маска облаков SCL; гармонизация
baseline (−1000 с 2022-01-25).
"""
from __future__ import annotations
import pathlib

import numpy as np
import pandas as pd
import geopandas as gpd
import httpx
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio import features as rfeatures
from rasterio.enums import Resampling
from shapely.geometry import mapping
from shapely.ops import unary_union

import mpc

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHAPE = ROOT / "data" / "fields" / "fields.shp"
STATS_PARQUET = ROOT / "data" / "stats.parquet"
HIST_PARQUET = ROOT / "data" / "histograms.parquet"

MAX_CLOUD = 80.0     # сценовый фильтр мягкий — отбор идёт по полю (clear_pct)
CLEAR_MIN = 95.0     # % чистых пикселей поля для «безоблачного» вывода
PAD = 20.0           # буфер окна чтения вокруг поля, м
INNER_BUF = 10.0     # внутренний буфер границы поля, м
MIN_PX_HIST = 50     # минимум валидных пикселей для гистограммы
WORKERS = 8

BANDMAP = {"blue": "B02", "green": "B03", "red": "B04", "re1": "B05",
           "nir": "B08", "swir16": "B11", "swir22": "B12", "scl": "SCL"}

IDX10 = ["NDVI", "EVI", "EVI2", "SAVI", "MSAVI", "OSAVI", "GNDVI", "GCI", "NDWI"]
IDX20 = ["NDRE", "RECI", "NDMI", "NDTI", "STI", "NDSVI", "BSI"]
INDICES = IDX10 + IDX20
BANDS10 = ["blue", "green", "red", "re1", "nir", "swir16", "swir22"]
BANDS20 = ["blue", "red", "re1", "nir", "swir16", "swir22"]

HIST_SPEC = {
    "NDVI":  (-0.20, 1.00, 48), "EVI":   (-0.20, 1.40, 64),
    "EVI2":  (-0.20, 1.40, 64), "SAVI":  (-0.20, 1.00, 48),
    "MSAVI": (-0.20, 1.00, 48), "OSAVI": (-0.20, 1.00, 48),
    "GNDVI": (-0.20, 1.00, 48), "NDRE":  (-0.20, 0.80, 40),
    "RECI":  ( 0.00, 8.00, 40), "GCI":   ( 0.00, 8.00, 40),
    "NDWI":  (-1.00, 0.60, 64), "NDMI":  (-0.60, 0.80, 56),
    "NDTI":  (-0.40, 0.40, 32), "STI":   ( 0.50, 2.50, 40),
    "NDSVI": (-0.40, 0.60, 40), "BSI":   (-0.60, 0.60, 48),
}
PLAUS_MAX = {"NDVI": 0.92, "GCI": 12.0, "RECI": 6.0, "NDMI": 0.60}


def load_fields():
    """Поля с устойчивыми id: field_id — порядковый, field_key — из кадастра."""
    gdf = gpd.read_file(SHAPE)
    gdf = gdf[gdf.geometry.notna()].reset_index(drop=True)
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf["field_id"] = range(1, len(gdf) + 1)
    seen, keys = {}, []
    for c in gdf["Cad_number"]:
        base = str(c).replace("/", "_").replace(" ", "")
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    gdf["field_key"] = keys
    return gdf


def aoi_of(gdf_wgs):
    """Выпуклые оболочки кластеров — для поиска сцен через intersects."""
    cx = gdf_wgs.to_crs(3857).geometry.centroid.to_crs(4326).x
    west = gdf_wgs[cx < 70]; east = gdf_wgs[cx >= 70]
    hulls = [g.union_all().convex_hull.buffer(0.01)
             for g in (west.geometry, east.geometry) if len(g)]
    return unary_union(hulls)


def search_scenes(geom_wgs, dt_from, dt_to, max_cloud=MAX_CLOUD):
    feats, seen = [], set()
    body = {"collections": ["sentinel-2-l2a"], "intersects": mapping(geom_wgs),
            "datetime": f"{dt_from}T00:00:00Z/{dt_to}T23:59:59Z", "limit": 100,
            "query": {"eo:cloud_cover": {"lt": max_cloud}},
            "sortby": [{"field": "properties.datetime", "direction": "asc"}]}
    with httpx.Client(timeout=60) as c:
        while True:
            r = c.post(mpc.MPC_STAC, json=body)
            r.raise_for_status()
            j = r.json()
            feats += [f for f in j.get("features", []) if f.get("id") not in seen]
            seen |= {f.get("id") for f in feats}
            nxt = next((l for l in j.get("links", []) if l.get("rel") == "next"), None)
            if not nxt or not nxt.get("body"):
                break
            body = nxt["body"]
    out = []
    for f in feats:
        a = f.get("assets", {})
        na = {k: {"href": a[mb]["href"]} for k, mb in BANDMAP.items()
              if a.get(mb, {}).get("href")}
        if len(na) == len(BANDMAP):
            out.append({"assets": na,
                        "date": (f.get("properties", {}).get("datetime", "") or "")[:10],
                        "tile": f.get("properties", {}).get("s2:mgrs_tile", "")})
    return out


def compute_indices(b, names):
    eps = 1e-10
    o = {}
    for n in names:
        if n == "NDVI":    o[n] = (b["nir"] - b["red"]) / (b["nir"] + b["red"] + eps)
        elif n == "EVI":   o[n] = 2.5 * (b["nir"] - b["red"]) / (b["nir"] + 6*b["red"] - 7.5*b["blue"] + 1 + eps)
        elif n == "EVI2":  o[n] = 2.5 * (b["nir"] - b["red"]) / (b["nir"] + 2.4*b["red"] + 1 + eps)
        elif n == "SAVI":  o[n] = 1.5 * (b["nir"] - b["red"]) / (b["nir"] + b["red"] + 0.5 + eps)
        elif n == "MSAVI": o[n] = 0.5 * (2*b["nir"] + 1 - np.sqrt(np.maximum((2*b["nir"] + 1)**2 - 8*(b["nir"] - b["red"]), 0)))
        elif n == "OSAVI": o[n] = (b["nir"] - b["red"]) / (b["nir"] + b["red"] + 0.16 + eps)
        elif n == "GNDVI": o[n] = (b["nir"] - b["green"]) / (b["nir"] + b["green"] + eps)
        elif n == "GCI":   o[n] = b["nir"] / (b["green"] + eps) - 1.0
        elif n == "NDWI":  o[n] = (b["green"] - b["nir"]) / (b["green"] + b["nir"] + eps)
        elif n == "NDRE":  o[n] = (b["nir"] - b["re1"]) / (b["nir"] + b["re1"] + eps)
        elif n == "RECI":  o[n] = b["nir"] / (b["re1"] + eps) - 1.0
        elif n == "NDMI":  o[n] = (b["nir"] - b["swir16"]) / (b["nir"] + b["swir16"] + eps)
        elif n == "NDTI":  o[n] = (b["swir16"] - b["swir22"]) / (b["swir16"] + b["swir22"] + eps)
        elif n == "STI":   o[n] = b["swir16"] / (b["swir22"] + eps)
        elif n == "NDSVI": o[n] = (b["swir16"] - b["red"]) / (b["swir16"] + b["red"] + eps)
        elif n == "BSI":   o[n] = (((b["swir16"] + b["red"]) - (b["nir"] + b["blue"]))
                                   / ((b["swir16"] + b["red"]) + (b["nir"] + b["blue"]) + eps))
    return o


def grid_stats(v):
    c = len(v)
    m = float(v.mean())
    st = float(v.std(ddof=1)) if c > 1 else None
    p = np.percentile(v, [5, 25, 50, 75, 95])
    cv = round(st / m, 4) if st is not None and abs(m) > 1e-6 else None
    return {"n": c, "std": round(st, 4) if st is not None else None,
            "p05": round(float(p[0]), 4), "p25": round(float(p[1]), 4),
            "p50": round(float(p[2]), 4), "p75": round(float(p[3]), 4),
            "p95": round(float(p[4]), 4), "cv": cv}


def histogram(v, name):
    lo, hi, nb = HIST_SPEC[name]
    below = int((v < lo).sum()); above = int((v > hi).sum())
    rows = []
    if len(v) >= MIN_PX_HIST:
        inb = v[(v >= lo) & (v <= hi)]
        cnt, edges = np.histogram(inb, bins=nb, range=(lo, hi))
        tot = int(cnt.sum()) or 1
        for i, k in enumerate(cnt):
            if k:
                rows.append({"bin_lo": round(float(edges[i]), 4),
                             "bin_hi": round(float(edges[i+1]), 4),
                             "count": int(k), "freq": round(int(k) / tot, 5)})
    return rows, below, above


def _read_grid(assets, bnds, res, bands, date):
    minx, miny, maxx, maxy = bnds
    W = max(1, int(round((maxx - minx) / res)))
    H = max(1, int(round((maxy - miny) / res)))
    raw = {b: mpc._read_block(mpc.mpc_sign(assets[b]["href"]), bnds, H, W,
                              Resampling.bilinear) for b in bands}
    scl = mpc._read_block(mpc.mpc_sign(assets["scl"]["href"]), bnds, H, W,
                          Resampling.nearest)
    off = mpc.dn_offset(date)
    refl = {b: (raw[b] - off) / mpc.SCALE for b in bands}
    good = ~((scl == 3) | (scl >= 8)) & (raw["red"] > 0)
    return refl, good, W, H


def process_scene(sc, fields_scene):
    """Сцена → (stats_rows, hist_rows). Два окна на поле: 10 м и 20 м."""
    assets = sc["assets"]; date = sc["date"]
    for attempt in (1, 2):
        try:
            with rasterio.open(mpc.mpc_sign(assets["red"]["href"])) as ds:
                scene_crs, sb = ds.crs, ds.bounds
            sel = fields_scene.to_crs(scene_crs)
            sel = sel.cx[sb.left:sb.right, sb.bottom:sb.top]
            if sel.empty:
                return [], []
            stats_rows, hist_rows = [], []
            for fid, geom in zip(sel["field_id"], sel.geometry):
                gbuf = geom.buffer(-INNER_BUF)
                buffered = (not gbuf.is_empty) and gbuf.area >= MIN_PX_HIST * 100.0
                g = gbuf if buffered else geom
                gminx, gminy, gmaxx, gmaxy = g.bounds
                minx = max(gminx - PAD, sb.left);  miny = max(gminy - PAD, sb.bottom)
                maxx = min(gmaxx + PAD, sb.right); maxy = min(gmaxy + PAD, sb.top)
                if maxx <= minx or maxy <= miny:
                    continue
                minx = np.floor(minx / 20) * 20; miny = np.floor(miny / 20) * 20
                maxx = np.ceil(maxx / 20) * 20;  maxy = np.ceil(maxy / 20) * 20
                bnds = (minx, miny, maxx, maxy)

                refl, good, W, H = _read_grid(assets, bnds, 10.0, BANDS10, date)
                tr = transform_from_bounds(minx, miny, maxx, maxy, W, H)
                mask = rfeatures.rasterize([(g, 1)], out_shape=(H, W), transform=tr,
                                           fill=0, dtype="uint8").astype(bool)
                n_px = int(mask.sum())
                if n_px == 0:
                    continue
                good10 = good & mask
                clear = int(good10.sum())
                if clear == 0:
                    continue
                idx = compute_indices(refl, INDICES)
                row = {"field_id": int(fid), "date": date, "tile": sc["tile"],
                       "buffered": buffered, "n_px": n_px, "clear_px": clear,
                       "clear_pct": round(100.0 * clear / n_px, 1)}
                implaus = []
                for name in INDICES:
                    a = idx[name]
                    vm = good10 & np.isfinite(a) & (a > -50) & (a < 50)
                    c = int(vm.sum())
                    if not c:
                        row[name] = None
                        continue
                    v = a[vm].astype("float64")
                    mean = float(v.mean())
                    row[name] = round(mean, 4)
                    if name in PLAUS_MAX and mean > PLAUS_MAX[name]:
                        implaus.append(name)
                    if name in IDX10:
                        row.update({f"{name}_{k}": val for k, val in grid_stats(v).items()})
                        hr, below, above = histogram(v, name)
                        row[f"{name}_below"] = below; row[f"{name}_above"] = above
                        row[f"{name}_hist"] = bool(hr)
                        for h in hr:
                            hist_rows.append({"field_id": int(fid), "date": date,
                                              "tile": sc["tile"], "index": name,
                                              "grid_m": 10, **h})
                refl2, good2, W2, H2 = _read_grid(assets, bnds, 20.0, BANDS20, date)
                tr2 = transform_from_bounds(minx, miny, maxx, maxy, W2, H2)
                mask2 = rfeatures.rasterize([(g, 1)], out_shape=(H2, W2), transform=tr2,
                                            fill=0, dtype="uint8").astype(bool)
                good20 = good2 & mask2
                idx2 = compute_indices(refl2, IDX20)
                for name in IDX20:
                    a = idx2[name]
                    vm = good20 & np.isfinite(a) & (a > -50) & (a < 50)
                    c = int(vm.sum())
                    if not c:
                        row[f"{name}_hist"] = False
                        continue
                    v = a[vm].astype("float64")
                    row.update({f"{name}_{k}": val for k, val in grid_stats(v).items()})
                    hr, below, above = histogram(v, name)
                    row[f"{name}_below"] = below; row[f"{name}_above"] = above
                    row[f"{name}_hist"] = bool(hr)
                    for h in hr:
                        hist_rows.append({"field_id": int(fid), "date": date,
                                          "tile": sc["tile"], "index": name,
                                          "grid_m": 20, **h})
                row["implausible"] = ",".join(implaus) if implaus else None
                stats_rows.append(row)
            return stats_rows, hist_rows
        except Exception as e:
            if attempt == 1 and ("403" in str(e) or "Forbidden" in str(e)):
                mpc.mpc_force_refresh()
                continue
            print(f"    [warn] сцена {date}/{sc['tile']}: {str(e)[:110]}")
            return [], []
    return [], []


def dedup_stats(df):
    """Поле в двух перекрывающихся тайлах одной даты — оставить с макс. clear_px."""
    df = df.sort_values(["field_id", "date", "clear_px"], ascending=[True, True, False])
    return df.drop_duplicates(["field_id", "date"], keep="first").reset_index(drop=True)
