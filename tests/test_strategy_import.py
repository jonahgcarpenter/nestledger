import unittest
from dataclasses import FrozenInstanceError

from nestledger.strategy_import import (
    MAX_FILE_SIZE,
    ParsedCategory,
    ParsedHolding,
    ParsedStrategy,
    StrategyImportError,
    parse_strategy_csv,
)


HEADER = "Strategy,Category,Asset,Ticker,Allocation\n"


def csv_bytes(*rows):
    return (HEADER + "".join(f"{row}\n" for row in rows)).encode("utf-8")


class StrategyImportSuccessTests(unittest.TestCase):
    def test_bom_total_order_grouping_and_basis_points(self):
        data = (
            "\ufeff" + HEADER
            + "Balanced,Bonds,Treasury fund,IEF,25%\n"
            + "Balanced,Stocks,International fund,,30.50%\n"
            + "Balanced,bonds,Corporate fund,LQD,44.50\n"
            + "Ignored,ToTaL,,,100.00%\n"
        ).encode("utf-8")

        parsed = parse_strategy_csv(data, "uploaded.CSV")

        self.assertEqual(
            parsed,
            ParsedStrategy(
                "Balanced",
                (
                    ParsedCategory(
                        "Bonds",
                        (
                            ParsedHolding("Treasury fund", "IEF", 2500),
                            ParsedHolding("Corporate fund", "LQD", 4450),
                        ),
                    ),
                    ParsedCategory(
                        "Stocks",
                        (ParsedHolding("International fund", "", 3050),),
                    ),
                ),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            parsed.name = "Changed"

    def test_reordered_exact_headers_are_supported(self):
        data = b"Asset,Allocation,Ticker,Category,Strategy\nFund,100,,Core,Simple\n"
        parsed = parse_strategy_csv(data, "simple.csv")
        self.assertEqual(parsed.categories[0].holdings[0].allocation_bps, 10_000)

    def test_exact_limits_are_accepted(self):
        rows = [f"{'S' * 120},Category,{'A' * 120},{'T' * 32},0.20%" for _ in range(500)]
        parsed = parse_strategy_csv(csv_bytes(*rows), "limits.csv")
        self.assertEqual(len(parsed.categories[0].holdings), 500)


class StrategyImportValidationTests(unittest.TestCase):
    def assert_invalid(self, data, pattern, filename="strategy.csv"):
        with self.assertRaisesRegex(StrategyImportError, pattern):
            parse_strategy_csv(data, filename)

    def test_file_and_encoding_validation(self):
        self.assert_invalid(csv_bytes("S,C,A,,100"), r"\.csv extension", "strategy.txt")
        self.assert_invalid(b"x" * (MAX_FILE_SIZE + 1), "256 KiB")
        self.assert_invalid(HEADER.encode() + b"S,C,\xff,,100\n", "UTF-8")
        self.assert_invalid(csv_bytes("S,C,Nu\x00ll,,100"), "control character")
        self.assert_invalid(csv_bytes("S,C,Del\x7f,,100"), "control character")

    def test_headers_must_be_exact_and_unique(self):
        cases = (
            (b"Strategy,Category,Asset,Ticker\n", "missing headers"),
            (HEADER.replace("\n", ",Notes\n").encode(), "extra headers"),
            (b"Strategy,Category,Asset,Ticker,Ticker\n", "duplicate headers"),
            (b"strategy,Category,Asset,Ticker,Allocation\n", "missing headers"),
        )
        for data, message in cases:
            with self.subTest(message=message, data=data):
                self.assert_invalid(data, message)

    def test_malformed_csv_and_row_widths_are_rejected(self):
        cases = (
            (HEADER.encode() + b"S,C,A,100\n", "4 columns"),
            (HEADER.encode() + b"S,C,A,,100,extra\n", "6 columns"),
            (HEADER.encode() + b'S,C,"unterminated,,100\n', "Malformed CSV"),
            (HEADER.encode() + b"\n", "0 columns"),
        )
        for data, message in cases:
            with self.subTest(message=message):
                self.assert_invalid(data, message)

    def test_required_names_and_consistent_strategy(self):
        cases = (
            (csv_bytes(",C,A,,100"), "Strategy is required"),
            (csv_bytes("S,,A,,100"), "Category is required"),
            (csv_bytes("S,C,,,100"), "Asset is required"),
            (csv_bytes("S,C,A,,50", "Other,C,B,,50"), "does not match"),
            (csv_bytes("S,Total,,,100"), "no holdings"),
        )
        for data, message in cases:
            with self.subTest(message=message):
                self.assert_invalid(data, message)

    def test_text_length_limits(self):
        cases = (
            (f"{'S' * 121},C,A,,100", "Strategy exceeds 120"),
            (f"S,{'C' * 121},A,,100", "Category exceeds 120"),
            (f"S,C,{'A' * 121},,100", "Asset exceeds 120"),
            (f"S,C,A,{'T' * 33},100", "Ticker exceeds 32"),
        )
        for row, message in cases:
            with self.subTest(message=message):
                self.assert_invalid(csv_bytes(row), message)

    def test_allocation_validation(self):
        cases = (
            ("", "Allocation is required"),
            ("words", "Invalid allocation"),
            ("NaN", "finite"),
            ("Infinity", "finite"),
            ("-1", "negative"),
            ("1e999999", "exceed 100"),
            ("100.001", "two decimal places"),
            ("99.99", "not 100.00"),
        )
        for allocation, message in cases:
            with self.subTest(allocation=allocation):
                self.assert_invalid(csv_bytes(f"S,C,A,,{allocation}"), message)

    def test_holding_and_case_insensitive_category_limits(self):
        too_many_holdings = ["S,C,A,,0"] * 500 + ["S,C,A,,100"]
        self.assert_invalid(csv_bytes(*too_many_holdings), "at most 500 holdings")

        too_many_categories = [f"S,C{i},A,,0" for i in range(100)]
        too_many_categories.append("S,C100,A,,100")
        self.assert_invalid(csv_bytes(*too_many_categories), "at most 100 categories")

        rows = ["S,Core,A,,0", "S,cORE,B,,100"]
        parsed = parse_strategy_csv(csv_bytes(*rows), "case.csv")
        self.assertEqual(len(parsed.categories), 1)
        self.assertEqual(parsed.categories[0].name, "Core")


if __name__ == "__main__":
    unittest.main()
