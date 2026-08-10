"""Deterministic finance calculations; no language model arithmetic."""
from __future__ import annotations

from dataclasses import dataclass
from math import pow


def percentage_change(start: float, end: float) -> float:
    if start == 0:
        raise ValueError("Starting value cannot be zero")
    return (end - start) / start * 100


def cagr(start: float, end: float, years: float) -> float:
    if start <= 0 or end < 0 or years <= 0:
        raise ValueError("CAGR requires positive start/years and non-negative end")
    return (pow(end / start, 1 / years) - 1) * 100


def simple_interest(principal: float, annual_rate_pct: float, years: float) -> float:
    return principal * annual_rate_pct / 100 * years


def compound_interest(principal: float, annual_rate_pct: float, years: float, compounds_per_year: int = 1) -> float:
    if compounds_per_year <= 0:
        raise ValueError("Compounding frequency must be positive")
    return principal * pow(1 + annual_rate_pct / 100 / compounds_per_year, compounds_per_year * years) - principal


def pe_ratio(price: float, earnings_per_share: float) -> float:
    if earnings_per_share == 0:
        raise ValueError("EPS cannot be zero")
    return price / earnings_per_share


def profit_loss(shares: float, buy_price: float, sell_price: float, fees: float = 0) -> dict[str, float]:
    cost = shares * buy_price
    proceeds = shares * sell_price
    profit = proceeds - cost - fees
    return {"cost": cost, "proceeds": proceeds, "profit": profit, "return_pct": percentage_change(cost, proceeds - fees)}


def average_cost(purchases: list[tuple[float, float]]) -> float:
    shares = sum(quantity for quantity, _ in purchases)
    if shares == 0:
        raise ValueError("Total shares cannot be zero")
    return sum(quantity * price for quantity, price in purchases) / shares


def weighted_return(weights_and_returns: list[tuple[float, float]]) -> float:
    return sum(weight * return_pct for weight, return_pct in weights_and_returns) / 100


def drawdown(peak: float, trough: float) -> float:
    return percentage_change(peak, trough)


def position_size(capital: float, risk_pct: float, entry: float, stop: float) -> float:
    per_unit_risk = abs(entry - stop)
    if per_unit_risk == 0:
        raise ValueError("Entry and stop cannot be equal")
    return capital * risk_pct / 100 / per_unit_risk
