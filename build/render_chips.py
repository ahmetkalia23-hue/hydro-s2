r"""
render_chips.py — PNG-вырезки Sentinel-2 по полям Hydrosat для статичного сайта.

На каждое поле × безоблачную дату (из indices_2026_v2) рендерит 5 слоёв
(постановка ВП-11): natural (B04/B03/B02), false (B08/B04/B03),
ndvi (RdYlGn 0..1), ndvi_contrast (растяжка 2-98% внутри окна), ndmi (BrBG).
Чип реपроецируется в EPSG:4326 (ровно ложится на Leaflet ImageOverlay).

Выход: site/data/chips/<field_id>/<date>_<layer>.png (+ chips_index.json)
Инкрементно: существующие чипы пропускаются.

Запуск: C:\...\envs\super\python.exe F:\Alua\Hydrosat\hydro-s2-site\build\render_chips.py
"""
from __future__ import annotations
import sys, json, time, pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, r"C:\Users\a.akhmetkali\PycharmProjects\PythonProject1")
import field_indices as fi

import numpy as np
import pandas as pd
import geopandas as gpd
import httpx
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio import features as rfeatures
from rasterio.warp import reproject, Resampling, calculate_default_transform
from shapely.geometry import mapping
from shapely.ops import unary_union
from PIL import Image
from matplotlib import cm

SHAPE   = r"F:\Alua\Hydrosat\Поля_Новые_Конечные\Поля_Новые_Конечные.shp"
STATS   = r"F:\Alua\Hydrosat\indices_2026_v2\hydrosat_stats_all.parquet"
SITE    = pathlib.Path(r"F:\Alua\Hydrosat\hydro-s2-site\site")
CHIPS   = SITE / "data" / "chips"
DT_FROM, DT_TO = "2026-03-01", "2026-08-24"
PAD     = 30.0    # запас окна вокруг поля, м (чип обрезается по границе альфой)
RES     = 10.0
WORKERS = 14
CLEAR_MIN_CHIP = 80.0   # чипы рендерим и для слегка облачных — видно глазами
LAYERS  = ["natural", "false", "ndvi", "ndvi_contrast", "ndmi"]
BANDS   = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "swir16": "B11"}

CM_NDVI = cm.get_cmap("RdYlGn")
CM_NDMI = cm.get_cmap("BrBG")


def search_scenes(geom_wgs):
    feats, seen = [], set()
    body = {"collections": ["sentinel-2-l2a"], "intersects": mapping(geom_wgs),
            "datetime": f"{DT_FROM}T00:00:00Z/{DT_TO}T23:59:59Z", "limit": 100,
            "query": {"eo:cloud_cover": {"lt": 80}},
            "sortby": [{"field": "properties.datetime", "direction": "asc"}]}
    with httpx.Client(timeout=60) as c:
        while True:
            r = c.post(fi.MPC_STAC, json=body); r.raise_for_status()
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
        na = {k: a[mb]["href"] for k, mb in BANDS.items() if a.get(mb, {}).get("href")}
        if len(na) == len(BANDS):
            out.append({"assets": na,
                        "date": (f.get("properties", {}).get("datetime", "") or "")[:10],
                        "tile": f.get("properties", {}).get("s2:mgrs_tile", "")})
    return out


def to_png_rgb(rgb, path, alpha):
    img = (np.clip(rgb, 0, 1) * 255).astype("uint8")
    rgba = np.dstack([img, alpha])
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def to_png_cmap(a, vmin, vmax, cmap, path, alpha):
    x = np.clip((a - vmin) / (vmax - vmin + 1e-12), 0, 1)
    rgba = (cmap(x) * 255).astype("uint8")
    rgba[..., 3] = alpha
    Image.fromarray(rgba).save(path, optimize=True)


def reproj_stack(arrs, src_crs, src_bnds, geom_wgs):
    """Стек массивов UTM → EPSG:4326; возвращает (стек, bounds4326, alpha-маска
    по границе поля: 255 внутри полигона, 0 снаружи — чипы обрезаны по полю)."""
    h, w = arrs[0].shape
    src_tr = transform_from_bounds(*src_bnds, w, h)
    dst_tr, dw, dh = calculate_default_transform(src_crs, "EPSG:4326", w, h,
                                                 *src_bnds)
    out = []
    for a in arrs:
        d = np.zeros((dh, dw), dtype="float32")
        reproject(a, d, src_transform=src_tr, src_crs=src_crs,
                  dst_transform=dst_tr, dst_crs="EPSG:4326",
                  resampling=Resampling.bilinear)
        out.append(d)
    # all_touched=True — краевые пиксели, задетые границей, тоже закрашиваются,
    # иначе вдоль контура остаётся незалитая «лесенка»
    alpha = (rfeatures.rasterize([(geom_wgs, 1)], out_shape=(dh, dw),
                                 transform=dst_tr, fill=0, dtype="uint8",
                                 all_touched=True) * 255)
    west = dst_tr.c; north = dst_tr.f
    east = west + dst_tr.a * dw; south = north + dst_tr.e * dh
    return out, [south, west, north, east], alpha


