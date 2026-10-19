"""Keyless FRED series fetch.

`fredgraph.csv?id=SERIES` returns CSV with no API key and no account — verified
2026-08-17. Header is `observation_date,<SERIES>`; missing observations appear
as `.` and are dropped.

Same failure discipline as yf_util: retries with backoff, never raises.
"""
import io, sys, time, warnings
warnings.filterwarnings("ignore")
import urllib.request, urllib.error
import pandas as pd

BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def fred(series_id, attempts=3, quiet=False):
    """Daily/weekly/monthly observations as a tz-naive pandas Series, or None."""
    delay = 5
    for n in range(attempts):
        try:
            # NO custom headers. Measured 2026-08-17: FRED serves Python's
            # default urllib User-Agent in ~0.3s, but tarpits a spoofed
            # `Mozilla/5.0` UA into a 25s timeout, and an honest custom UA into
            # RemoteDisconnected. This is the exact inverse of Google News RSS,
            # which needs a browser UA. Do not "fix" this by adding headers.
            req = urllib.request.Request(BASE + series_id)
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            df = pd.read_csv(io.StringIO(raw))
            if df.shape[1] < 2:
                reason = "unexpected CSV shape"
            else:
                datecol, valcol = df.columns[0], df.columns[1]
                df[valcol] = pd.to_numeric(df[valcol], errors="coerce")
                df = df.dropna(subset=[valcol])
                s = pd.Series(df[valcol].values,
                              index=pd.to_datetime(df[datecol]).values).sort_index()
                if len(s):
                    return s
                reason = "empty after parse"
        except Exception as e:                       # noqa: BLE001 - deliberate
            reason = f"{type(e).__name__}: {e}"
        if n < attempts - 1:
            if not quiet:
                print(f"  retry {n+1}/{attempts-1} FRED:{series_id}: {reason}", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    if not quiet:
        print(f"  GIVE UP FRED:{series_id}: {reason}", file=sys.stderr)
    return None
