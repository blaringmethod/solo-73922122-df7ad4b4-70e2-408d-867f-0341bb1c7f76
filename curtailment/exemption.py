"""免补额度核算（纯计算，不依赖 Flask/SQLite）。

规则摘要
--------
* 免补额度按 ``(月份, 限发原因)`` 设置，全场共用；每月从零累计。
* 按时刻升序消费额度：某一时刻把各风机该原因的已归因损失相加，
  未超过剩余额度则全部免补；超过则把剩余额度按各风机该格损失比例
  分摊（最大余数法补尾差，余数相同按风机编号升序）。
* 额度用尽后，后续时刻的损失全部计为可补。
* 所有金额以 0.001 kWh 为最小单位（毫单位整数）计算，避免浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from .allocation import MILL, q, to_decimal


def to_mills(value) -> int:
    """把电量转成整数毫单位（0.001 kWh）。"""
    return int(q(to_decimal(value)) * MILL)


def from_mills(value: int) -> Decimal:
    """整数毫单位转回电量，保留 3 位小数。"""
    return q(Decimal(int(value)) / MILL)


@dataclass
class SlotExemption:
    """某一时刻的免补分摊结果（毫单位）。"""

    start_utc: str
    loss: dict[str, int] = field(default_factory=dict)
    exempt: dict[str, int] = field(default_factory=dict)
    remaining_after: int = 0

    @property
    def loss_total(self) -> int:
        return sum(self.loss.values())

    @property
    def exempt_total(self) -> int:
        return sum(self.exempt.values())


@dataclass
class DayExemption:
    """某日按时刻累计后的免补结果（毫单位）。"""

    turbines: list[str] = field(default_factory=list)
    loss: dict[str, int] = field(default_factory=dict)
    exempt: dict[str, int] = field(default_factory=dict)
    compensable: dict[str, int] = field(default_factory=dict)
    remaining_after: int = 0
    slots: list[SlotExemption] = field(default_factory=list)


def proportional_split(weights: Mapping[str, int], total_mills: int) -> dict[str, int]:
    """按权重比例把 ``total_mills`` 分摊为整数毫单位。

    使用最大余数法：先取下整，再把尾差按余数从大到小逐个 +1；
    余数相同按风机编号升序。仅用于“剩余额度 < 该格损失总量”的场景，
    因此任何一个风机分到的免补不会超过它在该格的损失。

    ``weights`` 应已按风机编号升序传入（查询均按编号排序），
    保证余数相同时的补位顺序稳定。
    """
    result = {turbine: 0 for turbine in weights}
    positive = {turbine: int(weight) for turbine, weight in weights.items() if int(weight) > 0}
    if total_mills <= 0 or not positive:
        return result

    total_weight = sum(positive.values())
    if total_mills >= total_weight:
        # 额度足够覆盖全部损失（调用方通常已走整格免补分支）。
        for turbine, weight in positive.items():
            result[turbine] = weight
        return result

    remainders: list[tuple[int, str]] = []
    for turbine, weight in positive.items():
        numerator = total_mills * weight
        result[turbine] = numerator // total_weight
        remainders.append((-(numerator % total_weight), turbine))

    leftover = total_mills - sum(result.values())
    order = [turbine for _, turbine in sorted(remainders)]
    for index in range(leftover):
        result[order[index % len(order)]] += 1
    return result


def compute_day(
    turbines: list[str],
    slot_losses: Mapping[str, Mapping[str, int]],
    remaining_mills: int,
) -> DayExemption:
    """按时刻消费额度，返回一日各风机的免补/可补及逐格分摊。

    ``slot_losses`` 形如 ``{时刻: {风机: 该原因损失毫单位}}``。
    """
    ordered: list[str] = list(dict.fromkeys(turbines))
    for slot in slot_losses.values():
        for turbine in slot:
            if turbine not in ordered:
                ordered.append(turbine)

    loss = {turbine: 0 for turbine in ordered}
    exempt = {turbine: 0 for turbine in ordered}
    remaining = max(0, int(remaining_mills))
    slots: list[SlotExemption] = []

    for start_utc in sorted(slot_losses):
        slot = {
            turbine: int(amount)
            for turbine, amount in slot_losses[start_utc].items()
            if int(amount) > 0
        }
        for turbine, amount in slot.items():
            loss[turbine] += amount

        total = sum(slot.values())
        allocation: dict[str, int] = {}
        if remaining > 0 and total > 0:
            if total <= remaining:
                allocation = dict(slot)
                remaining -= total
            else:
                allocation = proportional_split(slot, remaining)
                remaining = 0

        for turbine, amount in allocation.items():
            exempt[turbine] += amount
        slots.append(
            SlotExemption(
                start_utc=start_utc,
                loss=slot,
                exempt=allocation,
                remaining_after=remaining,
            )
        )

    compensable = {
        turbine: loss[turbine] - exempt[turbine] for turbine in ordered
    }
    return DayExemption(
        turbines=ordered,
        loss=loss,
        exempt=exempt,
        compensable=compensable,
        remaining_after=remaining,
        slots=slots,
    )
