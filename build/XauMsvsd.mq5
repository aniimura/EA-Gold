//+------------------------------------------------------------------+
//| XauMsvsd.mq5                                                     |
//| XAU Multi-Speed Volatility-Scaled Donchian Trend                 |
//|                                                                  |
//| Third implementation of one specification. The Pine script runs  |
//| on TradingView, msvsd/ runs in Python, this runs in the MT5       |
//| Strategy Tester. It exists to CHECK the Python result on an       |
//| engine that models the broker's real swap, real spread and real  |
//| order handling - not to be a better backtest.                    |
//|                                                                  |
//| HAND-WRITTEN, not produced by codegen/mq5gen.py. That generator  |
//| emits one position driven by one entry expression; this strategy |
//| is three virtual sleeves netted into a single broker position,   |
//| which the declarative spec cannot express.                       |
//|                                                                  |
//| EXECUTION MODEL - matches msvsd/engine.py exactly                |
//|   Everything is evaluated on the COMPLETED bar (shift 1) and the |
//|   resulting order is sent immediately, which is the open of the  |
//|   new bar (shift 0). An order decided from bar k-1's close        |
//|   therefore fills at bar k's open, never earlier.                |
//|                                                                  |
//|   Donchian levels exclude the evaluation bar: they are taken     |
//|   over shifts 2..n+1, which is ta.highest(high[1], n) in Pine    |
//|   and donchian(..., include_current=False) in Python.            |
//|                                                                  |
//| WHAT DELIBERATELY DIFFERS FROM THE PYTHON RUN                    |
//|   Swap      MT5 applies the BROKER's real historical swap. The   |
//|             Python engine applies one assumed rate pair. This is |
//|             the single most valuable thing this EA checks.       |
//|   Spread    the tester uses the spread in the tick/minute data;  |
//|             Python uses the spread column of the H4 cache.       |
//|   Slippage  the tester models none; Python adds 5 points/side.   |
//|   Expect the SIGNALS to reconcile bar by bar and the P&L to      |
//|   differ by those three items. Both are reported.                |
//+------------------------------------------------------------------+
#property copyright "FxTrade_202608"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//--- sleeve windows ---------------------------------------------------------
input int    InpFastEntry      = 20;      // Fast   entry window
input int    InpFastExit       = 10;      // Fast   exit window
input int    InpMedEntry       = 55;      // Medium entry window
input int    InpMedExit        = 20;      // Medium exit window
input int    InpSlowEntry      = 120;     // Slow   entry window
input int    InpSlowExit       = 40;      // Slow   exit window
input bool   InpUseFast        = true;
input bool   InpUseMed         = true;
input bool   InpUseSlow        = true;

//--- risk -------------------------------------------------------------------
input double InpRiskPct        = 0.10;    // risk % of equity per sleeve
input int    InpAtrLen         = 20;      // ATR length (H4 bars)
input double InpAtrMult        = 2.5;     // ATR stop multiplier
input double InpMaxNotionalX   = 1.5;     // max net notional, x equity

//--- behaviour --------------------------------------------------------------
input bool   InpAllowReversal  = true;    // a sleeve may flip on its exit bar
input bool   InpUseFriday      = true;    // block new sleeves late Friday
input int    InpFridayHourNY   = 13;      // New York cut-off hour
input int    InpFridayBasis    = 0;       // 0 = bar OPEN (Python default), 1 = bar CLOSE (Pine)
input int    InpBrokerGmtWinter= 2;       // server offset in winter (FxPro = EET)
input string InpAtrSeedFrom    = "2021.12.31 16:00";  // seed ta.atr here (server time);
                                          // empty = use all available history. Set this to the
                                          // first bar of the Python H4 cache and the RMA
                                          // recursions become identical rather than merely
                                          // convergent - see the reconciliation notes.

//--- execution --------------------------------------------------------------
input long   InpMagic          = 20260902;
input int    InpSlippagePoints = 30;      // max deviation allowed on a market order

//--- minimum-lot override (OFF by default) ----------------------------------
// Lets a small account trade the broker minimum when the normal 0.10 % size
// rounds below it. The two caps are PERMISSION limits, not sizing targets -
// the target stays InpRiskPct. Mirrors msvsd/sizing.py exactly.
input bool   InpEnableMinLotOverride = false;
input double InpOverrideMaxRiskPct   = 0.50;  // per-sleeve cap, % of equity
input double InpMaxTotalOpenRiskPct  = 1.00;  // gross open-risk cap, % of equity
input double InpStopExitSlipPoints   = 5.0;   // assumed slippage on a stop exit
input double InpCommissionPerLotRT   = 7.85;  // USD per lot round turn, for the gate
// Normally these come from SYMBOL_VOLUME_MIN / SYMBOL_VOLUME_STEP / tick data.
// The explicit overrides exist so Python and MQL5 can be reconciled under
// identical contract assumptions; 0 means "ask the symbol".
input double InpMinLotOverrideVal    = 0.0;
input double InpLotStepOverrideVal   = 0.0;
input double InpTickSizeOverrideVal  = 0.0;
input double InpTickValueOverrideVal = 0.0;
input double InpContractOverrideVal  = 0.0;
input bool   InpSizingSelfTest       = false; // run the parity table, then stop

//--- output -----------------------------------------------------------------
input bool   InpWriteTrades    = true;
input bool   InpWriteBars      = false;   // per-bar debug export for --reconcile
input int    InpBarsCsvMax     = 20000;

//============================================================================
// state
//============================================================================
#define NS 3                                // number of sleeves
string  g_name[NS]   = {"fast","medium","slow"};
int     g_entLen[NS], g_exLen[NS];
bool    g_use[NS];

int     g_dir[NS];                          // -1 short, 0 flat, +1 long
double  g_lots[NS];                         // sleeve size, frozen at entry
double  g_entryPx[NS];                      // actual fill price
double  g_stopPx[NS];                       // initial stop; never widened
double  g_atrEnt[NS];                       // ATR frozen at the entry signal
int     g_pending[NS];                      // 1 = filled at the NEXT bar's open
datetime g_entryTime[NS];
int     g_entryBar[NS];
double  g_rawLots[NS];                      // pre-rounding size, for stats

