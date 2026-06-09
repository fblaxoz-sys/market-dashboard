#!/usr/bin/env python3
"""
Daily swing digest → emails the top-N ETFs and stocks that are within ±BAND%
of their breakout level (so both 'about to break' and 'just broke out' qualify),
ranked by score. Pulls the live dashboard's /etf-scan and /stock-scan JSON,
optionally tags the current macro Quad, and sends via Gmail SMTP.

Designed to run from a GitHub Actions cron. All config via env vars:
  DASHBOARD_URL       e.g. https://your-app.onrender.com   (required)
  GMAIL_USER          your gmail address                    (required to send)
  GMAIL_APP_PASSWORD  16-char Google App Password           (required to send)
  DIGEST_TO           recipient (default: GMAIL_USER)
  FRED_KEY            optional — adds the macro-Quad header
  BAND                distance band in %, default 2.5
  TOPN                names per list, default 10
If GMAIL_USER / GMAIL_APP_PASSWORD aren't set, it logs and exits 0 (no failure),
so the scheduled job stays green until you finish setup.
"""
import os, sys, json, time, ssl, smtplib, urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

# `or` (not the get-default) so empty strings from unset GH vars fall back too
URL  = (os.environ.get('DASHBOARD_URL') or '').rstrip('/')
USER = os.environ.get('GMAIL_USER') or ''
PWD  = os.environ.get('GMAIL_APP_PASSWORD') or ''
TO   = os.environ.get('DIGEST_TO') or USER
FRED = os.environ.get('FRED_KEY') or ''
BAND = float(os.environ.get('BAND') or '2.5')
TOPN = int(os.environ.get('TOPN') or '10')
DRY  = bool(os.environ.get('DRY_RUN'))

if not URL:
    print("DASHBOARD_URL not set — nothing to do."); sys.exit(0)
if not DRY and not (USER and PWD):
    print("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping send (configure secrets to enable)."); sys.exit(0)

