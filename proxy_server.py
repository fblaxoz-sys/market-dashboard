#!/usr/bin/env python3
"""
Local dev server for Market Dashboard.
  GET /fred-proxy?url=<encoded>               — CORS proxy for FRED
  GET /gdp-nowcast?fred_key=<k>&quarters=<n>  — DFM + XGBoost nowcast
  Everything else                             — static files from Downloads/
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.request, urllib.parse, json, os, traceback, time

# ── NOWCAST MODEL ────────────────────────────────────────────────────────────

def run_gdp_nowcast(fred_key, bt_quarters=12):
    import warnings; warnings.filterwarnings("ignore")
    import numpy as np
    import pandas as pd
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_squared_error
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    from statsmodels.tsa.statespace.dynamic_factor_mq import DynamicFactorMQ

    try:
        from xgboost import XGBRegressor
        HAS_XGB = True
    except Exception:
        HAS_XGB = False
        print("  XGBoost unavailable — using HistGradientBoosting")

    # ── Series definitions ───────────────────────────────────────────────
    # fmt: (fred_frequency_param_or_None, limit)
    # Using FRED's frequency=m param to convert daily/weekly to monthly server-side
    ALL_SERIES = {
        'INDPRO':    (None,  96),   # Industrial Production (monthly)
        'PAYEMS':    (None,  96),   # Nonfarm Payrolls (monthly)
        'RSAFS':     (None,  96),   # Retail Sales (monthly)
        'UMCSENT':   (None,  96),   # Consumer Sentiment (monthly)
        'T10Y2Y':    ('m',   96),   # Yield Curve 10Y-2Y (daily → monthly avg)
        'BAA10YM':   (None,  96),   # Moody's Baa Credit Spread (monthly)
        'ICSA':      ('m',   96),   # Initial Claims (weekly → monthly avg)
        'VIXCLS':    ('m',   96),   # VIX (daily → monthly avg)
        'NASDAQCOM': ('m',   96),   # NASDAQ Composite (daily → monthly avg)
    }
    CORE_IDS = ['INDPRO', 'PAYEMS', 'RSAFS', 'UMCSENT']  # go into DFM

    # ── Fetch helper ─────────────────────────────────────────────────────
    def fetch(sid, limit, freq=None):
        freq_param = f"&frequency={freq}&aggregation_method=avg" if freq else ""
        url = (f"https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={sid}&api_key={fred_key}"
               f"&sort_order=desc&limit={limit}&file_type=json{freq_param}")
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        if 'observations' not in data:
            raise ValueError(f"FRED error {sid}: {data}")
        rows = [(o['date'], float(o['value']))
                for o in data['observations'] if o['value'] != '.']
        rows.reverse()
        return pd.Series(
            [r[1] for r in rows],
            index=pd.to_datetime([r[0] for r in rows]),
            name=sid
        )

    def fetch_safe(sid, limit, freq=None, delay=1.2):
        try:
            time.sleep(delay)
            return fetch(sid, limit, freq)
        except Exception as e:
            print(f"  Warning: skipping {sid}: {e}")
            return None

    # ── Fetch all ────────────────────────────────────────────────────────
    print("  Fetching GDPC1 …")
    gdpc1 = fetch('GDPC1', 100)

    raw = {}
    for sid, (freq, limit) in ALL_SERIES.items():
        print(f"  Fetching {sid} …")
        s = fetch_safe(sid, limit, freq)
        if s is not None:
            # All are now monthly; resample just in case
            raw[sid] = s.resample('MS').last()

    # ── GDP YoY % ────────────────────────────────────────────────────────
    gdp_yoy = ((gdpc1 / gdpc1.shift(4)) - 1) * 100
    gdp_yoy = gdp_yoy.dropna()

    # ── DFM on core real-economy indicators ──────────────────────────────
    core_ids = [s for s in CORE_IDS if s in raw]
    core_df  = pd.DataFrame({s: raw[s] for s in core_ids})

    # Ragged edge: append 3 NaN months after last GDP quarter
    last_gdp  = gdpc1.index[-1]
    future    = pd.date_range(last_gdp + pd.DateOffset(months=1),
                               last_gdp + pd.DateOffset(months=3), freq='MS')
    ragged    = pd.DataFrame(float('nan'), index=future, columns=core_df.columns)
    core_full = pd.concat([core_df, ragged])
    core_full = core_full[core_full.index >= gdp_yoy.index[0]]

    # Standardise (fit on observed rows only)
    obs_ok   = core_full.notna().all(axis=1)
    scaler   = StandardScaler().fit(core_full[obs_ok])
    core_sc  = pd.DataFrame(
        scaler.transform(core_full.fillna(core_full[obs_ok].mean())),
        index=core_full.index, columns=core_full.columns)
    for col in core_sc.columns:
        core_sc.loc[~obs_ok, col] = float('nan')

    print("  Fitting DFM …")
    dfm     = DynamicFactorMQ(core_sc, factors=1, factor_orders=1, idiosyncratic_ar1=True)
    dfm_res = dfm.fit(disp=False, maxiter=500)
    print(f"  DFM log-likelihood: {dfm_res.llf:.1f}")
    factor_m       = dfm_res.factors['smoothed'].iloc[:, 0]
    factor_m.index = core_full.index   # restore DatetimeIndex

    # ── MIDAS quarterly aggregation ───────────────────────────────────────
    # Weights [0.5, 0.3, 0.2] for [most-recent, 2nd, 3rd] month in quarter
    W = np.array([0.5, 0.3, 0.2])

    def midas_q(series):
        out = {}
        for qdate, grp in series.resample('QS'):
            v = grp.dropna().values
            if   len(v) == 0: out[qdate] = np.nan
            elif len(v) == 1: out[qdate] = v[0]
            elif len(v) == 2: out[qdate] = 0.6*v[-1] + 0.4*v[-2]
            else:             out[qdate] = np.dot(W, v[-3:][::-1])
        return pd.Series(out)

    factor_q = midas_q(factor_m)

    def to_q(sid, agg='midas'):
        if sid not in raw: return None
        return midas_q(raw[sid]) if agg == 'midas' else raw[sid].resample('QS').mean()

    # ── Build feature matrix ──────────────────────────────────────────────
    df = pd.DataFrame({'factor': factor_q, 'gdp': gdp_yoy}).dropna()
    df['gdp_lag'] = df['gdp'].shift(1)
    df['gdp_mom'] = df['gdp'] - df['gdp_lag']   # acceleration / deceleration

    # Yield curve: level + 2-quarter change (most leading signal for recessions)
    t10y2y_q = to_q('T10Y2Y')
    if t10y2y_q is not None:
        df['yc']     = t10y2y_q
        df['yc_chg'] = t10y2y_q.diff(2)   # 6-month change

    # Credit spreads: level + change (widens before downturns)
    baa_q = to_q('BAA10YM')
    if baa_q is not None:
        df['spread']     = baa_q
        df['spread_chg'] = baa_q.diff(2)

    # Initial claims YoY% (leading labor-market indicator, inverted)
    icsa_q = to_q('ICSA')
    if icsa_q is not None:
        df['claims_yoy'] = ((icsa_q / icsa_q.shift(4)) - 1) * 100

    # VIX: market fear gauge
    vix_q = to_q('VIXCLS')
    if vix_q is not None:
        df['vix'] = vix_q

    # Equity market quarterly return
    eq_q = to_q('NASDAQCOM')
    if eq_q is not None:
        df['equity_ret'] = eq_q.pct_change(1) * 100

    df = df.dropna()
    feat_cols = [c for c in df.columns if c != 'gdp']
    print(f"  Feature set ({len(feat_cols)}): {feat_cols}")

    n_test = min(bt_quarters, len(df) - 8)
    train  = df.iloc[:-n_test]
    test   = df.iloc[-n_test:]
    X_tr, y_tr = train[feat_cols].values, train['gdp'].values
    X_te, y_te = test[feat_cols].values,  test['gdp'].values

    # ── Ridge baseline ────────────────────────────────────────────────────
    ridge      = Ridge(alpha=1.0).fit(X_tr, y_tr)
    ridge_te   = ridge.predict(X_te)
    ridge_rmse = float(np.sqrt(mean_squared_error(y_te, ridge_te)))
    print(f"  Ridge OOS RMSE: {ridge_rmse:.3f}")

    # ── Gradient Boosting with TimeSeriesSplit CV ─────────────────────────
    tscv = TimeSeriesSplit(n_splits=4)

    if HAS_XGB:
        ModelClass = XGBRegressor
        param_grid = [
            {'n_estimators': n, 'learning_rate': lr, 'max_depth': d,
             'subsample': 0.8, 'colsample_bytree': 0.8, 'random_state': 42, 'verbosity': 0}
            for n in [50, 100, 200]
            for lr in [0.03, 0.05, 0.1]
            for d in [2, 3]
        ]
    else:
        ModelClass = HistGradientBoostingRegressor
        param_grid = [
            {'max_iter': n, 'learning_rate': lr, 'max_depth': d, 'random_state': 42}
            for n in [50, 100, 200]
            for lr in [0.03, 0.05, 0.1]
            for d in [2, 3, None]
        ]

    best_cv, best_params = float('inf'), param_grid[0]
    for params in param_grid:
        scores = []
        for tr_idx, val_idx in tscv.split(X_tr):
            if len(val_idx) < 2: continue
            m = ModelClass(**params)
            m.fit(X_tr[tr_idx], y_tr[tr_idx])
            scores.append(np.sqrt(mean_squared_error(
                y_tr[val_idx], m.predict(X_tr[val_idx]))))
        if scores and np.mean(scores) < best_cv:
            best_cv, best_params = np.mean(scores), params

    print(f"  CV best RMSE: {best_cv:.3f} | params: { {k:v for k,v in best_params.items() if k not in ('random_state','verbosity')} }")
    gbm = ModelClass(**best_params).fit(X_tr, y_tr)
    gbm_te   = gbm.predict(X_te)
    gbm_rmse = float(np.sqrt(mean_squared_error(y_te, gbm_te)))
    print(f"  GBM OOS RMSE: {gbm_rmse:.3f}")

    # ── Inverse-RMSE weighted ensemble (Ridge + GBM) ──────────────────────
    w_r = 1.0 / (ridge_rmse + 1e-6)
    w_g = 1.0 / (gbm_rmse  + 1e-6)
    w_r /= (w_r + w_g); w_g /= (w_r + w_g)

    pred_tr_blend = w_r * ridge.predict(X_tr) + w_g * gbm.predict(X_tr)
    pred_te_blend = w_r * ridge_te             + w_g * gbm_te
    blend_rmse    = float(np.sqrt(mean_squared_error(y_te, pred_te_blend)))
    print(f"  Blend OOS RMSE: {blend_rmse:.3f} (w_ridge={w_r:.2f}, w_gbm={w_g:.2f})")

    # Feature importance from GBM
    if HAS_XGB:
        feat_imp = dict(zip(feat_cols, gbm.feature_importances_.tolist()))
    else:
        feat_imp = dict(zip(feat_cols,
            (gbm.feature_importances_ / gbm.feature_importances_.sum()).tolist()))

    # ── Build nowcast feature row ─────────────────────────────────────────
    def last(series, n=1):
        s = series.dropna()
        return float(s.iloc[-n]) if len(s) >= n else float('nan')

    nc_row = {'factor': last(factor_q), 'gdp_lag': last(gdp_yoy),
              'gdp_mom': last(gdp_yoy) - last(gdp_yoy, 2)}
    if t10y2y_q is not None:
        nc_row['yc']     = last(t10y2y_q)
        nc_row['yc_chg'] = last(t10y2y_q) - last(t10y2y_q, 3)
    if baa_q is not None:
        nc_row['spread']     = last(baa_q)
        nc_row['spread_chg'] = last(baa_q) - last(baa_q, 3)
    if icsa_q is not None and 'claims_yoy' in df.columns:
        nc_row['claims_yoy'] = float(df['claims_yoy'].iloc[-1])
    if vix_q is not None:
        nc_row['vix'] = last(vix_q)
    if eq_q is not None and 'equity_ret' in df.columns:
        nc_row['equity_ret'] = float(df['equity_ret'].iloc[-1])

    nc_X  = np.array([[nc_row.get(c, 0.0) for c in feat_cols]])
    nc_r  = float(ridge.predict(nc_X)[0])
    nc_g  = float(gbm.predict(nc_X)[0])
    nowcast_val = w_r * nc_r + w_g * nc_g

    last_gdp_date  = gdpc1.index[-1]
    nowcast_date   = last_gdp_date + pd.DateOffset(months=3)
    nowcast_label  = f"{nowcast_date.year}Q{(nowcast_date.month-1)//3+1}"
    last_known_gdp = float(gdp_yoy.iloc[-1])
    print(f"  Nowcast {nowcast_label}: {nowcast_val:.2f}%")

    # ── History + backtest ────────────────────────────────────────────────
    all_pred = list(pred_tr_blend) + list(pred_te_blend)
    history  = [
        {'date': idx.strftime('%Y-%m'), 'actual': round(float(row['gdp']), 2),
         'predicted': round(float(all_pred[i]), 2)}
        for i, (idx, row) in enumerate(df.iterrows())
    ]
    history.append({'date': nowcast_date.strftime('%Y-%m'), 'actual': None,
                    'predicted': round(nowcast_val, 2), 'is_nowcast': True})

    prev_actuals = [float(train['gdp'].iloc[-1])] + list(test['gdp'].iloc[:-1])
    bt_rows = []
    for i, (idx, row) in enumerate(test.iterrows()):
        actual = round(float(row['gdp']), 2)
        pred   = round(float(pred_te_blend[i]), 2)
        err    = round(pred - actual, 2)
        dir_ok = ((actual - prev_actuals[i]) * (pred - prev_actuals[i])) >= 0
        bt_rows.append({'date': idx.strftime('%Y-%m'), 'actual': actual,
                        'predicted': pred, 'err': err, 'dir_ok': bool(dir_ok)})

    mae      = round(float(np.mean([abs(r['err']) for r in bt_rows])), 3)
    rmse_in  = round(float(np.sqrt(mean_squared_error(y_tr, pred_tr_blend))), 3)
    dir_acc  = round(sum(r['dir_ok'] for r in bt_rows) / len(bt_rows) * 100) if bt_rows else 0

    factor_display = [
        {'date': d.strftime('%Y-%m'), 'value': round(float(v), 3)}
        for d, v in factor_m.items() if not np.isnan(v)
    ][-72:]

    return {
        'nowcast': {'quarter': nowcast_label, 'value': round(nowcast_val, 2),
                    'last_actual': round(last_known_gdp, 2)},
        'history': history,
        'factor':  factor_display,
        'bt_rows': bt_rows,
        'metrics': {
            'rmse_in':    rmse_in,
            'rmse_out':   round(blend_rmse, 3),
            'ridge_rmse': round(ridge_rmse, 3),
            'gbm_rmse':   round(gbm_rmse, 3),
            'mae':        mae,
            'dir_acc':    dir_acc,
            'n_features': len(feat_cols),
            'w_ridge':    round(w_r, 2),
            'w_gbm':      round(w_g, 2),
            'feat_imp':   {k: round(v, 3) for k, v in
                           sorted(feat_imp.items(), key=lambda x: -x[1])},
        },
    }


# ── HTTP SERVER ──────────────────────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/fred-proxy':
            qs  = urllib.parse.parse_qs(parsed.query)
            url = qs.get('url', [''])[0]
            if not url.startswith('https://api.stlouisfed.org/'):
                self.send_error(403, 'Only FRED URLs allowed'); return
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(data)
            except Exception as e:
                self.send_error(502, str(e))
            return

        if parsed.path == '/gdp-nowcast':
            qs       = urllib.parse.parse_qs(parsed.query)
            fred_key = qs.get('fred_key', [''])[0]
            quarters = int(qs.get('quarters', ['12'])[0])
            if not fred_key:
                self.send_error(400, 'fred_key required'); return
            try:
                print(f"\n[nowcast] Starting model ({quarters} quarters backtest) …")
                result  = run_gdp_nowcast(fred_key, quarters)
                payload = json.dumps(result).encode()
                print(f"[nowcast] Done → {result['nowcast']['value']}%")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception:
                err = traceback.format_exc()
                print(f"[nowcast] ERROR:\n{err}")
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': err}).encode())
            return

        super().do_GET()

    def log_message(self, fmt, *args):
        pass


PORT = int(os.environ.get('PORT', 8765))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
print(f'Server → http://0.0.0.0:{PORT}')
HTTPServer(('', PORT), Handler).serve_forever()