// per-bar cache of channel levels, so the CSV and the logic cannot diverge
double  g_entHi[NS], g_entLo[NS], g_exHi[NS], g_exLo[NS];
int     g_reason[NS];

double  g_atr        = 0.0;                 // Pine ta.atr(20): RMA of TR
bool    g_atrSeeded  = false;
double  g_prevClose  = 0.0;

CTrade  g_trade;
int     g_fhTrades   = INVALID_HANDLE;
int     g_fhBars     = INVALID_HANDLE;
int     g_tradeIdx   = 0;
int     g_barsWritten= 0;
int     g_barIndex   = 0;                   // evaluation-bar counter
datetime g_lastBar   = 0;

double  g_contract   = 100.0;
double  g_volStep    = 0.01;
double  g_volMin     = 0.01;
double  g_volMax     = 100.0;
double  g_point      = 0.01;
int     g_digits     = 2;
bool    g_hedging    = false;

double  g_minLot     = 0.01;                // effective broker minimum
double  g_tickSize   = 0.01;
double  g_tickValue  = 1.0;
int     g_fhSizing   = INVALID_HANDLE;
int     g_sizeRows   = 0;

// Mutable mirrors of the gate inputs. The live path copies the inputs into
// these once at init and never touches them again; the self test varies them
// per case, which an `input` cannot do.
bool    g_ovEnable   = false;
double  g_ovSleeveCap= 0.50;
double  g_ovTotalCap = 1.00;
double  g_slipPts    = 5.0;
double  g_stopSlipPts= 5.0;
double  g_commLotRT  = 7.85;

// stable labels - a wire format shared with msvsd/sizing.py and the Pine
#define RSN_ACCEPT_NORMAL   "ORDER_ACCEPTED_NORMAL_SIZE"
#define RSN_ACCEPT_OVERRIDE "ORDER_ACCEPTED_MINIMUM_OVERRIDE"
#define RSN_OVERRIDE_OFF    "OVERRIDE_DISABLED"
#define RSN_SLEEVE_RISK     "OVERRIDE_SLEEVE_RISK_EXCEEDED"
#define RSN_PORTFOLIO_RISK  "PORTFOLIO_OPEN_RISK_EXCEEDED"
#define CND_BELOW_MIN       "NORMAL_SIZE_BELOW_MINIMUM"
#define CND_NORMAL          "NORMAL_SIZE_OK"

struct SizeDecision
  {
   double raw_lots, rounded_lots, final_lots;
   double stop_distance, entry_price, stop_price;
   double price_stop_loss, est_entry_cost, est_exit_cost, est_costs;
   double actual_stop_risk, actual_stop_risk_pct;
   double open_before, open_after, open_pct_before, open_pct_after;
   bool   override_considered, override_used;
   string condition, reason;
  };

double  g_netTarget  = 0.0;                 // last computed target, in lots
double  g_netRaw     = 0.0;
double  g_capLots    = 0.0;

// reason codes - a wire format shared with msvsd/__init__.py and the Pine.
// Append, never renumber.
#define R_NONE        0
#define R_ENTRY_LONG  1
#define R_ENTRY_SHORT 2
#define R_EXIT_CHAN   3
#define R_EXIT_STOP   4
#define R_EXIT_DISABL 5
#define R_EXIT_DIRMODE 6
#define R_EXIT_END    7
#define R_EXIT_GAP    8

//============================================================================
// time helpers - the server->UTC->New York chain, spelled out
//============================================================================
// Both engines must agree on what "Friday 13:00 New York" means, and the two
// DST rules are different: the broker runs the EU rule, New York the US one.
// core/timeutil.py implements the EU half; pandas' tz database the US half.
// Both are reimplemented here rather than approximated with a fixed offset.
datetime LastSundayOfMonth(int year,int month)
  {
   MqlDateTime t; t.year=year; t.mon=month; t.day=1; t.hour=0; t.min=0; t.sec=0;
   int dim[13] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
   int d = dim[month];
   if(month==2 && ((year%4==0 && year%100!=0) || year%400==0)) d=29;
   t.day=d;
   datetime last = StructToTime(t);
   MqlDateTime lt; TimeToStruct(last,lt);
   return last - lt.day_of_week*86400;
  }

datetime NthSundayOfMonth(int year,int month,int nth)
  {
   MqlDateTime t; t.year=year; t.mon=month; t.day=1; t.hour=0; t.min=0; t.sec=0;
   datetime first = StructToTime(t);
   MqlDateTime ft; TimeToStruct(first,ft);
   int offs = (7 - ft.day_of_week) % 7;      // days to the first Sunday
   return first + (offs + 7*(nth-1))*86400;
  }

// EU summer time: last Sunday of March 01:00 UTC .. last Sunday of October 01:00 UTC
bool EuDstActive(datetime utc)
  {
   MqlDateTime t; TimeToStruct(utc,t);
   datetime start = LastSundayOfMonth(t.year,3)  + 3600;
   datetime end   = LastSundayOfMonth(t.year,10) + 3600;
   return (utc>=start && utc<end);
  }

// US summer time: 2nd Sunday of March 02:00 local .. 1st Sunday of November 02:00
bool UsDstActive(datetime utc)
  {
   MqlDateTime t; TimeToStruct(utc,t);
   datetime start = NthSundayOfMonth(t.year,3,2) + 2*3600 + 5*3600;   // 02:00 EST
   datetime end   = NthSundayOfMonth(t.year,11,1)+ 2*3600 + 4*3600;   // 02:00 EDT
   return (utc>=start && utc<end);
  }

datetime ServerToUtc(datetime server)
  {
   // decide DST on the winter-shifted stamp, exactly as core/timeutil.py does
   datetime probe = server - InpBrokerGmtWinter*3600;
   int off = InpBrokerGmtWinter + (EuDstActive(probe) ? 1 : 0);
   return server - off*3600;
  }

datetime UtcToNy(datetime utc)
  {
   return utc - (UsDstActive(utc) ? 4 : 5)*3600;
  }

