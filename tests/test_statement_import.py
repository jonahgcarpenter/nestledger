import tempfile
import unittest
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from statement_import import (
    InvalidPDFError,
    UnsupportedStatementError,
    detect_issuer,
    extract_pdf_text,
    normalize_merchant,
    parse_statement,
    parse_us_amount,
    validate_pdf,
)


class PDFValidationTests(unittest.TestCase):
    def test_magic_validation_is_separate(self):
        validate_pdf(b"%PDF-1.7\nredacted")
        with self.assertRaises(InvalidPDFError):
            validate_pdf(b"not a pdf")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload.pdf"
            path.write_bytes(b"renamed plain text")
            with self.assertRaises(InvalidPDFError):
                validate_pdf(path)

    @patch("statement_import._run")
    def test_embedded_text_is_preferred(self, run):
        def command(args, _timeout, _tool):
            Path(args[-1]).write_text(
                "01/02 TEST MERCHANT $10.00", encoding="utf-8"
            )
            return subprocess.CompletedProcess(args, 0, "", "")

        run.side_effect = command
        result = extract_pdf_text(b"%PDF-1.7\nsynthetic")
        self.assertEqual(result.method, "text")
        self.assertIn("TEST MERCHANT", result.text)
        self.assertEqual(run.call_count, 1)

    @patch("statement_import._run")
    def test_ocr_fallback_combines_rendered_pages(self, run):
        def command(args, _timeout, _tool):
            if args[0] == "pdftotext":
                Path(args[-1]).write_text("image only", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "pdftoppm":
                Path(str(args[-1]) + "-1.png").write_bytes(b"image")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "pdfinfo":
                return subprocess.CompletedProcess(args, 0, "Pages:          1\n", "")
            return subprocess.CompletedProcess(
                args, 0, "01/02 OCR MERCHANT $12.00", ""
            )

        run.side_effect = command
        result = extract_pdf_text(b"%PDF-1.7\nsynthetic")
        self.assertEqual(result.method, "ocr")
        self.assertIn("OCR MERCHANT", result.text)


class DetectionAndAmountTests(unittest.TestCase):
    def test_detects_all_supported_issuers(self):
        samples = {
            "CHASE BANK account": "Chase",
            "Capital One account statement": "Capital One",
            "DISCOVER account": "Discover",
            "Card issued by Goldman Sachs": "Apple Card/Goldman Sachs",
            "AmericanExpress.com account": "American Express",
        }
        for text, expected in samples.items():
            with self.subTest(expected):
                self.assertEqual(detect_issuer(text), expected)

    def test_amount_signs(self):
        self.assertEqual(parse_us_amount("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_us_amount("-$12.00"), Decimal("-12.00"))
        self.assertEqual(parse_us_amount("($19.20)"), Decimal("-19.20"))
        self.assertEqual(parse_us_amount("45.67 CR"), Decimal("-45.67"))
        self.assertEqual(parse_us_amount("$80.00", "ONLINE PAYMENT"), Decimal("-80.00"))
        self.assertEqual(
            parse_us_amount("$19.99", "CREDIT PROTECTION SERVICE"),
            Decimal("19.99"),
        )


class IssuerParsingTests(unittest.TestCase):
    def test_chase_and_year_rollover(self):
        statement = parse_statement("""
JPMorgan Chase Card Services
Opening/Closing Date 12/15/2023 - 01/14/2024
12/29 12/30 SAMPLE GROCERY ANYTOWN XX $52.18
01/03 01/04 ONLINE PAYMENT THANK YOU $500.00
""")
        self.assertEqual(statement.closing_date, date(2024, 1, 14))
        self.assertEqual(statement.transactions[0].transaction_date, date(2023, 12, 29))
        self.assertEqual(statement.transactions[1].transaction_date, date(2024, 1, 3))
        self.assertEqual(statement.transactions[1].amount, Decimal("-500.00"))

    def test_capital_one_month_name_rows(self):
        statement = parse_statement("""
Capital One
Billing Period: Dec 18, 2023 - Jan 17, 2024
Dec 22 SAMPLE GROCERY #123 $43.21
Jan 05 REFUND RETAILER $10.00
""")
        self.assertEqual(statement.issuer, "Capital One")
        self.assertEqual(statement.transactions[0].transaction_date, date(2023, 12, 22))
        self.assertEqual(statement.transactions[1].amount, Decimal("-10.00"))

    def test_discover_full_date_and_credit(self):
        statement = parse_statement("""
Discover it Card
Statement Closing Date: 02/20/2024
02/02/2024 COFFEE SHOP $6.40
02/04/2024 CASHBACK BONUS 5.00 CR
""")
        self.assertEqual(statement.issuer, "Discover")
        self.assertEqual([tx.amount for tx in statement.transactions], [Decimal("6.40"), Decimal("-5.00")])

    def test_apple_card_with_multiline_description(self):
        statement = parse_statement("""
Apple Card issued by Goldman Sachs Bank USA
Statement Date: January 31, 2024
01/08 APPLE STORE $99.00
       ONLINE PURCHASE
01/12 CARD PAYMENT ($50.00)
""")
        self.assertEqual(statement.issuer, "Apple Card/Goldman Sachs")
        self.assertIn("ONLINE PURCHASE", statement.transactions[0].description)
        self.assertEqual(statement.transactions[1].amount, Decimal("-50.00"))

    def test_american_express_optional_posting_date(self):
        statement = parse_statement("""
American Express
Closing Date 03/22/2024
03/01 03/03 AIRLINE TICKET $1,025.70
Mar 04 RESTAURANT CREDIT $24.10
""")
        self.assertEqual(statement.issuer, "American Express")
        self.assertEqual(statement.transactions[0].posting_date, date(2024, 3, 3))
        self.assertEqual(statement.transactions[1].amount, Decimal("-24.10"))

    def test_known_issuer_without_rows_is_clear_error(self):
        with self.assertRaisesRegex(UnsupportedStatementError, "no supported transaction rows"):
            parse_statement("Capital One\nClosing Date: 01/31/2024\nNew Balance $20.00")

    def test_heading_words_in_merchants_do_not_drop_transactions(self):
        discover = parse_statement("""
Discover Card
Closing Date: 04/30/2024
04/02 ACME PURCHASES $20.00
""")
        apple = parse_statement("""
Apple Card issued by Goldman Sachs
Statement Date: April 30, 2024
04/03 CITY TRANSACTIONS LLC $30.00
""")
        self.assertEqual(discover.transactions[0].merchant, "ACME PURCHASES")
        self.assertEqual(apple.transactions[0].merchant, "CITY TRANSACTIONS LLC")

    def test_amex_footnotes_on_date_and_amount(self):
        statement = parse_statement("""
American Express
Closing Date 07/27/2026
New Charges
06/17/26* AUTOPAY PAYMENT - THANK YOU -$44.07
07/20/26 SAMPLE MERCHANT $10.14 ⧫
Fees
""")
        self.assertEqual(
            [transaction.amount for transaction in statement.transactions],
            [Decimal("-44.07"), Decimal("10.14")],
        )
        self.assertFalse(any(transaction.needs_review for transaction in statement.transactions))

    def test_capital_one_uses_detail_section_and_spaced_negative(self):
        statement = parse_statement("""
Capital One
Billing Period: Jun 20, 2026 - Jul 20, 2026
Aug 14 Summary Columns Payments - $624.84
REDACTED CARDHOLDER #1234: Payments, Credits and Adjustments
Jul 12 Jul 13 CAPITAL ONE MOBILE PYMTTHANK YOU - $624.84
REDACTED CARDHOLDER #1234: Transactions
Jun 21 Jun 22 SAMPLE MERCHANT $37.38
REDACTED CARDHOLDER #1234: Total Transactions $37.38
Total Transactions for This Period $37.38
""")
        self.assertEqual(len(statement.transactions), 2)
        self.assertEqual(
            [transaction.amount for transaction in statement.transactions],
            [Decimal("-624.84"), Decimal("37.38")],
        )
        self.assertEqual(statement.transactions[0].transaction_date, date(2026, 7, 12))
        self.assertIsNone(statement.transactions[0].category)

    def test_chase_section_ignores_dated_summary_columns(self):
        statement = parse_statement("""
Chase Bank
Opening/Closing Date 06/07/2026 - 07/06/2026
08/03/26 Minimum Payment Due $35.00
ACCOUNT ACTIVITY
06/09 SAMPLE MERCHANT 42.76
INTEREST CHARGES
""")
        self.assertEqual(len(statement.transactions), 1)
        self.assertEqual(statement.transactions[0].amount, Decimal("42.76"))
        self.assertEqual(statement.warnings, [])

    def test_apple_section_ignores_totals_after_transactions(self):
        statement = parse_statement("""
Apple Card issued by Goldman Sachs
Statement Date: July 31, 2026
Payments
07/01/2026 PAYMENT -$100.00
Transactions
07/02/2026 SAMPLE MERCHANT 3% $0.64 $21.39
Total charges, credits and returns $21.39
08/15/2026 DATED DISCLOSURE $999.00
""")
        self.assertEqual(len(statement.transactions), 2)
        self.assertEqual(
            [transaction.amount for transaction in statement.transactions],
            [Decimal("-100.00"), Decimal("21.39")],
        )

    def test_apple_em_dash_statement_period(self):
        statement = parse_statement("""
Apple Card issued by Goldman Sachs
Statement
Jul 1 — Jul 31, 2026
Payment Due By Aug 31, 2026
Payments
07/01/2026 PAYMENT -$100.00
Total charges, credits and returns $0.00
""")
        self.assertEqual(statement.period_start, date(2026, 7, 1))
        self.assertEqual(statement.period_end, date(2026, 7, 31))
        self.assertEqual(statement.closing_date, date(2026, 7, 31))

    def test_amex_derives_period_start_from_billing_days(self):
        statement = parse_statement("""
American Express
Closing Date 07/27/26
New Charges
07/20/26 SAMPLE MERCHANT $10.14
Fees
Days in Billing Period: 31
""")
        self.assertEqual(statement.period_start, date(2026, 6, 27))
        self.assertEqual(statement.period_end, date(2026, 7, 27))

    def test_layout_columns_do_not_become_merchant_text(self):
        apple = parse_statement("""
Apple Card issued by Goldman Sachs
Statement Date: July 31, 2026
Payments
07/01/2026                   ACH Deposit Internet transfer from account ending in 1234                                                                 -$100.00
Transactions
07/02/2026                   SAMPLE DIGITAL MERCHANT 100 TEST STREET ANYTOWN 00000 XX USA             3%            $0.64             $21.39
Total charges, credits and returns $21.39
""")
        chase = parse_statement("""
Chase Bank
Opening/Closing Date 06/07/2026 - 07/06/2026
ACCOUNT ACTIVITY
06/09                  SAMPLE STORE*REFERENCE ANYTOWN XX                    42.76
                       Order Number TEST-REFERENCE-12345
INTEREST CHARGES
""")
        self.assertEqual(
            apple.transactions[1].merchant,
            "SAMPLE DIGITAL MERCHANT 100 TEST STREET ANYTOWN 00000 XX USA",
        )
        self.assertNotIn("3%", apple.transactions[1].merchant)
        self.assertEqual(chase.transactions[0].merchant, "SAMPLE STORE*REFERENCE")
        self.assertNotIn("Order Number", chase.transactions[0].description)

    def test_malformed_transaction_is_retained_for_review(self):
        statement = parse_statement("""
Discover Card
Closing Date: 04/30/2024
04/12 REDACTED MERCHANT $12
""")
        transaction = statement.transactions[0]
        self.assertIsNone(transaction.amount)
        self.assertTrue(transaction.needs_review)
        self.assertTrue(transaction.warnings)


class MerchantNormalizationTests(unittest.TestCase):
    def test_parser_does_not_assign_categories(self):
        statement = parse_statement("""
Discover Card
Closing Date: 04/30/2024
04/02 SAMPLE GROCERY #123 $20.00
""")
        transaction = statement.transactions[0]
        self.assertEqual(normalize_merchant("SHOP NAME REF: ABC12345"), "SHOP NAME")
        self.assertIsNone(transaction.category)

    def test_uploaded_statement_merchant_patterns(self):
        self.assertEqual(
            normalize_merchant("REGIONAL FIBER LLC ANYTOWN XX"),
            "REGIONAL FIBER LLC",
        )
        self.assertEqual(
            normalize_merchant("AplPay SAMPLE GAS STATION ANYTOWN XX"),
            "SAMPLE GAS STATION",
        )
        statement = parse_statement("""
American Express
Closing Date 07/17/2026
New Charges
06/17/26 MOBILE PAYMENT - THANK YOU -$44.07
06/18/26 SAMPLE STREAMING SERVICE $24.60
Fees
""")
        self.assertEqual(
            [transaction.category for transaction in statement.transactions],
            [None, None],
        )


if __name__ == "__main__":
    unittest.main()
