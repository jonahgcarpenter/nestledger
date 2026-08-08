import tempfile
import unittest
import stat
import sqlite3
from pathlib import Path

from flask import Flask

from nestledger import database
from nestledger.strategy_import import parse_strategy_csv


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temporary.name) / "test.sqlite3")
        database.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()
        self.temporary.cleanup()

    def test_import_lifecycle_and_summary_excludes_payments(self):
        categories = {row["name"]: row["id"] for row in database.list_categories()}
        import_id = database.create_import(
            "a" * 64,
            "statement.pdf",
            [
                {
                    "source_row": 0,
                    "transaction_date": "2024-01-02",
                    "original_description": "STORE",
                    "merchant": "STORE",
                    "normalized_merchant": "store",
                    "amount_cents": 2500,
                    "category_id": categories["Shopping"],
                },
                {
                    "source_row": 1,
                    "transaction_date": "2024-01-03",
                    "original_description": "PAYMENT",
                    "merchant": "PAYMENT",
                    "normalized_merchant": "payment",
                    "amount_cents": -2500,
                    "category_id": None,
                    "excluded": True,
                },
            ],
            card_name="Chase Sapphire",
            issuer="Chase",
            statement_posting_date="2024-01-31",
        )
        self.assertEqual(database.get_import(import_id)["status"], "draft")
        self.assertTrue(database.confirm_import(import_id))
        self.assertEqual(database.list_card_names(), ["Chase Sapphire"])
        self.assertEqual(
            len(database.list_transactions(card_name="Chase Sapphire")), 2
        )
        self.assertEqual(database.list_transactions(card_name="Another Card"), [])
        self.assertEqual(
            len(database.list_transactions(
                statement_posting_from="2024-01-01",
                statement_posting_to="2024-02-01",
            )),
            2,
        )
        self.assertEqual(
            database.list_transactions(
                statement_posting_from="2024-02-01",
                statement_posting_to="2024-03-01",
            ),
            [],
        )
        summary = database.transaction_summary()
        self.assertEqual([(row["category"], row["amount_cents"]) for row in summary], [("Shopping", 2500)])
        self.assertEqual(
            database.transaction_summary(
                statement_posting_from="2024-02-01",
                statement_posting_to="2024-03-01",
            ),
            [],
        )

    def test_import_migration_backfills_statement_posting_date(self):
        database.close_db()
        path = Path(self.app.config["DATABASE"])
        path.unlink()
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE imports (
                id INTEGER PRIMARY KEY,
                content_sha256 TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                card_name TEXT,
                issuer TEXT,
                statement_start TEXT,
                statement_end TEXT,
                extraction_method TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                warnings TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TEXT
            );
            INSERT INTO imports(content_sha256, filename, statement_end)
            VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'legacy.pdf', '2024-06-30');
            """
        )
        connection.close()

        database.init_db()

        imported = database.get_import(1)
        self.assertEqual(imported["statement_posting_date"], "2024-06-30")
        index_names = {
            row["name"]
            for row in database.get_db().execute("PRAGMA index_list(imports)")
        }
        self.assertIn("idx_imports_statement_posting_date", index_names)

    def test_fresh_defaults_are_short_and_payments_are_exclusion_only(self):
        self.assertEqual(
            {row["name"] for row in database.list_categories()},
            {
                "Groceries",
                "Dining",
                "Subscriptions",
                "Bills & Utilities",
                "Shopping",
                "Other",
            },
        )
        filters = database.list_filters()
        self.assertEqual(len(filters), 21)
        payment_filters = [row for row in filters if row["excluded"]]
        self.assertEqual(len(payment_filters), 8)
        self.assertTrue(all(row["category_id"] is None for row in payment_filters))

    def test_filters_and_cascade_delete(self):
        category = database.list_categories()[0]
        for item in database.list_filters():
            database.delete_filter(item["id"])
        filter_id = database.create_filter("market", "contains", category["id"])
        self.assertEqual(database.list_filters()[0]["id"], filter_id)
        self.assertTrue(database.delete_filter(filter_id))
        self.assertEqual(database.list_filters(), [])

    def test_category_changes_persist_and_deletion_keeps_transactions(self):
        categories = {row["name"]: row["id"] for row in database.list_categories()}
        import_id = database.create_import(
            "c" * 64,
            "statement.pdf",
            [{
                "source_row": 0,
                "transaction_date": "2024-03-01",
                "original_description": "SCHOOL",
                "merchant": "SCHOOL",
                "normalized_merchant": "school",
                "amount_cents": 5000,
                "category_id": categories["Other"],
            }],
        )
        category_id = database.create_category("Education")
        database.create_filter("school", "contains", category_id)
        self.assertTrue(database.update_category(category_id, "Learning"))
        self.assertTrue(database.delete_category(category_id))
        transaction = database.list_transactions(import_id=import_id)[0]
        self.assertEqual(transaction["amount_cents"], 5000)
        database.init_db()
        self.assertNotIn("Learning", {row["name"] for row in database.list_categories()})

    def test_database_is_owner_only(self):
        path = Path(self.app.config["DATABASE"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_strategy_lifecycle_preserves_order_and_cascades(self):
        parsed = parse_strategy_csv(
            (
                "Strategy,Category,Asset,Ticker,Allocation\n"
                "Retirement plan,Stocks,Index fund,INDEX,70.00%\n"
                "Retirement plan,Bonds,Treasury fund,BOND,30.00%\n"
            ).encode(),
            "retirement.csv",
        )
        strategy_id = database.create_ira_strategy(
            parsed, "retirement-plan", "retirement.csv"
        )
        strategy = database.get_ira_strategy(strategy_id)
        self.assertEqual(strategy["name"], "Retirement plan")
        self.assertEqual(strategy["risk_score"], 5)
        self.assertEqual(
            [category["name"] for category in strategy["categories"]],
            ["Stocks", "Bonds"],
        )
        self.assertEqual(
            [holding["allocation_bps"] for holding in strategy["holdings"]],
            [7000, 3000],
        )
        conflicting = parse_strategy_csv(
            (
                "Strategy,Category,Asset,Ticker,Allocation\n"
                "Another plan,Cash,Money market,,100.00%\n"
            ).encode(),
            "another.csv",
        )
        database.create_ira_strategy(conflicting, "another-plan", "another.csv")
        self.assertEqual(
            [row["name"] for row in database.list_ira_strategies()],
            ["Another plan", "Retirement plan"],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            database.replace_ira_strategy(strategy_id, conflicting)
        self.assertEqual(database.get_ira_strategy(strategy_id)["name"], "Retirement plan")
        self.assertEqual(len(database.get_ira_strategy(strategy_id)["holdings"]), 2)
        self.assertTrue(database.delete_ira_strategy(strategy_id))
        self.assertEqual(
            database.get_db()
            .execute(
                "SELECT COUNT(*) FROM ira_strategy_categories WHERE strategy_id = ?",
                (strategy_id,),
            )
            .fetchone()[0],
            0,
        )

    def test_existing_strategies_receive_default_risk_score(self):
        db = database.get_db()
        db.executescript(
            """DROP TABLE ira_strategy_holdings;
               DROP TABLE ira_strategy_categories;
               DROP TABLE ira_strategies;
               CREATE TABLE ira_strategies (
                   id INTEGER PRIMARY KEY,
                   name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                   slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
                   source_filename TEXT,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               );
               INSERT INTO ira_strategies(name, slug)
               VALUES ('Existing plan', 'existing-plan');"""
        )
        database.init_db()
        strategy = db.execute(
            "SELECT * FROM ira_strategies WHERE slug = 'existing-plan'"
        ).fetchone()
        self.assertEqual(strategy["risk_score"], 5)


if __name__ == "__main__":
    unittest.main()