//============================================================================
// indicators - Pine semantics
//============================================================================
double TrueRangeAt(const MqlRates &r[], int i)
  {
   // r[] is series-ordered (0 = newest). i+1 is the previous bar.
   if(i+1 >= ArraySize(r)) return r[i].high - r[i].low;
   double pc = r[i+1].close;
   return MathMax(r[i].high - r[i].low,
          MathMax(MathAbs(r[i].high - pc), MathAbs(r[i].low - pc)));
  }

// ta.atr(len) == ta.rma(ta.tr, len), seeded with the SMA of the first `len`
// true ranges. Seeded once from the oldest bar the terminal holds, then
// advanced one bar at a time so the recursion matches Python's exactly.
bool SeedAtr(int evalShift)
  {
   int avail = Bars(_Symbol,_Period);
   int depth = MathMin(avail-1, 5000);

   // Anchor the seed to a specific bar when asked. ta.rma is a recursion with
   // infinite memory: two runs that start it at different bars converge but
   // never become equal, which shows up as a small early ATR difference and a
   // handful of differently-sized entries. Starting both at the same bar makes
   // the two engines agree exactly instead of approximately.
   if(StringLen(InpAtrSeedFrom) > 0)
     {
      datetime anchor = StringToTime(InpAtrSeedFrom);
      int sh = iBarShift(_Symbol,_Period,anchor,false);
      if(sh > evalShift) depth = MathMin(sh - evalShift + 1, 20000);
      else PrintFormat("ATR seed anchor %s is not before the first evaluation bar "
                       "- falling back to all available history", InpAtrSeedFrom);
     }
   if(depth < InpAtrLen*3) return false;

   MqlRates r[];
   ArraySetAsSeries(r,true);
   int got = CopyRates(_Symbol,_Period,evalShift,depth,r);
   if(got < InpAtrLen*3) return false;

   // walk from the oldest bar forward
   int oldest = got-1;
   double sum = 0.0;
   for(int k=0;k<InpAtrLen;k++)
      sum += TrueRangeAt(r, oldest-k);
   double a = sum/InpAtrLen;                       // SMA seed
   double alpha = 1.0/InpAtrLen;
   for(int i=oldest-InpAtrLen; i>=0; i--)
      a = alpha*TrueRangeAt(r,i) + (1.0-alpha)*a;
   g_atr = a;
   g_atrSeeded = true;
   return true;
  }

void AdvanceAtr(const MqlRates &r[], int evalIdx)
  {
   double alpha = 1.0/InpAtrLen;
   g_atr = alpha*TrueRangeAt(r,evalIdx) + (1.0-alpha)*g_atr;
  }

// Donchian over the `len` bars BEFORE the evaluation bar.
// evalIdx is the index of the evaluation bar inside r[]; the window is
// evalIdx+1 .. evalIdx+len, which is ta.highest(high[1], len).
void DonchianAt(const MqlRates &r[], int evalIdx, int len, double &hi, double &lo)
  {
   hi = -DBL_MAX; lo = DBL_MAX;
   for(int k=1;k<=len;k++)
     {
      int j = evalIdx + k;
      if(j >= ArraySize(r)) { hi = EMPTY_VALUE; lo = EMPTY_VALUE; return; }
      if(r[j].high > hi) hi = r[j].high;
      if(r[j].low  < lo) lo = r[j].low;
     }
  }

double FloorStep(double v,double step)
  {
   if(step<=0) return MathMax(0.0,v);
   return MathMax(0.0, MathFloor(v/step + 1e-9)*step);
  }

//============================================================================
// position management - one net exposure, whatever the account mode
//============================================================================
double NetPositionLots()
  {
   double net = 0.0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk==0) continue;
      if(!PositionSelectByTicket(tk)) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
      double v = PositionGetDouble(POSITION_VOLUME);
      net += (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY) ? v : -v;
     }
   return net;
  }

double TotalSwapPaid()
  {
   // swap accrued on positions still open; closed-position swap comes from the
   // deal history and is added by ClosedSwap()
   double s = 0.0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk==0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      s += PositionGetDouble(POSITION_SWAP);
     }
   return s;
  }

double g_closedSwap = 0.0;        // accumulated as positions close

// Reduce exposure by `want` lots on the side opposite to `dir`, closing
// existing positions (partially where needed). Returns the lots actually closed.
double ReduceSide(int closeType,double want)
  {
   double done = 0.0;
   for(int i=PositionsTotal()-1;i>=0 && want>g_volStep/2.0;i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk==0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
      if((int)PositionGetInteger(POSITION_TYPE)!=closeType) continue;

      double vol = PositionGetDouble(POSITION_VOLUME);
      double take = MathMin(vol, want);
      take = FloorStep(take, g_volStep);
      if(take < g_volMin - 1e-9)
        {
         if(vol <= want + 1e-9) take = vol; else continue;
        }
      g_closedSwap += PositionGetDouble(POSITION_SWAP);
      bool ok = (take >= vol - 1e-9) ? g_trade.PositionClose(tk)
                                     : g_trade.PositionClosePartial(tk, take);
      if(ok) { done += take; want -= take; }
      else   { break; }
     }
   return done;
  }

// Drive the account to `target` signed lots, trading only the delta.
void SetNetPosition(double target)
  {
   double cur   = NetPositionLots();
   double delta = target - cur;
   if(MathAbs(delta) < g_volStep/2.0) return;

   if(delta > 0)
     {
      double need = delta;
      double closed = ReduceSide(POSITION_TYPE_SELL, need);   // buy back shorts first
      need -= closed;
      need = FloorStep(need, g_volStep);
      if(need >= g_volMin - 1e-9)
         g_trade.Buy(NormalizeDouble(need,2), _Symbol, 0.0, 0.0, 0.0, "msvsd net+");
     }
   else
     {
      double need = -delta;
      double closed = ReduceSide(POSITION_TYPE_BUY, need);
      need -= closed;
      need = FloorStep(need, g_volStep);
      if(need >= g_volMin - 1e-9)
         g_trade.Sell(NormalizeDouble(need,2), _Symbol, 0.0, 0.0, 0.0, "msvsd net-");
     }
  }

