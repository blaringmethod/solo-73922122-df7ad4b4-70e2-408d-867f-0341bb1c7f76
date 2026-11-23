"""免补额度核算单元测试（纯计算）。"""

from decimal import Decimal

from curtailment.exemption import (
    compute_day,
    from_mills,
    proportional_split,
    to_mills,
)


def test_to_from_mills_roundtrip():
    assert to_mills("1.234") == 1234
    assert to_mills(Decimal("0.001")) == 1
    assert from_mills(1234) == Decimal("1.234")


def test_quota_covers_all_losses():
    result = compute_day(
        ["WT-2", "WT-1"],
        {
            "2026-01-01T00:00:00Z": {"WT-1": 6000, "WT-2": 4000},
            "2026-01-01T00:15:00Z": {"WT-1": 6000},
        },
        20000,
    )
    assert result.exempt == {"WT-2": 4000, "WT-1": 12000}
    assert result.compensable == {"WT-2": 0, "WT-1": 0}
    assert result.remaining_after == 4000


def test_quota_exhausted_mid_slot_and_compensable_after():
    # 第一格损失 10+10=20，额度 15 -> 按 1:1 分 7.5 / 7.5；第二格全部可补。
    result = compute_day(
        ["WT-1", "WT-2"],
        {
            "2026-01-01T00:00:00Z": {"WT-1": 10000, "WT-2": 10000},
            "2026-01-01T00:15:00Z": {"WT-1": 3000, "WT-2": 3000},
        },
        15000,
    )
    assert result.exempt == {"WT-1": 7500, "WT-2": 7500}
    assert result.compensable == {"WT-1": 5500, "WT-2": 5500}
    assert result.remaining_after == 0
    assert result.slots[1].exempt == {}


def test_earlier_slots_consume_quota_before_later():
    result = compute_day(
        ["WT-1"],
        {
            "2026-01-01T00:15:00Z": {"WT-1": 6000},
            "2026-01-01T00:00:00Z": {"WT-1": 6000},
        },
        10000,
    )
    assert [slot.start_utc for slot in result.slots] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:15:00Z",
    ]
    assert result.slots[0].exempt == {"WT-1": 6000}
    assert result.slots[1].exempt == {"WT-1": 4000}
    assert result.compensable == {"WT-1": 2000}


def test_proportional_split_largest_remainder_and_tiebreak():
    # 剩余 2 毫单位，权重 1:1:1 -> 余数相同，按风机编号升序补给 WT-1、WT-2。
    shares = proportional_split({"WT-3": 5, "WT-1": 5, "WT-2": 5}, 2)
    assert shares == {"WT-1": 1, "WT-2": 1, "WT-3": 0}


def test_proportional_split_larger_remainder_wins():
    # 剩余 100，权重 1:2:3：精确 16.67/33.33/50，余数最大的是 WT-1 与 WT-2？
    # 100*1/6=16.667 -> 余 4/6；100*2/6=33.333 -> 余 2/6；100*3/6=50 整。
    # 下整 16+33+50=99，尾差 1 给余数最大的 WT-1。
    shares = proportional_split({"WT-1": 100, "WT-2": 200, "WT-3": 300}, 100)
    assert shares == {"WT-1": 17, "WT-2": 33, "WT-3": 50}
    assert sum(shares.values()) == 100


def test_proportional_split_never_exceeds_weight():
    shares = proportional_split({"WT-1": 1, "WT-2": 1000}, 999)
    assert shares["WT-1"] <= 1
    assert shares["WT-2"] <= 1000
    assert sum(shares.values()) == 999


def test_zero_quota_all_compensable():
    result = compute_day(
        ["WT-1"], {"2026-01-01T00:00:00Z": {"WT-1": 5000}}, 0
    )
    assert result.exempt == {"WT-1": 0}
    assert result.compensable == {"WT-1": 5000}


def test_no_loss_slots_keep_remaining():
    result = compute_day(["WT-1"], {}, 5000)
    assert result.turbines == ["WT-1"]
    assert result.remaining_after == 5000
