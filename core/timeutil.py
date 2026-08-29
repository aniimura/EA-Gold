# -*- coding: utf-8 -*-
"""Bar calendar values, including a server-time -> UTC conversion.

MT5 stamps bars in BROKER server time, but strategies are usually specified in
UTC (a TradingView script certainly is).  Getting this wrong shifts every
session and news-blackout filter by one or two hours - and worse, by a
DIFFERENT amount in summer than in winter, so a filter calibrated in January
quietly drifts in July.

``TimeGMTOffset()`` is not usable here: inside the Strategy Tester it reports
the offset of the real clock, not of the simulated bar.  So the DST rule is
implemented explicitly, identically, on both sides:

    offset = winter_offset + (1 if the EU summer-time rule is active)
    EU summer time = last Sunday of March 01:00 UTC .. last Sunday of
                     October 01:00 UTC

FxPro (and most MT5 brokers) run EET/EEST, i.e. winter_offset = 2.

The conversion is applied to ``server_time - winter_offset`` to decide whether
DST is active.  Within the one ambiguous hour per year that can be off by an
hour; both engines make the same choice, so reconciliation is unaffected.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DST_NONE = "none"
DST_EU = "eu"


def _last_sunday(year, month):
    """Date of the last Sunday in the given month."""
    d = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
    return d - pd.Timedelta(days=(d.dayofweek + 1) % 7)


def eu_dst_active(utc_like):
    """Boolean array: is EU summer time in force at these UTC timestamps?"""
    ts = pd.to_datetime(pd.Series(np.asarray(utc_like)))
    out = np.zeros(len(ts), dtype=bool)
    for year in ts.dt.year.dropna().unique():
        start = _last_sunday(int(year), 3) + pd.Timedelta(hours=1)
        end = _last_sunday(int(year), 10) + pd.Timedelta(hours=1)
        m = (ts >= start) & (ts < end)
        out |= m.to_numpy()
    return out


def server_to_utc(server_times, winter_offset_hours=2, dst=DST_EU):
    """Convert broker server timestamps to UTC."""
    ts = pd.to_datetime(pd.Series(np.asarray(server_times)))
    base = ts - pd.Timedelta(hours=int(winter_offset_hours))
    if dst == DST_EU:
        extra = eu_dst_active(base).astype(int)
        return base - pd.to_timedelta(extra, unit="h")
    return base


def time_fields(server_times, winter_offset_hours=2, dst=DST_EU):
    """All calendar pseudo-fields the expression language exposes."""
    srv = pd.to_datetime(pd.Series(np.asarray(server_times)))
    utc = server_to_utc(srv, winter_offset_hours, dst)
    return {
        "hour": srv.dt.hour.to_numpy(dtype=float),
        "minute_of_day": (srv.dt.hour * 60 + srv.dt.minute).to_numpy(dtype=float),
        "dow": srv.dt.dayofweek.to_numpy(dtype=float),
        "utc_hour": utc.dt.hour.to_numpy(dtype=float),
        "utc_minute_of_day": (utc.dt.hour * 60 + utc.dt.minute).to_numpy(dtype=float),
    }


# --------------------------------------------------------------------------
def mq5_time_helpers(winter_offset_hours=2, dst=DST_EU):
    """The same rule, emitted as MQL5."""
    use_dst = "true" if dst == DST_EU else "false"
    return """
//======================== calendar helpers ==============================
// Server time -> UTC.  TimeGMTOffset() reports the REAL clock's offset even
// inside the tester, so the rule is spelled out here instead and mirrored
// exactly in core/timeutil.py.
#define BROKER_WINTER_OFFSET %d
#define BROKER_USES_EU_DST   %s

datetime LastSundayUtc(int year, int month, int hour)
  {
   MqlDateTime d;
   d.year = year; d.mon = month; d.day = 1;
   d.hour = 0; d.min = 0; d.sec = 0;
   int days_in_month = 31;
   if(month == 4 || month == 6 || month == 9 || month == 11)
      days_in_month = 30;
   else if(month == 2)
      days_in_month = ((year %% 4 == 0 && year %% 100 != 0) || year %% 400 == 0) ? 29 : 28;
   d.day = days_in_month;
   datetime last = StructToTime(d);
   MqlDateTime ld;
   TimeToStruct(last, ld);
   last -= ld.day_of_week * 86400;      // day_of_week: Sunday = 0
   return(last + hour * 3600);
  }

bool EuDstActive(datetime utc_like)
  {
   MqlDateTime d;
   TimeToStruct(utc_like, d);
   datetime start = LastSundayUtc(d.year, 3, 1);
   datetime end   = LastSundayUtc(d.year, 10, 1);
   return(utc_like >= start && utc_like < end);
  }

datetime ServerToUtc(datetime t)
  {
   datetime base = t - BROKER_WINTER_OFFSET * 3600;
   if(BROKER_USES_EU_DST && EuDstActive(base))
      base -= 3600;
   return(base);
  }

double SrvHour(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return((double)d.hour);
  }

double SrvMinuteOfDay(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return((double)(d.hour * 60 + d.min));
  }

double SrvDayOfWeek(datetime t)
  {
   MqlDateTime d; TimeToStruct(t, d);
   return((double)((d.day_of_week + 6) %% 7));   // MQL5 Sunday=0 -> Monday=0
  }

double UtcHour(datetime t)
  {
   MqlDateTime d; TimeToStruct(ServerToUtc(t), d);
   return((double)d.hour);
  }

double UtcMinuteOfDay(datetime t)
  {
   MqlDateTime d; TimeToStruct(ServerToUtc(t), d);
   return((double)(d.hour * 60 + d.min));
  }
//========================================================================
""" % (int(winter_offset_hours), use_dst)