//============================================================================
// sizing and the minimum-lot override   (mirror of msvsd/sizing.py)
//============================================================================
// USD per 1.0 of price movement, per lot. Prefers the tick metadata, which is
// the platform-native form and generalises off XAUUSD; falls back to contract
// size. For a 100 oz gold contract the two agree exactly: 1.0 / 0.01 == 100.
double MoneyPerPricePerLot()
  {
   if(g_tickSize > 0.0 && g_tickValue > 0.0) return g_tickValue/g_tickSize;
   return g_contract;
  }

double CommPerOzSide()
  {
   return (g_commLotRT/2.0)/g_contract;
  }

// Bars are BID: a long pays the spread entering, a short pays it on exit, so a
// round trip costs the same either way and the gate cannot favour a side.
double EntryCostOf(double lots,int dir,double spread_price)
  {
   double oz = lots*g_contract;
   double spr = (dir > 0) ? spread_price : 0.0;
   return (spr + g_slipPts*g_point)*oz + CommPerOzSide()*oz;
  }

double ExitCostOf(double lots,int dir,double spread_price)
  {
   double oz = lots*g_contract;
   double spr = (dir < 0) ? spread_price : 0.0;
   return (spr + g_stopSlipPts*g_point)*oz + CommPerOzSide()*oz;
  }

// Capital still at risk in one open sleeve, from here to its stop. GROSS: a
// long and a short of equal size leave the BROKER flat, yet both can still lose
// at their own stop, and netting them would hide that. A stop that locks in a
// profit contributes ZERO, never a negative - a winner may not finance a new
// position.
double SleeveOpenRiskAt(int idx,double price,double spread_price)
  {
   if(g_dir[idx] == 0 || g_lots[idx] <= 0.0 || g_stopPx[idx] == EMPTY_VALUE)
      return 0.0;
   double adverse = (g_dir[idx] > 0) ? (price - g_stopPx[idx])
                                     : (g_stopPx[idx] - price);
   if(adverse <= 0.0) return 0.0;
   return adverse*MoneyPerPricePerLot()*g_lots[idx]
          + ExitCostOf(g_lots[idx], g_dir[idx], spread_price);
  }

double TotalOpenRiskAt(int exclude_idx,double price,double spread_price)
  {
   double t = 0.0;
   for(int k=0;k<NS;k++)
      if(k != exclude_idx) t += SleeveOpenRiskAt(k, price, spread_price);
   return t;
  }

SizeDecision DecideSize(int idx,int dir,double atr_now,double riskCash,
                        double equity,double price,double spread_price)
  {
   SizeDecision d;
   double mpp = MoneyPerPricePerLot();
   d.stop_distance = atr_now*InpAtrMult;
   d.entry_price   = price;
   d.stop_price    = price - d.stop_distance*dir;
   d.override_considered = false;
   d.override_used = false;
   d.final_lots = 0.0;
   d.price_stop_loss = 0.0; d.est_entry_cost = 0.0; d.est_exit_cost = 0.0;
   d.est_costs = 0.0; d.actual_stop_risk = 0.0; d.actual_stop_risk_pct = 0.0;
   d.condition = CND_NORMAL; d.reason = "";

   double riskPerLot = d.stop_distance*mpp;
   d.raw_lots     = (riskPerLot > 0.0) ? riskCash/riskPerLot : 0.0;
   d.rounded_lots = FloorStep(d.raw_lots, g_volStep);

   d.open_before     = TotalOpenRiskAt(idx, price, spread_price);
   d.open_pct_before = (equity > 0.0) ? 100.0*d.open_before/equity : 0.0;
   d.open_after      = d.open_before;
   d.open_pct_after  = d.open_pct_before;

   double test = 0.0;
   if(d.rounded_lots >= g_minLot - 1e-12 && d.rounded_lots > 0.0)
     {
      d.reason = RSN_ACCEPT_NORMAL;
      d.final_lots = d.rounded_lots;
      test = d.final_lots;
     }
   else
     {
      d.condition = CND_BELOW_MIN;
      if(!g_ovEnable)
        {
         d.reason = RSN_OVERRIDE_OFF;
         return d;
        }
      d.override_considered = true;
      test = g_minLot;            // exactly one minimum-size position, never more
     }

   d.price_stop_loss = d.stop_distance*mpp*test;
   d.est_entry_cost  = EntryCostOf(test, dir, spread_price);
   d.est_exit_cost   = ExitCostOf(test, dir, spread_price);
   d.est_costs       = d.est_entry_cost + d.est_exit_cost;
   d.actual_stop_risk     = d.price_stop_loss + d.est_costs;
   d.actual_stop_risk_pct = (equity > 0.0) ? 100.0*d.actual_stop_risk/equity : 0.0;
   d.open_after     = d.open_before + d.actual_stop_risk;
   d.open_pct_after = (equity > 0.0) ? 100.0*d.open_after/equity : 0.0;

   if(d.reason == RSN_ACCEPT_NORMAL) return d;   // normal path ignores the caps

   double EPS = 1e-9;
   if(d.actual_stop_risk_pct > g_ovSleeveCap + EPS)
     { d.reason = RSN_SLEEVE_RISK; return d; }
   if(d.open_pct_after > g_ovTotalCap + EPS)
     { d.reason = RSN_PORTFOLIO_RISK; return d; }

   d.final_lots = test;
   d.override_used = true;
   d.reason = RSN_ACCEPT_OVERRIDE;
   return d;
  }

void WriteSizingRow(datetime when,int idx,int dir,const SizeDecision &d)
  {
   if(g_fhSizing == INVALID_HANDLE) return;
   g_sizeRows++;
   FileWrite(g_fhSizing,
      TimeToString(when, TIME_DATE|TIME_SECONDS),
      g_name[idx], (dir==1 ? "long" : "short"),
      DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2),
      DoubleToString(g_atr,8),
      DoubleToString(d.entry_price,g_digits), DoubleToString(d.stop_price,g_digits),
      DoubleToString(d.stop_distance,g_digits),
      DoubleToString(d.raw_lots,6), DoubleToString(d.rounded_lots,6),
      DoubleToString(d.final_lots,6), DoubleToString(g_minLot,6),
      DoubleToString(g_volStep,6),
      (d.override_considered ? "true" : "false"),
      (d.override_used ? "true" : "false"),
      DoubleToString(d.price_stop_loss,6),
      DoubleToString(d.est_entry_cost,6), DoubleToString(d.est_exit_cost,6),
      DoubleToString(d.est_costs,6),
      DoubleToString(d.actual_stop_risk,6),
      DoubleToString(d.actual_stop_risk_pct,6),
      DoubleToString(d.open_before,6), DoubleToString(d.open_after,6),
      DoubleToString(d.open_pct_before,6), DoubleToString(d.open_pct_after,6),
      d.condition, d.reason);
  }

