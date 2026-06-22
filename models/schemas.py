from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class StockInfo:
    symbol: str
    code: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class IndicatorPoint:
    date: str
    value: float | None
    ma_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "value": self.value,
            "ma_value": self.ma_value,
        }


@dataclass
class IndicatorRequest:
    action: str
    indicator: str
    symbol: str
    start_date: str
    end_date: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndicatorRequest":
        return cls(
            action=data.get("action", ""),
            indicator=data.get("indicator", ""),
            symbol=data.get("symbol", ""),
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
        )


def indicator_response(
    indicator: str, symbol: str, data: list[IndicatorPoint]
) -> dict[str, Any]:
    return {
        "action": "indicator_data",
        "indicator": indicator,
        "symbol": symbol,
        "data": [p.to_dict() for p in data],
    }


def error_response(message: str) -> dict[str, str]:
    return {"action": "error", "message": message}
