"""The risk engine. Pure, deterministic, fully unit-testable.

Enforces every hard rule. This is the real guarantee — not the Claude prompt and not
the Pydantic schema. A passing intent becomes `pending_approval`; a failing one is
recorded as `rejected_by_risk` and is never approvable.

Rules enforced:
  0. Daily-loss kill switch (halt at -DAILY_LOSS_LIMIT_PCT for the day)
  1. Stop loss required on every trade
  2. Limit orders only (no market orders)
  3. Max 10% of portfolio per trade
  4. Max 3 open positions
  5. No margin (cash-only)
  6. No options
  7. No market orders on penny stocks (< $5)
  8. No buying stocks up more than 50% intraday
"""
from dataclasses import dataclass, field
from decimal import Decimal

from app.config import settings


@dataclass
class Violation:
    code: str
    message: str


@dataclass
class RiskInput:
    intent: dict                      # TradeIntent fields as a dict
    equity: Decimal
    cash: Decimal
    open_positions: int
    held_symbols: set[str]
    day_pnl_pct: Decimal              # negative = loss on the day
    last_price: Decimal
    intraday_change_pct: Decimal      # change vs today's open
    is_margin_account: bool
    is_option: bool
    trading_halted: bool


@dataclass
class RiskResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    computed_qty: int | None = None
    computed_notional: Decimal | None = None

    def as_violation_dicts(self) -> list[dict]:
        return [{"code": v.code, "message": v.message} for v in self.violations]


def _dec(value, default="0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def evaluate(r: RiskInput) -> RiskResult:
    v: list[Violation] = []
    intent = r.intent
    action = intent.get("action")

    # Rule 0 — daily-loss kill switch (applies to every actionable intent)
    if r.trading_halted or r.day_pnl_pct <= -settings.daily_loss_limit_pct:
        v.append(
            Violation(
                "DAILY_LOSS_HALT",
                f"Trading halted: day P&L {r.day_pnl_pct}% has hit the "
                f"-{settings.daily_loss_limit_pct}% daily loss limit",
            )
        )

    # "watch" / "avoid" intents take no position — only the halt check applies.
    if action in ("watch", "avoid"):
        return RiskResult(passed=not v, violations=v)

    # Rule 1 — stop loss required
    stop_pct = _dec(intent.get("stop_loss_percent"))
    if stop_pct <= 0:
        v.append(Violation("NO_STOP_LOSS", "Every trade must define a positive stop loss"))

    # Rule 2 — limit orders only
    order_type = intent.get("order_type")
    if order_type != "limit":
        v.append(Violation("NON_LIMIT_ORDER", f"Order type '{order_type}' not allowed; limit only"))

    # Rule 3 — position size <= 10% of portfolio
    size_pct = _dec(intent.get("position_size_percent"))
    if size_pct > settings.max_position_pct:
        v.append(
            Violation(
                "POSITION_TOO_LARGE",
                f"position_size_percent {size_pct}% exceeds max {settings.max_position_pct}%",
            )
        )
    if size_pct <= 0:
        v.append(Violation("ZERO_SIZE", "position_size_percent must be greater than 0 for a trade"))

    notional = (size_pct / Decimal("100")) * r.equity

    # Rule 4 — max open positions (only when opening a NEW position)
    if action == "buy" and intent.get("ticker") not in r.held_symbols:
        if r.open_positions >= settings.max_open_positions:
            v.append(
                Violation(
                    "TOO_MANY_POSITIONS",
                    f"Already holding {r.open_positions} positions "
                    f"(max {settings.max_open_positions})",
                )
            )

    # Rule 5 — no margin (cash-only). We do NOT reject merely because the account is
    # margin-enabled (Alpaca paper accounts are margin-enabled by default, which would
    # otherwise block every trade). We reject only when the order would actually REQUIRE
    # margin — i.e. its notional exceeds available cash. This keeps every fill cash-funded.
    if action == "buy":
        if notional > r.cash:
            v.append(
                Violation(
                    "MARGIN_REQUIRED",
                    f"Order notional ${notional:.2f} exceeds available cash ${r.cash:.2f} "
                    f"(would require margin)",
                )
            )

    # Rule 6 — no options
    if r.is_option:
        v.append(Violation("OPTIONS_FORBIDDEN", "Options instruments are not allowed"))

    # Rule 7 — no market orders on penny stocks (we already require limit, so this
    # additionally guards the penny-stock case explicitly).
    if r.last_price < settings.penny_stock_price and order_type != "limit":
        v.append(
            Violation(
                "PENNY_MARKET_ORDER",
                f"No market orders on sub-${settings.penny_stock_price} stocks "
                f"(price ${r.last_price})",
            )
        )

    # Rule 8 — don't buy parabolic names (> 50% intraday gain)
    if action == "buy" and r.intraday_change_pct > settings.max_intraday_gain_pct:
        v.append(
            Violation(
                "PARABOLIC_MOVE",
                f"{intent.get('ticker')} is up {r.intraday_change_pct}% intraday "
                f"(> {settings.max_intraday_gain_pct}% limit)",
            )
        )

    # Compute server-side share count. Claude never sets absolute size.
    computed_qty = None
    computed_notional = None
    if not v:
        limit = _dec(intent.get("limit_price"))
        if limit <= 0:
            v.append(Violation("INVALID_LIMIT", "limit_price must be greater than 0"))
        else:
            computed_qty = int(notional / limit)
            computed_notional = Decimal(computed_qty) * limit
            if computed_qty < 1:
                v.append(Violation("ZERO_QTY", "Computed quantity rounds to zero shares"))
                computed_qty = None
                computed_notional = None

    return RiskResult(
        passed=not v,
        violations=v,
        computed_qty=computed_qty,
        computed_notional=computed_notional,
    )