//============================================================================
// sleeve bookkeeping
//============================================================================
void SleeveReset(int s)
  {
   g_dir[s]=0; g_lots[s]=0.0; g_entryPx[s]=EMPTY_VALUE; g_stopPx[s]=EMPTY_VALUE;
   g_atrEnt[s]=EMPTY_VALUE; g_pending[s]=0; g_entryTime[s]=0; g_entryBar[s]=-1;
   g_rawLots[s]=0.0;
  }

string ReasonName(int code)
  {
   switch(code)
     {
      case R_ENTRY_LONG:  return "ENTRY_LONG";
      case R_ENTRY_SHORT: return "ENTRY_SHORT";
      case R_EXIT_CHAN:   return "EXIT_CHANNEL";
      case R_EXIT_STOP:   return "EXIT_PROTECTIVE_STOP";
      case R_EXIT_DISABL: return "EXIT_SLEEVE_DISABLED";
      case R_EXIT_DIRMODE:return "EXIT_DIRECTION_MODE";
      case R_EXIT_END:    return "EXIT_END_OF_DATA";
      case R_EXIT_GAP:    return "EXIT_STOP_GAP";
     }
   return "NONE";
  }

void WriteTrade(int s,int dir,double lots,double entryPx,double stopPx,
                double atrEnt,datetime entryTime,int entryBar,
                double exitPx,datetime exitTime,int exitBar,int reason)
  {
   if(!InpWriteTrades || g_fhTrades==INVALID_HANDLE) return;
   double stopDist = atrEnt*InpAtrMult;
   double pts = (exitPx-entryPx)*dir;
   g_tradeIdx++;
   FileWrite(g_fhTrades,
      g_tradeIdx,
      g_name[s],
      (dir==1 ? "long" : "short"),
      entryBar,
      TimeToString(entryTime, TIME_DATE|TIME_SECONDS),
      DoubleToString(entryPx, g_digits),
      DoubleToString(stopPx,  g_digits),
      "0",                                   // tp - the strategy has none
      DoubleToString(atrEnt, 5),
      "0",                                   // entry_spread_points (unused)
      exitBar,
      TimeToString(exitTime, TIME_DATE|TIME_SECONDS),
      DoubleToString(exitPx, g_digits),
      ReasonName(reason),
      exitBar-entryBar,
      DoubleToString(lots,2),
      DoubleToString(pts,g_digits),
      DoubleToString(pts*lots*g_contract,2),
      DoubleToString(stopDist>0 ? pts/stopDist : 0.0, 5));
  }


//============================================================================
// Python / MQL5 sizing parity self test
//   Duplicated verbatim from tests/parity_cases.py. Both sides decide every
//   row; tests/test_min_lot_override.py compares them and asserts the row
//   COUNT matches, so a case added on one side and not the other fails loudly
//   instead of silently comparing fewer rows.
//============================================================================
void SelfTestCase(string id, double equity, double stop_dist, int dir,
                  bool enable, double contract_oz, double min_lot,
                  double lot_step, double spread, double entry_slip,
                  double stop_slip, double comm_lot_rt,
                  int n_open, const int &odir[], const double &olots[],
                  const double &ostop[])
  {
   // stage the instrument and cost assumptions for this row
   g_contract = contract_oz; g_minLot = min_lot; g_volStep = lot_step;
   g_tickSize = 0.0; g_tickValue = 0.0;          // force the contract-size path
   g_ovEnable = enable; g_ovSleeveCap = 0.50; g_ovTotalCap = 1.00;
   g_slipPts = entry_slip/g_point; g_stopSlipPts = stop_slip/g_point;
   g_commLotRT = comm_lot_rt;

   for(int k=0;k<NS;k++) { g_dir[k]=0; g_lots[k]=0.0; g_stopPx[k]=EMPTY_VALUE; }
   for(int j=0;j<n_open && j<NS-1;j++)
     { g_dir[j+1]=odir[j]; g_lots[j+1]=olots[j]; g_stopPx[j+1]=ostop[j]; }

   double price = 2000.0;
   double riskCash = equity*0.10/100.0;
   SizeDecision d = DecideSize(0, dir, stop_dist/InpAtrMult, riskCash,
                               equity, price, spread);
   FileWrite(g_fhSizing, id, DoubleToString(d.final_lots,6),
             DoubleToString(d.actual_stop_risk,6),
             DoubleToString(d.actual_stop_risk_pct,6),
             DoubleToString(d.open_before,6), DoubleToString(d.open_after,6),
             (d.override_used ? "true" : "false"), d.condition, d.reason);
  }

