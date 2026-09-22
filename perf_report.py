"""Сводка производительности загрузок: `python perf_report.py [часов]`.

Читает таблицу perf из базы статистики (см. stats.record_perf). Показывает
по сайтам и движкам: сколько загрузок, исходы, время до первого байта,
скорость и как часто пришлось менять видеосервер (признак DPI).
"""
import sys

import stats


def _mb(v):
    return "—" if v is None else f"{v / 1048576:.1f}"


def _s(v):
    return "—" if v is None else f"{v / 1000:.1f}"


def main() -> None:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    rep = stats.perf_report(hours)
    print(f"За {rep['hours']} ч: {rep['rows']} загрузок\n")
    for g in rep["groups"]:
        print(f"{g['site']} [{g['engine']}]: {g['count']} шт — готово {g['finished']}, "
              f"ошибок {g['errors']}, отмен {g['cancelled']}")
        if g["error_kinds"]:
            print(f"  ошибки: {g['error_kinds']}")
        print(f"  до первого байта: медиана {_s(g['prepare_ms_p50'])} с, "
              f"90% {_s(g['prepare_ms_p90'])} с")
        print(f"  скорость: медиана {_mb(g['speed_p50'])} МБ/с, "
              f"худшие 10% {_mb(g['speed_p10'])} МБ/с, медленнее 2 МБ/с: {g['slow_under_2mb']}")
        print(f"  всего на задачу: медиана {_s(g['total_ms_p50'])} с, 90% {_s(g['total_ms_p90'])} с; "
              f"очередь 90% {_s(g['queue_ms_p90'])} с")
        if g["engine"] == "racefd":
            print(f"  смена сервера: {g['mirror_switches']} раз, "
                  f"непробившихся соединений {g['conn_fail_share'] * 100:.0f}%")
        print()


if __name__ == "__main__":
    main()
