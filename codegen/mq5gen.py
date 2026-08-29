# -*- coding: utf-8 -*-
"""Generate an MQL5 Expert Advisor from a :class:`core.spec.Strategy`.

The EA is a mechanical translation of the same spec the Python engine runs, so
"port the strategy to MQ5" stops being a hand-written step that can introduce
bugs.  Three rules make the two sides agree:

  1. No built-in indicators.  Every value is re-derived from raw OHLC with the
     formula emitted by ``core.indicators`` - iATR/iMA/iStdDev never appear.
  2. Bar index 0 (the forming bar) is never read; signals come from index 1.
  3. Every guard the Python engine applies (STOPLEVEL, NormalizeDouble, the
     re-entry gap, no same-bar re-entry) is emitted here as well.

The EA also writes a trade CSV - and optionally a per-bar indicator CSV - into
the terminal's Common\\Files folder so the reconciler can diff them directly
instead of scraping the tester log.
"""
from __future__ import annotations

import os

from core.expr import PRICE_FIELDS, _mq_num
from core.spec import LONG, SHORT, Strategy
from core.timeutil import mq5_time_helpers
from core.types import TIMEFRAMES

BUILD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build")


def _mq_bool_expr(compiled):
    return compiled.mq5_code if compiled is not None else "false"


def _indicator_functions(strategy: Strategy) -> str:
    out = []
    for name in strategy.order:
        ind = strategy.indicators[name]
        out.append("//--- %s = %s" % (name, ind.describe()))
        out.append(ind.mq5_body(name))
    return "\n".join(out)


def _distance_code(strategy: Strategy) -> str:
    """MQL5 that fills sl_dist / tp_dist (price units) at entry time."""
    e = strategy.exits
    lines = ["   double sl_dist = 0.0, tp_dist = 0.0;"]
    if e.uses_atr():
        lines.append("   double atr_v = Ind_%s(0);" % e.atr_name)
        lines.append("   if(!MathIsValidNumber(atr_v) || atr_v <= 0.0) return;")
        if e.sl_atr is not None:
            lines.append("   sl_dist = %s * atr_v;" % _mq_num(e.sl_atr))
        if e.tp_atr is not None:
            lines.append("   tp_dist = %s * atr_v;" % _mq_num(e.tp_atr))
    else:
        lines.append("   double atr_v = 0.0;")
    if e.sl_points is not None:
        lines.append("   sl_dist = %d * g_point;" % int(e.sl_points))
    if e.tp_points is not None:
        lines.append("   tp_dist = %d * g_point;" % int(e.tp_points))
    if e.sl_min_points:
        lines.append("   if(sl_dist > 0.0)")
        lines.append("      sl_dist = MathMax(sl_dist, %d * g_point);"
                     % int(e.sl_min_points))
    return "\n".join(lines)


def _sizing_code(strategy: Strategy) -> str:
    """MQL5 that fills ``lots`` and bails out when the trade is too small."""
    z = strategy.sizing
    if not z.is_risk():
        return "   double lots = InpLot;"
    return (
        "   // Risk sizing: a stop-out should cost about %s.\n"
        "   if(sl_dist <= 0.0) return;\n"
        "   double money_per_price = g_contract;          // per 1.0 lot\n"
        "   double raw_lots = %s / (sl_dist * money_per_price);\n"
        "   double lots = MathFloor(raw_lots / %s) * %s;\n"
        "   if(lots < %s) return;\n"
        "   if(lots > %s) lots = %s;"
        % (_mq_num(z.risk_money), _mq_num(z.risk_money),
           _mq_num(z.lot_step), _mq_num(z.lot_step),
           _mq_num(z.lot_min), _mq_num(z.lot_max), _mq_num(z.lot_max))
    )


