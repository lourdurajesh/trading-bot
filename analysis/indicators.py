"""
indicators.py
─────────────
All technical indicators as pure, stateless functions.
Each function takes a pandas DataFrame (with OHLCV columns)
and returns a Series or scalar value.

No side effects. No global state. Fully vectorised with numpy/pandas.
Strategies import and call these directly.
"""

import numpy as np
import pandas as pd
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# MOVING AVERAGES
# ─────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Volume Weighted Average Price.
    Resets each session (day). df must have: high, low, close, volume.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cumvol  = df["volume"].cumsum()
    cumtpvol = (tp * df["volume"]).cumsum()
    return cumtpvol / cumvol


# ─────────────────────────────────────────────────────────────────
# MOMENTUM
# ─────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's smoothed).
    Returns values 0–100.
    """
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD indicator.
    Returns: (macd_line, signal_line, histogram)
    """
    ema_fast   = ema(series, fast)
    ema_slow   = ema(series, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(
    df: pd.DataFrame,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """
    Stochastic Oscillator.
    Returns: (%K, %D)
    """
    lowest_low   = df["low"].rolling(k_period).min()
    highest_high = df["high"].rolling(k_period).max()
    pct_k = 100 * (df["close"] - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    pct_d = pct_k.rolling(d_period).mean()
    return pct_k, pct_d


# ─────────────────────────────────────────────────────────────────
# VOLATILITY
# ─────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    Returns: (upper_band, middle_band, lower_band)
    """
    middle = sma(series, period)
    std    = series.rolling(period).std()
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    return upper, middle, lower


def bollinger_width(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """
    Bollinger Band Width — measures volatility expansion/contraction.
    High = volatile/trending, Low = squeeze (potential breakout).
    """
    upper, middle, lower = bollinger_bands(series, period, std_dev)
    return (upper - lower) / middle.replace(0, np.nan)


def keltner_channel(
    df: pd.DataFrame,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Keltner Channel.
    Returns: (upper, middle, lower)
    """
    middle = ema(df["close"], ema_period)
    atr_val = atr(df, atr_period)
    upper = middle + multiplier * atr_val
    lower = middle - multiplier * atr_val
    return upper, middle, lower


# ─────────────────────────────────────────────────────────────────
# TREND STRENGTH
# ─────────────────────────────────────────────────────────────────

def adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Average Directional Index.
    Returns: (ADX, +DI, -DI)
    ADX > 25 = strong trend, < 20 = weak/ranging.
    """
    prev_high  = df["high"].shift(1)
    prev_low   = df["low"].shift(1)
    prev_close = df["close"].shift(1)

    plus_dm  = df["high"] - prev_high
    minus_dm = prev_low - df["low"]
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    tr_val = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)

    smooth_tr      = tr_val.ewm(alpha=1 / period, adjust=False).mean()
    smooth_plus    = plus_dm.ewm(alpha=1 / period, adjust=False).mean()
    smooth_minus   = minus_dm.ewm(alpha=1 / period, adjust=False).mean()

    plus_di  = 100 * smooth_plus  / smooth_tr.replace(0, np.nan)
    minus_di = 100 * smooth_minus / smooth_tr.replace(0, np.nan)

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / period, adjust=False).mean()

    return adx_val, plus_di, minus_di


def ema_slope(series: pd.Series, period: int = 20, lookback: int = 5) -> pd.Series:
    """
    Slope of EMA over the last `lookback` candles.
    Positive = uptrend, Negative = downtrend.
    Normalised by price level (% per bar).
    """
    ema_val = ema(series, period)
    slope   = (ema_val - ema_val.shift(lookback)) / ema_val.shift(lookback) * 100
    return slope


# ─────────────────────────────────────────────────────────────────
# VOLUME
# ─────────────────────────────────────────────────────────────────

def obv(df: pd.DataFrame) -> pd.Series:
    """On Balance Volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Relative Volume (RVOL) — current volume vs average volume.
    > 1.5 = significantly above average (confirms breakouts).
    """
    avg_vol = df["volume"].rolling(period).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return df["volume"].rolling(period).mean()


# ─────────────────────────────────────────────────────────────────
# PIVOT / SUPPORT & RESISTANCE
# ─────────────────────────────────────────────────────────────────

def pivot_points(df: pd.DataFrame) -> dict:
    """
    Classic Pivot Points based on previous session OHLC.
    Uses the last completed candle.
    Returns dict: P, R1, R2, R3, S1, S2, S3
    """
    if len(df) < 2:
        return {}
    prev = df.iloc[-2]
    h, l, c = prev["high"], prev["low"], prev["close"]
    P  = (h + l + c) / 3
    R1 = 2 * P - l
    S1 = 2 * P - h
    R2 = P + (h - l)
    S2 = P - (h - l)
    R3 = h + 2 * (P - l)
    S3 = l - 2 * (h - P)
    return {"P": P, "R1": R1, "R2": R2, "R3": R3, "S1": S1, "S2": S2, "S3": S3}


def swing_highs(series: pd.Series, lookback: int = 5) -> pd.Series:
    """
    Returns a boolean Series — True at local swing high pivots.
    A swing high = higher than `lookback` bars on both sides.
    """
    is_high = pd.Series(False, index=series.index)
    for i in range(lookback, len(series) - lookback):
        window = series.iloc[i - lookback: i + lookback + 1]
        if series.iloc[i] == window.max():
            is_high.iloc[i] = True
    return is_high


def swing_lows(series: pd.Series, lookback: int = 5) -> pd.Series:
    """
    Returns a boolean Series — True at local swing low pivots.
    """
    is_low = pd.Series(False, index=series.index)
    for i in range(lookback, len(series) - lookback):
        window = series.iloc[i - lookback: i + lookback + 1]
        if series.iloc[i] == window.min():
            is_low.iloc[i] = True
    return is_low


# ─────────────────────────────────────────────────────────────────
# COMPOSITE HELPERS  (used by strategies directly)
# ─────────────────────────────────────────────────────────────────

def ema_alignment(df: pd.DataFrame) -> dict:
    """
    Returns EMA(9), EMA(21), EMA(50), EMA(200) values for the latest candle,
    plus a 'bullish' and 'bearish' boolean flag.

    Bullish alignment: EMA9 > EMA21 > EMA50 > EMA200
    Bearish alignment: EMA9 < EMA21 < EMA50 < EMA200
    """
    close = df["close"]
    e9   = ema(close, 9).iloc[-1]
    e21  = ema(close, 21).iloc[-1]
    e50  = ema(close, 50).iloc[-1]
    e200 = ema(close, 200).iloc[-1]

    return {
        "ema9":    e9,
        "ema21":   e21,
        "ema50":   e50,
        "ema200":  e200,
        "bullish": e9 > e21 > e50 > e200,
        "bearish": e9 < e21 < e50 < e200,
    }


def momentum_score(df: pd.DataFrame) -> float:
    """
    Composite momentum score from 0 (weak) to 10 (strong).
    Combines: RSI, MACD, EMA alignment, RVOL, ADX.
    Used by strategy_selector to rank symbols.
    """
    close = df["close"]
    score = 0.0

    # RSI contribution (0-2 pts)
    rsi_val = rsi(close).iloc[-1]
    if 55 < rsi_val < 80:
        score += 2.0
    elif 50 < rsi_val <= 55:
        score += 1.0

    # MACD contribution (0-2 pts)
    _, _, hist = macd(close)
    if hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]:
        score += 2.0
    elif hist.iloc[-1] > 0:
        score += 1.0

    # EMA alignment (0-2 pts)
    align = ema_alignment(df)
    if align["bullish"]:
        score += 2.0
    elif align["ema9"] > align["ema21"] > align["ema50"]:
        score += 1.0

    # Volume confirmation (0-2 pts)
    rvol = relative_volume(df).iloc[-1]
    if rvol > 2.0:
        score += 2.0
    elif rvol > 1.5:
        score += 1.0

    # ADX trend strength (0-2 pts)
    adx_val, _, _ = adx(df)
    adx_now = adx_val.iloc[-1]
    if adx_now > 30:
        score += 2.0
    elif adx_now > 20:
        score += 1.0

    return round(min(score, 10.0), 2)
