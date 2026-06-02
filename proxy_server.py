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

    # ── Build nowcast feature row (every model predicts this) ─────────────
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
    nc_X = np.array([[nc_row.get(c, 0.0) for c in feat_cols]])

    # Scaled features for the linear models
    fscaler = StandardScaler().fit(X_tr)
    Xs_tr, Xs_te, nc_Xs = fscaler.transform(X_tr), fscaler.transform(X_te), fscaler.transform(nc_X)

    tscv     = TimeSeriesSplit(n_splits=4)
    models   = {}        # name -> {tr, te, nc, rmse}
    feat_imp = None      # filled by first available tree model

    def register(name, tr, te, nc):
        rmse = float(np.sqrt(mean_squared_error(y_te, te)))
        models[name] = {'tr': np.asarray(tr, float), 'te': np.asarray(te, float),
                        'nc': float(nc), 'rmse': rmse}
        print(f"  {name:14s} OOS RMSE: {rmse:.3f}")

    def fit_tabular(name, est, scaled=False):
        Xtr = Xs_tr if scaled else X_tr
        Xte = Xs_te if scaled else X_te
        Xnc = nc_Xs if scaled else nc_X
        est.fit(Xtr, y_tr)
        register(name, est.predict(Xtr), est.predict(Xte), est.predict(Xnc)[0])
        return est

    # ── 1) Ridge (linear) ─────────────────────────────────────────────────
    fit_tabular('Ridge', Ridge(alpha=1.0), scaled=True)

    # ── 2) ElasticNet (linear, auto feature selection) ────────────────────
    from sklearn.linear_model import ElasticNet
    fit_tabular('ElasticNet', ElasticNet(alpha=0.3, l1_ratio=0.5, max_iter=5000), scaled=True)

    # ── 3) Random Forest ──────────────────────────────────────────────────
    from sklearn.ensemble import RandomForestRegressor
    rf = fit_tabular('RandomForest',
                     RandomForestRegressor(n_estimators=300, max_depth=4,
                                           min_samples_leaf=2, random_state=42))

    # ── 4) XGBoost (TimeSeriesSplit CV-tuned) ─────────────────────────────
    if HAS_XGB:
        grid = [{'n_estimators': n, 'learning_rate': lr, 'max_depth': d,
                 'subsample': 0.8, 'colsample_bytree': 0.8, 'random_state': 42, 'verbosity': 0}
                for n in [50, 100, 200] for lr in [0.03, 0.05, 0.1] for d in [2, 3]]
        best_cv, best_params = float('inf'), grid[0]
        for params in grid:
            sc = []
            for tr_idx, val_idx in tscv.split(X_tr):
                if len(val_idx) < 2: continue
                m = XGBRegressor(**params); m.fit(X_tr[tr_idx], y_tr[tr_idx])
                sc.append(np.sqrt(mean_squared_error(y_tr[val_idx], m.predict(X_tr[val_idx]))))
            if sc and np.mean(sc) < best_cv: best_cv, best_params = np.mean(sc), params
        xgb = fit_tabular('XGBoost', XGBRegressor(**best_params))
        feat_imp = dict(zip(feat_cols, xgb.feature_importances_.tolist()))
    else:
        fit_tabular('HistGBM', HistGradientBoostingRegressor(
            max_iter=200, learning_rate=0.05, max_depth=3, random_state=42))

    # ── 5) LightGBM (optional) ────────────────────────────────────────────
    try:
        from lightgbm import LGBMRegressor
        lgbm = fit_tabular('LightGBM', LGBMRegressor(
            n_estimators=200, learning_rate=0.05, max_depth=3,
            num_leaves=15, verbose=-1, random_state=42))
        if feat_imp is None:
            imp = lgbm.feature_importances_.astype(float)
            feat_imp = dict(zip(feat_cols, (imp / max(imp.sum(), 1e-9)).tolist()))
    except Exception as e:
        print(f"  LightGBM skipped: {e}")

    # ── 6) CatBoost (optional) ────────────────────────────────────────────
    try:
        from catboost import CatBoostRegressor
        fit_tabular('CatBoost', CatBoostRegressor(
            iterations=200, learning_rate=0.05, depth=3, verbose=0, random_state=42))
    except Exception as e:
        print(f"  CatBoost skipped: {e}")

    # ── 7) ARIMA (univariate on GDP YoY, AIC-selected order) ──────────────
    try:
        from statsmodels.tsa.arima.model import ARIMA as _ARIMA
        best_aic, best_order, best_fit = float('inf'), None, None
        for order in [(1,0,0), (1,0,1), (2,0,1), (2,0,2), (1,1,1)]:
            try:
                f = _ARIMA(y_tr, order=order).fit()
                if f.aic < best_aic: best_aic, best_order, best_fit = f.aic, order, f
            except Exception: pass
        if best_fit is not None:
            tr_pred = np.asarray(best_fit.predict(start=0, end=len(y_tr)-1), float)
            te_pred = np.asarray(best_fit.forecast(steps=n_test), float)
            full_fit = _ARIMA(df['gdp'].values, order=best_order).fit()
            nc_pred  = float(np.asarray(full_fit.forecast(steps=1), float)[0])
            register(f'ARIMA{best_order}', tr_pred, te_pred, nc_pred)
    except Exception as e:
        print(f"  ARIMA skipped: {e}")

    # ── 8) VAR (GDP YoY + common factor move together) ────────────────────
    try:
        from statsmodels.tsa.api import VAR as _VAR
        vdat = df[['gdp', 'factor']].dropna()
        vtr  = vdat.iloc[:-n_test]
        vfit = _VAR(vtr).fit(maxlags=2, ic='aic')
        k    = max(vfit.k_ar, 1)
        fitted  = vfit.fittedvalues['gdp'].values
        tr_pred = np.concatenate([vtr['gdp'].values[:k], fitted])[:len(y_tr)]
        te_pred = vfit.forecast(vtr.values[-k:], steps=n_test)[:, 0]
        vfull   = _VAR(vdat).fit(maxlags=2, ic='aic')
        kf      = max(vfull.k_ar, 1)
        nc_pred = float(vfull.forecast(vdat.values[-kf:], steps=1)[0, 0])
        register('VAR', tr_pred, te_pred, nc_pred)
    except Exception as e:
        print(f"  VAR skipped: {e}")

    # ── Pruned, inverse-RMSE² weighted ensemble ───────────────────────────
    # Drop any model whose error is >2.5x the best model's (they'd only add
    # noise). Weight the rest by 1/RMSE² so accurate models dominate.
    all_names = list(models.keys())
    best_rmse = min(models[n]['rmse'] for n in all_names)
    kept      = [n for n in all_names if models[n]['rmse'] <= 2.5 * best_rmse]

    raw_w   = {n: 1.0 / (models[n]['rmse']**2 + 1e-6) for n in kept}
    wsum    = sum(raw_w.values())
    weights = {n: (raw_w[n] / wsum if n in kept else 0.0) for n in all_names}

    pred_tr_blend = sum(weights[n] * models[n]['tr'] for n in kept)
    pred_te_blend = sum(weights[n] * models[n]['te'] for n in kept)
    nowcast_val   = float(sum(weights[n] * models[n]['nc'] for n in kept))
    blend_rmse    = float(np.sqrt(mean_squared_error(y_te, pred_te_blend)))
    dropped = [n for n in all_names if n not in kept]
    print(f"  ── Ensemble of {len(kept)}/{len(all_names)} models — OOS RMSE: {blend_rmse:.3f}"
          + (f" (dropped: {', '.join(dropped)})" if dropped else ""))
    names = all_names   # for metrics reporting

    if feat_imp is None:
        feat_imp = {c: 0.0 for c in feat_cols}

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
            'mae':        mae,
            'dir_acc':    dir_acc,
            'n_features': len(feat_cols),
            'n_models':   len(names),
            'model_rmse': {n: round(models[n]['rmse'], 3) for n in names},
            'model_wt':   {n: round(weights[n], 3) for n in names},
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


if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 8765))
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'Server → http://0.0.0.0:{PORT}')
    HTTPServer(('', PORT), Handler).serve_forever()
