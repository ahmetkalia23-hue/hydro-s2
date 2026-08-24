r"""
build_data.py — JSON-данные для статичного сайта из выходов indices_2026_v2.

Выход (site/data/):
  fields.geojson      — границы полей WGS84 + атрибуты (id, культура, площадь)
  series.json         — ряды по полям: {fid: {dates:[], IDX: {mean:[], p25:[], p75:[], cv:[]}}}
  hist/<fid>.json     — гистограммы поля: {IDX: {date: {lo, hi, step, counts:[]}}}
  meta.json           — даты, слои, легенды, интерпретации, пороги
"""
import json, pathlib
import numpy as np
import pandas as pd
import geopandas as gpd

V2   = pathlib.Path(r"F:\Alua\Hydrosat\indices_2026_v2")
SITE = pathlib.Path(r"F:\Alua\Hydrosat\hydro-s2-site\site")
SHAPE = r"F:\Alua\Hydrosat\Поля_Новые_Конечные\Поля_Новые_Конечные.shp"

INDICES = ["NDVI", "EVI", "EVI2", "SAVI", "MSAVI", "OSAVI", "GNDVI", "GCI", "NDWI",
           "NDRE", "RECI", "NDMI", "NDTI", "STI", "NDSVI", "BSI"]

INTERP = {
 "NDVI": "Нормализованный вегетационный индекс (B08 NIR, B04 Red). Основная мера зелёной биомассы: 0.2–0.4 разреженная растительность, >0.6 плотный здоровый посев.",
 "NDMI": "Индекс влажности растительности (B08 NIR 842 нм, B11 SWIR 1610 нм). SWIR поглощается водой в тканях листа: падение NDMI при стабильном NDVI — ранний признак водного стресса, до видимого угнетения.",
 "NDRE": "Red-edge индекс (B08, B05 705 нм). Чувствителен к хлорофиллу в плотном пологе, где NDVI насыщается.",
 "EVI": "Улучшенный вегетационный индекс — устойчив к влиянию почвы и атмосферы, не насыщается на плотном пологе.",
 "NDWI": "Водный индекс МакФитерса (B03 Green, B08 NIR). Положительные значения — открытая вода.",
 "GCI": "Хлорофилловый индекс по зелёному каналу (NIR/Green − 1). Пропорционален содержанию хлорофилла.",
 "RECI": "Хлорофилловый red-edge индекс (NIR/RedEdge − 1).",
 "BSI": "Индекс открытой почвы: высокие значения — голая почва, пар, свежая вспашка.",
 "NDTI": "Индекс пожнивных остатков (B11/B12).",
 "SAVI": "NDVI с поправкой на почву (L=0.5) для разреженного покрова.",
 "MSAVI": "Модифицированный SAVI — самонастраивающаяся почвенная поправка.",
 "OSAVI": "Оптимизированный SAVI (L=0.16).",
 "GNDVI": "NDVI по зелёному каналу — чувствителен к азотному статусу.",
 "EVI2": "Двухканальный EVI (без синего канала).",
 "NDSVI": "Индекс старения растительности (B11, B04).",
 "STI": "Отношение B11/B12 — пожнивные остатки.",
}

LAYERS_META = {
 "ndvi": {"name": "NDVI", "legend": {"min": 0.0, "max": 1.0, "cmap": "RdYlGn"},
          "interp": INTERP["NDVI"]},
 "ndvi_contrast": {"name": "NDVI контраст", "legend": {"min": None, "max": None, "cmap": "RdYlGn"},
          "interp": "Тот же NDVI, но растянутый по локальному диапазону окна (2–98 персентиль) — подчёркивает неоднородность внутри поля даже при слабом сигнале."},
 "ndmi": {"name": "NDMI влага", "legend": {"min": -0.4, "max": 0.6, "cmap": "BrBG"},
          "interp": INTERP["NDMI"]},
 "false": {"name": "False color", "legend": None,
          "interp": "Композит B08/B04/B03: растительность — красная, вода — тёмная, почва — серо-зелёная. Классика для оценки состояния посевов."},
 "natural": {"name": "Natural color", "legend": None,
          "interp": "Естественные цвета (B04/B03/B02) — как видит глаз."},
}


def main():
    (SITE / "data" / "hist").mkdir(parents=True, exist_ok=True)

    gdf = gpd.read_file(SHAPE)
    gdf = gdf[gdf.geometry.notna()].reset_index(drop=True)
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf["field_id"] = range(1, len(gdf) + 1)
    stats = pd.read_parquet(V2 / "hydrosat_stats_all.parquet")
    hist = pd.read_parquet(V2 / "hydrosat_histograms.parquet")

    # fields.geojson
    out = gdf[["field_id", "Cad_number", "Culture", "Area_ha", "type_irrag", "geometry"]].copy()
    out["Area_ha"] = out["Area_ha"].round(2)
    out = out.to_crs(4326)
    out.to_file(SITE / "data" / "fields.geojson", driver="GeoJSON")

    # series.json (только clear >= 95 в ряды)
    clear = stats[stats.clear_pct >= 95].sort_values(["field_id", "date"])
    series = {}
    for fid, g in clear.groupby("field_id"):
        d = {"dates": g.date.tolist(), "clear_pct": g.clear_pct.tolist()}
        for idx in INDICES:
            e = {"mean": g[idx].round(4).where(g[idx].notna(), None).tolist()}
            for k in ("p05", "p25", "p50", "p75", "p95", "cv"):
                col = f"{idx}_{k}"
                if col in g:
                    e[k] = g[col].round(4).where(g[col].notna(), None).tolist()
            d[idx] = e
        series[str(int(fid))] = d
    (SITE / "data" / "series.json").write_text(
        json.dumps(series, ensure_ascii=False), encoding="utf-8")

    # hist/<fid>.json — компактно: counts по всем бинам спецификации
    spec = {}
    for idx, g in hist.groupby("index"):
        lo = float(g.bin_lo.min()); step = float((g.bin_hi - g.bin_lo).mode().iloc[0])
        hi = float(g.bin_hi.max())
        nb = int(round((hi - lo) / step))
        spec[idx] = {"lo": round(lo, 4), "step": round(step, 5), "nb": nb}
    for fid, gf in hist.groupby("field_id"):
        obj = {}
        for idx, gi in gf.groupby("index"):
            s = spec[idx]
            per = {}
            for date, gd in gi.groupby("date"):
                counts = [0] * s["nb"]
                for _, r in gd.iterrows():
                    k = int(round((r.bin_lo - s["lo"]) / s["step"]))
                    if 0 <= k < s["nb"]:
                        counts[k] = int(r["count"])
                per[date] = counts
            obj[idx] = {"lo": s["lo"], "step": s["step"], "grid_m": int(gi.grid_m.iloc[0]),
                        "dates": per}
        (SITE / "data" / "hist" / f"{int(fid)}.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    # meta.json
    dates_all = sorted(clear.date.unique())
    meta = {"updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "period": [dates_all[0], dates_all[-1]], "n_fields": int(gdf.field_id.max()),
            "indices": INDICES, "interp": INTERP, "layers": LAYERS_META,
            "implausible": stats[stats.implausible.notna()][["field_id", "date", "implausible"]]
                             .to_dict("records")}
    (SITE / "data" / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("fields.geojson, series.json, hist/*.json, meta.json готовы")
    print("полей в series:", len(series), "· hist-файлов:", len(list((SITE/'data'/'hist').glob('*.json'))))


if __name__ == "__main__":
    main()
