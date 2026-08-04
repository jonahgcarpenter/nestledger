"""Strict parsing for editable IRA strategy CSV imports."""

from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path


MAX_FILE_SIZE = 256 * 1024
MAX_HOLDINGS = 500
MAX_CATEGORIES = 100
REQUIRED_HEADERS = ("Strategy", "Category", "Asset", "Ticker", "Allocation")


class StrategyImportError(Exception):
    """The supplied strategy CSV is invalid."""


@dataclass(frozen=True)
class ParsedHolding:
    asset: str
    ticker: str
    allocation_bps: int


@dataclass(frozen=True)
class ParsedCategory:
    name: str
    holdings: tuple[ParsedHolding, ...]


@dataclass(frozen=True)
class ParsedStrategy:
    name: str
    categories: tuple[ParsedCategory, ...]


def _validate_text(value: str) -> None:
    for character in value:
        if unicodedata.category(character) == "Cc" and character not in "\t\r\n":
            raise StrategyImportError("CSV contains a NUL or control character")


def _parse_allocation(value: str, row_number: int) -> int:
    allocation_text = value.strip()
    if allocation_text.endswith("%"):
        allocation_text = allocation_text[:-1].strip()
    if not allocation_text:
        raise StrategyImportError(f"Allocation is required on row {row_number}")
    try:
        allocation = Decimal(allocation_text)
    except DecimalException as exc:
        raise StrategyImportError(
            f"Invalid allocation on row {row_number}: {value!r}"
        ) from exc
    if not allocation.is_finite():
        raise StrategyImportError(f"Allocation must be finite on row {row_number}")
    if allocation < 0:
        raise StrategyImportError(f"Allocation cannot be negative on row {row_number}")
    if allocation.as_tuple().exponent < -2:
        raise StrategyImportError(
            f"Allocation may have at most two decimal places on row {row_number}"
        )
    if allocation > 100:
        raise StrategyImportError(f"Allocation cannot exceed 100 on row {row_number}")
    try:
        return int(allocation * 100)
    except DecimalException as exc:
        raise StrategyImportError(f"Invalid allocation on row {row_number}") from exc


def parse_strategy_csv(data: bytes, source_filename: str) -> ParsedStrategy:
    """Validate and parse one strategy CSV without modifying its source name."""
    if not isinstance(data, bytes):
        raise StrategyImportError("Strategy CSV content must be bytes")
    if len(data) > MAX_FILE_SIZE:
        raise StrategyImportError("Strategy CSV exceeds the 256 KiB size limit")
    if Path(source_filename).suffix.lower() != ".csv":
        raise StrategyImportError("Strategy filename must have a .csv extension")

    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise StrategyImportError("Strategy CSV must be valid UTF-8") from exc
    _validate_text(text)

    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows, None)
        if header is None:
            raise StrategyImportError("Strategy CSV is empty")
        if len(header) != len(set(header)):
            raise StrategyImportError("Strategy CSV contains duplicate headers")
        missing = [name for name in REQUIRED_HEADERS if name not in header]
        extra = [name for name in header if name not in REQUIRED_HEADERS]
        if missing or extra or len(header) != len(REQUIRED_HEADERS):
            details = []
            if missing:
                details.append(f"missing headers: {', '.join(missing)}")
            if extra:
                details.append(f"extra headers: {', '.join(extra)}")
            raise StrategyImportError("Invalid strategy headers (" + "; ".join(details) + ")")

        indexes = {name: header.index(name) for name in REQUIRED_HEADERS}
        strategy_name: str | None = None
        category_order: list[str] = []
        categories: dict[str, tuple[str, list[ParsedHolding]]] = {}
        holding_count = 0
        total_bps = 0

        for row_number, row in enumerate(rows, start=2):
            if len(row) != len(REQUIRED_HEADERS):
                raise StrategyImportError(
                    f"Row {row_number} has {len(row)} columns; expected {len(REQUIRED_HEADERS)}"
                )

            category = row[indexes["Category"]].strip()
            if category.casefold() == "total":
                continue

            name = row[indexes["Strategy"]].strip()
            asset = row[indexes["Asset"]].strip()
            ticker = row[indexes["Ticker"]].strip()
            allocation_value = row[indexes["Allocation"]]

            if not name:
                raise StrategyImportError(f"Strategy is required on row {row_number}")
            if not category:
                raise StrategyImportError(f"Category is required on row {row_number}")
            if not asset:
                raise StrategyImportError(f"Asset is required on row {row_number}")
            for field_name, value, limit in (
                ("Strategy", name, 120),
                ("Category", category, 120),
                ("Asset", asset, 120),
                ("Ticker", ticker, 32),
            ):
                if len(value) > limit:
                    raise StrategyImportError(
                        f"{field_name} exceeds {limit} characters on row {row_number}"
                    )

            if strategy_name is None:
                strategy_name = name
            elif name != strategy_name:
                raise StrategyImportError(
                    f"Strategy name does not match earlier rows on row {row_number}"
                )

            allocation_bps = _parse_allocation(allocation_value, row_number)
            holding_count += 1
            if holding_count > MAX_HOLDINGS:
                raise StrategyImportError(
                    f"Strategy may contain at most {MAX_HOLDINGS} holdings"
                )

            category_key = category.casefold()
            if category_key not in categories:
                if len(categories) >= MAX_CATEGORIES:
                    raise StrategyImportError(
                        f"Strategy may contain at most {MAX_CATEGORIES} categories"
                    )
                category_order.append(category_key)
                categories[category_key] = (category, [])
            categories[category_key][1].append(
                ParsedHolding(asset, ticker, allocation_bps)
            )
            total_bps += allocation_bps
    except csv.Error as exc:
        raise StrategyImportError(f"Malformed CSV: {exc}") from exc

    if strategy_name is None:
        raise StrategyImportError("Strategy CSV contains no holdings")
    if total_bps != 10_000:
        raise StrategyImportError(
            f"Allocations total {Decimal(total_bps) / 100:.2f}%, not 100.00%"
        )

    parsed_categories = tuple(
        ParsedCategory(categories[key][0], tuple(categories[key][1]))
        for key in category_order
    )
    return ParsedStrategy(strategy_name, parsed_categories)


__all__ = [
    "ParsedCategory",
    "ParsedHolding",
    "ParsedStrategy",
    "StrategyImportError",
    "parse_strategy_csv",
]