def process_scene(sc, jobs_by_tiledate, fields_utm_cache):
    """Одна сцена: рендер чипов для всех полей, которым нужна эта (tile,date)."""
    key = (sc["tile"], sc["date"])
    jobs = jobs_by_tiledate.get(key, [])
    if not jobs:
        return 0
    date = sc["date"]
    n_done = 0
    for attempt in (1, 2):
        try:
            with rasterio.open(fi.mpc_sign(sc["assets"]["red"])) as ds:
                scene_crs, sb = ds.crs, ds.bounds
            for fid, geom_wgs in jobs:
                out_dir = CHIPS / str(fid)
                out_dir.mkdir(parents=True, exist_ok=True)
                if all((out_dir / f"{date}_{l}.png").exists() for l in LAYERS):
                    n_done += 1
                    continue
                geom = fields_utm_cache(fid, scene_crs)
                gminx, gminy, gmaxx, gmaxy = geom.bounds
                minx = max(gminx - PAD, sb.left);  miny = max(gminy - PAD, sb.bottom)
                maxx = min(gmaxx + PAD, sb.right); maxy = min(gmaxy + PAD, sb.top)
                if maxx <= minx or maxy <= miny:
                    continue
                W = max(2, int(round((maxx - minx) / RES)))
                H = max(2, int(round((maxy - miny) / RES)))
                bnds = (minx, miny, maxx, maxy)
                off = fi.dn_offset(date)
                b = {}
                for name in BANDS:
                    raw = fi._read_block(fi.mpc_sign(sc["assets"][name]), bnds, H, W,
                                         Resampling.bilinear)
                    b[name] = (raw - off) / fi.SCALE
                (blue, green, red, nir, swir), bounds, alpha = reproj_stack(
                    [b["blue"], b["green"], b["red"], b["nir"], b["swir16"]],
                    scene_crs, bnds, geom_wgs)
                if not alpha.any():
                    continue
                eps = 1e-10
                ndvi = (nir - red) / (nir + red + eps)
                ndmi = (nir - swir) / (nir + swir + eps)
                inside = alpha > 0
                to_png_rgb(np.dstack([red, green, blue]) / 0.30, out_dir / f"{date}_natural.png", alpha)
                to_png_rgb(np.dstack([nir, red, green]) / 0.45, out_dir / f"{date}_false.png", alpha)
                to_png_cmap(ndvi, 0.0, 1.0, CM_NDVI, out_dir / f"{date}_ndvi.png", alpha)
                # растяжка контраста считается ТОЛЬКО по пикселям внутри поля
                lo, hi = np.percentile(ndvi[inside], [2, 98])
                to_png_cmap(ndvi, float(lo), float(hi) + 1e-6, CM_NDVI,
                            out_dir / f"{date}_ndvi_contrast.png", alpha)
                to_png_cmap(ndmi, -0.4, 0.6, CM_NDMI, out_dir / f"{date}_ndmi.png", alpha)
                bf = out_dir / "_bounds.json"
                if not bf.exists():
                    bf.write_text(json.dumps({"bounds": bounds}), encoding="utf-8")
                n_done += 1
            return n_done
        except Exception as e:
            if attempt == 1 and ("403" in str(e) or "Forbidden" in str(e)):
                fi.mpc_force_refresh(); continue
            print(f"  [warn] {date}/{sc['tile']}: {str(e)[:100]}")
            return n_done
    return n_done


def main():
    t0 = time.time()
    CHIPS.mkdir(parents=True, exist_ok=True)
    try:
        from field_indices_batch import keep_awake; keep_awake()
    except Exception:
        pass

    gdf = gpd.read_file(SHAPE)
    gdf = gdf[gdf.geometry.notna()].reset_index(drop=True)
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf["field_id"] = range(1, len(gdf) + 1)
    wgs = gdf.to_crs(4326)

    stats = pd.read_parquet(STATS)
    stats = stats[stats.clear_pct >= CLEAR_MIN_CHIP]
    need = stats[["field_id", "date", "tile"]].drop_duplicates()
    print(f"Полей: {len(gdf)} · чипов к рендеру: {len(need)} поле×дата × {len(LAYERS)} слоёв")

    utm_cache = {}
    def fields_utm(fid, crs):
        k = (fid, str(crs))
        if k not in utm_cache:
            utm_cache[k] = wgs[wgs.field_id == fid].to_crs(crs).geometry.iloc[0]
        return utm_cache[k]

    jobs_by_tiledate = {}
    for _, r in need.iterrows():
        g = wgs[wgs.field_id == r.field_id].geometry.iloc[0]
        jobs_by_tiledate.setdefault((r.tile, r.date), []).append((int(r.field_id), g))

    west = wgs[wgs.geometry.centroid.x < 70]; east = wgs[wgs.geometry.centroid.x >= 70]
    hulls = [g.union_all().convex_hull.buffer(0.01) for g in (west.geometry, east.geometry) if len(g)]
    scenes = search_scenes(unary_union(hulls))
    scenes = [s for s in scenes if (s["tile"], s["date"]) in jobs_by_tiledate]
    print(f"Сцен к обработке: {len(scenes)}")

    total = 0; done_sc = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process_scene, sc, jobs_by_tiledate, fields_utm) for sc in scenes]
        for fut in as_completed(futs):
            try:
                total += fut.result()
            except Exception as e:
                print(f"  [warn] {str(e)[:80]}")
            done_sc += 1
            if done_sc % 15 == 0 or done_sc == len(scenes):
                print(f"  сцен {done_sc}/{len(scenes)} · чипов поле×дата {total} · {time.time()-t0:.0f}s")

    # индекс чипов: field_id -> {bounds, dates:[...]}
    index = {}
    for d in sorted(CHIPS.iterdir()):
        if not d.is_dir():
            continue
        bf = d / "_bounds.json"
        dates = sorted({p.name.split("_")[0] for p in d.glob("*.png")})
        if bf.exists() and dates:
            index[d.name] = {"bounds": json.loads(bf.read_text())["bounds"], "dates": dates}
    (SITE / "data" / "chips_index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(f"Готово: {total} поле×дата · индекс {len(index)} полей · {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
