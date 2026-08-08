import io
import hashlib
import stat
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import nestledger.app as application
from nestledger import database
from nestledger.statement_import import (
    ExtractionResult,
    ParsedStatement,
    ParsedTransaction,
    StatementImportError,
)
from nestledger.strategy_import import parse_strategy_csv

DEFAULT_INSTANCE_PATH = Path(application.app.instance_path)
DEFAULT_DATABASE_PATH = Path(application.app.config["DATABASE"])
DEFAULT_STATEMENTS_PATH = Path(application.app.config["STATEMENTS_DIR"])


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        application.app.config.update(
            TESTING=True,
            DATABASE=str(Path(self.temporary.name) / "test.sqlite3"),
            STATEMENTS_DIR=str(Path(self.temporary.name) / "statements"),
            SECRET_KEY="test",
        )
        with application.app.app_context():
            database.close_db()
            database.init_db()
            for slug, name, risk_score in (
                ("low_risk", "Low risk", 2),
                ("medium_risk", "Medium risk", 5),
                ("high_risk", "High risk", 8),
            ):
                parsed = parse_strategy_csv(
                    (
                        "Strategy,Category,Asset,Ticker,Allocation\n"
                        f"{name},US stocks,Example fund,EXAMPLE,100.00%\n"
                    ).encode(),
                    f"{slug}.csv",
                )
                database.create_ira_strategy(
                    parsed, slug, f"{slug}.csv", risk_score
                )
        self.client = application.app.test_client()
        self.csrf = "test-csrf-token"
        with self.client.session_transaction() as session:
            session["csrf_token"] = self.csrf

    def tearDown(self):
        self.temporary.cleanup()

    def test_spending_pages_load(self):
        for path in (
            "/ira/analyzer/low_risk",
            "/spending/analyzer",
            "/spending/statements",
            "/spending/filters",
        ):
            with self.subTest(path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"NestLedger", response.data)
                self.assertIn(b'class="brand" href="/">NestLedger</a>', response.data)
                self.assertIn(b'href="/ira/analyzer"', response.data)
                self.assertIn(b'href="/ira/strategies"', response.data)
                self.assertIn(b'href="/spending/analyzer"', response.data)
                self.assertIn(b'href="/spending/statements"', response.data)
                self.assertIn(b'href="/spending/filters"', response.data)
                self.assertIn(b">Analyzer</a>", response.data)
                self.assertIn(b'class="nav-dropdown"', response.data)

        analyzer_page = self.client.get("/ira/analyzer/low_risk").data
        self.assertIn(b'href="/ira/analyzer/low_risk"', analyzer_page)
        self.assertIn(b'href="/ira/analyzer/medium_risk"', analyzer_page)
        self.assertIn(b'href="/ira/analyzer/high_risk"', analyzer_page)
        self.assertLess(
            analyzer_page.index(b'href="/ira/analyzer/low_risk"'),
            analyzer_page.index(b'href="/ira/analyzer/medium_risk"'),
        )
        self.assertLess(
            analyzer_page.index(b'href="/ira/analyzer/medium_risk"'),
            analyzer_page.index(b'href="/ira/analyzer/high_risk"'),
        )

        strategies_page = self.client.get("/ira/strategies").data
        self.assertIn(b'action="/ira/strategies/1/delete"', strategies_page)
        self.assertIn(b">Delete</button>", strategies_page)

        statements_page = self.client.get("/spending/statements").data
        self.assertIn(b'id="statementDropZone"', statements_page)
        self.assertIn(b"Drop statement PDFs here", statements_page)
        self.assertIn(b"multiple required", statements_page)

    def test_spending_only_includes_statements_posted_this_month(self):
        with application.app.app_context():
            categories = {
                row["name"]: row["id"] for row in database.list_categories()
            }
            transaction = {
                "source_row": 0,
                "transaction_date": "2024-01-02",
                "original_description": "CURRENT STATEMENT MERCHANT",
                "merchant": "Current Statement Merchant",
                "normalized_merchant": "current statement merchant",
                "amount_cents": 2500,
                "category_id": categories["Groceries"],
            }
            database.create_import(
                "1" * 64,
                "current.pdf",
                [transaction],
                statement_posting_date=date.today().replace(day=1).isoformat(),
                status="confirmed",
            )
            database.create_import(
                "2" * 64,
                "old.pdf",
                [{
                    **transaction,
                    "transaction_date": date.today().isoformat(),
                    "original_description": "OLD STATEMENT MERCHANT",
                    "merchant": "Old Statement Merchant",
                    "normalized_merchant": "old statement merchant",
                    "category_id": categories["Shopping"],
                }],
                statement_posting_date="2000-01-31",
                status="confirmed",
            )
            database.create_import(
                "3" * 64,
                "unknown.pdf",
                [{
                    **transaction,
                    "original_description": "UNKNOWN STATEMENT MERCHANT",
                    "merchant": "Unknown Statement Merchant",
                    "normalized_merchant": "unknown statement merchant",
                    "category_id": categories["Dining"],
                }],
                status="confirmed",
            )

        page = self.client.get("/spending/analyzer").data
        self.assertIn(b"Current Statement Merchant", page)
        self.assertNotIn(b"Old Statement Merchant", page)
        self.assertNotIn(b"Unknown Statement Merchant", page)
        self.assertIn(
            f'type="month" value="{date.today():%Y-%m}"'.encode(),
            page,
        )
        self.assertIn(b'"name": "Groceries"', page)
        self.assertNotIn(b'"name": "Shopping"', page)
        self.assertNotIn(b'"name": "Dining"', page)

        narrowed = self.client.get(
            "/spending/analyzer?from=2025-01-01"
        ).data
        self.assertNotIn(b"Current Statement Merchant", narrowed)

        old_month = self.client.get("/spending/analyzer?month=2000-01").data
        self.assertIn(b"Old Statement Merchant", old_month)
        self.assertNotIn(b"Current Statement Merchant", old_month)
        self.assertIn(b'"name": "Shopping"', old_month)
        self.assertNotIn(b'"name": "Groceries"', old_month)
        self.assertIn(b'type="month" value="2000-01"', old_month)

        invalid_month = self.client.get("/spending/analyzer?month=invalid").data
        self.assertIn(b"Current Statement Merchant", invalid_month)
        self.assertIn(
            f'type="month" value="{date.today():%Y-%m}"'.encode(),
            invalid_month,
        )

    def test_default_storage_paths(self):
        expected_data_path = application.PROJECT_ROOT / "data"
        self.assertEqual(DEFAULT_INSTANCE_PATH, expected_data_path)
        self.assertEqual(DEFAULT_DATABASE_PATH, expected_data_path / "nestledger.db")
        self.assertEqual(DEFAULT_STATEMENTS_PATH, expected_data_path / "statements")

    def test_missing_strategies_are_a_valid_first_run(self):
        with application.app.app_context():
            for strategy in database.list_ira_strategies():
                database.delete_ira_strategy(strategy["id"])
            response = self.client.get("/ira/strategies")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No strategies yet", response.data)
        self.assertIn(b"Import your first strategy", response.data)
        analyzer = self.client.get("/ira/analyzer")
        self.assertEqual(analyzer.status_code, 200)
        self.assertIn(b"Import a strategy before using", analyzer.data)

    def test_section_templates_extend_shared_base(self):
        templates = Path(application.__file__).resolve().parent / "templates"
        page_templates = (
            templates / "ira_strategies" / "index.html",
            templates / "ira_strategies" / "manage.html",
            templates / "ira_strategies" / "import.html",
            templates / "ira_strategies" / "review_import.html",
            templates / "ira_strategies" / "edit.html",
            templates / "spending" / "index.html",
            templates / "spending" / "filters.html",
            templates / "spending" / "statements" / "import.html",
            templates / "spending" / "statements" / "review.html",
            templates / "spending" / "statements" / "results.html",
        )
        for template in page_templates:
            with self.subTest(template=template):
                self.assertTrue(template.read_text().startswith('{% extends "base.html" %}'))
        self.assertFalse((templates / "index.html").exists())
        strategy_page = self.client.get("/ira/analyzer/low_risk").data
        self.assertIn(b'href="/static/app.css"', strategy_page)
        self.assertIn(b'href="/static/ira_strategies.css"', strategy_page)

    def test_strategy_import_replace_edit_and_delete(self):
        csv_content = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Custom plan,Bonds,Treasury fund,TEST,100.00%\n"
        )
        imported = self.client.post(
            "/ira/strategies/import",
            data={
                "csrf_token": self.csrf,
                "strategy": (io.BytesIO(csv_content), "custom.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(imported.status_code, 302)
        self.assertRegex(imported.headers["Location"], r"/ira/strategies/imports/\d+$")
        review_url = imported.headers["Location"]
        review = self.client.get(review_url)
        self.assertIn(b"Score your strategies", review.data)
        self.assertIn(b"Custom plan", review.data)
        drafts_page = self.client.get("/ira/strategies/import")
        self.assertIn(b"Draft imports", drafts_page.data)
        self.assertIn(b"Custom plan", drafts_page.data)
        self.assertIn(review_url.encode(), drafts_page.data)
        imported = self.client.post(
            review_url,
            data={"csrf_token": self.csrf, "risk_score": "7"},
        )
        self.assertEqual(imported.headers["Location"], "/ira/strategies")
        with application.app.app_context():
            strategy = database.get_ira_strategy_by_name("Custom plan")
            self.assertEqual(strategy["source_filename"], "custom.csv")
            self.assertEqual(strategy["risk_score"], 7)
            strategy_id = strategy["id"]
            slug = strategy["slug"]

        conflict = self.client.post(
            "/ira/strategies/import",
            data={
                "csrf_token": self.csrf,
                "risk_score": "7",
                "strategy": (io.BytesIO(csv_content), "again.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertIn(f"replace={strategy_id}", conflict.headers["Location"])

        replacement_content = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Custom plan,Stocks,Index fund,INDEX,60.00%\n"
            b"Custom plan,Bonds,Treasury fund,BOND,40.00%\n"
        )
        replaced = self.client.post(
            f"/ira/strategies/import?replace={strategy_id}",
            data={
                "csrf_token": self.csrf,
                "replace": str(strategy_id),
                "strategy": (io.BytesIO(replacement_content), "replacement.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(replaced.status_code, 302)
        self.assertRegex(replaced.headers["Location"], r"/ira/strategies/imports/\d+$")
        replaced = self.client.post(
            replaced.headers["Location"],
            data={"csrf_token": self.csrf, "risk_score": "8"},
        )
        self.assertEqual(replaced.headers["Location"], "/ira/strategies")
        with application.app.app_context():
            strategy = database.get_ira_strategy(strategy_id)
            self.assertEqual(strategy["slug"], slug)
            self.assertEqual(strategy["risk_score"], 8)
            self.assertEqual(len(strategy["holdings"]), 2)

        invalid_edit = self.client.post(
            f"/ira/strategies/{strategy_id}/edit",
            data={
                "csrf_token": self.csrf,
                "name": "Unsaved custom name",
                "risk_score": "6",
                "category": ["Stocks"],
                "asset": ["Unsaved fund"],
                "ticker": ["TEST"],
                "allocation": ["90.00"],
            },
        )
        self.assertEqual(invalid_edit.status_code, 200)
        self.assertIn(b"Unsaved custom name", invalid_edit.data)
        self.assertIn(b"Unsaved fund", invalid_edit.data)
        with application.app.app_context():
            self.assertEqual(database.get_ira_strategy(strategy_id)["name"], "Custom plan")

        edited = self.client.post(
            f"/ira/strategies/{strategy_id}/edit",
            data={
                "csrf_token": self.csrf,
                "name": "Custom retirement plan",
                "risk_score": "6",
                "category": ["Stocks", "Cash"],
                "asset": ["Index fund", "Money market"],
                "ticker": ["INDEX", ""],
                "allocation": ["75.00", "25.00"],
            },
        )
        self.assertEqual(edited.status_code, 302)
        with application.app.app_context():
            strategy = database.get_ira_strategy(strategy_id)
            self.assertEqual(strategy["name"], "Custom retirement plan")
            self.assertEqual(strategy["slug"], slug)
            self.assertEqual(strategy["risk_score"], 6)

        deleted = self.client.post(
            f"/ira/strategies/{strategy_id}/delete",
            data={"csrf_token": self.csrf},
        )
        self.assertEqual(deleted.headers["Location"], "/ira/strategies")
        with application.app.app_context():
            self.assertIsNone(database.get_ira_strategy(strategy_id))

    def test_multiple_strategy_imports_have_separate_risk_scores(self):
        conservative = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Batch conservative,Bonds,Treasury fund,BOND,100.00%\n"
        )
        aggressive = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Batch aggressive,Stocks,Index fund,INDEX,100.00%\n"
        )
        imported = self.client.post(
            "/ira/strategies/import",
            data={
                "csrf_token": self.csrf,
                "strategy": [
                    (io.BytesIO(conservative), "conservative.csv"),
                    (io.BytesIO(aggressive), "aggressive.csv"),
                ],
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(imported.status_code, 302)
        self.assertRegex(imported.headers["Location"], r"/ira/strategies/imports/\d+$")
        review_url = imported.headers["Location"]
        review = self.client.get(review_url)
        self.assertIn(b"Batch conservative", review.data)
        self.assertIn(b"Batch aggressive", review.data)
        self.assertIn(b'value="0" required', review.data)
        with application.app.app_context():
            self.assertIsNone(database.get_ira_strategy_by_name("Batch conservative"))
            self.assertIsNone(database.get_ira_strategy_by_name("Batch aggressive"))

        invalid_score = self.client.post(
            review_url,
            data={"csrf_token": self.csrf, "risk_score": ["3", "0"]},
        )
        self.assertEqual(invalid_score.status_code, 200)
        self.assertIn(b"whole numbers from 1 to 10", invalid_score.data)

        imported = self.client.post(
            review_url,
            data={"csrf_token": self.csrf, "risk_score": ["3", "9"]},
        )
        self.assertEqual(imported.headers["Location"], "/ira/strategies")
        with application.app.app_context():
            conservative_strategy = database.get_ira_strategy_by_name(
                "Batch conservative"
            )
            aggressive_strategy = database.get_ira_strategy_by_name(
                "Batch aggressive"
            )
            self.assertEqual(conservative_strategy["risk_score"], 3)
            self.assertEqual(aggressive_strategy["risk_score"], 9)
            self.assertEqual(
                conservative_strategy["source_filename"], "conservative.csv"
            )
            self.assertEqual(
                aggressive_strategy["source_filename"], "aggressive.csv"
            )

    def test_invalid_strategy_batch_is_not_partially_imported(self):
        valid = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Unsaved valid,Bonds,Treasury fund,BOND,100.00%\n"
        )
        invalid = (
            b"Strategy,Category,Asset,Ticker,Allocation\n"
            b"Unsaved invalid,Stocks,Index fund,INDEX,90.00%\n"
        )
        response = self.client.post(
            "/ira/strategies/import",
            data={
                "csrf_token": self.csrf,
                "strategy": [
                    (io.BytesIO(valid), "valid.csv"),
                    (io.BytesIO(invalid), "invalid.csv"),
                ],
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not 100.00%", response.data)
        with application.app.app_context():
            self.assertIsNone(database.get_ira_strategy_by_name("Unsaved valid"))
            self.assertIsNone(database.get_ira_strategy_by_name("Unsaved invalid"))

    @patch("nestledger.app.parse_statement")
    @patch("nestledger.app.extract_pdf_text")
    def test_upload_review_confirm_and_duplicate(self, extract, parse):
        extract.return_value = ExtractionResult("redacted", "text")
        parse.return_value = ParsedStatement(
            issuer="Chase",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 31),
            closing_date=date.today(),
            transactions=[
                ParsedTransaction(
                    transaction_date=date(2024, 1, 4),
                    description="SAMPLE GROCERY #123",
                    merchant="SAMPLE GROCERY",
                    amount=Decimal("25.50"),
                ),
                ParsedTransaction(
                    transaction_date=date(2024, 1, 5),
                    description="AUTOPAY PAYMENT - THANK YOU",
                    merchant="AUTOPAY PAYMENT - THANK YOU",
                    amount=Decimal("-25.50"),
                ),
            ],
        )
        payload = b"%PDF-1.7\nsynthetic"
        response = self.client.post(
            "/spending/statements",
            data={
                "csrf_token": self.csrf,
                "statement": (io.BytesIO(payload), "statement.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        review_path = response.headers["Location"]
        digest = hashlib.sha256(payload).hexdigest()
        archived = Path(application.app.config["STATEMENTS_DIR"]) / f"{digest}.pdf"
        self.assertEqual(archived.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(archived.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(archived.parent.stat().st_mode), 0o700)
        review = self.client.get(review_path)
        self.assertIn(b"SAMPLE GROCERY", review.data)
        self.assertIn(b"View original PDF", review.data)
        self.assertIn(b'name="card_name"', review.data)

        pdf_response = self.client.get(review_path + "/pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.data, payload)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertTrue(pdf_response.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(pdf_response.headers["Cache-Control"], "private, no-store")
        pdf_response.close()

        with application.app.app_context():
            imported_transactions = database.list_transactions()
            transaction = next(
                row for row in imported_transactions if row["amount_cents"] > 0
            )
            payment = next(
                row for row in imported_transactions if row["amount_cents"] < 0
            )
            self.assertIsNone(payment["category_name"])
            self.assertEqual(payment["excluded"], 1)
        prefix = f"transaction-{transaction['id']}-"
        payment_prefix = f"transaction-{payment['id']}-"
        response = self.client.post(
            review_path,
            data={
                prefix + "date": "2024-01-04",
                prefix + "merchant": "Sample Grocery",
                prefix + "amount": "25.50",
                prefix + "category": str(transaction["category_id"]),
                payment_prefix + "date": "2024-01-05",
                payment_prefix + "merchant": "Autopay Payment",
                payment_prefix + "amount": "-25.50",
                payment_prefix + "excluded": "on",
                "card_name": "Chase Freedom",
                "action": "confirm",
                "csrf_token": self.csrf,
            },
        )
        self.assertEqual(response.headers["Location"], "/spending/analyzer")
        spending_page = self.client.get("/spending/analyzer").data
        self.assertIn(b"Sample Grocery", spending_page)
        self.assertNotIn(b"Autopay Payment", spending_page)
        self.assertIn(b'id="categoryChart"', spending_page)
        self.assertIn(b'"name": "Groceries"', spending_page)
        self.assertIn(b'id="transactionFilters"', spending_page)
        self.assertIn(b"<th>Card</th>", spending_page)
        self.assertIn(b"Chase Freedom", spending_page)
        self.assertIn(b'name="card"', spending_page)
        self.assertIn(b'action="/spending/analyzer#confirmed-activity"', spending_page)
        self.assertIn(b"transactionFilters.requestSubmit()", spending_page)
        self.assertNotIn(b"Apply filters", spending_page)
        self.assertNotIn(b"Statement history", spending_page)
        filtered_page = self.client.get(
            "/spending/analyzer?card=Chase+Freedom"
        ).data
        self.assertIn(b'<option value="Chase Freedom" selected>', filtered_page)
        self.assertIn(b"Sample Grocery", filtered_page)
        statements_page = self.client.get("/spending/statements").data
        self.assertIn(b"Previous submissions", statements_page)
        self.assertIn(b"Card Name", statements_page)
        self.assertIn(b"Chase Freedom", statements_page)
        self.assertNotIn(b">statement.pdf<", statements_page)

        confirmed_review = self.client.get(review_path).data
        self.assertIn(b"Save changes", confirmed_review)
        with application.app.app_context():
            shopping_id = next(
                row["id"]
                for row in database.list_categories()
                if row["name"] == "Shopping"
            )
        response = self.client.post(
            review_path,
            data={
                prefix + "date": "2024-01-06",
                prefix + "merchant": "Corrected Merchant",
                prefix + "amount": "30.00",
                prefix + "category": str(shopping_id),
                payment_prefix + "date": "2024-01-05",
                payment_prefix + "merchant": "Autopay Payment",
                payment_prefix + "amount": "-25.50",
                payment_prefix + "excluded": "on",
                "card_name": "Chase Freedom Unlimited",
                "action": "save",
                "csrf_token": self.csrf,
            },
        )
        self.assertEqual(response.headers["Location"], review_path)
        with application.app.app_context():
            imported = database.get_import(transaction["import_id"])
            self.assertEqual(imported["statement_posting_date"], date.today().isoformat())
            corrected = database.get_transaction(transaction["id"])
            self.assertEqual(corrected["transaction_date"], "2024-01-06")
            self.assertEqual(corrected["merchant"], "Corrected Merchant")
            self.assertEqual(corrected["amount_cents"], 3000)
            self.assertEqual(corrected["category_id"], shopping_id)
            self.assertEqual(database.get_import(transaction["import_id"])["card_name"], "Chase Freedom Unlimited")

        duplicate = self.client.post(
            "/spending/statements",
            data={
                "csrf_token": self.csrf,
                "statement": (io.BytesIO(payload), "again.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 302)
        self.assertIn("/spending/statements/", duplicate.headers["Location"])
        self.assertEqual(list(archived.parent.glob("*.pdf")), [archived])

        deleted = self.client.post(
            review_path + "/delete",
            data={"csrf_token": self.csrf},
        )
        self.assertEqual(deleted.headers["Location"], "/spending/analyzer")
        self.assertFalse(archived.exists())
        with application.app.app_context():
            self.assertEqual(database.list_imports(), [])

    @patch("nestledger.app.extract_pdf_text")
    def test_failed_import_does_not_retain_pdf(self, extract):
        extract.side_effect = StatementImportError("Unreadable statement")
        payload = b"%PDF-1.7\nunreadable"
        digest = hashlib.sha256(payload).hexdigest()
        response = self.client.post(
            "/spending/statements",
            data={
                "csrf_token": self.csrf,
                "statement": (io.BytesIO(payload), "bad.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        archived = Path(application.app.config["STATEMENTS_DIR"]) / f"{digest}.pdf"
        self.assertFalse(archived.exists())
        with application.app.app_context():
            self.assertEqual(database.list_imports(), [])

    @patch("nestledger.app.parse_statement")
    @patch("nestledger.app.extract_pdf_text")
    def test_bulk_upload_keeps_successes_when_one_file_fails(self, extract, parse):
        extract.return_value = ExtractionResult("redacted", "text")
        parse.return_value = ParsedStatement(
            issuer="Chase",
            period_start=date(2024, 2, 1),
            period_end=date(2024, 2, 29),
            transactions=[
                ParsedTransaction(
                    transaction_date=date(2024, 2, 4),
                    description="SAMPLE MERCHANT",
                    merchant="SAMPLE MERCHANT",
                    amount=Decimal("12.00"),
                )
            ],
        )
        response = self.client.post(
            "/spending/statements",
            data={
                "csrf_token": self.csrf,
                "statements": [
                    (io.BytesIO(b"%PDF-1.7\nfirst"), "first.pdf"),
                    (io.BytesIO(b"%PDF-1.7\nsecond"), "second.pdf"),
                    (io.BytesIO(b"not a pdf"), "notes.txt"),
                ],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Bulk statement import", response.data)
        self.assertIn(b"first.pdf", response.data)
        self.assertIn(b"second.pdf", response.data)
        self.assertIn(b"notes.txt", response.data)
        self.assertIn(b"Statement files must use the .pdf extension", response.data)
        with application.app.app_context():
            imports = database.list_imports()
            self.assertEqual(len(imports), 2)
            self.assertTrue(all(row["status"] == "draft" for row in imports))
        self.assertEqual(
            len(list(Path(application.app.config["STATEMENTS_DIR"]).glob("*.pdf"))),
            2,
        )

    def test_rule_applies_to_existing_transactions(self):
        with application.app.app_context():
            categories = {row["name"]: row["id"] for row in database.list_categories()}
            database.create_import(
                "b" * 64,
                "test.pdf",
                [{
                    "source_row": 0,
                    "transaction_date": "2024-02-01",
                    "original_description": "ACME MARKET 42",
                    "merchant": "ACME MARKET",
                    "normalized_merchant": "acme market",
                    "amount_cents": 1000,
                    "category_id": categories["Other"],
                    "needs_review": True,
                }],
            )
        self.client.post(
            "/spending/filters",
            data={
                "pattern": "acme market",
                "match_type": "exact",
                "category_id": categories["Groceries"],
                "csrf_token": self.csrf,
            },
        )
        with application.app.app_context():
            transaction = database.list_transactions()[0]
            self.assertEqual(transaction["category_id"], categories["Groceries"])
            self.assertEqual(transaction["needs_review"], 0)
            self.assertEqual(transaction["excluded"], 0)

        self.client.post(
            "/spending/filters",
            data={
                "pattern": "acme",
                "match_type": "contains",
                "excluded": "yes",
                "csrf_token": self.csrf,
            },
        )
        with application.app.app_context():
            transaction = database.list_transactions()[0]
            self.assertEqual(transaction["category_id"], categories["Groceries"])
            self.assertEqual(transaction["excluded"], 1)

    def test_post_requires_csrf_token(self):
        response = self.client.post("/spending/filters", data={})
        self.assertEqual(response.status_code, 400)

    def test_confirmation_requires_card_name(self):
        with application.app.app_context():
            import_id = database.create_import("d" * 64, "statement.pdf")
        review_path = f"/spending/statements/{import_id}"

        response = self.client.post(
            review_path,
            data={"action": "confirm", "csrf_token": self.csrf},
        )
        self.assertEqual(response.headers["Location"], review_path)
        with application.app.app_context():
            self.assertEqual(database.get_import(import_id)["status"], "draft")

        response = self.client.post(
            review_path,
            data={
                "card_name": "Amex Gold",
                "action": "confirm",
                "csrf_token": self.csrf,
            },
        )
        self.assertEqual(response.headers["Location"], "/spending/analyzer")
        with application.app.app_context():
            imported = database.get_import(import_id)
            self.assertEqual(imported["card_name"], "Amex Gold")
            self.assertEqual(imported["status"], "confirmed")

    def test_category_management(self):
        self.client.post(
            "/spending/filters/categories",
            data={"name": "Education", "csrf_token": self.csrf},
        )
        with application.app.app_context():
            category = next(
                row for row in database.list_categories() if row["name"] == "Education"
            )
            database.create_import(
                "c" * 64,
                "sample.pdf",
                [
                    {
                        "source_row": 0,
                        "transaction_date": "2024-01-10",
                        "original_description": "BOOK STORE",
                        "merchant": "BOOK STORE",
                        "normalized_merchant": "book store",
                        "amount_cents": 2500,
                        "category_id": category["id"],
                        "excluded": False,
                        "needs_review": False,
                    }
                ],
            )
        self.client.post(
            f"/spending/filters/categories/{category['id']}/update",
            data={"name": "Learning", "csrf_token": self.csrf},
        )
        response = self.client.get("/spending/filters")
        self.assertIn(b"Learning", response.data)
        self.assertIn(b"data-category-rename", response.data)
        with application.app.app_context():
            self.assertEqual(database.list_transactions()[0]["category_name"], "Learning")
        self.client.post(
            f"/spending/filters/categories/{category['id']}/delete",
            data={"csrf_token": self.csrf},
        )
        with application.app.app_context():
            self.assertNotIn(
                "Learning", {row["name"] for row in database.list_categories()}
            )


if __name__ == "__main__":
    unittest.main()
