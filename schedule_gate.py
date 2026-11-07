"""
排程規則：由 bot 自己決定今次跑唔跑（cron-job.org 照舊每 10 分鐘觸發）。

config.yaml → schedule:
  timezone: Europe/London
  quiet_hours: "00:00-07:00"   # 呢段時間唔跑（可以跨午夜，例如 "23:00-07:00"）
  quiet_days: [0..6]           # 邊幾日有靜音時段（0=星期一 … 6=星期日）
  weekday_every_minutes: 10    # 平日最少隔幾耐跑一次
  weekend_every_minutes: 60    # 星期六日最少隔幾耐跑一次
  catchup_after_minutes: 120   # 距離上次 run 超過呢個時間 = 補發模式
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo


def _hm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def in_quiet(local: datetime, sc: dict) -> bool:
    q = sc.get("quiet_hours")
    if not q or local.weekday() not in sc.get("quiet_days", range(7)):
        return False
    start, end = (_hm(x) for x in q.split("-"))
    now_m = local.hour * 60 + local.minute
    return start <= now_m < end if start < end else (now_m >= start or now_m < end)


def should_run(cfg: dict, state: dict, now: float | None = None) -> tuple[bool, str]:
    sc = cfg.get("schedule") or {}
    if not sc:
        return True, "冇設排程規則"
    now = now or time.time()
    local = datetime.fromtimestamp(now, ZoneInfo(sc.get("timezone", "Europe/London")))
    if in_quiet(local, sc):
        return False, f"靜音時段（{local:%a %H:%M}，{sc['quiet_hours']}）"
    weekend = local.weekday() >= 5
    every = float(sc.get("weekend_every_minutes" if weekend else "weekday_every_minutes", 10))
    last = state.get("last_run", 0)
    # 留 2 分鐘彈性：觸發時間有少少飄移都唔會錯過
    if last and (now - last) < every * 60 - 120:
        return False, f"{'週末' if weekend else '平日'}每 {every:g} 分鐘一次，距離上次 {(now - last) / 60:.0f} 分鐘"
    return True, f"{local:%a %H:%M} 跑"


def catchup_hours(cfg: dict, state: dict, now: float | None = None) -> float:
    """距離上次 run 幾多個鐘；超過 catchup_after_minutes 先算補發，否則返回 0。"""
    now = now or time.time()
    last = state.get("last_run")
    if not last:
        return 0.0
    gap = (now - last) / 3600
    after = float((cfg.get("schedule") or {}).get("catchup_after_minutes", 120)) / 60
    return gap if gap >= after else 0.0