def _trail_code(strategy: Strategy) -> str:
    """MQL5 trailing-stop update, mirroring _Engine.update_trail()."""
    t = strategy.trail
    if not t.active():
        return "void UpdateTrail() { }"
    return """void UpdateTrail()
  {
   if(!g_in_pos || g_bar_index <= g_entry_bar)
      return;
   if(!PositionSelectByTicket(g_ticket))
      return;
   double money_per_price = g_contract * g_o_lots;
   if(money_per_price <= 0.0)
      return;

   // Use the bar that has just CLOSED (index 1).  Using index 0 would let the
   // stop react to a high the bar has not finished making - look-ahead.
   double profit;
   if(g_dir_long)
     {
      g_peak = MathMax(g_peak, g_rates[1].high);
      profit = (g_peak - g_o_price) * money_per_price;
     }
   else
     {
      g_peak = MathMin(g_peak, g_rates[1].low);
      profit = (g_o_price - g_peak) * money_per_price;
     }
   if(profit < %s)
      return;

   double lock  = MathMax(profit - %s, 0.0);
   double delta = lock / money_per_price;
   double new_sl = NormalizeDouble(g_dir_long ? g_o_price + delta
                                              : g_o_price - delta, g_digits);
   double cur_sl = PositionGetDouble(POSITION_SL);
   if(g_dir_long)
     {
      if(!(new_sl > cur_sl && new_sl > g_o_price)) return;
     }
   else
     {
      if(!(new_sl < cur_sl && new_sl < g_o_price)) return;
     }

   // The broker rejects a stop already on the wrong side of the market, so
   // check it here too - otherwise the Python engine would "exit" at a level
   // MT5 never accepted and the two runs diverge from that trade onwards.
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double stop_min = g_stoplevel * g_point;
   if(g_dir_long)
     {
      if(!(new_sl < bid - stop_min)) return;
     }
   else
     {
      if(!(new_sl > ask + stop_min)) return;
     }

   if(g_trade.PositionModify(g_ticket, new_sl, PositionGetDouble(POSITION_TP)))
     {
      g_o_sl = new_sl;
      g_trailed = true;
      if(g_cur_slot >= 0)
        {
         // The snapshot must track the stop as it moves, otherwise the CSV
         // reports the level this position STARTED with, not the one it was
         // closed on, and every trailed trade looks like a mismatch.
         g_rec_sl[g_cur_slot] = new_sl;
         g_rec_trailed[g_cur_slot] = true;
        }
     }
  }""" % (_mq_num(t.start_money), _mq_num(t.step_money))


def _htf_code(strategy: Strategy):
    """Declarations and per-bar refresh for every higher timeframe used."""
    htf = strategy.htf_timeframes()
    if not htf:
        return "", "", ""
    decls, refresh, guards = [], [], []
    for tf, inner_warm in htf.items():
        count = max(int(inner_warm) + 64, 128)
        decls.append("MqlRates g_rates_%s[];\n#define RATES_COUNT_%s %d"
                     % (tf, tf, count))
        refresh.append(
            "   if(CopyRates(_Symbol, %s, 0, RATES_COUNT_%s, g_rates_%s)"
            " < RATES_COUNT_%s)\n      return;" % (TIMEFRAMES[tf][1], tf, tf, tf))
        guards.append("   ArraySetAsSeries(g_rates_%s, true);" % tf)
    return "\n".join(decls), "\n".join(refresh), "\n".join(guards)


