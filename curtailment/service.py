"""业务服务层：导入计量表、提交/更正限发单、按日重算归因、免补额度核算。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from .allocation import (
    QUANT,
    Conflict,
    DayResult,
    Interval,
    Order,
    allocate,
    aggregate_days,
    clamp_curtailment,
    q,
    to_decimal,
    validate_order,
)
from .db import snapshot, tx
from .exemption import compute_day, from_mills, to_mills
from .parsing import (
    MeterRow,
    canonical,
    day_bounds,
    expected_slots,
    month_bounds,
    next_date,
    parse_date,
    parse_order_time,
    parse_utc,
)

FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_NAME = 64


def validate_token(value: str, *, field: str) -> str:
    """单号/风机名会出现在 URL 里：拒绝斜杠与控制字符。"""
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    if len(text) > MAX_NAME:
        raise ValidationError(f"{field}不能超过 {MAX_NAME} 个字符")
    if "/" in text or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise ValidationError(f"{field}不能包含斜杠或控制字符：{text!r}")
    return text

class ValidationError(ValueError):
    """表单/数据校验失败。"""

    def __init__(self, message: str, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.errors = errors if errors else [message]


class OrderConflict(ValidationError):
    """限发单冲突：整单退回。"""

    def __init__(self, order_no: str, conflicts: Sequence[Conflict]):
        self.order_no = order_no
        self.conflicts = list(conflicts)
        super().__init__(
            f"单据 {order_no} 有 {len(self.conflicts)} 个冲突格（实发量超过每格上限），"
            "已退回整单，原单据与归因结果保持不变。",
            [
                f"{item.start_utc} 实发 {q(item.actual)} > 上限 {q(item.cap)}"
                for item in self.conflicts
            ],
        )


@dataclass
class OrderInput:
    order_no: str
    cause: str
    start_utc: str
    end_utc: str
    cap: object

    @classmethod
    def from_form(cls, form: dict) -> "OrderInput":
        def get(key: str) -> str:
            value = form.get(key)
            return "" if value is None else str(value).strip()

        errors: list[str] = []
        order_no = get("order_no")
        cause = get("cause")
        try:
            order_no = validate_token(order_no, field="单号")
        except ValidationError as exc:
            errors.append(str(exc))
        if not cause:
            errors.append("原因不能为空")
        elif len(cause) > MAX_NAME:
            errors.append(f"原因不能超过 {MAX_NAME} 个字符")

        start_dt = end_dt = None
        try:
            start_dt = parse_order_time(get("start_utc") or get("start"), field="开始时刻")
        except ValueError as exc:
            errors.append(str(exc))
        try:
            end_dt = parse_order_time(get("end_utc") or get("end"), field="结束时刻")
        except ValueError as exc:
            errors.append(str(exc))

        cap = None
        cap_text = get("cap")
        if cap_text == "":
            errors.append("每格电量上限不能为空")
        else:
            try:
                cap = to_decimal(cap_text.replace(",", ""))
                if cap < 0:
                    errors.append("每格电量上限不能为负数")
            except ValueError as exc:
                errors.append(str(exc))

        if start_dt and end_dt:
            if end_dt <= start_dt:
                errors.append("结束时刻必须晚于开始时刻")
            else:
                try:
                    expected_slots(start_dt, end_dt)
                except ValueError as exc:
                    errors.append(str(exc))

        if errors:
            raise ValidationError("单据校验失败", errors)
        if start_dt is None or end_dt is None or cap is None:
            raise ValidationError("单据校验失败")

        return cls(
            order_no=order_no,
            cause=cause,
            start_utc=canonical(start_dt),
            end_utc=canonical(end_dt),
            cap=q(cap),
        )


@dataclass
class SubmitResult:
    order_no: str
    revision: int
    corrected: bool
    dates: list[str]
    days: list[DayResult]


@dataclass
class ImportResult:
    turbine: str
    inserted: int
    updated: int
    dates: list[str]
    warnings: list[str] = field(default_factory=list)
    order_conflicts: dict[str, list[Conflict]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _row_to_interval(row: sqlite3.Row) -> Interval:
    return Interval(
        slot=row["id"],
        start_utc=row["start_utc"],
        available=to_decimal(row["available"]),
        actual=to_decimal(row["actual"]),
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_no=row["order_no"],
        cause=row["cause"],
        start_utc=row["start_utc"],
        end_utc=row["end_utc"],
        cap=to_decimal(row["cap"]),
        revision=row["revision"],
    )


def load_intervals(
    conn: sqlite3.Connection,
    turbine: str,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
) -> list[Interval]:
    sql = "SELECT id, start_utc, available, actual FROM meters WHERE turbine = ?"
    params: list = [turbine]
    if start_utc is not None:
        sql += " AND start_utc >= ?"
        params.append(start_utc)
    if end_utc is not None:
        sql += " AND start_utc < ?"
        params.append(end_utc)
    sql += " ORDER BY start_utc"
    return [_row_to_interval(row) for row in conn.execute(sql, params)]


def load_all_intervals(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[Interval]:
    """区间内所有风机的格（冲突检查用：限发单是全风场口径）。"""
    return [
        _row_to_interval(row)
        for row in conn.execute(
            "SELECT id, start_utc, available, actual FROM meters "
            "WHERE start_utc >= ? AND start_utc < ? ORDER BY turbine, start_utc",
            (start_utc, end_utc),
        )
    ]


def load_orders(conn: sqlite3.Connection, *, include_conflicted: bool = False) -> list[Order]:
    """启用且未因导入冲突被暂停的限发单。"""
    sql = "SELECT * FROM orders WHERE active = 1"
    if not include_conflicted:
        sql += " AND conflicted = 0"
    sql += " ORDER BY order_no"
    return [_row_to_order(row) for row in conn.execute(sql)]


def load_order(conn: sqlite3.Connection, order_no: str) -> Optional[Order]:
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return _row_to_order(row) if row else None


def order_dates(start_utc: str, end_utc: str) -> list[str]:
    """单据覆盖（含边界日）的 UTC 日期；end 为开区间。"""
    start = parse_utc(start_utc, field="开始时刻")
    end = parse_utc(end_utc, field="结束时刻")
    if end <= start:
        return []
    last = (end - timedelta(microseconds=1)).date()
    current = start.date()
    dates: list[str] = []
    while current <= last:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def order_conflicts(conn: sqlite3.Connection, order: Order) -> list[Conflict]:
    return validate_order(
        order, load_all_intervals(conn, order.start_utc, order.end_utc)
    )


# ---------------------------------------------------------------------------
# 落库与重算
# ---------------------------------------------------------------------------


def _turbines_with_meters(conn: sqlite3.Connection, date: str) -> list[str]:
    start, end = day_bounds(date)
    return [
        row["turbine"]
        for row in conn.execute(
            "SELECT DISTINCT turbine FROM meters WHERE start_utc >= ? AND start_utc < ? "
            "ORDER BY turbine",
            (start, end),
        )
    ]


def _write_day(
    conn: sqlite3.Connection, turbine: str, date: str, day: DayResult, results
) -> None:
    now = datetime.now(timezone.utc).strftime(FMT)
    conn.execute(
        "INSERT OR REPLACE INTO attribution_days (turbine, date, computed_at) VALUES (?, ?, ?)",
        (turbine, date, now),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_slots "
        "(turbine, start_utc, date, available, actual, unsplit) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                turbine,
                res.start_utc,
                date,
                str(q(res.available)),
                str(q(res.actual)),
                str(q(res.unsplit)),
            )
            for res in results
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_losses "
        "(turbine, start_utc, date, cause, amount) VALUES (?, ?, ?, ?, ?)",
        [
            (turbine, res.start_utc, date, cause, str(q(amount)))
            for res in results
            for cause, amount in res.losses.items()
        ],
    )


def recompute_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[DayResult]:
    """重算并落库某风机某日的归因；该日无计量时清空旧结果并返回 None。

    归因只取该日的格与覆盖该日的启用单，因此重算严格限于日期，不影响其他日期。
    """
    start, end = day_bounds(date)

    conn.execute("DELETE FROM attribution_losses WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_slots WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date))

    intervals = load_intervals(conn, turbine, start, end)
    if not intervals:
        return None

    orders = [
        order
        for order in load_orders(conn)
        if order.start_utc < end and order.end_utc > start
    ]
    results = allocate(intervals, orders)
    day = aggregate_days(results)[0]
    _write_day(conn, turbine, date, day, results)
    return day


def _turbines_with_attribution(conn: sqlite3.Connection, date: str) -> list[str]:
    """该日已存归因的风机（计量被删除后也要清掉其旧结果与免补合计）。"""
    return [
        row["turbine"]
        for row in conn.execute(
            "SELECT DISTINCT turbine FROM attribution_slots WHERE date = ? ORDER BY turbine",
            (date,),
        )
    ]


def recompute_dates(conn: sqlite3.Connection, dates: Iterable[str]) -> list[DayResult]:
    """只重算给定日期：这些日期中有计量数据的每台风机各重算整日。"""
    dates = sorted(set(dates))
    days: list[DayResult] = []
    for date in dates:
        turbines = set(_turbines_with_meters(conn, date)) | set(
            _turbines_with_attribution(conn, date)
        )
        for turbine in sorted(turbines):
            result = recompute_day(conn, turbine, date)
            if result is not None:
                days.append(result)
    # 归因变化会影响当月免补累计，日级免补合计与归因在同一事务内更新。
    _refresh_exemption_months(conn, {date[:7] for date in dates}, changed_dates=dates)
    return days


# ---------------------------------------------------------------------------
# 免补额度
# ---------------------------------------------------------------------------


def _month_of(value: str) -> str:
    """接受 ``YYYY-MM`` 或 ``YYYY-MM-DD``，返回月份。"""
    text = str(value or "").strip()
    if len(text) == 7:
        try:
            datetime.strptime(text, "%Y-%m")
        except ValueError as exc:
            raise ValidationError(f"月份格式应为 YYYY-MM：{value!r}") from exc
        return text
    return parse_date(text, field="日期")[:7]


def _quota_mills(conn: sqlite3.Connection, month: str, cause: str) -> Optional[int]:
    row = conn.execute(
        "SELECT amount FROM exemption_quotas WHERE month = ? AND cause = ?", (month, cause)
    ).fetchone()
    if row is None:
        return None
    return to_mills(row["amount"])


def _attribution_dates(conn: sqlite3.Connection, month: str) -> list[str]:
    start, end = month_bounds(month)
    return [
        row["date"]
        for row in conn.execute(
            "SELECT DISTINCT date FROM attribution_slots "
            "WHERE date >= ? AND date < ? ORDER BY date",
            (start, end),
        )
    ]


def _day_turbines(conn: sqlite3.Connection, date: str) -> list[str]:
    return [
        row["turbine"]
        for row in conn.execute(
            "SELECT DISTINCT turbine FROM attribution_slots WHERE date = ? ORDER BY turbine",
            (date,),
        )
    ]


def _slot_losses(
    conn: sqlite3.Connection, date: str, cause: str
) -> dict[str, dict[str, int]]:
    """某日某原因各时刻的逐风机损失（毫单位）。"""
    slots: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT start_utc, turbine, amount FROM attribution_losses "
        "WHERE date = ? AND cause = ? ORDER BY start_utc, turbine",
        (date, cause),
    ):
        amount = to_mills(row["amount"])
        if amount <= 0:
            continue
        slots.setdefault(row["start_utc"], {})[row["turbine"]] = amount
    return slots


def _write_exemption_day(
    conn: sqlite3.Connection,
    date: str,
    cause: str,
    turbines: Sequence[str],
    result,
    now: str,
) -> None:
    conn.execute("DELETE FROM exemption_days WHERE date = ? AND cause = ?", (date, cause))
    rows = [
        (
            turbine,
            date,
            cause,
            str(from_mills(result.loss.get(turbine, 0))),
            str(from_mills(result.exempt.get(turbine, 0))),
            str(from_mills(result.compensable.get(turbine, 0))),
            now,
        )
        for turbine in turbines
        if result.loss.get(turbine, 0) > 0
    ]
    if rows:
        conn.executemany(
            "INSERT INTO exemption_days "
            "(turbine, date, cause, loss, exempt, compensable, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _month_causes(conn: sqlite3.Connection, month: str) -> list[str]:
    """该月需要核算的原因：已设额度、已归因、已存免补合计的并集。"""
    start, end = month_bounds(month)
    causes: set[str] = {
        row["cause"]
        for row in conn.execute(
            "SELECT cause FROM exemption_quotas WHERE month = ?", (month,)
        )
    }
    causes |= {
        row["cause"]
        for row in conn.execute(
            "SELECT DISTINCT cause FROM attribution_losses WHERE date >= ? AND date < ?",
            (start, end),
        )
    }
    causes |= {
        row["cause"]
        for row in conn.execute(
            "SELECT DISTINCT cause FROM exemption_days WHERE date >= ? AND date < ?",
            (start, end),
        )
    }
    return sorted(causes)


def _recompute_exemption_range(
    conn: sqlite3.Connection,
    month: str,
    cause: str,
    *,
    start_date: Optional[str] = None,
) -> None:
    """从 ``start_date``（含）起到月末重算某原因的免补日级合计。

    月内更早的日期不再读取逐格归因，只累加已存的日级免补合计；
    若发现更早日期有归因但缺少已存合计（旧库首次纳入），则从月初补算。
    """
    month_start, month_end = month_bounds(month)
    start_date = max(month_start, start_date or month_start)
    if start_date >= month_end:
        return

    quota = _quota_mills(conn, month, cause)
    dates = _attribution_dates(conn, month)

    # 月内更早日期只读已存日级合计；若该月尚未完整核算过（旧库首次纳入），
    # 从月初补算，保证累计起点正确。
    if start_date > month_start:
        ready = conn.execute(
            "SELECT 1 FROM exemption_months WHERE month = ? AND cause = ?", (month, cause)
        ).fetchone()
        if ready is None:
            start_date = month_start

    remaining = quota or 0
    if start_date > month_start:
        for row in conn.execute(
            "SELECT exempt FROM exemption_days WHERE date >= ? AND date < ? AND cause = ?",
            (month_start, start_date, cause),
        ):
            remaining -= to_mills(row["exempt"])
    remaining = max(0, remaining)

    in_range = [date for date in dates if start_date <= date < month_end]
    in_range_set = set(in_range)
    # 归因结果已删除/迁走的日期，免补合计也要一并清除。
    for row in conn.execute(
        "SELECT DISTINCT date FROM exemption_days WHERE date >= ? AND date < ? AND cause = ?",
        (start_date, month_end, cause),
    ):
        if row["date"] not in in_range_set:
            conn.execute(
                "DELETE FROM exemption_days WHERE date = ? AND cause = ?", (row["date"], cause)
            )

    now = datetime.now(timezone.utc).strftime(FMT)
    for date in in_range:
        turbines = _day_turbines(conn, date)
        slot_losses = _slot_losses(conn, date, cause)
        if not slot_losses:
            conn.execute(
                "DELETE FROM exemption_days WHERE date = ? AND cause = ?", (date, cause)
            )
            continue
        result = compute_day(turbines, slot_losses, remaining)
        remaining = result.remaining_after
        _write_exemption_day(conn, date, cause, turbines, result, now)

    conn.execute(
        "INSERT OR REPLACE INTO exemption_months (month, cause, computed_at) VALUES (?, ?, ?)",
        (month, cause, now),
    )


def _refresh_exemption_months(
    conn: sqlite3.Connection,
    months: Iterable[str],
    *,
    changed_dates: Optional[Iterable[str]] = None,
) -> None:
    """归因变化后重算受影响月份中、变化日及之后日期的免补合计。"""
    dates = sorted(set(changed_dates or ()))
    for month in sorted(set(months)):
        month_start, _ = month_bounds(month)
        month_dates = [date for date in dates if date[:7] == month]
        start_date = max(month_start, min(month_dates)) if month_dates else None
        for cause in _month_causes(conn, month):
            _recompute_exemption_range(conn, month, cause, start_date=start_date)


def backfill_exemptions(conn: sqlite3.Connection) -> None:
    """把旧库既有日报纳入免补核算（应用启动时调用）。

    只补算缺少日级免补合计的月份；已有合计的月份保持原样，
    后续归因变化走增量重算。
    """
    months = {
        row["date"][:7]
        for row in conn.execute("SELECT DISTINCT date FROM attribution_slots")
    }
    if not months:
        return
    done = {
        (row["month"], row["cause"])
        for row in conn.execute("SELECT month, cause FROM exemption_months")
    }
    with tx(conn):
        for month in sorted(months):
            for cause in _month_causes(conn, month):
                if (month, cause) in done:
                    continue
                _recompute_exemption_range(conn, month, cause, start_date=None)


def set_exemption_quota(
    conn: sqlite3.Connection, date: str, cause: str, amount
) -> dict:
    """设置某月某原因的全场免补额度，并重算该月日级免补合计。

    ``date`` 可为 ``YYYY-MM-DD``（取所在月）或 ``YYYY-MM``。
    ``amount`` 必须为非负数且最多 3 位小数（0.001 kWh）。
    """
    month = _month_of(date)
    cause = validate_token(cause, field="限发原因")
    try:
        value = to_decimal(amount)
    except ValueError as exc:
        raise ValidationError(f"免补额度无效：{exc}") from exc
    if value < 0:
        raise ValidationError("免补额度不能为负数")
    if value != value.quantize(QUANT):
        raise ValidationError("免补额度最多精确到 0.001 kWh（最多 3 位小数）")
    value = q(value)

    now = datetime.now(timezone.utc).strftime(FMT)
    with tx(conn):
        conn.execute(
            "INSERT INTO exemption_quotas (month, cause, amount, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(month, cause) DO UPDATE SET "
            "amount = excluded.amount, updated_at = excluded.updated_at",
            (month, cause, str(value), now),
        )
        _recompute_exemption_range(conn, month, cause, start_date=None)

    return {"month": month, "cause": cause, "amount": value}


def get_exemption_quota(
    conn: sqlite3.Connection, month: str, cause: str
) -> Optional[Decimal]:
    row = conn.execute(
        "SELECT amount FROM exemption_quotas WHERE month = ? AND cause = ?",
        (_month_of(month), cause),
    ).fetchone()
    return to_decimal(row["amount"]) if row else None


def list_exemption_quotas(
    conn: sqlite3.Connection, month: Optional[str] = None
) -> list[dict]:
    sql = "SELECT month, cause, amount, updated_at FROM exemption_quotas"
    params: list = []
    if month is not None:
        sql += " WHERE month = ?"
        params.append(_month_of(month))
    sql += " ORDER BY month, cause"
    return [
        {
            "month": row["month"],
            "cause": row["cause"],
            "amount": to_decimal(row["amount"]),
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(sql, params)
    ]


def list_causes(conn: sqlite3.Connection) -> list[str]:
    """单据与归因结果中出现过的原因，供页面下拉选择。"""
    causes = {
        row["cause"]
        for row in conn.execute("SELECT DISTINCT cause FROM orders ORDER BY cause")
    }
    causes |= {
        row["cause"]
        for row in conn.execute("SELECT DISTINCT cause FROM attribution_losses")
    }
    causes |= {
        row["cause"]
        for row in conn.execute("SELECT DISTINCT cause FROM exemption_quotas")
    }
    return sorted(causes)


def get_exemption_day(
    conn: sqlite3.Connection, date: str, cause: str
) -> dict:
    """核算某日某原因各风机的免补/可补电量。

    所选日读取逐格归因（按时刻累计）；月内更早日期只读已存的日级合计。
    只读查询，不写库。
    """
    with snapshot(conn):
        return _get_exemption_day(conn, date, cause)


def _get_exemption_day(conn: sqlite3.Connection, date: str, cause: str) -> dict:
    date = parse_date(date, field="日期")
    cause = validate_token(cause, field="限发原因")
    month = date[:7]
    month_start, _ = month_bounds(month)

    quota = _quota_mills(conn, month, cause)
    prior_exempt = 0
    prior_loss = 0
    for row in conn.execute(
        "SELECT loss, exempt FROM exemption_days WHERE date >= ? AND date < ? AND cause = ?",
        (month_start, date, cause),
    ):
        prior_loss += to_mills(row["loss"])
        prior_exempt += to_mills(row["exempt"])

    quota_amount = from_mills(quota or 0)
    remaining = max(0, (quota or 0) - prior_exempt)
    turbines = _day_turbines(conn, date)
    result = compute_day(turbines, _slot_losses(conn, date, cause), remaining)

    rows = [
        {
            "turbine": turbine,
            "loss": from_mills(result.loss.get(turbine, 0)),
            "exempt": from_mills(result.exempt.get(turbine, 0)),
            "compensable": from_mills(result.compensable.get(turbine, 0)),
        }
        for turbine in sorted(turbines)
    ]
    slots = [
        {
            "start_utc": item.start_utc,
            "loss": from_mills(item.loss_total),
            "exempt": from_mills(item.exempt_total),
            "remaining_after": from_mills(item.remaining_after),
        }
        for item in result.slots
    ]
    return {
        "date": date,
        "month": month,
        "cause": cause,
        "quota": quota_amount,
        "has_quota": quota is not None,
        "prior_loss": from_mills(prior_loss),
        "prior_exempt": from_mills(prior_exempt),
        "remaining_before": from_mills(remaining),
        "remaining_after": from_mills(result.remaining_after),
        "turbines": rows,
        "slots": slots,
        "total_loss": from_mills(sum(result.loss.values())),
        "total_exempt": from_mills(sum(result.exempt.values())),
        "total_compensable": from_mills(sum(result.compensable.values())),
    }


def get_turbine_exemptions(
    conn: sqlite3.Connection, turbine: str, date: str
) -> list[dict]:
    """某风机某日已存的原因级免补合计（日级展示用，不重算命）。"""
    with snapshot(conn):
        return [
            {
                "cause": row["cause"],
                "loss": to_decimal(row["loss"]),
                "exempt": to_decimal(row["exempt"]),
                "compensable": to_decimal(row["compensable"]),
                "computed_at": row["computed_at"],
            }
            for row in conn.execute(
                "SELECT cause, loss, exempt, compensable, computed_at FROM exemption_days "
                "WHERE turbine = ? AND date = ? ORDER BY cause",
                (turbine, date),
            )
        ]


def _refresh_conflicted_orders(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> tuple[list[str], set[str]]:
    """导入计量后复核与该区间重叠的单据；冲突单整单暂停（conflicted=1）。

    返回 ``(状态发生变化的单号, 仍处于冲突的单号)``。
    """
    changed: list[str] = []
    conflicted: set[str] = set()
    rows = conn.execute(
        "SELECT * FROM orders WHERE active = 1 AND start_utc < ? AND end_utc > ?",
        (end_utc, start_utc),
    ).fetchall()
    for row in rows:
        order = _row_to_order(row)
        hits = order_conflicts(conn, order)
        flag = 1 if hits else 0
        if flag:
            conflicted.add(order.order_no)
        if flag != row["conflicted"]:
            conn.execute(
                "UPDATE orders SET conflicted = ? WHERE order_no = ?",
                (flag, order.order_no),
            )
            changed.append(order.order_no)
    return changed, conflicted


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def import_meter(
    conn: sqlite3.Connection,
    turbine: str,
    rows: Sequence[MeterRow],
    *,
    replace: bool = False,
) -> ImportResult:
    """导入/更新计量行，并在同一事务内重算受影响日期。"""
    turbine = validate_token(turbine, field="风机编号")
    if not rows:
        raise ValidationError("没有可导入的数据行")

    seen: set[str] = set()
    for row in rows:
        if row.start_utc in seen:
            raise ValidationError(f"文件内时刻重复：{row.start_utc}")
        seen.add(row.start_utc)

    dates = sorted({row.start_utc[:10] for row in rows})
    inserted = updated = 0
    range_start = f"{dates[0]}T00:00:00Z"
    range_end = f"{next_date(dates[-1])}T00:00:00Z"

    with tx(conn):
        if replace:
            conn.execute(
                "DELETE FROM meters WHERE turbine = ? AND start_utc >= ? AND start_utc < ?",
                (turbine, range_start, range_end),
            )
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM meters WHERE turbine = ? AND start_utc = ?",
                (turbine, row.start_utc),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE meters SET available = ?, actual = ?, imported_at = "
                    "(strftime('%Y-%m-%dT%H:%M:%SZ','now')) WHERE id = ?",
                    (str(q(row.available)), str(q(row.actual)), existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO meters (turbine, start_utc, available, actual) VALUES (?, ?, ?, ?)",
                    (turbine, row.start_utc, str(q(row.available)), str(q(row.actual))),
                )
                inserted += 1

        changed_orders, still_conflicted = _refresh_conflicted_orders(
            conn, range_start, range_end
        )

        # 重算导入日期；若某单的暂停状态变化，它覆盖的日期也要重算（日期口径）。
        recompute = set(dates)
        for order_no in changed_orders:
            row = conn.execute(
                "SELECT start_utc, end_utc FROM orders WHERE order_no = ?", (order_no,)
            ).fetchone()
            recompute |= set(order_dates(row["start_utc"], row["end_utc"]))
        recompute_dates(conn, recompute)

        detail: dict[str, list[Conflict]] = {}
        for order_no in sorted(still_conflicted):
            row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
            detail[order_no] = order_conflicts(conn, _row_to_order(row))

    return ImportResult(
        turbine=turbine,
        inserted=inserted,
        updated=updated,
        dates=dates,
        warnings=[],
        order_conflicts=detail,
    )


# ---------------------------------------------------------------------------
# 单据提交 / 更正
# ---------------------------------------------------------------------------


def submit_order(conn: sqlite3.Connection, data: OrderInput) -> SubmitResult:
    """新建或整单更正限发单。

    * 任意覆盖格 ``实发量 > 上限`` → 抛 :class:`OrderConflict`，整单退回；
      事务回滚，原单据与归因结果均不变。
    * 成功 → 单据与归因结果同一事务生效；只重算新旧单覆盖的日期，
      重算时所有启用单（含其他重叠单据/更正）一并参与。
    """
    with tx(conn):
        existing = load_order(conn, data.order_no)
        if existing is not None:
            revision = existing.revision + 1
            corrected = True
            old_dates = set(order_dates(existing.start_utc, existing.end_utc))
        else:
            revision = 1
            corrected = False
            old_dates = set()

        candidate = Order(
            order_no=data.order_no,
            cause=data.cause,
            start_utc=data.start_utc,
            end_utc=data.end_utc,
            cap=data.cap,
            revision=revision,
        )

        conflicts = order_conflicts(conn, candidate)
        if conflicts:
            raise OrderConflict(data.order_no, conflicts)

        now = datetime.now(timezone.utc).strftime(FMT)
        cap_text = str(q(candidate.cap))
        if corrected:
            conn.execute(
                "UPDATE orders SET cause = ?, start_utc = ?, end_utc = ?, cap = ?, "
                "revision = ?, active = 1, conflicted = 0, updated_at = ? WHERE order_no = ?",
                (
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    candidate.order_no,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO orders (order_no, cause, start_utc, end_utc, cap, revision, "
                "active, conflicted, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    candidate.order_no,
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO order_revisions (order_no, revision, cause, start_utc, end_utc, "
            "cap, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                candidate.order_no,
                revision,
                candidate.cause,
                candidate.start_utc,
                candidate.end_utc,
                cap_text,
                now,
            ),
        )

        dates = set(order_dates(candidate.start_utc, candidate.end_utc)) | old_dates
        days = recompute_dates(conn, dates)

    return SubmitResult(
        order_no=candidate.order_no,
        revision=revision,
        corrected=corrected,
        dates=sorted(dates),
        days=sorted(days, key=lambda day: day.date),
    )


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------


def list_turbines(conn: sqlite3.Connection) -> list[str]:
    return [
        row["turbine"]
        for row in conn.execute("SELECT DISTINCT turbine FROM meters ORDER BY turbine")
    ]


def list_days(conn: sqlite3.Connection, turbine: str) -> list[dict]:
    """按日汇总：各原因损失、待查合计、免补/可补日级合计。"""
    with snapshot(conn):
        return _list_days(conn, turbine)


def _list_days(conn: sqlite3.Connection, turbine: str) -> list[dict]:
    date_rows = conn.execute(
        "SELECT DISTINCT date FROM attribution_slots WHERE turbine = ? ORDER BY date",
        (turbine,),
    ).fetchall()
    days: list[dict] = []
    for row in date_rows:
        date = row["date"]
        losses: dict[str, object] = {}
        for item in conn.execute(
            "SELECT cause, amount FROM attribution_losses WHERE turbine = ? AND date = ?",
            (turbine, date),
        ):
            losses[item["cause"]] = losses.get(item["cause"], q(0)) + to_decimal(item["amount"])
        losses = {cause: q(amount) for cause, amount in sorted(losses.items()) if q(amount) > 0}

        unsplit = q(0)
        curtailment = q(0)
        unsplit_count = 0
        slot_count = 0
        for slot in conn.execute(
            "SELECT available, actual, unsplit FROM attribution_slots WHERE turbine = ? AND date = ?",
            (turbine, date),
        ):
            unsplit += to_decimal(slot["unsplit"])
            curtailment += clamp_curtailment(
                to_decimal(slot["available"]), to_decimal(slot["actual"])
            )
            slot_count += 1
            if to_decimal(slot["unsplit"]) > 0:
                unsplit_count += 1

        exempt = q(0)
        compensable = q(0)
        exempt_causes: list[str] = []
        for item in conn.execute(
            "SELECT cause, exempt, compensable FROM exemption_days "
            "WHERE turbine = ? AND date = ? ORDER BY cause",
            (turbine, date),
        ):
            exempt += to_decimal(item["exempt"])
            compensable += to_decimal(item["compensable"])
            exempt_causes.append(item["cause"])

        days.append(
            {
                "date": date,
                "losses": losses,
                "unsplit": q(unsplit),
                "unsplit_count": unsplit_count,
                "curtailment": q(curtailment),
                "slot_count": slot_count,
                "exempt": q(exempt),
                "compensable": q(compensable),
                "exempt_causes": exempt_causes,
            }
        )
    return days


def get_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[dict]:
    with snapshot(conn):
        return _get_day(conn, turbine, date)


def _get_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[dict]:
    day_row = conn.execute(
        "SELECT * FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date)
    ).fetchone()
    if day_row is None:
        return None

    slot_rows = conn.execute(
        "SELECT start_utc, available, actual, unsplit FROM attribution_slots "
        "WHERE turbine = ? AND date = ? ORDER BY start_utc",
        (turbine, date),
    ).fetchall()
    if not slot_rows:
        return None

    losses_by_slot: dict[str, dict[str, object]] = {}
    for item in conn.execute(
        "SELECT start_utc, cause, amount FROM attribution_losses WHERE turbine = ? AND date = ?",
        (turbine, date),
    ):
        losses_by_slot.setdefault(item["start_utc"], {})[item["cause"]] = to_decimal(item["amount"])

    slots: list[dict] = []
    for row in slot_rows:
        available = to_decimal(row["available"])
        actual = to_decimal(row["actual"])
        losses = {
            cause: q(amount)
            for cause, amount in sorted(losses_by_slot.get(row["start_utc"], {}).items())
        }
        slots.append(
            {
                "start_utc": row["start_utc"],
                "available": q(available),
                "actual": q(actual),
                "curtailment": clamp_curtailment(available, actual),
                "losses": losses,
                "unsplit": q(to_decimal(row["unsplit"])),
            }
        )

    day_losses: dict[str, object] = {}
    for slot in slots:
        for cause, amount in slot["losses"].items():
            day_losses[cause] = day_losses.get(cause, q(0)) + amount
    day_losses = {cause: q(amount) for cause, amount in sorted(day_losses.items()) if q(amount) > 0}
    unsplit_slots = [slot for slot in slots if slot["unsplit"] > 0]
    unsplit_total = q(sum((slot["unsplit"] for slot in unsplit_slots), q(0)))
    curtailment = q(sum((slot["curtailment"] for slot in slots), q(0)))

    day_start, day_end = day_bounds(date)
    orders = [
        order
        for order in load_orders(conn, include_conflicted=True)
        if order.start_utc < day_end and order.end_utc > day_start
    ]
    paused: dict[str, list[Conflict]] = {}
    for order in orders:
        hits = order_conflicts(conn, order)
        if hits:
            paused[order.order_no] = hits

    return {
        "turbine": turbine,
        "date": date,
        "computed_at": day_row["computed_at"],
        "losses": day_losses,
        "unsplit": unsplit_total,
        "unsplit_slots": unsplit_slots,
        "slots": slots,
        "curtailment": curtailment,
        "orders": orders,
        "paused_orders": paused,
    }


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "order_no": row["order_no"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "revision": row["revision"],
            "active": bool(row["active"]),
            "conflicted": bool(row["conflicted"]),
            "updated_at": row["updated_at"],
        }
        for row in conn.execute("SELECT * FROM orders ORDER BY order_no")
    ]


def get_order(conn: sqlite3.Connection, order_no: str) -> Optional[dict]:
    current = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    if current is None:
        return None
    revisions = [
        {
            "revision": row["revision"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM order_revisions WHERE order_no = ? ORDER BY revision DESC",
            (order_no,),
        )
    ]
    order = _row_to_order(current)
    return {
        "order_no": current["order_no"],
        "cause": current["cause"],
        "start_utc": current["start_utc"],
        "end_utc": current["end_utc"],
        "cap": to_decimal(current["cap"]),
        "revision": current["revision"],
        "active": bool(current["active"]),
        "conflicted": bool(current["conflicted"]),
        "updated_at": current["updated_at"],
        "revisions": revisions,
        "conflicts": order_conflicts(conn, order),
    }
