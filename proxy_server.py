#!/usr/bin/env python3
"""
Local dev server for Market Dashboard.
  GET /fred-proxy?url=<encoded>               — CORS proxy for FRED
  GET /gdp-nowcast?fred_key=<k>&quarters=<n>  — DFM + XGBoost nowcast
  Everything else                             — static files from Downloads/
"""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import urllib.request, urllib.parse, json, os, traceback, time, threading

# Serialize heavy ML jobs (one at a time → avoids OOM on 512MB free tier) and
# cache results so repeat clicks are instant instead of recomputing for minutes.
_ML_LOCK  = threading.Lock()
_ML_CACHE = {}          # key -> (timestamp, result_dict)
_ML_TTL   = 6 * 3600    # serve cached result for 6 hours

def cached_ml(key, fn, ttl=_ML_TTL):
    now = time.time()
    hit = _ML_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        print(f"[cache] hit for {key}")
        return hit[1]
    with _ML_LOCK:                      # only one heavy job runs at a time
        hit = _ML_CACHE.get(key)        # re-check after acquiring lock
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        result = fn()
        _ML_CACHE[key] = (time.time(), result)
        return result

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
    # limit = 300 monthly obs ≈ 25 years of history for walk-forward backtesting
    L = 300
    ALL_SERIES = {
        'INDPRO':    (None,  L),   # Industrial Production (monthly)
        'PAYEMS':    (None,  L),   # Nonfarm Payrolls (monthly)
        'RSAFS':     (None,  L),   # Retail Sales (monthly)
        'UMCSENT':   (None,  L),   # Consumer Sentiment (monthly)
        'T10Y2Y':    ('m',   L),   # Yield Curve 10Y-2Y (daily → monthly avg)
        'BAA10YM':   (None,  L),   # Moody's Baa Credit Spread (monthly)
        'ICSA':      ('m',   L),   # Initial Claims (weekly → monthly avg)
        'VIXCLS':    ('m',   L),   # VIX (daily → monthly avg)
        'NASDAQCOM': ('m',   L),   # NASDAQ Composite (daily → monthly avg)
        # ── added via 25-yr feature discovery (strongest GDP leads) ──
        'NEWORDER':  (None,  L),   # Core capital-goods orders (capex lead)
        'TOTALSA':   (None,  L),   # Total vehicle sales
        'CSUSHPINSA':(None,  L),   # Case-Shiller home prices
        'PERMIT':    (None,  L),   # Building permits (housing lead)
    }
    CORE_IDS = ['INDPRO', 'PAYEMS', 'RSAFS', 'UMCSENT']  # go into DFM

    # ── Fetch helper ──────────────────────────────────────────────────────
    # NOTE on vintage data: naive initial-release (output_type=4) on GDP levels
    # is corrupted by base-year rebasings — the YoY ratio across first-prints
    # produces spurious 12-15% "growth". Proper real-time GDP needs full ALFRED
    # vintage replay (future work), so GDP uses revised levels (consistent base).
    def _get(url):
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read())
    def fetch(sid, limit, freq=None):
        base = (f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={sid}&api_key={fred_key}"
                f"&sort_order=desc&limit={limit}&file_type=json")
        freq_param = f"&frequency={freq}&aggregation_method=avg" if freq else ""
        data = _get(base + freq_param)
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
    gdpc1 = fetch('GDPC1', 110)   # ~27 yrs of quarterly GDP

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

    # ── Discovery-added leading indicators ────────────────────────────────
    no_q = to_q('NEWORDER')                   # core capex orders, YoY
    if no_q is not None:
        df['neworder_yoy'] = (no_q / no_q.shift(4) - 1) * 100
    veh_q = to_q('TOTALSA')                   # vehicle sales, YoY
    if veh_q is not None:
        df['vehicles_yoy'] = (veh_q / veh_q.shift(4) - 1) * 100
    hp_q = to_q('CSUSHPINSA')                 # home prices, YoY
    if hp_q is not None:
        df['homeprice_yoy'] = (hp_q / hp_q.shift(4) - 1) * 100
    perm_q = to_q('PERMIT')                   # building permits, YoY
    if perm_q is not None:
        df['permits_yoy'] = (perm_q / perm_q.shift(4) - 1) * 100

    # COVID flag: 2020-2021 were ±30% outliers that distort training for every
    # other period. The flag lets models isolate them (it's 0 for normal times,
    # including the live nowcast, so it never affects current predictions).
    df['covid'] = ((df.index >= '2020-02-01') & (df.index <= '2021-12-31')).astype(float)

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

    def last_yoy(q):
        qq = q.dropna()
        return float(qq.iloc[-1] / qq.iloc[-5] - 1) * 100 if len(qq) >= 5 else 0.0

    if 'neworder_yoy'  in df.columns: nc_row['neworder_yoy']  = last_yoy(no_q)
    if 'vehicles_yoy'  in df.columns: nc_row['vehicles_yoy']  = last_yoy(veh_q)
    if 'homeprice_yoy' in df.columns: nc_row['homeprice_yoy'] = last_yoy(hp_q)
    if 'permits_yoy'   in df.columns: nc_row['permits_yoy']   = last_yoy(perm_q)
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
    ranked    = sorted(all_names, key=lambda n: models[n]['rmse'])
    best_rmse = models[ranked[0]]['rmse']
    kept      = [n for n in ranked if models[n]['rmse'] <= 2.5 * best_rmse]
    if len(kept) < 3:                      # never collapse to 1-2 models
        kept = ranked[:3]

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

    if feat_imp is None:   # no boosters → use RandomForest importances
        imp = rf.feature_importances_.astype(float)
        feat_imp = dict(zip(feat_cols, (imp / max(imp.sum(), 1e-9)).tolist()))

    # ── 25-YEAR WALK-FORWARD BACKTEST (expanding window, no leakage) ───────
    # At each quarter we retrain a fast strong-model ensemble on ONLY the data
    # available up to that point, then predict the next quarter. This is the
    # rigorous way to measure real-world accuracy.
    print("  Walk-forward backtest (expanding window)…")
    from sklearn.linear_model import Ridge as _R, ElasticNet as _EN
    try:    from lightgbm import LGBMRegressor as _LG; _has_lg = True
    except Exception: _has_lg = False
    try:    from statsmodels.tsa.arima.model import ARIMA as _AR; _has_ar = True
    except Exception: _has_ar = False

    _HGB = HistGradientBoostingRegressor
    # Recipe members (HistGBM always included so there's a strong tree model even
    # when LightGBM isn't installed, e.g. on the free tier).
    member_defs = {
        'Ridge':      ('lin',  lambda: _R(alpha=1.0)),
        'ElasticNet': ('lin',  lambda: _EN(alpha=0.3, l1_ratio=0.5, max_iter=5000)),
        'HistGBM':    ('tree', lambda: _HGB(max_iter=200, learning_rate=0.05, max_depth=3, random_state=42)),
    }
    if _has_lg:
        member_defs['LightGBM'] = ('tree', lambda: _LG(n_estimators=150, learning_rate=0.05,
                                   max_depth=3, num_leaves=15, verbose=-1, random_state=42))
    mem_names = list(member_defs.keys())

    Xall, yall = df[feat_cols].values, df['gdp'].values
    dates_all  = list(df.index)
    min_train  = max(24, len(df) // 3)

    mem_pred = {n: [] for n in mem_names}
    arima_pred, wf_meta = [], []
    for i in range(min_train, len(df)):
        Xtr_w, ytr_w, Xte_w = Xall[:i], yall[:i], Xall[i:i+1]
        sc = StandardScaler().fit(Xtr_w)
        Xtr_s, Xte_s = sc.transform(Xtr_w), sc.transform(Xte_w)
        for n, (kind, mk) in member_defs.items():
            if kind == 'lin': mem_pred[n].append(mk().fit(Xtr_s, ytr_w).predict(Xte_s)[0])
            else:             mem_pred[n].append(mk().fit(Xtr_w, ytr_w).predict(Xte_w)[0])
        if _has_ar:
            try: arima_pred.append(float(np.asarray(_AR(ytr_w, order=(1,1,1)).fit().forecast(1), float)[0]))
            except Exception: arima_pred.append(float('nan'))
        wf_meta.append((dates_all[i], float(yall[i]), float(yall[i-1])))
    if _has_ar and not all(p != p for p in arima_pred):
        mem_pred['ARIMA'] = arima_pred; mem_names.append('ARIMA')

    # Per-member out-of-sample RMSE → inverse-RMSE² weights (best model dominates)
    actuals = np.array([a for (_, a, _) in wf_meta])
    mem_rmse = {}
    for n in mem_names:
        p = np.array(mem_pred[n], float); msk = ~np.isnan(p)
        mem_rmse[n] = float(np.sqrt(np.mean((p[msk] - actuals[msk])**2))) if msk.any() else 1e9
    _raw = {n: 1.0 / (mem_rmse[n]**2 + 1e-6) for n in mem_names}
    _sw  = sum(_raw.values())
    mem_wt = {n: _raw[n] / _sw for n in mem_names}
    print("  Recipe weights: " + ", ".join(f"{n} {mem_wt[n]*100:.0f}%(±{mem_rmse[n]:.2f})" for n in
          sorted(mem_names, key=lambda x: -mem_wt[x])))

    # Weighted walk-forward ensemble
    wf = []
    for idx, (d, a, pv) in enumerate(wf_meta):
        num = den = 0.0
        for n in mem_names:
            v = mem_pred[n][idx]
            if v == v: num += mem_wt[n] * v; den += mem_wt[n]
        wf.append((d, a, (num/den if den else a), pv))

    wf_err  = [abs(p - a) for (_, a, p, _) in wf]
    wf_mae  = float(np.mean(wf_err)) if wf_err else 0.0
    wf_rmse = float(np.sqrt(np.mean([(p-a)**2 for (_, a, p, _) in wf]))) if wf else 0.0
    wf_dir  = (float(np.mean([1.0 if ((a-pv)*(p-pv) >= 0) else 0.0
                              for (_, a, p, pv) in wf])) * 100) if wf else 0.0
    def acc_at(tol): return float(np.mean([e <= tol for e in wf_err])) if wf_err else 0.0
    wf_band = next((t/10 for t in range(3, 41) if acc_at(t/10) >= 0.85), 4.0)
    wf_acc  = round(acc_at(wf_band) * 100)
    print(f"  Walk-forward: {len(wf)} quarters | MAE {wf_mae:.2f} | "
          f"{wf_acc}% within ±{wf_band}pp | direction {wf_dir:.0f}%")

    wf_rows = [{'date': d.strftime('%Y-%m'), 'actual': round(a, 2),
                'predicted': round(p, 2), 'err': round(p - a, 2),
                'dir_ok': bool((a-pv)*(p-pv) >= 0)} for (d, a, p, pv) in wf]

    # Nowcast: same members refit on ALL data, blended with the same weights
    sc_full = StandardScaler().fit(Xall); nc_full_s = sc_full.transform(nc_X)
    nc_vals = {}
    for n, (kind, mk) in member_defs.items():
        nc_vals[n] = (mk().fit(sc_full.transform(Xall), yall).predict(nc_full_s)[0] if kind == 'lin'
                      else mk().fit(Xall, yall).predict(nc_X)[0])
    if 'ARIMA' in mem_names:
        try: nc_vals['ARIMA'] = float(np.asarray(_AR(yall, order=(1,1,1)).fit().forecast(1), float)[0])
        except Exception: nc_vals['ARIMA'] = float('nan')
    num = den = 0.0
    for n in mem_names:
        v = nc_vals.get(n, float('nan'))
        if v == v: num += mem_wt[n] * v; den += mem_wt[n]
    nowcast_val = float(num/den) if den else float(np.mean(list(nc_vals.values())))

    last_gdp_date  = gdpc1.index[-1]
    nowcast_date   = last_gdp_date + pd.DateOffset(months=3)
    nowcast_label  = f"{nowcast_date.year}Q{(nowcast_date.month-1)//3+1}"
    last_known_gdp = float(gdp_yoy.iloc[-1])
    print(f"  Nowcast {nowcast_label}: {nowcast_val:.2f}%")

    # ── History + backtest ────────────────────────────────────────────────
    # Chart predictions use the true walk-forward (out-of-sample) values where
    # available, falling back to the in-sample blend for the earliest quarters.
    all_pred = list(pred_tr_blend) + list(pred_te_blend)
    wf_pred_map = {d.strftime('%Y-%m'): p for (d, a, p, pv) in wf}
    history  = [
        {'date': idx.strftime('%Y-%m'), 'actual': round(float(row['gdp']), 2),
         'predicted': round(float(wf_pred_map.get(idx.strftime('%Y-%m'), all_pred[i])), 2)}
        for i, (idx, row) in enumerate(df.iterrows())
    ]
    history.append({'date': nowcast_date.strftime('%Y-%m'), 'actual': None,
                    'predicted': round(nowcast_val, 2), 'is_nowcast': True})

    # Headline metrics + backtest table come from the 25-yr WALK-FORWARD (real OOS)
    bt_rows = wf_rows
    rmse_in = round(float(np.sqrt(mean_squared_error(y_tr, pred_tr_blend))), 3)

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
            'accuracy':    wf_acc,                 # % within ±band (headline)
            'acc_band':    wf_band,                # the tolerance band
            'wf_quarters': len(wf),                # quarters backtested
            'wf_start':    wf[0][0].strftime('%Y-%m') if wf else None,
            'wf_mae':      round(wf_mae, 3),
            'wf_rmse':     round(wf_rmse, 3),
            'dir_acc':     round(wf_dir),
            'mae':         round(wf_mae, 3),
            'rmse_in':     rmse_in,
            'rmse_out':    round(blend_rmse, 3),
            'n_features':  len(feat_cols),
            'n_models':    len(mem_names),
            'model_rmse':  {n: round(mem_rmse[n], 3) for n in mem_names},
            'model_wt':    {n: round(mem_wt[n], 3) for n in mem_names},
            'feat_imp':    {k: round(v, 3) for k, v in
                            sorted(feat_imp.items(), key=lambda x: -x[1])},
        },
    }