void SizingSelfTest()
  {
   if(g_fhSizing == INVALID_HANDLE) { Print("self test: no output file"); return; }
   FileWrite(g_fhSizing,"id","final_lots","actual_stop_risk","actual_stop_risk_pct",
             "open_before","open_after","override_used","condition","reason");
   int    od[3]; double ol[3], os[3];
   ArrayInitialize(od,0); ArrayInitialize(ol,0.0); ArrayInitialize(os,0.0);

   SelfTestCase("normal_size_large_account",100000,32.5, 1,true ,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("normal_size_override_off", 100000,32.5, 1,false,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("below_min_override_off",    10000,20.0, 1,false,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("override_accept_20usd",     10000,20.0, 1,true ,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("override_accept_on_cap_50usd",10000,50.0,1,true,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("override_reject_sleeve_51usd",10000,51.0,1,true,100,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("override_short_symmetric",  10000,20.0,-1,true ,100,0.01,0.01,0,0,0,0,0,od,ol,os);

   od[0]= 1; ol[0]=0.01; os[0]=1930.0;
   SelfTestCase("override_reject_portfolio", 10000,40.0, 1,true ,100,0.01,0.01,0,0,0,0,1,od,ol,os);
   od[0]= 1; ol[0]=0.01; os[0]=1940.0;
   SelfTestCase("override_accept_on_portfolio_cap",10000,40.0,1,true,100,0.01,0.01,0,0,0,0,1,od,ol,os);
   od[0]= 1; ol[0]=0.01; os[0]=1955.0; od[1]=-1; ol[1]=0.01; os[1]=2045.0;
   SelfTestCase("opposing_sleeves_gross",    10000,20.0, 1,true ,100,0.01,0.01,0,0,0,0,2,od,ol,os);
   od[0]= 1; ol[0]=0.01; os[0]=2050.0; od[1]=0; ol[1]=0.0; os[1]=0.0;
   SelfTestCase("winning_stop_contributes_zero",10000,20.0,1,true,100,0.01,0.01,0,0,0,0,1,od,ol,os);
   od[0]=0; ol[0]=0.0; os[0]=0.0;

   SelfTestCase("with_costs",                10000,49.0, 1,true ,100,0.01,0.01,0.5,0.5,0.5,50.0,0,od,ol,os);
   SelfTestCase("micro_contract",            10000,20.0, 1,true , 10,0.01,0.01,0,0,0,0,0,od,ol,os);
   SelfTestCase("coarse_minimum_lot",        10000,20.0, 1,true ,100,0.10,0.10,0,0,0,0,0,od,ol,os);

   FileClose(g_fhSizing); g_fhSizing = INVALID_HANDLE;
   Print("sizing self test written: 14 cases");
  }

//============================================================================
// OnInit / OnDeinit
//============================================================================
int OnInit()
  {
   g_entLen[0]=InpFastEntry; g_exLen[0]=InpFastExit;  g_use[0]=InpUseFast;
   g_entLen[1]=InpMedEntry;  g_exLen[1]=InpMedExit;   g_use[1]=InpUseMed;
   g_entLen[2]=InpSlowEntry; g_exLen[2]=InpSlowExit;  g_use[2]=InpUseSlow;
   for(int s=0;s<NS;s++) { SleeveReset(s); g_reason[s]=R_NONE; }

   g_contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   g_volStep  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   g_volMin   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   g_volMax   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   g_point    = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   g_digits   = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   g_hedging  = ((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE)
                 == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);

   if(g_contract<=0) g_contract = 100.0;
   if(g_volStep<=0)  g_volStep  = 0.01;
   g_tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   g_tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   g_minLot    = g_volMin;
   // explicit test overrides, so Python and MQL5 can be reconciled under
   // identical contract assumptions
   if(InpContractOverrideVal  > 0.0) g_contract  = InpContractOverrideVal;
   if(InpMinLotOverrideVal    > 0.0) g_minLot    = InpMinLotOverrideVal;
   if(InpLotStepOverrideVal   > 0.0) g_volStep   = InpLotStepOverrideVal;
   if(InpTickSizeOverrideVal  > 0.0) g_tickSize  = InpTickSizeOverrideVal;
   if(InpTickValueOverrideVal > 0.0) g_tickValue = InpTickValueOverrideVal;
   if(g_minLot <= 0.0) g_minLot = g_volStep;
   g_ovEnable    = InpEnableMinLotOverride;
   g_ovSleeveCap = InpOverrideMaxRiskPct;
   g_ovTotalCap  = InpMaxTotalOpenRiskPct;
   g_slipPts     = InpSlippagePoints;
   g_stopSlipPts = InpStopExitSlipPoints;
   g_commLotRT   = InpCommissionPerLotRT;

   // Cross-check the tick-value maths against the platform calculation for BOTH
   // directions. If they disagree the risk gate is being fed a wrong number, and
   // that is worth knowing at init rather than after a backtest.
   double probe_lots = MathMax(g_minLot, g_volStep);
   double px = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(px <= 0.0) px = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(px > 0.0)
     {
      double dist = 10.0*g_point*100.0;   // an arbitrary but non-trivial distance
      double want = dist*MoneyPerPricePerLot()*probe_lots;
      double got_long=0.0, got_short=0.0;
      if(OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, probe_lots, px, px-dist, got_long) &&
         OrderCalcProfit(ORDER_TYPE_SELL, _Symbol, probe_lots, px, px+dist, got_short))
        {
         double el = MathAbs(MathAbs(got_long)-want), es = MathAbs(MathAbs(got_short)-want);
         double tol = MathMax(0.01, want*1e-6);
         PrintFormat("sizing cross-check: want=%.4f long=%.4f short=%.4f %s",
                     want, got_long, got_short,
                     (el<=tol && es<=tol) ? "AGREE" : "DISAGREE - CHECK TICK METADATA");
        }
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   if(InpWriteTrades)
     {
      g_fhTrades = FileOpen("fxtrade_XauMsvsd_trades.csv",
                            FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
      if(g_fhTrades!=INVALID_HANDLE)
         FileWrite(g_fhTrades,"idx","sleeve","direction","entry_bar","entry_time",
                   "entry_price","sl","tp","entry_atr","entry_spread_points",
                   "exit_bar","exit_time","exit_price","exit_reason","bars_held",
                   "lots","points","gross","r_multiple");
     }
   if(InpEnableMinLotOverride || InpWriteBars || InpSizingSelfTest)
     {
      g_fhSizing = FileOpen("fxtrade_XauMsvsd_sizing.csv",
                            FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
      if(g_fhSizing!=INVALID_HANDLE && !InpSizingSelfTest)
         FileWrite(g_fhSizing,"time","sleeve","direction","equity","atr",
                   "entry_price","stop_price","stop_distance","raw_lots",
                   "rounded_lots","final_lots","minimum_lot","lot_step",
                   "override_considered","override_used","price_stop_loss",
                   "estimated_entry_cost","estimated_exit_cost","estimated_costs",
                   "actual_stop_risk","actual_stop_risk_pct",
                   "total_open_risk_before","total_open_risk_after",
                   "total_open_risk_pct_before","total_open_risk_pct_after",
                   "condition","reason");
     }
   if(InpSizingSelfTest)
     {
      // The table below deliberately rewrites contract size, minimum lot and
      // sleeve state per row. Refuse to continue into a backtest on it.
      SizingSelfTest();
      Print("sizing self test complete - halting before the backtest");
      return(INIT_FAILED);
     }
   if(InpWriteBars)
     {
      g_fhBars = FileOpen("fxtrade_XauMsvsd_bars.csv",
                          FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
      if(g_fhBars!=INVALID_HANDLE)
         FileWrite(g_fhBars,
            "time","time_utc","spread",
            "dbg_atr",
            "dbg_state_fast","dbg_state_medium","dbg_state_slow",
            "dbg_qty_fast","dbg_qty_medium","dbg_qty_slow",
            "dbg_stop_fast","dbg_stop_medium","dbg_stop_slow",
            "dbg_net_target_lots","dbg_position_lots",
            "dbg_reason_fast","dbg_reason_medium","dbg_reason_slow",
            "dbg_ent_hi_fast","dbg_ent_lo_fast","dbg_exit_hi_fast","dbg_exit_lo_fast",
            "dbg_ent_hi_medium","dbg_ent_lo_medium","dbg_exit_hi_medium","dbg_exit_lo_medium",
            "dbg_ent_hi_slow","dbg_ent_lo_slow","dbg_exit_hi_slow","dbg_exit_lo_slow",
            "dbg_equity_ex_financing","equity","swap_cum");
     }

   PrintFormat("XauMsvsd init: contract=%.1f volStep=%.3f digits=%d hedging=%s",
               g_contract, g_volStep, g_digits, (g_hedging?"yes":"no"));
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   // The tester force-closes any open position after the last tick, so the
   // still-open sleeves are logged here to keep the trade list complete - the
   // same thing --log-open-at-end does on the Python side.
   MqlRates r[];
   ArraySetAsSeries(r,true);
   if(CopyRates(_Symbol,_Period,0,3,r)>=2)
      for(int s=0;s<NS;s++)
         if(g_dir[s]!=0 && g_entryPx[s]!=EMPTY_VALUE)
            WriteTrade(s,g_dir[s],g_lots[s],g_entryPx[s],g_stopPx[s],g_atrEnt[s],
                       g_entryTime[s],g_entryBar[s],r[1].close,r[1].time,
                       g_barIndex,R_EXIT_END);

   if(g_fhSizing!=INVALID_HANDLE) { FileClose(g_fhSizing); g_fhSizing=INVALID_HANDLE; }
   if(g_fhTrades!=INVALID_HANDLE) { FileClose(g_fhTrades); g_fhTrades=INVALID_HANDLE; }
   if(g_fhBars  !=INVALID_HANDLE) { FileClose(g_fhBars);   g_fhBars  =INVALID_HANDLE; }
  }

//============================================================================
// the bar engine
//============================================================================
bool EntriesBlocked(datetime evalOpen, datetime evalClose)
  {
   if(!InpUseFriday) return false;
   datetime basis = (InpFridayBasis==1) ? evalClose : evalOpen;
   datetime ny    = UtcToNy(ServerToUtc(basis));
   MqlDateTime t; TimeToStruct(ny,t);
   return (t.day_of_week==5 && t.hour>=InpFridayHourNY);
  }

void RunBar()
  {
   MqlRates r[];
   ArraySetAsSeries(r,true);
   int need = MathMax(InpSlowEntry, InpAtrLen) + 8;
   int got  = CopyRates(_Symbol,_Period,0,need,r);
   if(got < need) return;

   const int E = 1;                       // evaluation bar = the completed one
   double evOpen = r[E].open, evHigh = r[E].high, evLow = r[E].low, evClose = r[E].close;
   datetime evTime = r[E].time;

   if(!g_atrSeeded) { if(!SeedAtr(E)) return; }
   else             { AdvanceAtr(r,E); }
   if(g_atr<=0.0) return;

   g_barIndex++;
   for(int s=0;s<NS;s++) g_reason[s]=R_NONE;

   // ---- (A) register the fill of an order sent on the previous evaluation.
   // That order was executed at the open of the bar we are now evaluating, so
   // its fill price is evOpen. The stop is set here from the frozen ATR and is
   // never touched again.
   for(int s=0;s<NS;s++)
      if(g_pending[s]==1)
        {
         g_entryPx[s]  = evOpen;
         g_stopPx[s]   = (g_dir[s]==1) ? evOpen - g_atrEnt[s]*InpAtrMult
                                       : evOpen + g_atrEnt[s]*InpAtrMult;
         g_entryTime[s]= evTime;
         g_entryBar[s] = g_barIndex;
         g_pending[s]  = 0;
        }

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity*InpRiskPct/100.0;
   bool   blocked  = EntriesBlocked(evTime, evTime + PeriodSeconds(_Period));

   // ---- channel levels, current bar excluded ------------------------------
   for(int s=0;s<NS;s++)
     {
      DonchianAt(r,E,g_entLen[s],g_entHi[s],g_entLo[s]);
      DonchianAt(r,E,g_exLen[s], g_exHi[s], g_exLo[s]);
     }

   // ---- per-sleeve state machine ------------------------------------------
   for(int s=0;s<NS;s++)
     {
      int exitEv = R_NONE, entEv = R_NONE;

      // (B) protective stop - highest priority. Breach detected on the
      // completed bar's range; the exit fills at the next bar's open, which is
      // now. Gap risk beyond the stop is absorbed in full.
      if(g_dir[s]!=0 && g_stopPx[s]!=EMPTY_VALUE)
        {
         bool hit = (g_dir[s]==1) ? (evLow <= g_stopPx[s]) : (evHigh >= g_stopPx[s]);
         if(hit) exitEv = R_EXIT_STOP;
        }

      // (C) Donchian channel exit
      if(exitEv==R_NONE && g_dir[s]!=0 && g_exHi[s]!=EMPTY_VALUE)
        {
         bool ex = (g_dir[s]==1) ? (evClose < g_exLo[s]) : (evClose > g_exHi[s]);
         if(ex) exitEv = R_EXIT_CHAN;
        }

      // a sleeve switched off in the inputs is flattened, not left stranded
      if(exitEv==R_NONE && g_dir[s]!=0 && !g_use[s]) exitEv = R_EXIT_DISABL;

      // (D) entry. Flat sleeves only; never adds to an existing position.
      bool canEnter = g_use[s] && (g_dir[s]==0 || exitEv!=R_NONE) && !blocked
                      && g_entHi[s]!=EMPTY_VALUE && g_atr>0.0
                      && (exitEv==R_NONE || InpAllowReversal);
      int newDir = 0;
      if(canEnter)
        {
         if(evClose > g_entHi[s])      newDir = 1;
         else if(evClose < g_entLo[s]) newDir = -1;
        }

      double raw=0.0, lots=0.0;
      bool unsized=false;
      SizeDecision dec;
      if(newDir!=0)
        {
         double spreadPrice = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)*g_point;
         dec = DecideSize(s, newDir, g_atr, riskCash, equity, evClose, spreadPrice);
         WriteSizingRow(evTime, s, newDir, dec);
         raw  = dec.raw_lots;
         lots = dec.final_lots;
         if(lots <= 0.0) { newDir=0; unsized=true; }
        }

      // close the outgoing position BEFORE the new state overwrites it
      if(exitEv!=R_NONE && g_dir[s]!=0 && g_entryPx[s]!=EMPTY_VALUE)
        {
         WriteTrade(s,g_dir[s],g_lots[s],g_entryPx[s],g_stopPx[s],g_atrEnt[s],
                    g_entryTime[s],g_entryBar[s],
                    r[0].open, r[0].time, g_barIndex, exitEv);
         g_reason[s]=exitEv;
        }

      if(newDir!=0)
        {
         SleeveReset(s);
         g_dir[s]=newDir; g_lots[s]=lots; g_rawLots[s]=raw;
         g_atrEnt[s]=g_atr; g_pending[s]=1;
         entEv = (newDir==1) ? R_ENTRY_LONG : R_ENTRY_SHORT;
         g_reason[s]=entEv;
        }
      else if(exitEv!=R_NONE)
        {
         // NOTE: the reset happens whether or not a reversal was attempted and
         // failed to size. v1 of the Python engine had a bug here - an `elif`
         // that swallowed the exit when an unsizable reversal fired on the same
         // bar, leaving the sleeve holding a position it had been told to
         // close. See CHANGELOG_msvsd_v2.md, DEFECT-V1-EXIT-SWALLOW.
         SleeveReset(s);
        }
     }

   // ---- netting: signed sum -> notional cap -> floor to the lot step -------
   g_netRaw = 0.0;
   for(int s=0;s<NS;s++) g_netRaw += g_dir[s]*g_lots[s];
   double price = (evClose>0.0) ? evClose : r[0].open;
   g_capLots = (price>0.0) ? (equity*InpMaxNotionalX)/(g_contract*price) : 0.0;
   double clipped = MathMin(MathAbs(g_netRaw), g_capLots);
   double sign    = (g_netRaw>0) ? 1.0 : ((g_netRaw<0) ? -1.0 : 0.0);
   g_netTarget    = sign*FloorStep(clipped, g_volStep);
   if(MathAbs(g_netTarget) > g_volMax) g_netTarget = sign*g_volMax;

   // ---- trade only the delta, at this bar's open ---------------------------
   // The row is written with the position as it stood BEFORE this bar's order.
   // Python stamps bar i with the position produced by bar i-1's order, so
   // recording after SetNetPosition would shift the whole series by one bar.
   double posBefore = NetPositionLots();
   SetNetPosition(g_netTarget);

   WriteBarRow(evTime, equity, posBefore);
  }

void WriteBarRow(datetime evTime,double equity,double posBefore)
  {
   if(!InpWriteBars || g_fhBars==INVALID_HANDLE || g_barsWritten>=InpBarsCsvMax) return;
   g_barsWritten++;
   double swapCum = g_closedSwap + TotalSwapPaid();
   long   spread  = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   FileWrite(g_fhBars,
      TimeToString(evTime, TIME_DATE|TIME_SECONDS),
      TimeToString(ServerToUtc(evTime), TIME_DATE|TIME_SECONDS),
      (int)spread,
      DoubleToString(g_atr,8),
      g_dir[0], g_dir[1], g_dir[2],
      DoubleToString(g_dir[0]*g_lots[0],4),
      DoubleToString(g_dir[1]*g_lots[1],4),
      DoubleToString(g_dir[2]*g_lots[2],4),
      (g_stopPx[0]==EMPTY_VALUE ? "" : DoubleToString(g_stopPx[0],8)),
      (g_stopPx[1]==EMPTY_VALUE ? "" : DoubleToString(g_stopPx[1],8)),
      (g_stopPx[2]==EMPTY_VALUE ? "" : DoubleToString(g_stopPx[2],8)),
      DoubleToString(g_netTarget,4),
      DoubleToString(posBefore,4),
      g_reason[0], g_reason[1], g_reason[2],
      DoubleToString(g_entHi[0],8), DoubleToString(g_entLo[0],8),
      DoubleToString(g_exHi[0], 8), DoubleToString(g_exLo[0], 8),
      DoubleToString(g_entHi[1],8), DoubleToString(g_entLo[1],8),
      DoubleToString(g_exHi[1], 8), DoubleToString(g_exLo[1], 8),
      DoubleToString(g_entHi[2],8), DoubleToString(g_entLo[2],8),
      DoubleToString(g_exHi[2], 8), DoubleToString(g_exLo[2], 8),
      DoubleToString(equity - swapCum,2),
      DoubleToString(equity,2),
      DoubleToString(swapCum,2));
  }

//============================================================================
void OnTick()
  {
   datetime t0 = iTime(_Symbol,_Period,0);
   if(t0==0 || t0==g_lastBar) return;      // one evaluation per completed bar
   g_lastBar = t0;
   RunBar();
  }
//+------------------------------------------------------------------+
