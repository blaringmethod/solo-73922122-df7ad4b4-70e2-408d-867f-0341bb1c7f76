"""免补额度：服务层、重算链路与网页流程测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from curtailment import service
from curtailment.app import create_app
from curtailment.db import connect, init_db
from curtailment.parsing import MeterRow


def make_rows(spec: list[tuple[str, str, str]]) -> list[MeterRow]:
    return [
        MeterRow(start_utc=start, available=Decimal(a), actual=Decimal(act), line=i + 2)
        for i, (start, a, act) in enumerate(spec)
    ]


def seed_day(conn, day="2026-01-01", turbine="WT-1", slots=8, available="10", actual="4"):
    rows = [
        (f"{day}T{i // 4:02d}:{(i % 4) * 15:02d}:00Z", available, actual)
        for i in range(slots)
    ]
    service.import_meter(conn, turbine, make_rows(rows))


def seed_order(conn, no="O1", cause="调度", start="2026-01-01T00:00:00Z", end="2026-01-01T02:00:00Z", cap="4"):
    return service.submit_order(
        conn,
        service.OrderInput(
            order_no=no, cause=cause, start_utc=start, end_utc=end, cap=Decimal(cap)
        ),
    )


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "web.sqlite3"
    app = create_app({"DATABASE": str(db), "TESTING": True, "SECRET_KEY": "test"})
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 额度设置与校验
# ---------------------------------------------------------------------------


def test_quota_rejects_negative_and_excess_precision(conn):
    with pytest.raises(service.ValidationError):
        service.set_exemption_quota(conn, "2026-01-01", "调度", "-1")
    with pytest.raises(service.ValidationError):
        service.set_exemption_quota(conn, "2026-01-01", "调度", "1.2345")
    with pytest.raises(service.ValidationError):
        service.set_exemption_quota(conn, "2026-01-01", "调度", "abc")


def test_quota_accepts_three_decimals(conn):
    result = service.set_exemption_quota(conn, "2026-01-01", "调度", "12.345")
    assert result == {"month": "2026-01", "cause": "调度", "amount": Decimal("12.345")}
    assert service.get_exemption_quota(conn, "2026-01", "调度") == Decimal("12.345")


def test_invalid_quota_keeps_previous_value_and_totals(conn):
    seed_day(conn, slots=4)
    seed_order(conn, end="2026-01-01T01:00:00Z")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    before = service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")
    with pytest.raises(service.ValidationError):
        service.set_exemption_quota(conn, "2026-01", "调度", "-3")
    assert service.get_exemption_quota(conn, "2026-01", "调度") == Decimal("10.000")
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-01") == before


# ---------------------------------------------------------------------------
# 核算
# ---------------------------------------------------------------------------


def test_day_report_without_prior_days(conn):
    seed_day(conn)
    seed_order(conn)
    service.set_exemption_quota(conn, "2026-01-01", "调度", "20")
    report = service.get_exemption_day(conn, "2026-01-01", "调度")
    assert report["total_loss"] == Decimal("48.000")
    assert report["remaining_before"] == Decimal("20.000")
    # 首格损失 6，剩余 14；第二格 6，剩余 8；第三格 6，剩余 2；第四格只免 2。
    assert report["total_exempt"] == Decimal("20.000")
    assert report["total_compensable"] == Decimal("28.000")
    assert report["remaining_after"] == Decimal("0.000")


def test_prior_days_consume_quota_via_stored_totals(conn):
    seed_day(conn, day="2026-01-01")
    seed_day(conn, day="2026-01-02")
    seed_order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01-01", "调度", "50")
    day1 = service.get_exemption_day(conn, "2026-01-01", "调度")
    day2 = service.get_exemption_day(conn, "2026-01-02", "调度")
    assert day1["total_exempt"] == Decimal("48.000")
    assert day2["prior_exempt"] == Decimal("48.000")
    assert day2["remaining_before"] == Decimal("2.000")
    assert day2["total_exempt"] == Decimal("2.000")
    assert day2["total_compensable"] == Decimal("46.000")
    # 查询是只读的，不落库。
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]["exempt"] == Decimal(
        "2.000"
    )


def test_proportional_split_across_turbines_in_same_slot(conn):
    # 两台风机同格各损失 6，额度 9 -> 各 4.5。
    for turbine in ("WT-1", "WT-2"):
        seed_day(conn, slots=4, turbine=turbine)
    seed_order(conn, end="2026-01-01T01:00:00Z")
    service.set_exemption_quota(conn, "2026-01", "调度", "9")
    report = service.get_exemption_day(conn, "2026-01-01", "调度")
    assert {(row["turbine"], row["exempt"]) for row in report["turbines"]} == {
        ("WT-1", Decimal("4.500")),
        ("WT-2", Decimal("4.500")),
    }
    assert report["remaining_after"] == Decimal("0.000")
    # 日级合计按风机落库。
    assert service.get_turbine_exemptions(conn, "WT-2", "2026-01-01")[0]["compensable"] == Decimal(
        "19.500"
    )


def test_split_tiebreak_goes_to_lower_turbine_number(conn):
    # 四台风机同格各 1，额度 2 毫 kWh：余数相同，按编号升序给 WT-1、WT-2。
    for turbine in ("WT-1", "WT-2", "WT-3", "WT-4"):
        seed_day(conn, slots=4, turbine=turbine)
    seed_order(conn, end="2026-01-01T01:00:00Z")
    service.set_exemption_quota(conn, "2026-01", "调度", "0.002")
    report = service.get_exemption_day(conn, "2026-01-01", "调度")
    exempt = {row["turbine"]: row["exempt"] for row in report["turbines"]}
    assert exempt == {
        "WT-1": Decimal("0.001"),
        "WT-2": Decimal("0.001"),
        "WT-3": Decimal("0.000"),
        "WT-4": Decimal("0.000"),
    }
    assert report["total_exempt"] == Decimal("0.002")


def test_reimport_changes_later_days_accumulation(conn):
    seed_day(conn, day="2026-01-01")
    seed_day(conn, day="2026-01-02")
    seed_order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01-01", "调度", "50")
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]["exempt"] == Decimal(
        "2.000"
    )
    # 补传首日计量：首日损失降到 24，次日可用额度随之变为 26。
    service.import_meter(
        conn,
        "WT-1",
        make_rows(
            [
                ("2026-01-01T00:00:00Z", "7", "4"),
                ("2026-01-01T00:15:00Z", "7", "4"),
                ("2026-01-01T00:30:00Z", "7", "4"),
                ("2026-01-01T00:45:00Z", "7", "4"),
            ]
        ),
        replace=True,
    )
    day1 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")[0]
    day2 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]
    assert day1["loss"] == Decimal("12.000")
    assert day1["exempt"] == Decimal("12.000")
    # 首日用 12，剩 38；次日损失 48，免补 38、可补 10。
    assert day2["exempt"] == Decimal("38.000")
    assert day2["compensable"] == Decimal("10.000")


def test_order_correction_restates_following_days(conn):
    seed_day(conn, day="2026-01-01")
    seed_day(conn, day="2026-01-02")
    seed_order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "50")
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]["exempt"] == Decimal(
        "2.000"
    )
    # 更正单据上限到 8：每格损失变 2，首日 16，次日可用 34。
    seed_order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="8")
    day1 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")[0]
    day2 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]
    assert day1["loss"] == Decimal("16.000")
    assert day2["exempt"] == Decimal("16.000")
    assert day2["compensable"] == Decimal("0.000")


def test_quota_change_recomputes_whole_month(conn):
    seed_day(conn, day="2026-01-01")
    seed_day(conn, day="2026-01-02")
    seed_order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]["loss"] == Decimal(
        "48.000"
    )
    # 首日已用尽 10，次日应全部可补。
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]["exempt"] == Decimal(
        "0.000"
    )
    service.set_exemption_quota(conn, "2026-01", "调度", "100")
    day1 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")[0]
    day2 = service.get_turbine_exemptions(conn, "WT-1", "2026-01-02")[0]
    assert day1["exempt"] == Decimal("48.000")
    assert day2["exempt"] == Decimal("48.000")
    assert day2["compensable"] == Decimal("0.000")


def test_quota_is_per_cause(conn):
    seed_day(conn, slots=4)
    seed_order(conn, no="A", cause="调度", end="2026-01-01T01:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    service.set_exemption_quota(conn, "2026-01", "检修", "100")
    rows = {
        row["cause"]: row
        for row in service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")
    }
    assert rows["调度"]["exempt"] == Decimal("10.000")
    # 该日没有“检修”损失，不落免补行；查询时按 0 处理。
    assert "检修" not in rows
    report = service.get_exemption_day(conn, "2026-01-01", "检修")
    assert report["total_loss"] == Decimal("0.000")
    assert report["total_exempt"] == Decimal("0.000")


def test_month_boundary_resets_quota(conn):
    seed_day(conn, day="2026-01-31")
    seed_day(conn, day="2026-02-01")
    seed_order(conn, start="2026-01-31T00:00:00Z", end="2026-02-02T00:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    service.set_exemption_quota(conn, "2026-02", "调度", "10")
    jan = service.get_exemption_day(conn, "2026-01-31", "调度")
    feb = service.get_exemption_day(conn, "2026-02-01", "调度")
    assert jan["prior_exempt"] == Decimal("0.000")
    assert jan["total_exempt"] == Decimal("10.000")
    # 跨月从零累计，不继承 1 月余额或消耗。
    assert feb["prior_exempt"] == Decimal("0.000")
    assert feb["remaining_before"] == Decimal("10.000")
    assert feb["total_exempt"] == Decimal("10.000")


def test_exempt_never_exceeds_loss_and_totals_balance(conn):
    for turbine in ("WT-1", "WT-2", "WT-3"):
        seed_day(conn, slots=4, turbine=turbine, available="10", actual="4")
    seed_order(conn, end="2026-01-01T01:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "7.777")
    report = service.get_exemption_day(conn, "2026-01-01", "调度")
    assert sum(row["exempt"] for row in report["turbines"]) == Decimal("7.777")
    for row in report["turbines"]:
        assert row["exempt"] <= row["loss"]
        assert row["exempt"] + row["compensable"] == row["loss"]
    assert report["total_loss"] == Decimal("72.000")
    assert report["total_exempt"] + report["total_compensable"] == report["total_loss"]


def test_failed_order_correction_keeps_exemption_totals(conn, tmp_path):
    seed_day(conn, slots=4)
    seed_order(conn, end="2026-01-01T01:00:00Z", cap="4")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    before = service.get_turbine_exemptions(conn, "WT-1", "2026-01-01")
    read_before = service.get_exemption_day(conn, "2026-01-01", "调度")
    with pytest.raises(service.OrderConflict):
        seed_order(conn, end="2026-01-01T01:00:00Z", cap="2")
    assert service.get_turbine_exemptions(conn, "WT-1", "2026-01-01") == before
    after = service.get_exemption_day(conn, "2026-01-01", "调度")
    assert after["total_exempt"] == read_before["total_exempt"]
    assert after["turbines"] == read_before["turbines"]


def test_day_report_reads_committed_state_only(conn, tmp_path):
    seed_day(conn, slots=4)
    seed_order(conn, end="2026-01-01T01:00:00Z")
    service.set_exemption_quota(conn, "2026-01", "调度", "10")
    before = service.get_exemption_day(conn, "2026-01-01", "调度")
    # 另一个连接开启写事务并改动数据但尚未提交：查询应读到已提交状态。
    writer = connect(tmp_path / "test.sqlite3")
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM attribution_losses")
        writer.execute("DELETE FROM exemption_days")
        during = service.get_exemption_day(conn, "2026-01-01", "调度")
        assert during["total_exempt"] == before["total_exempt"]
        assert during["turbines"] == before["turbines"]
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    after = service.get_exemption_day(conn, "2026-01-01", "调度")
    assert after["total_exempt"] == before["total_exempt"]


def test_backfill_old_database_days(client):
    # 直接往库里写只有归因、没有免补表的“旧库”当天数据。
    path = client.application.config["DATABASE"]
    old = connect(path)
    try:
        old.execute(
            "INSERT INTO attribution_days (turbine, date, computed_at) VALUES ('WT-1','2026-02-01','x')"
        )
        old.execute(
            "INSERT INTO attribution_slots (turbine, start_utc, date, available, actual, unsplit) "
            "VALUES ('WT-1','2026-02-01T00:00:00Z','2026-02-01','10','4','0')"
        )
        old.execute(
            "INSERT INTO attribution_losses (turbine, start_utc, date, cause, amount) "
            "VALUES ('WT-1','2026-02-01T00:00:00Z','2026-02-01','调度','6')"
        )
    finally:
        old.close()

    backfilled = create_app({"DATABASE": path})
    conn = connect(backfilled.config["DATABASE"])
    try:
        rows = service.get_turbine_exemptions(conn, "WT-1", "2026-02-01")
    finally:
        conn.close()
    assert rows and rows[0]["cause"] == "调度"
    assert rows[0]["exempt"] == Decimal("0.000")


# ---------------------------------------------------------------------------
# 网页
# ---------------------------------------------------------------------------


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def upload(client, content: str, turbine="WT-1"):
    import io

    return client.post(
        "/import",
        data={
            "csrf": csrf(client),
            "turbine": turbine,
            "file": (io.BytesIO(content.encode("utf-8")), "meter.csv"),
        },
        content_type="multipart/form-data",
    )


def test_web_exemption_page_and_quota_form(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    client.post(
        "/orders",
        data={
            "csrf": token,
            "order_no": "CUR-1",
            "cause": "调度",
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T04:00:00Z",
            "cap": "4",
        },
    )
    response = client.post(
        "/exemption/quota",
        data={
            "csrf": token,
            "date": "2026-01-01",
            "cause": "调度",
            "amount": "10",
        },
    )
    assert response.status_code == 302

    page = client.get(
        "/exemption", query_string={"date": "2026-01-01", "cause": "调度"}
    ).get_data(as_text=True)
    assert "10.000" in page and "14.000" in page and "WT-1" in page

    index = client.get("/?turbine=WT-1").get_data(as_text=True)
    assert "免补" in index and "可补" in index

    day = client.get("/day/WT-1/2026-01-01").get_data(as_text=True)
    assert "免补 / 可补" in day


def test_web_invalid_date_shows_error(client):
    page = client.get(
        "/exemption", query_string={"date": "2026-13-40", "cause": "调度"}
    ).get_data(as_text=True)
    assert "格式应为 YYYY-MM-DD" in page


def test_web_quota_negative_rejected(client):
    token = csrf(client)
    response = client.post(
        "/exemption/quota",
        data={"csrf": token, "date": "2026-01-01", "cause": "调度", "amount": "-5"},
        follow_redirects=True,
    )
    assert "不能为负数" in response.get_data(as_text=True)
