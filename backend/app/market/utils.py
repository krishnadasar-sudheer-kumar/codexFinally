"""Small utilities shared by market data sources."""

from __future__ import annotations


def normalize_ticker(ticker: str) -> str:
    """Normalize ticker symbols at market-data boundaries."""
    return ticker.upper().strip()


def normalize_tickers(tickers: list[str]) -> list[str]:
    """Normalize and de-duplicate tickers while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in tickers:
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            seen.add(ticker)
            result.append(ticker)
    return result