TEMPLATE = r'''//+------------------------------------------------------------------+
//|  {NAME}.mq5
//|  AUTO-GENERATED by FxTrade_202608 - DO NOT EDIT BY HAND.
//|  Source of truth : {SRCFILE}
//|  Generated       : {STAMP}
//|
//|  Every indicator below is re-derived from raw OHLC so that it matches
//|  the Python engine bit for bit.  Built-in iATR/iMA/iStdDev are avoided
//|  on purpose - their smoothing conventions differ from pandas.
//+------------------------------------------------------------------+
#property copyright "FxTrade_202608"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- inputs -------------------------------------------------------------
input double InpLot          = {LOT};
input long   InpMagic        = {MAGIC};
input bool   InpWriteTrades  = true;
input bool   InpWriteBars    = false;
input int    InpBarsCsvMax   = 500000;

//--- constants generated from the spec -----------------------------------
#define STRAT_NAME        "{NAME}"
#define WARMUP_BARS       {WARMUP}
#define RATES_COUNT       {RATES}
#define MAX_HOLD_BARS     {MAXHOLD}
#define MIN_BARS_BETWEEN  {GAP}
#define EXPECT_TF         {TFENUM}

//--- higher timeframe buffers -------------------------------------------
{HTF_DECLS}

{TIME_HELPERS}

//--- state ---------------------------------------------------------------
CTrade   g_trade;
MqlRates g_rates[];
int      g_digits   = 5;
double   g_point    = 0.00001;
double   g_contract = 100000.0;
int      g_stoplevel= 0;
double   g_peak     = 0.0;
bool     g_trailed  = false;
datetime g_last_bar = 0;
long     g_bar_index      = 0;
long     g_entry_bar      = -1000000;
long     g_last_entry_bar = -1000000;
ulong    g_ticket   = 0;
bool     g_in_pos   = false;
bool     g_dir_long = true;
int      g_trade_no = 0;
int      g_bars_written = 0;

//--- open-trade record, completed when the position closes ---------------
string   g_o_dir      = "";
datetime g_o_time     = 0;
double   g_o_price    = 0.0;
double   g_o_sl       = 0.0;
double   g_o_tp       = 0.0;
double   g_o_atr      = 0.0;
double   g_o_spread   = 0.0;
double   g_o_lots     = 0.0;
long     g_o_bar      = 0;
string   g_close_reason = "";
ulong    g_logged_deal = 0;       // closing deal already written to the CSV

//--- per-position entry snapshots ----------------------------------------
// A closing deal can reach OnTradeTransaction AFTER the next position has
// already opened, so reading the "current" globals at that moment logs the
// wrong entry.  Snapshots are therefore keyed by position id.
#define REC_N 8
long     g_rec_pid[REC_N];
string   g_rec_dir[REC_N];
datetime g_rec_time[REC_N];
double   g_rec_price[REC_N];
double   g_rec_sl[REC_N];
double   g_rec_tp[REC_N];
double   g_rec_atr[REC_N];
double   g_rec_spread[REC_N];
double   g_rec_lots[REC_N];
long     g_rec_bar[REC_N];
bool     g_rec_trailed[REC_N];
int      g_rec_next = 0;
int      g_cur_slot = -1;
long     g_pos_id      = 0;      // identifier of the open position
bool     g_closed_in_bar = false;// it was closed inside the bar now starting

int RecFind(long pid)
  {{
   if(pid <= 0)
      return(-1);
   for(int i = 0; i < REC_N; i++)
      if(g_rec_pid[i] == pid)
         return(i);
   return(-1);
  }}

int      g_fh_trades = INVALID_HANDLE;
int      g_fh_bars   = INVALID_HANDLE;

//========================= generated indicators ==========================
{INDICATORS}
//========================================================================

//+------------------------------------------------------------------+
int OnInit()
  {{
   ArraySetAsSeries(g_rates, true);
{HTF_INIT}
   g_digits    = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   g_point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   g_contract  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   g_stoplevel = (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetDeviationInPoints(10);

   if(_Period != EXPECT_TF)
      PrintFormat("[%s] WARNING chart timeframe %d != spec timeframe %d",
                  STRAT_NAME, _Period, EXPECT_TF);
   if(_Symbol != "{SYMBOL}")
      PrintFormat("[%s] WARNING chart symbol %s != spec symbol %s",
                  STRAT_NAME, _Symbol, "{SYMBOL}");

   if(InpWriteTrades)
     {{
      g_fh_trades = FileOpen("{TRADES_CSV}",
                             FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
      if(g_fh_trades != INVALID_HANDLE)
         FileWrite(g_fh_trades, "idx", "direction", "entry_bar", "entry_time",
                   "entry_price", "sl", "tp", "entry_atr", "entry_spread_points",
                   "exit_bar", "exit_time", "exit_price", "exit_reason",
                   "bars_held", "profit", "swap", "commission",
                   "lots", "trailed");
      else
         PrintFormat("[%s] cannot open trades csv (%d)", STRAT_NAME, GetLastError());
     }}
   if(InpWriteBars)
     {{
      g_fh_bars = FileOpen("{BARS_CSV}",
                           FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
      if(g_fh_bars != INVALID_HANDLE)
         FileWrite(g_fh_bars, {BARS_HEADER});
     }}
   return(INIT_SUCCEEDED);
  }}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {{
   // The tester force-closes any open position AFTER the last OnTick, and that
   // closing deal can arrive too late for OnTradeTransaction.  Sweep history
   // once so the final trade is never silently dropped from the CSV.
   FlushPendingClose();
   if(g_fh_trades != INVALID_HANDLE) {{ FileClose(g_fh_trades); g_fh_trades = INVALID_HANDLE; }}
   if(g_fh_bars   != INVALID_HANDLE) {{ FileClose(g_fh_bars);   g_fh_bars   = INVALID_HANDLE; }}
  }}

//+------------------------------------------------------------------+
void FlushPendingClose()
  {{
   // The tester's end-of-run forced close is not observable from the EA in a
   // reliable order: the deal may land in history before OnDeinit, after it,
   // or reach OnTradeTransaction too late.  So try history first, and if the
   // position is still marked open, book it ourselves at the last known price
   // exactly as the Python engine's own EOD close does.
   bool wrote = false;
   if(HistorySelect(0, TimeCurrent() + 604800))
     {{
      for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
        {{
         ulong d = HistoryDealGetTicket(i);
         if(d == 0)
            continue;
         if(HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic)
            continue;
         if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT)
            continue;
         if(d != g_logged_deal)
           {{
            WriteTradeRow(d);
            wrote = true;
           }}
         break;                     // only the newest one can still be pending
        }}
     }}
   if(!wrote && g_in_pos)
      WriteOpenPositionRow();
  }}

//+------------------------------------------------------------------+
void WriteOpenPositionRow()
  {{
   if(!InpWriteTrades || g_fh_trades == INVALID_HANDLE)
      return;
   double xp = 0.0, pf = 0.0, sw = 0.0;
   bool   booked = PositionSelectByTicket(g_ticket);
   if(booked)
     {{
      xp = PositionGetDouble(POSITION_PRICE_CURRENT);
      pf = PositionGetDouble(POSITION_PROFIT);
      sw = PositionGetDouble(POSITION_SWAP);
     }}
   else
     {{
      // position already gone: fall back to the last completed bar's close,
      // which is what the Python engine uses for its EOD exit.  P/L is left
      // blank rather than zero so the reconciler derives it from price.
      xp = g_rates[1].close;
     }}
   string s_pf = booked ? DoubleToString(pf, 2) : "";
   string s_sw = booked ? DoubleToString(sw, 2) : "";
   g_trade_no++;
   FileWrite(g_fh_trades,
             IntegerToString(g_trade_no),
             g_o_dir,
             IntegerToString((int)g_o_bar),
             TimeToString(g_o_time, TIME_DATE|TIME_SECONDS),
             DoubleToString(g_o_price, g_digits),
             DoubleToString(g_o_sl, g_digits),
             DoubleToString(g_o_tp, g_digits),
             DoubleToString(g_o_atr, 8),
             DoubleToString(g_o_spread, 1),
             IntegerToString((int)g_bar_index),
             TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
             DoubleToString(xp, g_digits),
             "EOD",
             IntegerToString((int)(g_bar_index - g_o_bar)),
             s_pf,
             s_sw,
             "",
             DoubleToString(g_o_lots, 2),
             (g_trailed ? "1" : "0"));
   FileFlush(g_fh_trades);
  }}

//+------------------------------------------------------------------+
void WriteTradeRow(ulong deal)
  {{
   if(deal == 0 || deal == g_logged_deal)
      return;
   if(!InpWriteTrades || g_fh_trades == INVALID_HANDLE)
      return;

   long   dreason = HistoryDealGetInteger(deal, DEAL_REASON);
   string reason  = g_close_reason;
   if(dreason == DEAL_REASON_SL)      reason = "SL";
   else if(dreason == DEAL_REASON_TP) reason = "TP";
   else if(reason == "")              reason = "SIGNAL";

   datetime xt = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
   double   xp = HistoryDealGetDouble(deal, DEAL_PRICE);
   long     pid = HistoryDealGetInteger(deal, DEAL_POSITION_ID);

   // Entry data comes from the snapshot for THIS position, never from the
   // "current" globals - by now they may describe a newer trade.
   int k = RecFind(pid);
   string   e_dir    = (k >= 0) ? g_rec_dir[k]    : g_o_dir;
   datetime e_time   = (k >= 0) ? g_rec_time[k]   : g_o_time;
   double   e_price  = (k >= 0) ? g_rec_price[k]  : g_o_price;
   double   e_sl     = (k >= 0) ? g_rec_sl[k]     : g_o_sl;
   double   e_tp     = (k >= 0) ? g_rec_tp[k]     : g_o_tp;
   double   e_atr    = (k >= 0) ? g_rec_atr[k]    : g_o_atr;
   double   e_spread = (k >= 0) ? g_rec_spread[k] : g_o_spread;
   double   e_lots   = (k >= 0) ? g_rec_lots[k]   : g_o_lots;
   long     e_bar    = (k >= 0) ? g_rec_bar[k]    : g_o_bar;
   bool     e_trail  = (k >= 0) ? g_rec_trailed[k]: g_trailed;

   // Booked costs: the Python engine models price movement only, so these are
   // reported separately instead of being hidden inside a single P/L figure.
   // Commission is charged on BOTH the entry and the exit deal, so the whole
   // position must be summed - reading the closing deal alone halves it.
   double pf = 0.0, sw = 0.0, cm = 0.0;
   if(HistorySelectByPosition(pid))
     {{
      for(int k = 0; k < HistoryDealsTotal(); k++)
        {{
         ulong dk = HistoryDealGetTicket(k);
         if(dk == 0)
            continue;
         pf += HistoryDealGetDouble(dk, DEAL_PROFIT);
         sw += HistoryDealGetDouble(dk, DEAL_SWAP);
         cm += HistoryDealGetDouble(dk, DEAL_COMMISSION);
        }}
     }}
   long exit_bar = g_bar_index;
   long held     = exit_bar - e_bar;

   g_trade_no++;
   FileWrite(g_fh_trades,
             IntegerToString(g_trade_no),
             e_dir,
             IntegerToString((int)e_bar),
             TimeToString(e_time, TIME_DATE|TIME_SECONDS),
             DoubleToString(e_price, g_digits),
             DoubleToString(e_sl, g_digits),
             DoubleToString(e_tp, g_digits),
             DoubleToString(e_atr, 8),
             DoubleToString(e_spread, 1),
             IntegerToString((int)exit_bar),
             TimeToString(xt, TIME_DATE|TIME_SECONDS),
             DoubleToString(xp, g_digits),
             reason,
             IntegerToString((int)held),
             DoubleToString(pf, 2),
             DoubleToString(sw, 2),
             DoubleToString(cm, 2),
             DoubleToString(e_lots, 2),
             (e_trail ? "1" : "0"));
   FileFlush(g_fh_trades);
   g_logged_deal = deal;
  }}

//+------------------------------------------------------------------+
void OnTick()
  {{
   datetime bt = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(bt == 0 || bt == g_last_bar)
      return;
   g_last_bar = bt;
   OnNewBar();
  }}

//+------------------------------------------------------------------+
void SyncPosition()
  {{
   if(!g_in_pos)
      return;
   if(PositionSelectByTicket(g_ticket))
      return;
   g_in_pos = false;                 // closed broker-side by SL or TP

   // WHEN it closed decides whether this bar may open a new position.  A stop
   // that fired on the opening tick of this bar means the bar has already had
   // a fill, and neither the Python engine nor the original strategy allows a
   // re-entry on such a bar; without this the EA takes trades Python never
   // sees, and every later trade shifts.
   if(HistorySelectByPosition(g_pos_id))
     {{
      for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
        {{
         ulong d = HistoryDealGetTicket(i);
         if(d == 0)
            continue;
         if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT)
            continue;
         datetime dt_out = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         if(dt_out >= g_rates[0].time)
            g_closed_in_bar = true;
         break;
        }}
     }}
  }}

//+------------------------------------------------------------------+
ulong FindOurPosition()
  {{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {{
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return(tk);
     }}
   return(0);
  }}

//+------------------------------------------------------------------+
void TryEnter(bool is_long)
  {{
{DISTANCES}
   if(sl_dist <= 0.0 && tp_dist <= 0.0 && MAX_HOLD_BARS <= 0 && !{HAS_EXIT_EXPR})
      return;

{SIZING}

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double entry = is_long ? ask : bid;
   double sl = 0.0, tp = 0.0;
   if(sl_dist > 0.0)
      sl = NormalizeDouble(is_long ? entry - sl_dist : entry + sl_dist, g_digits);
   if(tp_dist > 0.0)
      tp = NormalizeDouble(is_long ? entry + tp_dist : entry - tp_dist, g_digits);

   double stop_min = g_stoplevel * g_point;
   if(stop_min > 0.0)
     {{
      if(sl > 0.0 && MathAbs(entry - sl) < stop_min) return;
      if(tp > 0.0 && MathAbs(entry - tp) < stop_min) return;
     }}

   g_o_atr    = atr_v;
   g_o_spread = (double)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   g_o_lots   = lots;

   bool ok = is_long ? g_trade.Buy(lots, _Symbol, 0.0, sl, tp, STRAT_NAME)
                     : g_trade.Sell(lots, _Symbol, 0.0, sl, tp, STRAT_NAME);
   if(!ok)
     {{
      PrintFormat("[%s] order failed retcode=%d %s",
                  STRAT_NAME, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
      return;
     }}

   ulong tk = FindOurPosition();
   if(tk == 0)
      return;
   g_ticket   = tk;
   g_in_pos   = true;
   g_dir_long = is_long;
   g_entry_bar = g_bar_index;
   g_last_entry_bar = g_bar_index;
   g_close_reason = "";
   g_trailed = false;

   if(PositionSelectByTicket(g_ticket))
     {{
      g_o_dir   = is_long ? "long" : "short";
      g_o_time  = (datetime)PositionGetInteger(POSITION_TIME);
      g_o_price = PositionGetDouble(POSITION_PRICE_OPEN);
      g_o_sl    = PositionGetDouble(POSITION_SL);
      g_o_tp    = PositionGetDouble(POSITION_TP);
      g_o_lots  = PositionGetDouble(POSITION_VOLUME);
      g_o_bar   = g_bar_index;
      g_peak    = g_o_price;

      int k = g_rec_next;
      g_rec_next = (g_rec_next + 1) % REC_N;
      g_cur_slot = k;
      g_pos_id         = PositionGetInteger(POSITION_IDENTIFIER);
      g_rec_pid[k]     = g_pos_id;
      g_rec_dir[k]     = g_o_dir;
      g_rec_time[k]    = g_o_time;
      g_rec_price[k]   = g_o_price;
      g_rec_sl[k]      = g_o_sl;
      g_rec_tp[k]      = g_o_tp;
      g_rec_atr[k]     = g_o_atr;
      g_rec_spread[k]  = g_o_spread;
      g_rec_lots[k]    = g_o_lots;
      g_rec_bar[k]     = g_bar_index;
      g_rec_trailed[k] = false;
     }}
  }}

//+------------------------------------------------------------------+
{TRAIL_FUNC}

//+------------------------------------------------------------------+
void ClosePos(string reason)
  {{
   if(!g_in_pos)
      return;
   g_close_reason = reason;
   if(!g_trade.PositionClose(g_ticket))
      PrintFormat("[%s] close failed retcode=%d", STRAT_NAME, g_trade.ResultRetcode());
   g_in_pos = false;
  }}

//+------------------------------------------------------------------+
void OnNewBar()
  {{
   g_bar_index++;
   // The tester serves history from BEFORE the start date, so warmup is a
   // question of "are enough bars available", never "how long has the EA been
   // running".  Counting bars since start would delay the first trade by
   // WARMUP_BARS versus the Python engine and break reconciliation.
   if(CopyRates(_Symbol, PERIOD_CURRENT, 0, RATES_COUNT, g_rates) < RATES_COUNT)
      return;
{HTF_REFRESH}

   g_closed_in_bar = false;
   SyncPosition();
   UpdateTrail();

   bool closed_this_bar = g_closed_in_bar;
   if(g_in_pos)
     {{
      long bars_held = g_bar_index - g_entry_bar;
      bool exit_sig = g_dir_long ? ({EXIT_LONG}) : ({EXIT_SHORT});
      if(MAX_HOLD_BARS > 0 && bars_held >= MAX_HOLD_BARS)
        {{ ClosePos("TIME"); closed_this_bar = true; }}
      else if(exit_sig)
        {{ ClosePos("SIGNAL"); closed_this_bar = true; }}
     }}

   if(!g_in_pos && !closed_this_bar &&
      (g_bar_index - g_last_entry_bar) >= MIN_BARS_BETWEEN)
     {{
      if({ENTRY_LONG})
         TryEnter(true);
      else if({ENTRY_SHORT})
         TryEnter(false);
     }}

   if(InpWriteBars && g_fh_bars != INVALID_HANDLE && g_bars_written < InpBarsCsvMax)
     {{
      g_bars_written++;
      FileWrite(g_fh_bars, {BARS_ROW});
     }}
  }}

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest    &request,
                        const MqlTradeResult     &result)
  {{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;
   ulong deal = trans.deal;
   if(deal == 0 || !HistoryDealSelect(deal))
      return;
   if(HistoryDealGetInteger(deal, DEAL_MAGIC) != InpMagic)
      return;
   if(HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
      return;

   WriteTradeRow(deal);
   g_in_pos = false;
   g_close_reason = "";
  }}
//+------------------------------------------------------------------+
'''


