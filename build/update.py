r"""
update.py — инкрементальное обновление данных (HYD-7).

Смотрит последнюю дату в data/stats.parquet и досчитывает ТОЛЬКО снимки после
неё: индексы, распределения, гистограммы, вырезки полей и JSON для сайта.
Если новых сцен нет — завершается за секунды, ничего не меняя.

Нахлёст LOOKBACK нужен потому, что L2A иногда появляется в каталоге с задержкой:
берём несколько дней назад и пропускаем пары «поле × дата», которые уже есть.

Запуск:
  python build/update.py                     # добрать новое (по умолчанию)
  python build/update.py --from 2026-03-01   # пересчитать период заново
  python build/update.py --dry-run           # только показать, что появилось
"""
from __future__ import annotations
import argparse, os, sys, time, pathlib, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import core
import render_chips
import build_data

LOOKBACK = 3        # дней нахлёста на случай задержки публикации L2A
SEASON_START = "-03-01"
SEASON_END = "-11-01"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dt_from", default=None,
                    help="считать с этой даты (иначе — с последней в данных минус нахлёст)")
    ap.add_argument("--to", dest="dt_to", default=None, help="по эту дату (иначе — сегодня)")
    ap.add_argument("--dry-run", action="store_true", help="не считать, только показать новые даты")
    args = ap.parse_args()

    t0 = time.time()
    today = dt.date.today()
    dt_to = args.dt_to or today.isoformat()

    stats = pd.read_parquet(core.STATS_PARQUET) if core.STATS_PARQUET.exists() else pd.DataFrame()
    have_pairs = set(zip(stats.field_id, stats.date)) if len(stats) else set()
    last = max(stats.date) if len(stats) else None

    if args.dt_from:
        dt_from = args.dt_from
    elif last:
        dt_from = (dt.date.fromisoformat(last) - dt.timedelta(days=LOOKBACK)).isoformat()
    else:
        dt_from = f"{today.year}{SEASON_START}"

    print(f"данные: {len(stats)} строк, последняя дата {last or '—'}")
    print(f"ищу снимки {dt_from} … {dt_to}")

    gdf = core.load_fields()
    wgs = gdf.to_crs(4326)
    scenes = core.search_scenes(core.aoi_of(wgs), dt_from, dt_to)
    dates = sorted({s["date"] for s in scenes})
    fresh = [d for d in dates if not any((f, d) in have_pairs for f in gdf.field_id)]
    print(f"сцен {len(scenes)} · дат {len(dates)} · из них новых {len(fresh)}"
          + (f" ({', '.join(fresh)})" if fresh else ""))

    if not fresh:
        print(f"новых дат нет — данные актуальны ({time.time()-t0:.0f}s)")
        return 0
    if args.dry_run:
        print("dry-run: расчёт не запускался")
        return 0

    todo = [s for s in scenes if s["date"] in fresh]
    fields_scene = wgs[["field_id", "geometry"]]
    S, H = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=core.WORKERS) as ex:
        futs = [ex.submit(core.process_scene, sc, fields_scene) for sc in todo]
        for fut in as_completed(futs):
            try:
                s, h = fut.result(); S += s; H += h
            except Exception as e:
                print(f"  [warn] {str(e)[:90]}")
            done += 1
            if done % 5 == 0 or done == len(todo):
                print(f"  сцен {done}/{len(todo)} · строк {len(S)} · {time.time()-t0:.0f}s")

    if not S:
        print("новые сцены есть, но валидных пикселей по полям нет (сплошная облачность)")
        return 0

    new = core.dedup_stats(pd.DataFrame(S))
    keep = set(zip(new.field_id, new.date, new.tile))
    newh = pd.DataFrame(H)
    newh = newh[[t in keep for t in zip(newh.field_id, newh.date, newh.tile)]]

    stats_out = (pd.concat([stats, new], ignore_index=True) if len(stats) else new)
    stats_out = core.dedup_stats(stats_out)
    hist = pd.read_parquet(core.HIST_PARQUET) if core.HIST_PARQUET.exists() else pd.DataFrame()
    hist_out = (pd.concat([hist, newh], ignore_index=True) if len(hist) else newh)
    hist_out = hist_out.drop_duplicates(["field_id", "date", "index", "grid_m", "bin_lo"],
                                        keep="last")
    stats_out.sort_values(["field_id", "date"], inplace=True, ignore_index=True)
    hist_out.sort_values(["field_id", "date", "index", "bin_lo"], inplace=True, ignore_index=True)
    stats_out.to_parquet(core.STATS_PARQUET, index=False)
    hist_out.to_parquet(core.HIST_PARQUET, index=False)
    print(f"данные: +{len(stats_out)-len(stats)} строк статистики, "
          f"+{len(hist_out)-len(hist)} строк гистограмм")

    print("рисую вырезки полей за новые даты…")
    render_chips.main()
    print("собираю JSON для сайта…")
    build_data.main()
    print(f"готово за {time.time()-t0:.0f}s · новые даты: {', '.join(fresh)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