# ── INFLATION NOWCAST MODEL ───────────────────────────────────────────────────

def run_inflation_nowcast(fred_key, bt_months=24):
    """Monthly CPI-YoY ML nowcast — same 8-model ensemble + 25-yr walk-forward
    as the GDP model, but at monthly frequency."""
    import warnings; warnings.filterwarnings("ignore")
    import numpy as np
    import pandas as pd
    from sklearn.linear_model import Ridge, ElasticNet
    from sklearn.ensemble import RandomForestRegressor, HistGradientBoostingRegressor
    from sklearn.metrics import mean_squared_error
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    try:    from xgboost import XGBRegressor; HAS_XGB = True
    except Exception: HAS_XGB = False

    L = 320  # ~26 yrs of monthly history
    # id : (transform, freq)
    SERIES = {
        'CPIAUCSL':      ('yoy',  None),  # target
        'CPILFESL':      ('yoy',  None),  # core CPI
        'PPIACO':        ('yoy',  None),  # producer prices
        'MICH':          ('lvl',  None),  # inflation expectations survey
        'T5YIE':         ('lvl',  'm'),   # 5y breakeven (market expectations)
        'CES0500000003': ('yoy',  None),  # avg hourly earnings (wages)
        'PALLFNFINDEXM': ('yoy',  None),  # global commodities
        'MCOILWTICO':    ('yoy',  None),  # oil
        'M2SL':          ('yoy',  None),  # money supply
        'UNRATE':        ('lvl',  None),  # unemployment
        'FEDFUNDS':      ('lvl',  None),  # policy rate
        # ── underlying-trend gauges (strip out noise) ──
        'MEDCPIM158SFRBCLE':   ('lvl', None),  # Cleveland median CPI (annualized)
        'CORESTICKM159SFRBATL':('lvl', None),  # Atlanta sticky-price core CPI YoY
        'PCETRIM12M159SFRBDAL':('lvl', None),  # Dallas trimmed-mean PCE (12-mo)
    }

    VINTAGE = "&output_type=4&realtime_start=1776-07-04&realtime_end=9999-12-31"
    def _get(u):
        with urllib.request.urlopen(u, timeout=25) as r:
            return json.loads(r.read())
    def _n_obs(d):
        return len([o for o in d.get('observations', []) if o['value'] != '.'])
    def fetch(sid, freq=None):
        base = (f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={sid}&api_key={fred_key}&sort_order=desc"
                f"&limit={L}&file_type=json")
        if freq:                       # rate/market series — not revised
            d = _get(base + f"&frequency={freq}&aggregation_method=avg")
        else:                          # revised macro series → initial release …
            d = _get(base + VINTAGE)
            if _n_obs(d) < 0.8 * L:    # … unless vintage archive too short → revised
                d = _get(base)
        rows = [(o['date'], float(o['value'])) for o in d.get('observations', [])
                if o['value'] != '.']
        rows.reverse()
        return pd.Series([v for _, v in rows],
                         index=pd.to_datetime([x for x, _ in rows]), name=sid)

    def transform(s, t):
        m = s.resample('MS').last()
        return (m / m.shift(12) - 1) * 100 if t == 'yoy' else m

    raw = {}
    cpi_level = None
    for sid, (t, freq) in SERIES.items():
        print(f"  [inf] fetching {sid} …")
        try:
            s = fetch(sid, freq)
            if s is not None and len(s) > 24:
                raw[sid] = transform(s, t)
                if sid == 'CPIAUCSL':
                    cpi_level = s.resample('MS').last()   # keep level for MoM features
        except Exception as e:
            print(f"    skip {sid}: {e}")
        time.sleep(1.0)

    if 'CPIAUCSL' not in raw:
        raise ValueError("Could not load CPI")

    # ── Feature matrix (monthly) ──────────────────────────────────────────
    df = pd.DataFrame({'cpi': raw['CPIAUCSL']}).dropna()
    df['cpi_lag'] = df['cpi'].shift(1)
    df['cpi_mom'] = df['cpi'] - df['cpi_lag']
    for sid in SERIES:
        if sid != 'CPIAUCSL' and sid in raw:
            df[sid.lower()] = raw[sid]
    # ── Momentum features (month-over-month inflation) ────────────────────
    # Lets the model react to hot/cold streaks instead of anchoring on YoY.
    if cpi_level is not None:
        mom = cpi_level.pct_change() * 100                 # monthly % change
        df['mom_1']     = mom                              # latest month
        df['mom_3']     = mom.rolling(3).mean()            # 3-mo average pace
        df['mom_6']     = mom.rolling(6).mean()            # 6-mo average pace
        df['mom_accel'] = mom - mom.shift(1).rolling(3).mean()  # speeding up / slowing
    # COVID flag (2020-2021 base-effect distortions). 0 for normal times incl. now.
    df['covid'] = ((df.index >= '2020-02-01') & (df.index <= '2021-12-31')).astype(float)
    # Ragged edge: some drivers publish later than CPI. Forward-fill them so the
    # most recent CPI month is kept (otherwise dropna would discard it and the
    # forecast would lag a month behind the data).
    df = df.ffill().dropna()
    feat_cols = list(df.columns)            # cpi + lag + mom + drivers + covid
    target = df['cpi'].shift(-1)            # predict NEXT month's CPI YoY
    data = df.copy(); data['__y__'] = target; data = data.dropna()
    X_all = data[feat_cols].values
    y_all = data['__y__'].values
    dates = list(data.index)
    print(f"  [inf] {len(data)} months, {len(feat_cols)} features")

    n_test = min(bt_months, len(data) - 24)
    X_tr, y_tr = X_all[:-n_test], y_all[:-n_test]
    X_te, y_te = X_all[-n_test:], y_all[-n_test:]

    fsc = StandardScaler().fit(X_tr)
    Xs_tr, Xs_te = fsc.transform(X_tr), fsc.transform(X_te)
    tscv = TimeSeriesSplit(n_splits=4)
    models, feat_imp = {}, None

    def reg(name, tr, te):
        models[name] = {'rmse': float(np.sqrt(mean_squared_error(y_te, te)))}
        print(f"    {name:14s} RMSE {models[name]['rmse']:.3f}")

    def tab(name, est, scaled=False):
        Xt, Xv = (Xs_tr, Xs_te) if scaled else (X_tr, X_te)
        est.fit(Xt, y_tr); reg(name, est.predict(Xt), est.predict(Xv)); return est

    tab('Ridge', Ridge(alpha=1.0), True)
    tab('ElasticNet', ElasticNet(alpha=0.3, l1_ratio=0.5, max_iter=5000), True)
    rf = tab('RandomForest', RandomForestRegressor(n_estimators=300, max_depth=5,
                                                   min_samples_leaf=2, random_state=42))
    if HAS_XGB:
        xgb = tab('XGBoost', XGBRegressor(n_estimators=150, learning_rate=0.05,
                  max_depth=3, subsample=0.8, colsample_bytree=0.8,
                  random_state=42, verbosity=0))
        feat_imp = dict(zip(feat_cols, xgb.feature_importances_.tolist()))
    else:
        tab('HistGBM', HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05,
                       max_depth=3, random_state=42))
    try:
        from lightgbm import LGBMRegressor
        lg = tab('LightGBM', LGBMRegressor(n_estimators=200, learning_rate=0.05,
                 max_depth=3, num_leaves=15, verbose=-1, random_state=42))
        if feat_imp is None:
            imp = lg.feature_importances_.astype(float)
            feat_imp = dict(zip(feat_cols, (imp/max(imp.sum(),1e-9)).tolist()))
    except Exception as e: print(f"    LightGBM skip: {e}")
    try:
        from catboost import CatBoostRegressor
        tab('CatBoost', CatBoostRegressor(iterations=200, learning_rate=0.05,
            depth=3, verbose=0, random_state=42))
    except Exception as e: print(f"    CatBoost skip: {e}")
    if feat_imp is None:   # no boosters → use RandomForest importances
        imp = rf.feature_importances_.astype(float)
        feat_imp = dict(zip(feat_cols, (imp/max(imp.sum(),1e-9)).tolist()))

    # ── Walk-forward (expanding window, monthly, performance-weighted) ─────
    print("  [inf] walk-forward …")
    from sklearn.linear_model import Ridge as _R, ElasticNet as _EN
    try:    from lightgbm import LGBMRegressor as _LG; _has_lg = True
    except Exception: _has_lg = False
    try:    from statsmodels.tsa.arima.model import ARIMA as _AR; _has_ar = True
    except Exception: _has_ar = False

    _HGB = HistGradientBoostingRegressor
    member_defs = {
        'Ridge':      ('lin',  lambda: _R(alpha=1.0)),
        'ElasticNet': ('lin',  lambda: _EN(alpha=0.3, l1_ratio=0.5, max_iter=5000)),
        'HistGBM':    ('tree', lambda: _HGB(max_iter=200, learning_rate=0.05, max_depth=3, random_state=42)),
    }
    if _has_lg:
        member_defs['LightGBM'] = ('tree', lambda: _LG(n_estimators=150, learning_rate=0.05,
                                   max_depth=3, num_leaves=15, verbose=-1, random_state=42))
    mem_names = list(member_defs.keys())

    # ── MoM models: predict next-month MoM, rebuild YoY from the KNOWN base ──
    # YoY for the target month depends on 12 monthly prices, 11 of which are
    # already published — only next month's MoM is unknown. Predicting that one
    # small number and reconstructing YoY shrinks the error vs predicting YoY.
    min_train = max(60, len(data) // 3)
    mem_pred = {n: [] for n in mem_names}
    mom_defs = {}
    if cpi_level is not None:
        mom_series = cpi_level.pct_change() * 100
        mom_y   = mom_series.shift(-1).reindex(data.index).values          # next-month MoM
        now_lvl = cpi_level.reindex(data.index).values                     # level at feature month
        base_lvl= cpi_level.reindex(data.index - pd.DateOffset(months=11)).values  # year-ago of target
        mom_y_fit = np.where(np.isnan(mom_y), 0.0, mom_y)
        if (~np.isnan(base_lvl)).sum() > min_train + 10:
            mom_defs = {'MoM·Ridge': 'lin', 'MoM·HGB': 'tree'}
            for n in mom_defs: mem_pred[n] = []

    def _recon(level_now, level_base, mom_pct):
        return ((level_now*(1+mom_pct/100)/level_base) - 1)*100 if (level_base and level_base==level_base) else float('nan')

    arima_pred, wf_meta = [], []
    for i in range(min_train, len(data)):
        Xt, yt, Xv = X_all[:i], y_all[:i], X_all[i:i+1]
        sc = StandardScaler().fit(Xt)
        Xts, Xvs = sc.transform(Xt), sc.transform(Xv)
        for n, (kind, mk) in member_defs.items():
            if kind == 'lin': mem_pred[n].append(mk().fit(Xts, yt).predict(Xvs)[0])
            else:             mem_pred[n].append(mk().fit(Xt, yt).predict(Xv)[0])
        for n, kind in mom_defs.items():        # MoM members → reconstructed YoY
            if kind == 'lin': mp = _R(alpha=1.0).fit(Xts, mom_y_fit[:i]).predict(Xvs)[0]
            else:             mp = _HGB(max_iter=200, learning_rate=0.05, max_depth=3, random_state=42).fit(Xt, mom_y_fit[:i]).predict(Xv)[0]
            mem_pred[n].append(_recon(now_lvl[i], base_lvl[i], mp))
        if _has_ar:
            try: arima_pred.append(float(np.asarray(_AR(yt, order=(1,1,1)).fit().forecast(1), float)[0]))
            except Exception: arima_pred.append(float('nan'))
        # label by the month being PREDICTED (target = feature month + 1), not the feature month
        wf_meta.append((dates[i] + pd.DateOffset(months=1), float(y_all[i]), float(data['cpi'].values[i])))
    for n in list(mom_defs):                    # keep MoM members only if valid
        if all(p != p for p in mem_pred[n]): del mem_pred[n]; del mom_defs[n]
        else: mem_names.append(n)
    if _has_ar and not all(p != p for p in arima_pred):
        mem_pred['ARIMA'] = arima_pred; mem_names.append('ARIMA')

    # Per-member overall RMSE (for display + final-forecast eligibility)
    _act = np.array([a for (_, a, _) in wf_meta])
    mem_rmse = {}
    for n in mem_names:
        p = np.array(mem_pred[n], float); msk = ~np.isnan(p)
        mem_rmse[n] = float(np.sqrt(np.mean((p[msk]-_act[msk])**2))) if msk.any() else 1e9

    def _inv2(names, rmses):
        raw = {n: 1.0/(rmses[n]**2 + 1e-6) for n in names}
        s = sum(raw.values()) or 1.0
        return {n: raw[n]/s for n in names}

    # ── QUALITY GATE: drop any model running worse than 0.50pp ─────────────
    # Applied per-month using only each model's track record SO FAR (no
    # hindsight). A model that's been off by >0.50pp is benched for that month;
    # the remaining models are blended by inverse-RMSE². Falls back to the single
    # best model in the rare month where none qualify.
    THRESH, MIN_HIST = 0.50, 12
    wf = []
    for idx, (d, a, pv) in enumerate(wf_meta):
        trail = {}
        for n in mem_names:
            if idx >= MIN_HIST:
                e = [(mem_pred[n][j]-_act[j])**2 for j in range(idx) if mem_pred[n][j] == mem_pred[n][j]]
                trail[n] = (sum(e)/len(e))**0.5 if e else 1e9
            else:
                trail[n] = mem_rmse[n]
        qual = [n for n in mem_names if trail[n] <= THRESH and mem_pred[n][idx] == mem_pred[n][idx]]
        if not qual:
            qual = [min(mem_names, key=lambda n: trail[n])]
        w = _inv2(qual, trail)
        wf.append((d, a, sum(w[n]*mem_pred[n][idx] for n in qual), pv))

    # Final-forecast eligibility: members within 0.50pp over the full backtest
    fc_members = [n for n in mem_names if mem_rmse[n] <= THRESH] or [min(mem_names, key=lambda n: mem_rmse[n])]
    mem_wt = _inv2(fc_members, mem_rmse)
    for n in mem_names: mem_wt.setdefault(n, 0.0)   # excluded models → 0 weight
    print(f"  [inf] eligible (<=0.50pp): " + ", ".join(f"{n} {mem_wt[n]*100:.0f}%(±{mem_rmse[n]:.2f})"
          for n in sorted(mem_names, key=lambda x: -mem_wt[x])))

    wf_err  = [abs(p-a) for (_, a, p, _) in wf]
    wf_mae  = float(np.mean(wf_err)) if wf_err else 0.0
    wf_rmse = float(np.sqrt(np.mean([(p-a)**2 for (_, a, p, _) in wf]))) if wf else 0.0
    wf_dir  = (float(np.mean([1.0 if ((a-pv)*(p-pv) >= 0) else 0.0
                              for (_, a, p, pv) in wf])) * 100) if wf else 0.0
    def acc_at(t): return float(np.mean([e <= t for e in wf_err])) if wf_err else 0.0
    band = next((t/100 for t in range(10, 200, 5) if acc_at(t/100) >= 0.85), 2.0)
    acc  = round(acc_at(band)*100)
    print(f"  [inf] {len(wf)} months | MAE {wf_mae:.2f} | {acc}% within ±{band}pp | dir {wf_dir:.0f}%")

    bt_rows = [{'date': d.strftime('%Y-%m'), 'actual': round(a, 2),
                'predicted': round(p, 2), 'err': round(p-a, 2),
                'dir_ok': bool((a-pv)*(p-pv) >= 0)} for (d, a, p, pv) in wf]

    # ── Multi-month forward forecast (iterative, validated recipe) ─────────
    # CPI data lags ~1 month, so we project H months ahead: each step feeds the
    # prediction back in (updating CPI's own lag/momentum) while the external
    # drivers are held at their latest values. Covers the current month + ahead.
    H = 1   # only the next unreleased month — matches the 1-month-ahead accuracy
    scf = StandardScaler().fit(X_all)
    fitted = {}                                  # same recipe members, refit on all data
    for n, (kind, mk) in member_defs.items():
        est = mk()
        est.fit(scf.transform(X_all), y_all) if kind == 'lin' else est.fit(X_all, y_all)
        fitted[n] = (kind, est)
    arima_fc = None
    if 'ARIMA' in mem_names:
        try: arima_fc = np.asarray(_AR(y_all, order=(1,1,1)).fit().forecast(H), float)
        except Exception: arima_fc = None

    # MoM members refit on all data + their reconstruction base for next month
    last_date = df.index[-1]
    mom_fitted = {}
    if mom_defs:
        for n, kind in mom_defs.items():
            est = _R(alpha=1.0) if kind == 'lin' else _HGB(max_iter=200, learning_rate=0.05, max_depth=3, random_state=42)
            est.fit(scf.transform(X_all), mom_y_fit) if kind == 'lin' else est.fit(X_all, mom_y_fit)
            mom_fitted[n] = (kind, est)
        nc_now_lvl  = float(cpi_level.reindex([last_date]).iloc[0])
        nc_base_lvl = float(cpi_level.reindex([last_date - pd.DateOffset(months=11)]).iloc[0])

    ci = feat_cols.index('cpi'); li = feat_cols.index('cpi_lag'); mi = feat_cols.index('cpi_mom')
    base   = df[feat_cols].iloc[-1].values.astype(float).copy()   # drivers held at latest
    prev_c = float(df['cpi'].iloc[-2])
    cur_c  = float(df['cpi'].iloc[-1])
    forecasts = []
    for h in range(H):
        r = base.copy(); r[ci] = cur_c; r[li] = prev_c; r[mi] = cur_c - prev_c
        num = den = 0.0
        for n, (kind, est) in fitted.items():
            v = est.predict(scf.transform([r]))[0] if kind == 'lin' else est.predict([r])[0]
            num += mem_wt[n]*v; den += mem_wt[n]
        if arima_fc is not None and 'ARIMA' in mem_wt:
            num += mem_wt['ARIMA']*float(arima_fc[h]); den += mem_wt['ARIMA']
        if h == 0 and mom_fitted:                  # MoM members (next month only)
            for n, (kind, est) in mom_fitted.items():
                if mem_wt.get(n, 0) <= 0: continue
                mp = est.predict(scf.transform([r]))[0] if kind == 'lin' else est.predict([r])[0]
                rec = _recon(nc_now_lvl, nc_base_lvl, mp)
                if rec == rec: num += mem_wt[n]*rec; den += mem_wt[n]
        pred  = float(num/den) if den else cur_c
        fdate = last_date + pd.DateOffset(months=h+1)
        # 85% prediction interval from the model's own historical error (acc_band)
        forecasts.append({'month': fdate.strftime('%Y-%m'), 'value': round(pred, 2),
                          'low': round(pred - band, 2), 'high': round(pred + band, 2)})
        prev_c, cur_c = cur_c, pred

    nowcast_val = forecasts[0]['value']
    nc_date     = last_date + pd.DateOffset(months=1)
    last_cpi    = float(df['cpi'].iloc[-1])

    history = [{'date': d.strftime('%Y-%m'), 'actual': round(a, 2),
                'predicted': round(p, 2)} for (d, a, p, _) in wf]
    for f in forecasts:
        history.append({'date': f['month'], 'actual': None,
                        'predicted': f['value'], 'is_nowcast': True})

    return {
        'nowcast': {'month': nc_date.strftime('%Y-%m'), 'value': round(nowcast_val, 2),
                    'last_actual': round(last_cpi, 2),
                    'low': round(nowcast_val - band, 2), 'high': round(nowcast_val + band, 2)},
        'forecasts': forecasts,                 # multi-month forward path
        'last_actual_month': last_date.strftime('%Y-%m'),
        'history': history,
        'bt_rows': bt_rows,
        'metrics': {
            'accuracy': acc, 'acc_band': round(band, 2),
            'wf_quarters': len(wf), 'wf_start': wf[0][0].strftime('%Y-%m') if wf else None,
            'wf_mae': round(wf_mae, 3), 'wf_rmse': round(wf_rmse, 3),
            'dir_acc': round(wf_dir), 'mae': round(wf_mae, 3),
            'n_features': len(feat_cols), 'n_models': len(mem_names),
            'model_rmse': {n: round(mem_rmse[n], 3) for n in mem_names},
            'model_wt': {n: round(mem_wt[n], 3) for n in mem_names},
            'feat_imp': {k: round(v, 3) for k, v in sorted(feat_imp.items(), key=lambda x: -x[1])},
        },
    }


# ── ETF MOMENTUM SCANNER ──────────────────────────────────────────────────────
# Finds range-bound ETFs breaking above (or approaching) a tested resistance level
# — the IGV pattern: floor ~76, ceiling ~89 tested repeatedly, then breaks through.
# Price/volume/charts from Yahoo Finance (free, no key). Universe + expense ratio
# from a curated vetted list (expanded by FMP when a key is supplied).

# Curated: liquid, non-leveraged, non-inverse ETFs with net expense ratio < 1.0%.
CURATED_ETFS = {
    # broad market
    'SPY':0.09,'VOO':0.03,'IVV':0.03,'QQQ':0.20,'QQQM':0.15,'IWM':0.19,'DIA':0.16,
    'MDY':0.23,'VTI':0.03,'ITOT':0.03,'RSP':0.20,'SCHX':0.03,'SCHB':0.03,'IWB':0.15,
    'IWV':0.20,'VXF':0.06,'IJH':0.05,'IJR':0.06,'VB':0.05,'VO':0.04,'VV':0.04,'MGK':0.07,
    # sectors (SPDR + Vanguard)
    'XLK':0.09,'XLF':0.09,'XLE':0.09,'XLV':0.09,'XLI':0.09,'XLY':0.09,'XLP':0.09,
    'XLU':0.09,'XLB':0.09,'XLRE':0.09,'XLC':0.09,
    'VGT':0.09,'VHT':0.09,'VFH':0.09,'VDE':0.09,'VIS':0.09,'VCR':0.09,'VDC':0.09,
    'VPU':0.09,'VAW':0.09,'VOX':0.09,'VNQ':0.13,'IYR':0.39,'IYW':0.39,'IYF':0.39,
    # industries / thematic
    'IGV':0.40,'SMH':0.35,'SOXX':0.35,'XSD':0.35,'PSI':0.57,'IGM':0.41,'KRE':0.35,
    'KBE':0.35,'KBWB':0.35,'XBI':0.35,'IBB':0.45,'ITB':0.40,'XHB':0.35,'NAIL':0.94,
    'XRT':0.35,'KIE':0.35,'XOP':0.35,'OIH':0.35,'AMLP':0.85,'JETS':0.60,'TAN':0.67,
    'ICLN':0.42,'LIT':0.75,'URA':0.69,'NLR':0.61,'IYT':0.39,'XME':0.35,'GDX':0.51,
    'GDXJ':0.52,'SIL':0.65,'COPX':0.65,'XAR':0.35,'ITA':0.40,'HACK':0.60,'CIBR':0.60,
    'BUG':0.65,'IHAK':0.47,'SKYY':0.60,'CLOU':0.68,'WCLD':0.45,'FDN':0.49,'ARKK':0.75,
    'ARKG':0.75,'ARKW':0.82,'ARKF':0.75,'BOTZ':0.68,'ROBO':0.95,'PAVE':0.47,'FINX':0.65,
    'BLOK':0.71,'ESPO':0.55,'HERO':0.50,'FFTY':0.80,'MOAT':0.46,'KWEB':0.70,'CQQQ':0.65,
    'KARS':0.70,'DRIV':0.68,'IDRV':0.47,'REM':0.48,'REZ':0.48,'SCHH':0.07,
    # factor / style / dividend
    'MTUM':0.15,'VLUE':0.15,'QUAL':0.15,'USMV':0.15,'SPLV':0.25,'VUG':0.04,'VTV':0.04,
    'IWF':0.19,'IWD':0.19,'SCHD':0.06,'DVY':0.38,'VIG':0.06,'VYM':0.06,'HDV':0.08,
    'SDY':0.35,'NOBL':0.35,'DGRO':0.08,'COWZ':0.49,'SPHD':0.30,'CALF':0.59,
    # international
    'EEM':0.68,'IEMG':0.09,'VWO':0.08,'EFA':0.32,'IEFA':0.07,'VEA':0.05,'ACWI':0.32,
    'VEU':0.07,'VT':0.06,'FXI':0.74,'MCHI':0.59,'KWEB':0.70,'ASHR':0.65,'EWZ':0.59,
    'EWJ':0.50,'INDA':0.64,'EWT':0.59,'EWY':0.59,'EWG':0.50,'EWU':0.50,'EWC':0.50,
    'EWA':0.50,'EWH':0.50,'EWW':0.50,'EWS':0.50,'EWP':0.50,'EWQ':0.50,'EWI':0.50,
    'EWL':0.50,'EWD':0.50,'EZU':0.59,'IEUR':0.09,'ILF':0.48,'EPP':0.49,'EWM':0.50,
    'THD':0.59,'TUR':0.59,'EZA':0.59,'GXC':0.59,
    # bonds (mostly filtered by the swing-range gate, but kept for completeness)
    'TLT':0.15,'IEF':0.15,'SHY':0.15,'LQD':0.14,'HYG':0.49,'JNK':0.40,'AGG':0.03,
    'BND':0.03,'TIP':0.19,'MUB':0.07,'EMB':0.39,'BKLN':0.65,'VCIT':0.04,'VCSH':0.04,
    # commodities / metals
    'GLD':0.40,'GLDM':0.10,'SLV':0.50,'IAU':0.25,'USO':0.60,'BNO':1.00,'UNG':0.90,
    'DBC':0.85,'PDBC':0.59,'DBA':0.93,'CPER':0.88,'WEAT':0.85,'CORN':0.79,'PPLT':0.60,
    'PALL':0.60,'SIVR':0.30,'SGOL':0.17,
}

# Liquid large/mid-cap stocks (price≥$10, heavily traded). Same breakout strategy.
STOCK_UNIVERSE = {s: None for s in [
    # mega-cap tech
    'AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','AVGO','ORCL','NFLX','AMD','ADBE',
    # semis
    'MU','INTC','QCOM','TXN','LRCX','AMAT','KLAC','ARM','SMCI','MRVL','ON','MPWR','ASML','TSM',
    # software / cloud / cyber
    'CRM','NOW','PANW','CRWD','SNOW','NET','DDOG','ZS','PLTR','MDB','SHOP','TEAM','WDAY','ADSK','INTU','FTNT','ANET','CSCO','IBM','UBER','ABNB',
    # fintech / financials
    'JPM','BAC','WFC','GS','MS','C','BLK','SCHW','V','MA','AXP','PYPL','SQ','COIN','HOOD','SOFI','KKR',
    # healthcare
    'UNH','JNJ','LLY','PFE','MRK','ABBV','TMO','ABT','DHR','ISRG','AMGN','GILD','VRTX','REGN','BMY','MDT','BSX',
    # consumer / retail
    'WMT','COST','HD','MCD','NKE','SBUX','TGT','LOW','DIS','BKNG','CMG','LULU','ROST','TJX','PG','KO','PEP','MDLZ',
    # industrials / defense
    'CAT','DE','BA','GE','HON','UPS','RTX','LMT','GD','NOC','EMR','ETN','PH','UNP','FDX',
    # energy / materials
    'XOM','CVX','COP','SLB','OXY','EOG','MPC','PSX','FCX','NEM','LIN','NUE',
    # comm / media / telecom
    'T','VZ','CMCSA','TMUS','WBD',
    # autos / EV / travel
    'F','GM','RIVN','LCID','DAL','UAL','CCL','RCL',
    # momentum / growth names
    'DKNG','RBLX','U','SNAP','PINS','SPOT','ZM','ROKU','DOCU','TTD','CVNA','AFRM','DASH','CELH','SMR','VST','CEG','GEV',
]}

# Sector tag per stock (for the macro-Quad cross-reference)
_SECTOR_GROUPS = {
    'Technology':             'AAPL MSFT ORCL ADBE RBLX U ZM DOCU TTD UBER ANET CSCO IBM',
    'Semiconductors':         'NVDA AVGO AMD MU INTC QCOM TXN LRCX AMAT KLAC ARM SMCI MRVL ON MPWR ASML TSM',
    'Software':               'CRM NOW PANW CRWD SNOW NET DDOG ZS PLTR MDB SHOP TEAM WDAY ADSK INTU FTNT',
    'Financials':             'JPM BAC WFC GS MS C BLK SCHW V MA AXP PYPL SQ COIN HOOD SOFI KKR AFRM',
    'Healthcare':             'UNH JNJ LLY PFE MRK ABBV TMO ABT DHR ISRG AMGN GILD VRTX REGN BMY MDT BSX',
    'Consumer Staples':       'WMT COST PG KO PEP MDLZ CELH',
    'Consumer Discretionary': 'AMZN TSLA HD MCD NKE SBUX TGT LOW BKNG CMG LULU ROST TJX ABNB DKNG CVNA DASH F GM RIVN LCID DAL UAL CCL RCL',
    'Industrials':            'CAT DE BA GE HON UPS RTX LMT GD NOC EMR ETN PH UNP FDX GEV',
    'Energy':                 'XOM CVX COP SLB OXY EOG MPC PSX',
    'Materials':              'FCX NEM LIN NUE',
    'Communications':         'GOOGL META NFLX T VZ CMCSA TMUS WBD DIS SNAP PINS SPOT ROKU',
    'Utilities':              'SMR VST CEG',
}
STOCK_SECTORS = {s: sec for sec, syms in _SECTOR_GROUPS.items() for s in syms.split()}

# Hedgeye-style Quad playbook: which sectors / ETFs historically lead in each
# Growth×Inflation rate-of-change regime.
QUAD_PLAYBOOK = {
    1: {'label': 'Goldilocks', 'growth': 'accelerating', 'inflation': 'decelerating',
        'note': 'Growth ↑, Inflation ↓ — risk-on. Growth & momentum lead: tech, semis, software, discretionary.',
        'sectors': ['Technology','Semiconductors','Software','Consumer Discretionary','Communications'],
        'etfs': ['XLK','XLY','XLC','VGT','VCR','VOX','QQQ','QQQM','IGV','IGM','IYW','SMH','SOXX','XSD','PSI','SKYY','CLOU','WCLD','FDN','ARKK','ARKW','ARKF','ARKG','BOTZ','ROBO','FINX','BLOK','ESPO','HERO','FFTY','KWEB','CQQQ','MTUM','VUG','IWF','MGK','HACK','CIBR','BUG','IHAK','XRT']},
    2: {'label': 'Reflation', 'growth': 'accelerating', 'inflation': 'accelerating',
        'note': 'Growth ↑, Inflation ↑ — reflation. Cyclicals win: energy, materials, industrials, financials, semis, small caps.',
        'sectors': ['Technology','Semiconductors','Software','Industrials','Materials','Financials','Energy','Consumer Discretionary'],
        'etfs': ['XLE','XLI','XLB','XLF','XLK','VDE','VIS','VAW','VFH','IYF','KRE','KBE','KBWB','XOP','OIH','AMLP','XME','GDX','GDXJ','SIL','COPX','PAVE','XAR','ITA','JETS','IYT','SMH','SOXX','IWM','MDY','IJR','VB','EEM','IEMG','VWO','EWZ','ILF','FXI','MCHI','KWEB','LIT','URA','NLR','KARS','DRIV','IDRV','DBC','PDBC','DBA','CPER','USO','BNO']},
    3: {'label': 'Stagflation', 'growth': 'decelerating', 'inflation': 'accelerating',
        'note': 'Growth ↓, Inflation ↑ — stagflation. Real assets & defensives: energy, gold, commodities, utilities, staples.',
        'sectors': ['Energy','Materials','Utilities','Healthcare','Consumer Staples'],
        'etfs': ['XLE','XLU','XLP','XLV','XLB','VDE','VPU','VDC','VHT','VAW','VNQ','IYR','SCHH','REZ','REM','XOP','OIH','AMLP','GLD','GLDM','IAU','SGOL','SLV','SIVR','GDX','GDXJ','SIL','DBC','PDBC','DBA','CPER','WEAT','CORN','PPLT','PALL','URA','NLR','XME','COPX','TIP','USO','BNO','UNG']},
    4: {'label': 'Deflation', 'growth': 'decelerating', 'inflation': 'decelerating',
        'note': 'Growth ↓, Inflation ↓ — risk-off. Duration & defensives: long Treasuries, USD, utilities, staples, healthcare.',
        'sectors': ['Utilities','Consumer Staples','Healthcare','Technology'],
        'etfs': ['TLT','IEF','AGG','BND','LQD','VCIT','VCSH','MUB','XLU','VPU','XLP','VDC','XLV','VHT','USMV','SPLV','QUAL','SCHD','VYM','VIG','HDV','DVY','SDY','NOBL','DGRO','SPHD','GLD','GLDM','IAU','SGOL']},
}

# Volatile, liquid "movers" — the universe where Opening-Range Breakout has an edge.
ORB_UNIVERSE = ['NVDA','TSLA','AMD','AAPL','MSFT','AMZN','META','GOOGL','NFLX','AVGO',
                'MU','INTC','QCOM','MRVL','ON','SMCI','PLTR','NET','CRWD','DDOG','SNOW',
                'MDB','PANW','SHOP','UBER','ABNB','RBLX','U','DKNG','COIN','MSTR','MARA',
                'RIOT','HOOD','SOFI','AFRM','PYPL','RIVN','LCID','NIO','GME','SOUN','IONQ',
                'RGTI','BBAI','UPST','CVNA','SNAP','ROKU','PINS','CELH','SMR','VST','BABA','TSM']

def _yahoo_5m_days(sym, rng="1mo"):
    """Fetch 5-min bars and group into US regular-session days (09:30–16:00 ET).
    Returns (list of (day_int, [(sod,o,h,l,c,v,epoch),...]) sorted, gmt_offset_seconds)."""
    import urllib.request, json as _json
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=5m&includePrePost=false"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = _json.load(r)
    res = d['chart']['result'][0]; meta = res.get('meta', {})
    ts = res.get('timestamp') or []; q = res['indicators']['quote'][0]
    gmt = meta.get('gmtoffset', 0); days = {}
    for i in range(len(ts)):
        o, h, l, c, v = q['open'][i], q['high'][i], q['low'][i], q['close'][i], q['volume'][i]
        if None in (o, h, l, c): continue
        loc = ts[i] + gmt; sod = loc % 86400; day = loc // 86400
        if 34200 <= sod < 57600:
            days.setdefault(day, []).append((sod, o, h, l, c, v or 0, ts[i]))
    return [(day, sorted(days[day])) for day in sorted(days)], gmt

def _yahoo_ohlc(sym, rng="2y"):
    import urllib.request, json as _json, datetime as _dt
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = _json.load(r)
    res = d['chart']['result'][0]; q = res['indicators']['quote'][0]
    ts = res['timestamp']
    rows = []
    for t, o, h, l, c, v in zip(ts, q['open'], q['high'], q['low'], q['close'], q['volume']):
        if None in (o, h, l, c, v): continue
        rows.append((_dt.date.fromtimestamp(t).isoformat(), o, h, l, c, v))
    return rows

def _atr_series(highs, lows, closes, p=14):
    import numpy as np
    n = len(closes); tr = [highs[0]-lows[0]]
    for k in range(1, n):
        tr.append(max(highs[k]-lows[k], abs(highs[k]-closes[k-1]), abs(lows[k]-closes[k-1])))
    return [float(np.mean(tr[max(0,k-p+1):k+1])) for k in range(n)]

def _wmom(closes, i):
    """IBD-style weighted 12-month momentum: 40% the most recent quarter,
    20% each of the prior three quarters. Returns None if <252 bars of history."""
    if i < 252: return None
    q1 = closes[i]/closes[i-63]      - 1
    q2 = closes[i-63]/closes[i-126]  - 1
    q3 = closes[i-126]/closes[i-189] - 1
    q4 = closes[i-189]/closes[i-252] - 1
    return 0.4*q1 + 0.2*q2 + 0.2*q3 + 0.2*q4

def _rank_pct(values):
    """Cross-sectional RS rating: {key: momentum} -> {key: percentile 1..99}."""
    import numpy as np
    keys = [k for k, v in values.items() if v is not None]
    if not keys: return {}
    arr = np.array([values[k] for k in keys]); m = len(arr)
    order = arr.argsort(); ranks = np.empty(m); ranks[order] = np.arange(m)
    return {k: (int(round(ranks[idx]/(m-1)*98 + 1)) if m > 1 else 50)
            for idx, k in enumerate(keys)}

ATR_MULT      = 6.0  # trailing-stop width (× ATR) — exits via trailing stop only (no profit-taking; 5-yr optimization showed it's best)
BREAKOUT_BUF  = 0.5  # breakout confirmation buffer (× ATR above the level) — volatility-scaled, beats a flat % (5-yr backtest)
RS_RATING_MIN = 70   # stocks: high-quality requires RS rating ≥ this (IBD-style top-30% momentum; 5-yr backtest)

def run_etf_scan(fmp_key=None, universe=None, is_stock=False):
    import numpy as np
    # Default universe = curated ETFs; callers can pass a stock universe instead.
    universe = dict(universe if universe is not None else CURATED_ETFS)

    def pivots(highs, lows, w=8):
        ph, pl = [], []
        for i in range(w, len(highs)-w):
            if highs[i] == max(highs[i-w:i+w+1]): ph.append(i)
            if lows[i]  == min(lows[i-w:i+w+1]):  pl.append(i)
        return ph, pl

    def cluster(levels, tol=0.025):
        out = []
        for lv in sorted(levels):
            for g in out:
                if abs(lv-g['m'])/g['m'] <= tol:
                    g['vals'].append(lv); g['m'] = float(np.mean(g['vals'])); break
            else:
                out.append({'m': lv, 'vals': [lv]})
        return [(g['m'], len(g['vals'])) for g in out]

    def rets(c):
        return {d: ((c[-1]/c[-1-d]-1)*100 if len(c) > d else 0.0) for d in (21, 63, 126)}

    def analyze(sym, expense, spy_ret, rows=None, rs_rating=None):
        if rows is None: rows = _yahoo_ohlc(sym)
        if len(rows) < 120: return None
        highs=[r[2] for r in rows]; lows=[r[3] for r in rows]; closes=[r[4] for r in rows]; vols=[r[5] for r in rows]
        atr_s = _atr_series(highs, lows, closes)
        price = closes[-1]; avgvol = float(np.mean(vols[-30:]))
        if price < 10 or avgvol < 50000: return None      # user's liquidity/price filters
        rng6 = (max(closes[-126:]) - min(closes[-126:])) / price      # ~6-month swing size
        if rng6 < 0.08: return None
        ph, pl = pivots(highs, lows)
        res = [(m, n) for m, n in cluster([highs[i] for i in ph]) if n >= 2]
        sup = [(m, n) for m, n in cluster([lows[i]  for i in pl]) if n >= 2]
        signal, lvl, touches, bo_idx = None, None, 0, None
        for m, n in res:
            below = any(closes[j] < m*0.995 for j in range(max(0,len(closes)-40), len(closes)-3))
            cross = next((j for j in range(max(1,len(closes)-15), len(closes))
                          if closes[j] > m + BREAKOUT_BUF*atr_s[j] and closes[j-1] <= m + BREAKOUT_BUF*atr_s[j-1]), None)
            if below and price > m and cross is not None:
                signal, lvl, touches, bo_idx = 'BREAKOUT', m, n, cross; break
        if not signal:
            for m, n in res:
                if 0 < (m-price)/price <= 0.04:
                    signal, lvl, touches = 'APPROACHING', m, n; break
        if not signal: return None

        # ── Relative strength vs SPY (1/3/6-mo, weighted toward 3-mo) ──────
        er = rets(closes)
        rs = round(0.25*(er[21]-spy_ret[21]) + 0.5*(er[63]-spy_ret[63]) + 0.25*(er[126]-spy_ret[126]), 1)

        # ── Volume confirmation on the breakout bar (≥1.5× 20-day avg) ─────
        vol_surge, vol_ok = None, False
        if bo_idx is not None and bo_idx >= 20:
            base = float(np.mean(vols[bo_idx-20:bo_idx]))
            if base > 0:
                vol_surge = round(vols[bo_idx]/base, 2); vol_ok = vol_surge >= 1.5

        # ── Break-and-retest: after breaking, dipped back to the level and held ─
        retested = False
        if bo_idx is not None:
            for k in range(bo_idx+1, len(closes)):
                if lows[k] <= lvl*1.015 and closes[k] >= lvl*0.99:   # pulled back to level
                    retested = price > lvl; break

        strength = max(0.0, price/lvl - 1) if signal == 'BREAKOUT' else 0.0
        clean    = min(touches, 5)
        prox     = max(0.0, 1 - (lvl - price)/(price*0.04)) if signal == 'APPROACHING' else 0.0
        rs_pts   = ((rs_rating-50)/50*20) if rs_rating is not None else max(-20.0, min(20.0, rs))  # stocks: RS rating; ETFs: RS vs SPY
        score = round(rng6*80 + clean*6 + strength*150 + prox*50 + rs_pts*1.5
                      + (12 if vol_ok else 0) + (15 if retested else 0), 1)
        # BUY trigger = the breakout level; STOP = ATR-based trailing stop (6× ATR,
        # ratchets up as price rises). Initial stop shown here.
        atr_now = atr_s[-1]
        stop = round(lvl - ATR_MULT*atr_now, 2)
        atr_pct = round(ATR_MULT*atr_now/lvl*100, 1)
        return {
            'sym': sym, 'price': round(price, 2), 'avgvol': int(avgvol),
            'expense': expense, 'signal': signal, 'level': round(lvl, 2),
            'entry': round(lvl, 2), 'stop': stop, 'atr_pct': atr_pct,
            'touches': touches, 'score': score, 'rs': rs, 'rs_rating': rs_rating,
            'sector': (STOCK_SECTORS.get(sym) if is_stock else None),
            'vol_surge': vol_surge, 'vol_ok': bool(vol_ok), 'retested': bool(retested),
            'support':    [round(m, 2) for m, n in sup][:3],
            'resistance': [round(m, 2) for m, n in res][-3:],
            'ohlc': [[r[0], round(r[1],2), round(r[2],2), round(r[3],2), round(r[4],2)] for r in rows[-260:]],
        }

    # SPY benchmark returns (for relative strength), fetched once
    try:
        spy_closes = [r[4] for r in _yahoo_ohlc('SPY')]
        spy_ret = rets(spy_closes)
    except Exception:
        spy_ret = {21: 0.0, 63: 0.0, 126: 0.0}

    # Fetch all price history once so stocks can be cross-sectionally ranked (RS rating)
    cache = {}
    for sym, exp in universe.items():
        if exp is not None and exp >= 1.0: continue       # net expense ratio < 1%
        try: cache[sym] = _yahoo_ohlc(sym)
        except Exception: pass
        time.sleep(0.15)                                  # be gentle to Yahoo
    rs_ratings = {}
    if is_stock:                                          # IBD-style RS rating (stocks only)
        moms = {s: _wmom([r[4] for r in rw], len(rw)-1) for s, rw in cache.items()}
        rs_ratings = _rank_pct(moms)
    breakouts, approaching = [], []
    for sym, exp in universe.items():
        rows = cache.get(sym)
        if rows is None: continue
        try:
            a = analyze(sym, exp, spy_ret, rows=rows, rs_rating=rs_ratings.get(sym))
            if a:
                (breakouts if a['signal'] == 'BREAKOUT' else approaching).append(a)
        except Exception:
            pass
    breakouts.sort(key=lambda x: -x['score'])
    approaching.sort(key=lambda x: -x['score'])
    print(f"  [etf] {len(breakouts)} breakouts, {len(approaching)} approaching")
    return {'breakouts': breakouts, 'approaching': approaching,
            'scanned': len(universe), 'source': 'curated'}


def run_etf_single(sym):
    """Analyze ONE ticker (any ETF or stock) for a breakout/momentum setup."""
    import numpy as np
    rows = _yahoo_ohlc(sym, '2y')
    if len(rows) < 120:
        return {'sym': sym, 'error': 'not enough price history'}
    highs=[r[2] for r in rows]; lows=[r[3] for r in rows]; closes=[r[4] for r in rows]; vols=[r[5] for r in rows]
    price = closes[-1]; avgvol = float(np.mean(vols[-30:]))
    def piv(h, l, w=8):
        ph=[i for i in range(w,len(h)-w) if h[i]==max(h[i-w:i+w+1])]
        pl=[i for i in range(w,len(l)-w) if l[i]==min(l[i-w:i+w+1])]
        return ph, pl
    def clu(levels, tol=0.025):
        out=[]
        for lv in sorted(levels):
            for g in out:
                if abs(lv-g['m'])/g['m']<=tol: g['vals'].append(lv); g['m']=float(np.mean(g['vals'])); break
            else: out.append({'m':lv,'vals':[lv]})
        return [(g['m'],len(g['vals'])) for g in out]
    ph, pl = piv(highs, lows)
    res = [(m,n) for m,n in clu([highs[i] for i in ph]) if n>=2]
    sup = [(m,n) for m,n in clu([lows[i]  for i in pl]) if n>=2]
    # SPY relative strength
    try:
        sc=[r[4] for r in _yahoo_ohlc('SPY','2y')]
        spy_r={d:(sc[-1]/sc[-1-d]-1)*100 for d in (21,63,126)}
        er={d:((closes[-1]/closes[-1-d]-1)*100 if len(closes)>d else 0) for d in (21,63,126)}
        rs=round(0.25*(er[21]-spy_r[21])+0.5*(er[63]-spy_r[63])+0.25*(er[126]-spy_r[126]),1)
    except Exception:
        rs=0.0
    atr_s = _atr_series(highs, lows, closes)
    signal, lvl, touches, bo = 'NONE', None, 0, None
    for m,n in res:
        below=any(closes[j]<m*0.995 for j in range(max(0,len(closes)-40),len(closes)-3))
        cross=next((j for j in range(max(1,len(closes)-15),len(closes)) if closes[j]>m+BREAKOUT_BUF*atr_s[j] and closes[j-1]<=m+BREAKOUT_BUF*atr_s[j-1]),None)
        if below and price>m and cross is not None: signal,lvl,touches,bo='BREAKOUT',m,n,cross; break
    if signal=='NONE':
        for m,n in res:
            if 0<(m-price)/price<=0.04: signal,lvl,touches='APPROACHING',m,n; break
    if signal=='NONE':                          # no setup → watch the nearest ceiling above price
        above=[(m,n) for m,n in res if m>price]
        if above: lvl,touches=min(above,key=lambda x:x[0])
        elif res: lvl,touches=res[-1]
        else: lvl,touches=round(price*1.05,2),0
    atr=atr_s[-1]
    vol_surge=None; vol_ok=False
    if bo is not None and bo>=20:
        b=float(np.mean(vols[bo-20:bo]));
        if b>0: vol_surge=round(vols[bo]/b,2); vol_ok=vol_surge>=1.5
    strength=max(0.0, price/lvl-1) if signal=='BREAKOUT' else 0.0
    rng6=(max(closes[-126:])-min(closes[-126:]))/price
    score=round(rng6*80+min(touches,5)*6+strength*150+max(-20,min(20,rs))*1.5+(12 if vol_ok else 0),1)
    return {
        'sym': sym, 'price': round(price,2), 'avgvol': int(avgvol), 'signal': signal,
        'level': round(lvl,2), 'entry': round(lvl,2), 'stop': round(lvl-ATR_MULT*atr,2),
        'rs': rs, 'score': score, 'touches': touches,
        'vol_surge': vol_surge, 'vol_ok': bool(vol_ok),
        'hi': bool(vol_ok and rs>0 and score>=60 and signal=='BREAKOUT'),
        'support': [round(m,2) for m,_ in sup][:3], 'resistance': [round(m,2) for m,_ in res][-3:],
        'ohlc': [[r[0],round(r[1],2),round(r[2],2),round(r[3],2),round(r[4],2)] for r in rows[-260:]],
    }


def run_etf_backtest(atr_mult=ATR_MULT, years=1, universe=None, is_stock=False):
    """Point-in-time backtest of the breakout BUY rule with an ATR trailing
    stop (Chandelier exit): enter at the close of the day price breaks above a
    tested resistance level BY A VOLATILITY BUFFER (BREAKOUT_BUF×ATR); trail a
    stop at (highest-high-since-entry − atr_mult×ATR) that only ratchets up; exit
    when price closes below it. No look-ahead. Stocks also require an IBD-style
    cross-sectional RS rating ≥ RS_RATING_MIN to count as 'high-quality'.
    `years` = how far back to generate signals (1, 2 or 5)."""
    import numpy as np

    def cl(levels, tol=0.025):
        out = []
        for lv in sorted(levels):
            for g in out:
                if abs(lv-g['m'])/g['m'] <= tol: g['vals'].append(lv); g['m'] = float(np.mean(g['vals'])); break
            else: out.append({'m': lv, 'vals': [lv]})
        return [(g['m'], len(g['vals'])) for g in out]

    rng = f"{max(2, years+1)}y"               # extra year of warmup for pivots/RS
    # SPY for point-in-time relative strength (date → close)
    try:
        spy_rows = _yahoo_ohlc('SPY', rng); spy_map = {r[0]: r[4] for r in spy_rows}
    except Exception:
        spy_map = {}

    W = 8
    trades = []

    def run_one(sym, rows, rank_by_date):
        if len(rows) < 200: return
        dates=[r[0] for r in rows]; highs=[r[2] for r in rows]; lows=[r[3] for r in rows]
        closes=[r[4] for r in rows]; vols=[r[5] for r in rows]
        n = len(closes)
        atr = _atr_series(highs, lows, closes)
        ph = [i for i in range(W, n-W) if highs[i] == max(highs[i-W:i+W+1])]
        start = max(252 if is_stock else 70, n - 252*years)   # signals over the last `years`
        in_pos = False; entry = lvl = 0.0; entry_i = 0
        e_vol = False; e_rs = 0.0; e_rating = None; e_score = 0.0
        hh = trail = 0.0
        i = start
        while i < n:
            if not in_pos:
                conf = [highs[p] for p in ph if p <= i-W]            # confirmed-by-now pivots
                levels = [(m, c) for m, c in cl(conf) if c >= 2]
                for m, c in levels:
                    if m <= 0: continue
                    below = any(closes[j] < m*0.995 for j in range(max(0, i-40), i))
                    if below and closes[i] > m + BREAKOUT_BUF*atr[i] and closes[i-1] <= m + BREAKOUT_BUF*atr[i-1]:
                        in_pos, entry, lvl, entry_i = True, closes[i], m, i
                        hh = highs[i]; trail = hh - atr_mult*atr[i]    # initial ATR stop
                        base = float(np.mean(vols[i-20:i])) if i >= 20 else 0
                        e_vol = base > 0 and vols[i] >= 1.5*base
                        d_now, d_then = dates[i], dates[i-63]
                        if i >= 63 and d_now in spy_map and d_then in spy_map:
                            e_rs = (closes[i]/closes[i-63]-1)*100 - (spy_map[d_now]/spy_map[d_then]-1)*100
                        else:
                            e_rs = 0.0
                        e_rating = rank_by_date.get(dates[i], {}).get(sym) if is_stock else None
                        rs_pts = ((e_rating-50)/50*20) if e_rating is not None else max(-20.0, min(20.0, e_rs))
                        # same swing-quality score the scanner uses, point-in-time at entry
                        wlo = max(0, i-126)
                        rng6 = (max(closes[wlo:i+1]) - min(closes[wlo:i+1])) / closes[i]
                        strength = closes[i]/m - 1
                        e_score = round(rng6*80 + min(c,5)*6 + strength*150 + rs_pts*1.5 + (12 if e_vol else 0), 1)
                        break
            else:
                hh = max(hh, highs[i])
                trail = max(trail, hh - atr_mult*atr[i])               # ratchet the stop up only
                if closes[i] < trail or i == n-1:
                    good_mom = (e_rating is not None and e_rating >= RS_RATING_MIN) if is_stock else (e_rs > 0)
                    trades.append({'sym': sym, 'date': dates[entry_i], 'exit_date': dates[i],
                                   'level': round(lvl,2),
                                   'entry': round(entry,2), 'exit': round(closes[i],2),
                                   'ret': round((closes[i]/entry-1)*100, 2),
                                   'days': i-entry_i,
                                   'why': 'open' if i == n-1 and closes[i] >= trail else 'trail-stop',
                                   'vol_ok': bool(e_vol), 'rs': round(e_rs,1),
                                   'rs_rating': e_rating, 'score': e_score,
                                   'hi': bool(e_vol and good_mom and e_score >= 60)})
                    in_pos = False
            i += 1

    uni = universe if universe is not None else CURATED_ETFS
    if is_stock:
        # Pre-load all stocks once → build cross-sectional RS-rating ranks per date
        data = {}
        for sym, exp in uni.items():
            try: data[sym] = _yahoo_ohlc(sym, rng)
            except Exception: pass
            time.sleep(0.1)
        mom_by_date = {}
        for sym, rows in data.items():
            closes = [r[4] for r in rows]; dts = [r[0] for r in rows]
            for i in range(252, len(closes)):
                mm = _wmom(closes, i)
                if mm is not None: mom_by_date.setdefault(dts[i], {})[sym] = mm
        rank_by_date = {d: _rank_pct(mm) for d, mm in mom_by_date.items()}
        for sym, rows in data.items():
            run_one(sym, rows, rank_by_date)
    else:
        for sym, exp in uni.items():
            if exp is not None and exp >= 1.0: continue
            try: rows = _yahoo_ohlc(sym, rng)
            except Exception: continue
            run_one(sym, rows, {})
            time.sleep(0.1)

    def agg(ts):
        rets = [t['ret'] for t in ts]
        wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
        n = len(rets); gains = sum(wins); pains = abs(sum(losses))
        return {
            'trades': n,
            'win_rate':   round(len(wins)/n*100) if n else 0,
            'avg_ret':    round(float(np.mean(rets)), 2) if n else 0,
            'avg_win':    round(float(np.mean(wins)), 2) if wins else 0,
            'avg_loss':   round(float(np.mean(losses)), 2) if losses else 0,
            'profit_factor': round(gains/pains, 2) if pains > 0 else None,
            'best':  round(max(rets), 2) if n else 0,
            'worst': round(min(rets), 2) if n else 0,
            'avg_days': round(float(np.mean([t['days'] for t in ts])), 0) if n else 0,
            'rule': f'{atr_mult:g}× ATR trailing stop',
        }

    trades.sort(key=lambda t: t['date'])
    hi = [t for t in trades if t['hi']]
    stats_all, stats_hi = agg(trades), agg(hi)
    print(f"  [etf-bt] ALL {stats_all['trades']} trades win {stats_all['win_rate']}% avg {stats_all['avg_ret']}% PF {stats_all['profit_factor']}")
    print(f"  [etf-bt] HI  {stats_hi['trades']} trades win {stats_hi['win_rate']}% avg {stats_hi['avg_ret']}% PF {stats_hi['profit_factor']}")
    return {'stats': stats_all, 'stats_all': stats_all, 'stats_hi': stats_hi,
            'years': years, 'trades': [t for t in trades if t['hi']][-120:]}


def run_quad(fred_key):
    """Macro Quad from the existing nowcasts: classify by the RATE OF CHANGE
    (accelerating vs decelerating) of GDP growth and CPI inflation. Flags when
    an axis is within its forecast error band → 'near a quad transition'."""
    gdp = cached_ml("gdp:12", lambda: run_gdp_nowcast(fred_key, 12))
    inf = cached_ml("inf:24", lambda: run_inflation_nowcast(fred_key, 24))
    g_now = gdp['nowcast']['value']; g_prev = gdp['nowcast']['last_actual']
    i_now = inf['nowcast']['value']; i_prev = inf['nowcast']['last_actual']
    g_d = round(g_now - g_prev, 2); i_d = round(i_now - i_prev, 2)
    g_up = g_d > 0; i_up = i_d > 0
    def q_of(gu, iu): return 1 if (gu and not iu) else 2 if (gu and iu) else 3 if (not gu and iu) else 4
    quad = q_of(g_up, i_up)
    g_band = float(gdp.get('metrics', {}).get('acc_band') or 0.5)
    i_band = float((inf['nowcast'].get('high', i_now) - i_now) or inf.get('metrics', {}).get('acc_band') or 0.3)
    g_near = abs(g_d) < g_band; i_near = abs(i_d) < i_band
    cand = []
    if g_near: cand.append((abs(g_d)/max(g_band, 1e-6), q_of(not g_up, i_up),
                            f"growth {'rolling over' if g_up else 'troughing'}"))
    if i_near: cand.append((abs(i_d)/max(i_band, 1e-6), q_of(g_up, not i_up),
                            f"inflation {'rolling over' if i_up else 're-accelerating'}"))
    cand.sort()
    return {
        'ok': True, 'quad': quad, 'label': QUAD_PLAYBOOK[quad]['label'],
        'growth':    {'value': round(g_now, 2), 'last': round(g_prev, 2), 'delta': g_d,
                      'dir': 'accelerating' if g_up else 'decelerating', 'near_flip': g_near},
        'inflation': {'value': round(i_now, 2), 'last': round(i_prev, 2), 'delta': i_d,
                      'dir': 'accelerating' if i_up else 'decelerating', 'near_flip': i_near},
        'asof': {'gdp': gdp['nowcast'].get('quarter'), 'cpi': inf['nowcast'].get('month')},
        'near_transition': bool(cand),
        'next_quad':   cand[0][1] if cand else None,
        'next_reason': cand[0][2] if cand else None,
        'playbook': QUAD_PLAYBOOK,
    }


def send_daily_digest(dry=False):
    """Build & email the top ETFs/stocks within ±DIGEST_BAND% of their breakout
    line, ranked by score. Creds from env (set on Render). dry=True skips the send."""
    import smtplib, ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from datetime import datetime, timezone
    user   = os.environ.get('GMAIL_USER') or ''
    pwd    = os.environ.get('GMAIL_APP_PASSWORD') or ''
    sg_key = os.environ.get('SENDGRID_API_KEY') or ''
    bv_key = os.environ.get('BREVO_API_KEY') or ''
    to     = os.environ.get('DIGEST_TO') or user
    frm    = os.environ.get('DIGEST_FROM') or user
    base = (os.environ.get('DASHBOARD_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    band = float(os.environ.get('DIGEST_BAND') or '2.5')
    topn = int(os.environ.get('DIGEST_TOPN') or '10')
    if not dry and not (sg_key or bv_key or (user and pwd)):
        return {'ok': False, 'error': 'No email method set. On Render add SENDGRID_API_KEY or BREVO_API_KEY (HTTPS — SMTP is blocked on Render).'}

    etf   = cached_ml('etf:scan',   lambda: run_etf_scan(None, None))
    stock = cached_ml('stock:scan', lambda: run_etf_scan(None, STOCK_UNIVERSE, is_stock=True))
    def pick(scan):
        rows = (scan.get('breakouts') or []) + (scan.get('approaching') or [])
        out = []
        for a in rows:
            lvl, px = a.get('level'), a.get('price')
            if not lvl or not px: continue
            d = (px/lvl - 1) * 100
            if abs(d) <= band: out.append(dict(a, _dist=round(d, 1)))
        out.sort(key=lambda x: -(x.get('score') or 0))
        return out[:topn]
    e_pick, s_pick = pick(etf), pick(stock)

    def rs_cell(a):
        if a.get('rs_rating') is not None: return f"RS {a['rs_rating']}"
        if a.get('rs') is not None: return f"{'+' if a['rs']>0 else ''}{a['rs']}% vs SPY"
        return "—"
    def row(a):
        broke = a.get('signal') == 'BREAKOUT'
        tag = '▲ broke out' if broke else '◇ approaching'
        return (f'<tr style="border-bottom:1px solid #eee">'
                f'<td style="padding:7px 10px;font-weight:700">{a["sym"]}</td>'
                f'<td style="padding:7px 10px;text-align:right">${a.get("price")}</td>'
                f'<td style="padding:7px 10px;text-align:right">${a.get("level")}</td>'
                f'<td style="padding:7px 10px;text-align:right">{a["_dist"]:+}%</td>'
                f'<td style="padding:7px 10px">{tag}</td>'
                f'<td style="padding:7px 10px;text-align:right">{rs_cell(a)}</td>'
                f'<td style="padding:7px 10px;text-align:right;font-weight:700">{a.get("score")}</td></tr>')
    def table(title, items):
        head = ('<tr style="background:#f4f4f7;text-align:left">'
                '<th style="padding:7px 10px">Ticker</th><th style="padding:7px 10px;text-align:right">Price</th>'
                '<th style="padding:7px 10px;text-align:right">Breakout</th><th style="padding:7px 10px;text-align:right">± Line</th>'
                '<th style="padding:7px 10px">Status</th><th style="padding:7px 10px;text-align:right">RS</th>'
                '<th style="padding:7px 10px;text-align:right">Score</th></tr>')
        body = ''.join(row(a) for a in items) or f'<tr><td colspan="7" style="padding:10px;color:#888">Nothing within ±{band:g}% today.</td></tr>'
        return (f'<h2 style="font:600 16px system-ui;margin:22px 0 8px">{title}</h2>'
                f'<table style="border-collapse:collapse;width:100%;font:13px system-ui;border:1px solid #eee">{head}{body}</table>')

    today = datetime.now(timezone.utc).strftime('%a %b %d, %Y')
    link = (f'<p style="margin:0 0 16px"><a href="{base}" style="display:inline-block;background:#5b8def;'
            f'color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font:600 14px system-ui">Open the dashboard →</a></p>') if base else ''
    html = (f'<div style="max-width:680px;margin:0 auto">'
            f'<h1 style="font:700 20px system-ui;margin:0 0 4px">📈 Daily Swing Digest</h1>'
            f'<p style="color:#888;font:12px system-ui;margin:0 0 14px">{today} · within ±{band:g}% of the breakout line, ranked by score</p>'
            f'{link}{table(f"Top {topn} ETFs", e_pick)}{table(f"Top {topn} Stocks", s_pick)}'
            f'<p style="color:#aaa;font:11px system-ui;margin-top:18px">Auto-generated from your market dashboard. '
            f'▲ = just broke out · ◇ = about to. Not financial advice.</p></div>')

    if dry:
        return {'ok': True, 'dry': True, 'etf': [a['sym'] for a in e_pick],
                'stock': [a['sym'] for a in s_pick], 'html_len': len(html), 'to': to}
    recips = [t.strip() for t in to.split(',') if t.strip()]
    subject = f"📈 Daily Swing Digest — {today}"
    ok = lambda via: {'ok': True, 'via': via, 'to': to,
                      'etf': [a['sym'] for a in e_pick], 'stock': [a['sym'] for a in s_pick]}

    if sg_key:                       # SendGrid HTTP API (port 443 — works on Render)
        payload = json.dumps({'personalizations': [{'to': [{'email': e} for e in recips]}],
                              'from': {'email': frm}, 'subject': subject,
                              'content': [{'type': 'text/html', 'value': html}]}).encode()
        req = urllib.request.Request('https://api.sendgrid.com/v3/mail/send', data=payload,
                headers={'Authorization': f'Bearer {sg_key}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r: r.read()
        print(f"[digest] sent via SendGrid to {recips}"); return ok('sendgrid')

    if bv_key:                       # Brevo HTTP API (port 443 — works on Render)
        payload = json.dumps({'sender': {'email': frm}, 'to': [{'email': e} for e in recips],
                              'subject': subject, 'htmlContent': html}).encode()
        req = urllib.request.Request('https://api.brevo.com/v3/smtp/email', data=payload,
                headers={'api-key': bv_key, 'Content-Type': 'application/json', 'accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r: r.read()
        print(f"[digest] sent via Brevo to {recips}"); return ok('brevo')

    # Gmail SMTP fallback — works locally / on GitHub, but Render blocks outbound SMTP
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject; msg['From'] = user; msg['To'] = to
    msg.attach(MIMEText("Open in an HTML-capable client to see the tables.", 'plain'))
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=20) as s:
        s.login(user, pwd)
        s.sendmail(user, recips, msg.as_string())
    print(f"[digest] sent via SMTP to {to}"); return ok('smtp')


def run_orb_scan(or_min=15):
    """Opening-Range Breakout setups for today's movers. For each name: the first
    `or_min` minutes' high/low define the range; long = break above (stop = range
    low), short = break below (stop = range high); flag 'in play' (RVOL≥1.5 or
    |gap|≥2%). 5-yr-of-evidence + our backtest: trade in-play names, hold to close."""
    import datetime as _dt
    or_end = 34200 + or_min*60; need = or_min // 5
    setups = []; gmt0 = -14400; session_day = None
    for sym in ORB_UNIVERSE:
        try: sessions, gmt = _yahoo_5m_days(sym)
        except Exception: continue
        if len(sessions) < 3: continue
        gmt0 = gmt
        today, trows = sessions[-1]
        prev_close = sessions[-2][1][-1][4]
        or_bars = [r for r in trows if r[0] < or_end]
        if not or_bars: continue
        complete = len(or_bars) >= need
        or_high = max(r[2] for r in or_bars); or_low = min(r[3] for r in or_bars)
        if not or_high > or_low: continue
        price = trows[-1][4]
        gap = round((or_bars[0][1]/prev_close - 1)*100, 2) if prev_close else 0.0
        ov = sum(r[5] for r in or_bars); pv = []
        for d, rows in sessions[:-1][-10:]:
            ob = [r for r in rows if r[0] < or_end]
            if ob: pv.append(sum(x[5] for x in ob))
        rvol = round(ov/(sum(pv)/len(pv)), 2) if pv and sum(pv) else None
        in_play = (rvol is not None and rvol >= 1.5) or abs(gap) >= 2.0
        post = [r for r in trows if r[0] >= or_end]
        side = None; status = 'OR forming'
        if complete:
            status = 'armed'
            for r in post:
                if r[2] >= or_high: side = 'LONG'; status = 'broke up'; break
                if r[3] <= or_low:  side = 'SHORT'; status = 'broke down'; break
        risk = round(or_high - or_low, 2)
        cpv = cv = 0.0; vwap = []
        for r in trows:
            tp = (r[2]+r[3]+r[4])/3; cpv += tp*r[5]; cv += r[5]
            vwap.append(round(cpv/cv, 4) if cv else round(r[4], 4))
        ohlc = [[r[6], round(r[1],4), round(r[2],4), round(r[3],4), round(r[4],4)] for r in trows]
        session_day = today
        setups.append({
            'sym': sym, 'price': round(price, 4), 'or_high': round(or_high, 4), 'or_low': round(or_low, 4),
            'gap': gap, 'rvol': rvol, 'in_play': bool(in_play), 'complete': bool(complete),
            'status': status, 'side': side, 'risk': risk, 'risk_pct': round(risk/price*100, 2),
            'dist_up': round((or_high-price)/price*100, 2), 'dist_dn': round((price-or_low)/price*100, 2),
            'ohlc': ohlc, 'vwap': vwap,
        })
        time.sleep(0.05)
    now_et = time.time() + gmt0; sod = int(now_et) % 86400
    sd = _dt.datetime.utcfromtimestamp(session_day*86400).strftime('%a %b %-d') if session_day else ''
    live = bool(session_day) and (session_day == int(now_et)//86400)
    mkt = ('open' if (live and 34200 <= sod < 57600) else ('closed (after hours)' if live else 'pre-market / last session'))
    setups.sort(key=lambda x: (not x['in_play'], x['status'] != 'armed', -(x['rvol'] or 0)))
    return {'setups': setups, 'session': sd, 'market': mkt, 'live': live,
            'in_play': sum(1 for s in setups if s['in_play']), 'scanned': len(ORB_UNIVERSE), 'or_min': or_min}


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
                print(f"\n[nowcast] Request ({quarters} quarters backtest) …")
                result  = cached_ml(f"gdp:{quarters}", lambda: run_gdp_nowcast(fred_key, quarters))
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

        if parsed.path == '/inflation-nowcast':
            qs       = urllib.parse.parse_qs(parsed.query)
            fred_key = qs.get('fred_key', [''])[0]
            months   = int(qs.get('months', ['24'])[0])
            if not fred_key:
                self.send_error(400, 'fred_key required'); return
            try:
                print(f"\n[inflation] Request ({months} months backtest) …")
                result  = cached_ml(f"inf:{months}", lambda: run_inflation_nowcast(fred_key, months))
                payload = json.dumps(result).encode()
                print(f"[inflation] Done → {result['nowcast']['value']}%")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception:
                err = traceback.format_exc()
                print(f"[inflation] ERROR:\n{err}")
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': err}).encode())
            return

        if parsed.path == '/send-digest':
            qs = urllib.parse.parse_qs(parsed.query)
            token = qs.get('token', [''])[0]
            want  = os.environ.get('DIGEST_TOKEN') or ''
            if not want or token != want:
                self.send_response(403); self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': 'bad or missing token'}).encode()); return
            sync = ('sync' in qs) or ('dry' in qs)
            dry  = 'dry' in qs
            try:
                if sync:
                    result = send_daily_digest(dry=dry)
                    code = 200 if result.get('ok') else 500
                else:
                    threading.Thread(target=lambda: send_daily_digest(), daemon=True).start()
                    result, code = {'ok': True, 'status': 'digest started'}, 200
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception:
                err = traceback.format_exc(); print(f"[digest] ERROR:\n{err}")
                self.send_response(500); self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': err}).encode())
            return

        if parsed.path == '/orb-scan':
            try:
                print("\n[orb] Opening-range scan …")
                result  = cached_ml('orb:scan', run_orb_scan, ttl=120)   # 2-min cache (intraday)
                payload = json.dumps(result).encode()
                print(f"[orb] Done → {result['in_play']} in-play of {len(result['setups'])}")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception:
                err = traceback.format_exc(); print(f"[orb] ERROR:\n{err}")
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': err}).encode())
            return

        if parsed.path == '/quad':
            qs = urllib.parse.parse_qs(parsed.query)
            fred_key = qs.get('fred_key', [''])[0]
            try:
                if not fred_key: raise ValueError('fred_key required')
                print("\n[quad] Macro quad request …")
                result  = run_quad(fred_key)
                payload = json.dumps(result).encode()
                print(f"[quad] Done → Quad {result['quad']} ({result['label']})")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())
            return

        if parsed.path in ('/etf-scan', '/stock-scan'):
            is_stock = parsed.path == '/stock-scan'
            try:
                tag = 'stock' if is_stock else 'etf'
                print(f"\n[{tag}] Scan request …")
                uni = STOCK_UNIVERSE if is_stock else None
                result  = cached_ml(f"{tag}:scan", lambda: run_etf_scan(None, uni, is_stock=is_stock))
                payload = json.dumps(result).encode()
                print(f"[{tag}] Done → {len(result['breakouts'])} breakouts")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception:
                err = traceback.format_exc()
                print(f"[scan] ERROR:\n{err}")
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': err}).encode())
            return

        if parsed.path == '/etf-chart':
            qs  = urllib.parse.parse_qs(parsed.query)
            sym = (qs.get('sym', [''])[0] or '').upper()
            if not sym:
                self.send_error(400, 'sym required'); return
            try:
                rows = _yahoo_ohlc(sym, '5y')     # cover multi-year backtest entries
                ohlc = [[r[0], round(r[1],2), round(r[2],2), round(r[3],2), round(r[4],2)] for r in rows[-1300:]]
                payload = json.dumps({'sym': sym, 'ohlc': ohlc}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if parsed.path == '/etf-analyze':
            qs  = urllib.parse.parse_qs(parsed.query)
            sym = (qs.get('sym', [''])[0] or '').upper().strip()
            if not sym:
                self.send_error(400, 'sym required'); return
            try:
                print(f"\n[etf-analyze] {sym} …")
                result  = run_etf_single(sym)
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'sym': sym, 'error': str(e)}).encode())
            return

        if parsed.path in ('/etf-backtest', '/stock-backtest'):
            is_stock = parsed.path == '/stock-backtest'
            qs    = urllib.parse.parse_qs(parsed.query)
            years = int(qs.get('years', ['1'])[0])
            if years not in (1, 2, 5): years = 1
            try:
                tag = 'stock-bt' if is_stock else 'etf-bt'
                print(f"\n[{tag}] {years}-year breakout backtest …")
                uni = STOCK_UNIVERSE if is_stock else None
                result  = cached_ml(f"{tag}:{years}", lambda: run_etf_backtest(years=years, universe=uni, is_stock=is_stock))
                payload = json.dumps(result).encode()
                print(f"[{tag}] Done → {result['stats']['trades']} trades")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers(); self.wfile.write(payload)
            except Exception:
                err = traceback.format_exc()
                print(f"[bt] ERROR:\n{err}")
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
    ThreadingHTTPServer(('', PORT), Handler).serve_forever()