def generate(strategy: Strategy, out_dir=None, source_file="<inline>") -> str:
    """Write ``<name>.mq5`` and return its path."""
    import datetime as _dt

    out_dir = out_dir or BUILD_DIR
    os.makedirs(out_dir, exist_ok=True)

    warm = strategy.warmup_bars()
    rates = warm + 64

    bar_names = ["time"] + list(strategy.order) + ["sig_long", "sig_short", "spread"]
    bars_header = ", ".join('"%s"' % b for b in bar_names)

    row_parts = ['TimeToString(g_rates[1].time, TIME_DATE|TIME_SECONDS)']
    for nm in strategy.order:
        row_parts.append("DoubleToString(Ind_%s(0), 10)" % nm)
    row_parts.append("(%s ? \"1\" : \"0\")" % _mq_bool_expr(strategy.compiled["entry_long"]))
    row_parts.append("(%s ? \"1\" : \"0\")" % _mq_bool_expr(strategy.compiled["entry_short"]))
    row_parts.append('IntegerToString((int)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD))')
    bars_row = ",\n                          ".join(row_parts)

    has_exit_expr = "true" if (strategy.exits.exit_long or strategy.exits.exit_short) else "false"
    htf_decls, htf_refresh, htf_init = _htf_code(strategy)

    text = TEMPLATE.format(
        HTF_DECLS=htf_decls,
        HTF_REFRESH=htf_refresh,
        HTF_INIT=htf_init,
        TIME_HELPERS=mq5_time_helpers(strategy.broker_gmt_offset,
                                      strategy.broker_dst),
        SIZING=_sizing_code(strategy),
        TRAIL_FUNC=_trail_code(strategy),
        NAME=strategy.name,
        SRCFILE=source_file,
        STAMP=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        SYMBOL=strategy.symbol,
        LOT="%.2f" % strategy.lot,
        MAGIC=int(strategy.magic),
        WARMUP=warm,
        RATES=rates,
        MAXHOLD=int(strategy.exits.max_hold_bars or 0),
        GAP=int(strategy.min_bars_between or 0),
        TFENUM=TIMEFRAMES[strategy.timeframe][1],
        INDICATORS=_indicator_functions(strategy),
        DISTANCES=_distance_code(strategy),
        HAS_EXIT_EXPR=has_exit_expr,
        ENTRY_LONG=_mq_bool_expr(strategy.compiled["entry_long"]),
        ENTRY_SHORT=_mq_bool_expr(strategy.compiled["entry_short"]),
        EXIT_LONG=_mq_bool_expr(strategy.compiled["exit_long"]),
        EXIT_SHORT=_mq_bool_expr(strategy.compiled["exit_short"]),
        TRADES_CSV=trades_csv_name(strategy),
        BARS_CSV=bars_csv_name(strategy),
        BARS_HEADER=bars_header,
        BARS_ROW=bars_row,
    )

    path = os.path.join(out_dir, "%s.mq5" % strategy.name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def trades_csv_name(strategy: Strategy) -> str:
    return "fxtrade_%s_trades.csv" % strategy.name


def bars_csv_name(strategy: Strategy) -> str:
    return "fxtrade_%s_bars.csv" % strategy.name