def fetch(path, timeout=200, tries=5):
    """Fetch JSON, tolerating Render cold-starts + fresh scan compute (~1-3 min)."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(URL + path, headers={'User-Agent': 'daily-digest'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            print(f"  fetch {path} attempt {attempt+1}/{tries} failed: {e}; retrying in 25s")
            time.sleep(25)
    raise last

def pick(scan):
    rows = (scan.get('breakouts') or []) + (scan.get('approaching') or [])
    out = []
    for a in rows:
        lvl, px = a.get('level'), a.get('price')
        if not lvl or not px:
            continue
        dist = (px / lvl - 1) * 100                 # +above / -below the breakout line
        if abs(dist) <= BAND:
            a['_dist'] = dist
            out.append(a)
    out.sort(key=lambda x: -(x.get('score') or 0))
    return out[:TOPN]

def rs_cell(a):
    if a.get('rs_rating') is not None:
        return f"RS {a['rs_rating']}"
    if a.get('rs') is not None:
        return f"{'+' if a['rs'] > 0 else ''}{a['rs']}% vs SPY"
    return "—"

def fmt_rev_b(b):
    """Revenue ($B) → clean at-a-glance string: $120B / $15.2B / $450M."""
    if b is None:
        return '—'
    if b >= 100:
        return f"${round(b)}B"
    if b >= 1:
        s = f"{b:.1f}"
        return f"${s[:-2] if s.endswith('.0') else s}B"
    return f"${round(b * 1000)}M"

def spark(vals):
    """Email-safe Unicode block sparkline (no SVG — Gmail strips that)."""
    blocks = '▁▂▃▄▅▆▇█'
    if not vals:
        return ''
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    return ''.join(blocks[min(7, int((v - lo) / rng * 7))] for v in vals)

def rev_cell(a):
    """Latest revenue + trend sparkline + change %, for the Stocks table."""
    rv = a.get('rev')
    if not rv or not rv.get('quarters'):
        return '<span style="color:#bbb">—</span>'
    vals = [q['b'] for q in rv['quarters']]
    col = '#1a8f5f' if rv.get('up') else '#c0392b'
    arrow = '▲' if rv.get('up') else '▼'
    chg = rv.get('change_pct')
    chg_html = ''
    if chg is not None:
        cc = '#1a8f5f' if chg >= 0 else '#c0392b'
        chg_html = f' <span style="color:{cc};font-weight:600">{"+" if chg >= 0 else ""}{chg}%</span>'
    return (f'<b style="font-size:14px">{fmt_rev_b(vals[-1])}</b> '
            f'<span style="color:{col};letter-spacing:1px">{spark(vals)}</span> '
            f'<span style="color:{col}">{arrow}</span>{chg_html}')

def rows_html(items, show_rev=False):
    ncols = 8 if show_rev else 7
    if not items:
        return f'<tr><td colspan="{ncols}" style="padding:10px;color:#888">No names within ±{BAND:.1f}% of a breakout today.</td></tr>'
    out = []
    for a in items:
        d = a['_dist']
        broke = a.get('signal') == 'BREAKOUT'
        tag = ('<span style="color:#1a8f5f;font-weight:600">▲ broke out</span>' if broke
               else '<span style="color:#b8860b;font-weight:600">◇ approaching</span>')
        dstr = f"{'+' if d >= 0 else ''}{d:.1f}%"
        sec = a.get('sector') or ''
        rev_td = f'<td style="padding:7px 10px;white-space:nowrap">{rev_cell(a)}</td>' if show_rev else ''
        out.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:7px 10px;font-weight:700">{a["sym"]}</td>'
            f'<td style="padding:7px 10px;text-align:right">${a.get("price")}</td>'
            f'<td style="padding:7px 10px;text-align:right">${a.get("level")}</td>'
            f'<td style="padding:7px 10px;text-align:right">{dstr}</td>'
            f'<td style="padding:7px 10px">{tag}</td>'
            f'<td style="padding:7px 10px;text-align:right">{rs_cell(a)}</td>'
            f'{rev_td}'
            f'<td style="padding:7px 10px;text-align:right;font-weight:700">{a.get("score")}</td>'
            f'</tr>')
    return "\n".join(out)

def table(title, items, show_rev=False):
    rev_th = '<th style="padding:7px 10px">Revenue (4Q)</th>' if show_rev else ''
    head = ('<tr style="background:#f4f4f7;text-align:left">'
            '<th style="padding:7px 10px">Ticker</th>'
            '<th style="padding:7px 10px;text-align:right">Price</th>'
            '<th style="padding:7px 10px;text-align:right">Breakout</th>'
            '<th style="padding:7px 10px;text-align:right">± Line</th>'
            '<th style="padding:7px 10px">Status</th>'
            '<th style="padding:7px 10px;text-align:right">RS</th>'
            f'{rev_th}'
            '<th style="padding:7px 10px;text-align:right">Score</th></tr>')
    return (f'<h2 style="font:600 16px system-ui;margin:22px 0 8px">{title}</h2>'
            f'<table style="border-collapse:collapse;width:100%;font:13px system-ui;'
            f'border:1px solid #eee">{head}{rows_html(items, show_rev)}</table>')

def main():
    print(f"Fetching scans from {URL} …")
    etf   = pick(fetch("/etf-scan"))
    stock = pick(fetch("/stock-scan"))
    print(f"  ETF picks: {[a['sym'] for a in etf]}")
    print(f"  Stock picks: {[a['sym'] for a in stock]}")

    today = datetime.now(timezone.utc).strftime('%a %b %d, %Y')
    html = (f'<div style="max-width:680px;margin:0 auto">'
            f'<h1 style="font:700 20px system-ui;margin:0 0 4px">📈 Daily Swing Digest</h1>'
            f'<p style="color:#888;font:12px system-ui;margin:0 0 14px">{today} · '
            f'within ±{BAND:g}% of the breakout line, ranked by score</p>'
            f'<p style="margin:0 0 16px"><a href="{URL}" style="display:inline-block;'
            f'background:#5b8def;color:#fff;text-decoration:none;padding:10px 18px;'
            f'border-radius:8px;font:600 14px system-ui">Open the dashboard →</a></p>'
            f'{table(f"Top {TOPN} ETFs", etf)}'
            f'{table(f"Top {TOPN} Stocks", stock, show_rev=True)}'
            f'<p style="color:#aaa;font:11px system-ui;margin-top:18px">'
            f'Auto-generated from your market dashboard. ▲ = just broke out · ◇ = about to. '
            f'Not financial advice.</p></div>')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📈 Daily Swing Digest — {today}"
    msg['From'] = USER
    msg['To'] = TO
    msg.attach(MIMEText("Open in an HTML-capable client to see the tables.", 'plain'))
    msg.attach(MIMEText(html, 'html'))

    if DRY:
        print(f"DRY_RUN — built the email ({len(html)} chars), not sending.")
        return

    print(f"Sending to {TO} via Gmail SMTP …")
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context()) as s:
        s.login(USER, PWD)
        s.sendmail(USER, [t.strip() for t in TO.split(',')], msg.as_string())
    print("Sent ✓")

if __name__ == '__main__':
    main()
