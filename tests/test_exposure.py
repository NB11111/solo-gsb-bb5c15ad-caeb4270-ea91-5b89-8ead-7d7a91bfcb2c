"""热暴露评估测试：精确积分、窗口裁剪、断档不清零、预算达成时刻、状态判定。

温度上限固定 -18°C：-16°C 时 f = max(T-上限, 0) = 2 ℃。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import review as rl
from app.main import app
from app.review import ReviewError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=CST)
client = TestClient(app)

PATH = "/api/v1/cold-storage/exposure"


def at(second: float) -> datetime:
    return T0 + timedelta(seconds=second)


def iso(second: float) -> str:
    return at(second).isoformat()


def run(readings, limit=-18.0, gap=120.0, window=(0.0, 120.0), budget=100.0):
    return rl.exposure(
        readings=[(at(t), temp) for t, temp in readings],
        limit=limit,
        max_sample_gap_seconds=gap,
        window=(at(window[0]), at(window[1])),
        budget_celsius_seconds=budget,
    )


def payload(**overrides):
    body = {
        "readings": [
            {"time": iso(0), "temperature": -16.0},
            {"time": iso(60), "temperature": -16.0},
            {"time": iso(120), "temperature": -16.0},
        ],
        "temperature_limit": -18.0,
        "max_sample_gap_seconds": 120,
        "storage_window": {"start": iso(0), "end": iso(120)},
        "exposure_budget_celsius_seconds": 100.0,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 算法层
# ---------------------------------------------------------------------------


def test_constant_over_temperature_exact_integral():
    # 恒温 -16°C（超上限 2℃），窗口 [0,120]：已知量 = 2 * 120 = 240 ℃·秒。
    result = run([(0, -16.0), (60, -16.0), (120, -16.0)], budget=100.0)
    assert result.known_exposure == pytest.approx(240.0)
    assert result.unknown_seconds == 0.0
    # 每秒累计 2 ℃·秒，达预算 100 需 50s：段内精确求解，不是下一个采样点 60。
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 50.0)
    assert result.status == "exceeded"


def test_linear_crossing_triangle_integral():
    # -20 -> -16 线性上升，t=50 处穿越上限；越限部分是底 50s、高 2℃ 的三角形 = 50 ℃·秒。
    result = run([(0, -20.0), (100, -16.0)], window=(0, 100), budget=12.5)
    assert result.known_exposure == pytest.approx(50.0)
    # 段内求解：累计 12.5 ℃·秒 -> t=75（不是下一个采样点 100）。
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 75.0)
    assert result.status == "exceeded"


def test_window_clips_segments():
    # 窗口 [30,90] 裁剪掉两端，已知量 = 2 * 60 = 120。
    result = run([(0, -16.0), (120, -16.0)], window=(30, 90), budget=1000.0)
    assert result.known_exposure == pytest.approx(120.0)
    assert result.unknown_seconds == 0.0
    assert result.budget_reached_at is None
    assert result.status == "within"


def test_gap_counts_unknown_but_does_not_reset():
    # 断档 (60,360)：未知 300s；累计不清零，预算 150 在第二段 t=375 处达成。
    result = run(
        [(0, -16.0), (60, -16.0), (360, -16.0), (420, -16.0)],
        window=(0, 420),
        budget=150.0,
    )
    assert result.known_exposure == pytest.approx(240.0)
    assert result.unknown_seconds == pytest.approx(300.0)
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 375.0)
    assert result.status == "exceeded"


def test_indeterminate_when_unknown_remains_and_budget_not_reached():
    result = run(
        [(0, -16.0), (60, -16.0), (360, -16.0), (420, -16.0)],
        window=(0, 420),
        budget=500.0,
    )
    assert result.known_exposure == pytest.approx(240.0)
    assert result.unknown_seconds == pytest.approx(300.0)
    assert result.budget_reached_at is None
    assert result.status == "indeterminate"


def test_within_when_no_unknown_and_under_budget():
    result = run([(0, -16.0), (120, -16.0)], budget=1000.0)
    assert result.known_exposure == pytest.approx(240.0)
    assert result.budget_reached_at is None
    assert result.status == "within"


def test_temperature_at_or_below_limit_contributes_zero():
    # 恰好等于上限不产生热暴露。
    result = run([(0, -18.0), (60, -18.0)], window=(0, 60), budget=1.0)
    assert result.known_exposure == 0.0
    assert result.status == "within"
    # 低于上限同样为零。
    result = run([(0, -19.0), (60, -20.0)], window=(0, 60), budget=1.0)
    assert result.known_exposure == 0.0


def test_gap_exactly_at_max_is_valid_segment():
    # 间隔恰好等于最大允许值：不算断档（与复盘接口同一规则）。
    result = run([(0, -16.0), (120, -16.0)], gap=120.0, budget=1.0)
    assert result.unknown_seconds == 0.0
    assert result.known_exposure == pytest.approx(240.0)


def test_window_covering_only_gap():
    # 窗口完全落在断档内：已知量为 0，全部计为未知。
    result = run([(0, -16.0), (300, -16.0)], window=(30, 90), budget=1.0)
    assert result.known_exposure == 0.0
    assert result.unknown_seconds == pytest.approx(60.0)
    assert result.status == "indeterminate"


def test_budget_reached_exactly_at_window_end():
    # 已知量恰好在窗口末尾达到预算：时刻为窗口终点，状态 exceeded。
    result = run([(0, -16.0), (120, -16.0)], budget=240.0)
    assert result.budget_reached_at == pytest.approx(T0.timestamp() + 120.0)
    assert result.status == "exceeded"


def test_invalid_window_rejected():
    with pytest.raises(ReviewError):
        run([(0, -16.0), (120, -16.0)], window=(90, 30))  # 空窗口
    with pytest.raises(ReviewError):
        run([(0, -16.0), (120, -16.0)], window=(-10, 60))  # 起点早于首读数
    with pytest.raises(ReviewError):
        run([(0, -16.0), (120, -16.0)], window=(60, 130))  # 终点晚于末读数


def test_non_positive_budget_rejected():
    with pytest.raises(ReviewError):
        run([(0, -16.0), (120, -16.0)], budget=0.0)
    with pytest.raises(ReviewError):
        run([(0, -16.0), (120, -16.0)], budget=-1.0)


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_exposure_end_to_end():
    resp = client.post(PATH, json=payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exceeded"
    assert data["known_exposure_celsius_seconds"] == 240.0
    assert data["unknown_seconds_in_window"] == 0.0
    # 10:00:50+08:00 -> 02:00:50Z，段内精确求解而非采样点。
    assert data["budget_reached_at"].startswith("2026-09-25T02:00:50")
    assert data["budget_reached_at"].endswith("Z")


def test_exposure_gap_does_not_reset_cumulative():
    resp = client.post(
        PATH,
        json=payload(
            readings=[
                {"time": iso(0), "temperature": -16.0},
                {"time": iso(60), "temperature": -16.0},
                {"time": iso(360), "temperature": -16.0},
                {"time": iso(420), "temperature": -16.0},
            ],
            storage_window={"start": iso(0), "end": iso(420)},
            exposure_budget_celsius_seconds=150.0,
        ),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exceeded"
    assert data["known_exposure_celsius_seconds"] == 240.0
    assert data["unknown_seconds_in_window"] == 300.0
    # 第一段累计 120，剩余 30 在第二段 15s 处达成：10:06:15+08:00 -> 02:06:15Z。
    assert data["budget_reached_at"].startswith("2026-09-25T02:06:15")


def test_exposure_indeterminate():
    resp = client.post(
        PATH,
        json=payload(
            readings=[
                {"time": iso(0), "temperature": -16.0},
                {"time": iso(60), "temperature": -16.0},
                {"time": iso(360), "temperature": -16.0},
                {"time": iso(420), "temperature": -16.0},
            ],
            storage_window={"start": iso(0), "end": iso(420)},
            exposure_budget_celsius_seconds=500.0,
        ),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "indeterminate"
    assert data["budget_reached_at"] is None


def test_exposure_within():
    resp = client.post(PATH, json=payload(exposure_budget_celsius_seconds=1000.0))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "within"
    assert data["budget_reached_at"] is None


def test_empty_window_rejected_422():
    resp = client.post(
        PATH, json=payload(storage_window={"start": iso(60), "end": iso(60)})
    )
    assert resp.status_code == 422
    resp = client.post(
        PATH, json=payload(storage_window={"start": iso(60), "end": iso(30)})
    )
    assert resp.status_code == 422


def test_window_outside_readings_rejected_422():
    resp = client.post(
        PATH, json=payload(storage_window={"start": iso(0), "end": iso(180)})
    )
    assert resp.status_code == 422
    assert "首末读数" in resp.text
    resp = client.post(
        PATH, json=payload(storage_window={"start": iso(-60), "end": iso(120)})
    )
    assert resp.status_code == 422


def test_naive_window_rejected_422():
    resp = client.post(
        PATH,
        json=payload(
            storage_window={"start": "2026-09-25T10:00:00", "end": iso(120)}
        ),
    )
    assert resp.status_code == 422


def test_non_positive_budget_rejected_422():
    resp = client.post(PATH, json=payload(exposure_budget_celsius_seconds=0))
    assert resp.status_code == 422
    resp = client.post(PATH, json=payload(exposure_budget_celsius_seconds=-5))
    assert resp.status_code == 422


def test_reading_validation_reused_422():
    # 非递增读数 -> 422（与复盘接口同一套校验）。
    resp = client.post(
        PATH,
        json=payload(
            readings=[
                {"time": iso(60), "temperature": -16.0},
                {"time": iso(0), "temperature": -16.0},
            ]
        ),
    )
    assert resp.status_code == 422
    # 缺时区 -> 422。
    body = payload()
    body["readings"][0]["time"] = "2026-09-25T10:00:00"
    resp = client.post(PATH, json=body)
    assert resp.status_code == 422


def test_alarm_endpoint_still_works():
    # 原报警接口保持不变。
    resp = client.post(
        "/api/v1/cold-storage/alarms/review",
        json={
            "readings": [
                {"time": iso(0), "temperature": -17.0},
                {"time": iso(60), "temperature": -17.0},
            ],
            "temperature_limit": -18.0,
            "max_sample_gap_seconds": 120,
            "min_alarm_seconds": 30,
            "maintenance_windows": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["alarm_count"] == 1
