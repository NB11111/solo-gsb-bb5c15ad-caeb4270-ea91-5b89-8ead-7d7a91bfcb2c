"""请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class TemperatureReading(BaseModel):
    """一条带时区的温度读数。"""

    time: datetime = Field(..., description="读数时间，必须带时区（如 2026-09-25T10:00:00+08:00）")
    temperature: float = Field(..., description="冷库温度（°C）")


class TimePeriod(BaseModel):
    """一个半开时间区间 [start, end)，维护豁免时段用。"""

    start: datetime = Field(..., description="开始时间（含），必须带时区")
    end: datetime = Field(..., description="结束时间（不含），必须带时区，且严格晚于开始时间")

    @model_validator(mode="after")
    def _check_order(self) -> "TimePeriod":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start 与 end 都必须带时区")
        if self.end <= self.start:
            raise ValueError("end 必须严格晚于 start")
        return self


class AlarmReviewRequest(BaseModel):
    """复盘请求。"""

    readings: list[TemperatureReading] = Field(
        ..., min_length=2, description="同一冷库的温度读数，时间必须严格递增"
    )
    temperature_limit: float = Field(..., description="温度上限（°C），严格大于才算越限")
    max_sample_gap_seconds: float = Field(
        ..., gt=0, description="允许的最大相邻采样间隔（秒），超过即视为采样断档"
    )
    min_alarm_seconds: float = Field(
        ..., ge=0, description="最短报警时长（秒），扣减豁免后达到该时长才报警"
    )
    maintenance_windows: list[TimePeriod] = Field(
        default_factory=list, description="维护豁免时段，落在其中的越限时间不计入报警"
    )

    @field_validator("readings")
    @classmethod
    def _check_strictly_increasing(cls, value: list[TemperatureReading]):
        if not value:
            return value
        for item in value:
            if item.time.tzinfo is None:
                raise ValueError("每条读数的 time 都必须带时区")
        prev = value[0].time
        for item in value[1:]:
            if item.time <= prev:
                raise ValueError("读数时间必须严格递增")
            prev = item.time
        return value

    @field_validator("temperature_limit")
    @classmethod
    def _check_limit_finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("温度上限必须是有限数值")
        return value


class IntervalOut(BaseModel):
    start: datetime = Field(..., description="区间开始时间（含），UTC")
    end: datetime = Field(..., description="区间结束时间（不含），UTC")
    duration_seconds: float = Field(..., description="持续时长（秒）")


class AlarmReviewResponse(BaseModel):
    """复盘响应。"""

    alarms: list[IntervalOut] = Field(
        default_factory=list,
        description="报警区间：扣减维护豁免后、时长达到最短报警时长的连续越限区间",
    )
    exceed_intervals: list[IntervalOut] = Field(
        default_factory=list, description="扣减豁免前的全部连续越限区间（仅作复盘参考）"
    )
    unknown_intervals: list[IntervalOut] = Field(
        default_factory=list, description="采样断档区间，状态未知，越限区间不会跨越这些区间"
    )
    alarm_count: int
    unknown_count: int
