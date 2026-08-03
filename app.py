import csv
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import database
from statement_import import (
    StatementImportError,
    extract_pdf_text,
    normalize_merchant,
    parse_statement,
)


BASE_DIR = Path(__file__).resolve().parent
STRATEGIES_DIR = BASE_DIR / "strategies"
REQUIRED_COLUMNS = {"Strategy", "Category", "Asset", "Ticker", "Allocation"}

app = Flask(__name__)
app.config.from_mapping(
    DATABASE=str(Path(app.instance_path) / "spending.sqlite3"),
    STATEMENTS_DIR=str(Path(app.instance_path) / "statements"),
    MAX_CONTENT_LENGTH=128 * 1024 * 1024,
    MAX_PDF_SIZE=16 * 1024 * 1024,
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
)
database.init_app(app)


def _statements_dir():
    directory = Path(app.config["STATEMENTS_DIR"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _statement_path(content_sha256):
    if len(content_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise ValueError("Invalid statement identifier")
    return _statements_dir() / f"{content_sha256}.pdf"


def _archive_statement(content, content_sha256):
    destination = _statement_path(content_sha256)
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{content_sha256}.",
            suffix=".tmp",
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(content)
        staged_path.chmod(0o600)
        os.replace(staged_path, destination)
        destination.chmod(0o600)
        return destination
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def verify_csrf_token():
    if request.method == "POST" and not secrets.compare_digest(
        session.get("csrf_token", ""), request.form.get("csrf_token", "")
    ):
        abort(400, "Invalid or missing form token")


@app.template_filter("money")
def format_money(cents):
    amount = Decimal(int(cents or 0)) / 100
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _parse_cents(value):
    cleaned = value.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("Enter a valid dollar amount") from error
    if not amount.is_finite():
        raise ValueError("Enter a valid dollar amount")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _apply_filters(merchant, filters):
    normalized = normalize_merchant(merchant).casefold()
    category_id = None
    excluded = False
    for item in filters:
        matches = (
            normalized == item["pattern"].casefold()
            if item["match_type"] == "exact"
            else item["pattern"].casefold() in normalized
        )
        if not matches:
            continue
        if category_id is None and item["category_id"] is not None:
            category_id = item["category_id"]
        excluded = excluded or bool(item["excluded"])
    return normalized, category_id, excluded


def _process_statement_upload(upload):
    filename = secure_filename(upload.filename) or "statement.pdf"
    if not filename.lower().endswith(".pdf"):
        return {
            "filename": filename,
            "status": "error",
            "message": "Statement files must use the .pdf extension.",
        }

    temp_path = None
    archived_path = None
    import_created = False
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            temp_path = Path(temporary.name)
            upload.save(temporary)
        file_size = temp_path.stat().st_size
        if file_size > app.config["MAX_PDF_SIZE"]:
            raise StatementImportError("The PDF is larger than the 16 MB per-file limit.")

        content = temp_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        duplicate = database.get_import_by_hash(digest)
        if duplicate:
            return {
                "filename": filename,
                "status": "duplicate",
                "message": "This exact statement has already been imported.",
                "import_id": duplicate["id"],
            }

        extraction = extract_pdf_text(temp_path)
        parsed = parse_statement(extraction.text, extraction.method)
        filters = database.list_filters()
        transaction_rows = []
        for source_row, transaction in enumerate(parsed.transactions):
            merchant = transaction.merchant or transaction.description
            normalized, category_id, excluded = _apply_filters(merchant, filters)
            amount_cents = 0
            needs_review = (
                transaction.needs_review
                or transaction.amount is None
                or (category_id is None and not excluded)
            )
            if transaction.amount is not None:
                amount_cents = int(
                    (transaction.amount * 100).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            transaction_rows.append(
                {
                    "source_row": source_row,
                    "transaction_date": (
                        transaction.transaction_date.isoformat()
                        if transaction.transaction_date
                        else None
                    ),
                    "original_description": transaction.description,
                    "merchant": merchant,
                    "normalized_merchant": normalized,
                    "amount_cents": amount_cents,
                    "category_id": category_id,
                    "excluded": excluded,
                    "needs_review": needs_review,
                }
            )
        archived_path = _archive_statement(content, digest)
        import_id = database.create_import(
            digest,
            filename,
            transaction_rows,
            issuer=parsed.issuer,
            statement_start=parsed.period_start.isoformat() if parsed.period_start else None,
            statement_end=parsed.period_end.isoformat() if parsed.period_end else None,
            extraction_method=extraction.method,
            warnings=parsed.warnings,
        )
        import_created = True
        return {
            "filename": filename,
            "status": "imported",
            "message": f"Found {len(transaction_rows)} transactions.",
            "import_id": import_id,
            "issuer": parsed.issuer,
            "transaction_count": len(transaction_rows),
        }
    except (StatementImportError, OSError, sqlite3.Error) as error:
        if archived_path is not None and not import_created:
            archived_path.unlink(missing_ok=True)
        return {"filename": filename, "status": "error", "message": str(error)}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_portfolios():
    portfolios = []
    errors = []

    for csv_path in STRATEGIES_DIR.glob("*.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
                    missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
                    raise ValueError(f"missing columns: {', '.join(missing)}")

                holdings = []
                strategy = ""
                for row_number, row in enumerate(reader, start=2):
                    if row["Category"].strip().lower() == "total":
                        continue

                    strategy = strategy or row["Strategy"].strip()

                    allocation_text = row["Allocation"].strip().removesuffix("%")
                    try:
                        allocation = float(allocation_text)
                    except ValueError as error:
                        raise ValueError(
                            f"invalid allocation on row {row_number}"
                        ) from error

                    if allocation < 0:
                        raise ValueError(f"negative allocation on row {row_number}")

                    holdings.append(
                        {
                            "category": row["Category"].strip(),
                            "asset": row["Asset"].strip(),
                            "ticker": row["Ticker"].strip(),
                            "allocation": allocation,
                        }
                    )

                if not holdings:
                    raise ValueError("no holdings found")

                total = sum(holding["allocation"] for holding in holdings)
                if abs(total - 100) > 0.01:
                    raise ValueError(f"allocations total {total:.2f}%, not 100%")

                strategy = strategy or csv_path.stem.replace("_", " ").title()
                portfolios.append(
                    {
                        "slug": csv_path.stem,
                        "strategy": strategy,
                        "holdings": holdings,
                    }
                )
        except (OSError, ValueError, csv.Error) as error:
            errors.append(f"{csv_path.name}: {error}")

    risk_order = {"low": 0, "medium": 1, "high": 2}
    portfolios.sort(
        key=lambda portfolio: (
            risk_order.get(portfolio["strategy"].split()[0].lower(), 99),
            portfolio["strategy"].lower(),
        )
    )
    return portfolios, errors


@app.context_processor
def navigation_strategies():
    portfolios, _errors = load_portfolios()
    return {"nav_strategies": portfolios}


@app.get("/")
def index():
    return redirect(url_for("strategies"))


@app.get("/ira/strategies")
def strategies():
    portfolios, errors = load_portfolios()
    if portfolios:
        return redirect(url_for("strategy", slug=portfolios[0]["slug"]))
    return render_template(
        "ira_strategies/index.html", portfolios=[], portfolio=None, errors=errors
    )


@app.get("/ira/strategies/<slug>")
def strategy(slug):
    portfolios, errors = load_portfolios()
    selected = next((item for item in portfolios if item["slug"] == slug), None)
    if selected is None:
        abort(404)

    categories = {}
    category_groups = {}
    for holding in selected["holdings"]:
        categories[holding["category"]] = (
            categories.get(holding["category"], 0) + holding["allocation"]
        )
        category_groups.setdefault(holding["category"], []).append(holding)

    selected["categories"] = [
        {"name": name, "allocation": allocation}
        for name, allocation in categories.items()
    ]
    selected["category_groups"] = [
        {
            "name": name,
            "allocation": categories[name],
            "holdings": holdings,
        }
        for name, holdings in category_groups.items()
    ]
    return render_template(
        "ira_strategies/index.html",
        portfolios=portfolios,
        portfolio=selected,
        errors=errors,
    )


@app.get("/spending/analyzer")
def spending():
    category_id = request.args.get("category", type=int)
    query = request.args.get("q", "").strip()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    transactions = database.list_transactions(
        status="confirmed",
        category_id=category_id,
        include_excluded=False,
        query=query or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    summaries = database.transaction_summary()
    category_chart = [
        {
            "name": row["category"],
            "amount_cents": row["amount_cents"],
        }
        for row in summaries
        if row["amount_cents"] > 0
    ]
    return render_template(
        "spending/index.html",
        transactions=transactions,
        category_chart=category_chart,
        categories=database.list_categories(),
        filters={
            "category": category_id,
            "q": query,
            "from": date_from,
            "to": date_to,
        },
    )


@app.route("/spending/statements", methods=("GET", "POST"))
def import_statement_pdf():
    if request.method == "GET":
        imports = database.list_imports()
        available_import_ids = {
            item["id"]
            for item in imports
            if _statement_path(item["content_sha256"]).is_file()
        }
        return render_template(
            "spending/statements/import.html",
            imports=imports,
            available_import_ids=available_import_ids,
            tools={
                "pdftotext": shutil.which("pdftotext") is not None,
                "pdftoppm": shutil.which("pdftoppm") is not None,
                "tesseract": shutil.which("tesseract") is not None,
            },
        )

    uploads = request.files.getlist("statements") or request.files.getlist("statement")
    uploads = [upload for upload in uploads if upload.filename]
    if not uploads:
        flash("Choose one or more PDF statements to import.", "error")
        return redirect(url_for("import_statement_pdf"))
    results = [_process_statement_upload(upload) for upload in uploads]
    if len(results) == 1:
        result = results[0]
        if result["status"] == "imported":
            flash(f"{result['message']} Review them before saving.", "success")
            return redirect(url_for("review_import", import_id=result["import_id"]))
        if result["status"] == "duplicate":
            flash(result["message"], "info")
            return redirect(url_for("review_import", import_id=result["import_id"]))
        flash(result["message"], "error")
        return redirect(url_for("import_statement_pdf"))
    return render_template("spending/statements/results.html", results=results)


@app.route("/spending/statements/<int:import_id>", methods=("GET", "POST"))
def review_import(import_id):
    imported = database.get_import(import_id)
    if imported is None:
        abort(404)
    categories = database.list_categories()
    transactions = database.list_transactions(import_id=import_id)

    if request.method == "POST":
        updates = []
        errors = []
        category_ids = {row["id"] for row in categories}
        for transaction in transactions:
            prefix = f"transaction-{transaction['id']}-"
            merchant = request.form.get(prefix + "merchant", "").strip()
            date_text = request.form.get(prefix + "date", "").strip()
            amount_text = request.form.get(prefix + "amount", "").strip()
            category_id = request.form.get(prefix + "category", type=int)
            excluded = prefix + "excluded" in request.form
            try:
                transaction_date = date.fromisoformat(date_text).isoformat()
                amount_cents = _parse_cents(amount_text)
                if not merchant:
                    raise ValueError("Merchant is required")
                if category_id is not None and category_id not in category_ids:
                    raise ValueError("Choose a valid category")
                if category_id is None and not excluded:
                    raise ValueError("Choose a category or exclude the transaction")
            except ValueError as error:
                errors.append(f"Row {transaction['source_row'] + 1}: {error}")
                continue
            updates.append(
                (
                    transaction["id"],
                    transaction_date,
                    merchant,
                    normalize_merchant(merchant).casefold(),
                    amount_cents,
                    category_id,
                    excluded,
                    prefix + "save-rule" in request.form,
                )
            )
        db = database.get_db()
        with db:
            for row in updates:
                transaction_id, tx_date, merchant, normalized, cents, category_id, excluded, save_rule = row
                db.execute(
                    """UPDATE transactions SET transaction_date = ?, merchant = ?,
                              normalized_merchant = ?, amount_cents = ?, category_id = ?,
                              excluded = ?, needs_review = 0, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND import_id = ?""",
                    (tx_date, merchant, normalized, cents, category_id, excluded, transaction_id, import_id),
                )
                if save_rule and normalized:
                    db.execute(
                        """INSERT INTO merchant_rules(
                               pattern, match_type, category_id, excluded
                           ) VALUES (?, 'exact', ?, ?)
                           ON CONFLICT(pattern, match_type) DO UPDATE SET
                               category_id = excluded.category_id,
                               excluded = excluded.excluded,
                               updated_at = CURRENT_TIMESTAMP""",
                        (normalized, category_id, excluded),
                    )
        if errors:
            for error in errors[:5]:
                flash(error, "error")
            flash("Valid rows were saved. Correct the highlighted row and try again.", "info")
            return redirect(url_for("review_import", import_id=import_id))
        if request.form.get("action") == "confirm" and imported["status"] == "draft":
            database.confirm_import(import_id)
            flash("Statement imported successfully.", "success")
            return redirect(url_for("spending"))
        flash(
            "Draft changes saved."
            if imported["status"] == "draft"
            else "Statement changes saved.",
            "success",
        )
        return redirect(url_for("review_import", import_id=import_id))

    warnings = json.loads(imported["warnings"] or "[]")
    return render_template(
        "spending/statements/review.html",
        imported=imported,
        transactions=transactions,
        categories=categories,
        warnings=warnings,
        pdf_available=_statement_path(imported["content_sha256"]).is_file(),
    )


@app.get("/spending/statements/<int:import_id>/pdf")
def view_statement_pdf(import_id):
    imported = database.get_import(import_id)
    if imported is None:
        abort(404)
    try:
        statement_path = _statement_path(imported["content_sha256"])
    except ValueError:
        abort(404)
    if not statement_path.is_file():
        abort(404)
    response = send_file(
        statement_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=secure_filename(imported["filename"]) or "statement.pdf",
        conditional=True,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/spending/statements/<int:import_id>/delete")
def remove_import(import_id):
    imported = database.get_import(import_id)
    if imported is None:
        abort(404)
    try:
        _statement_path(imported["content_sha256"]).unlink(missing_ok=True)
    except OSError:
        flash("The archived PDF could not be deleted; the import was kept.", "error")
        return redirect(url_for("review_import", import_id=import_id))
    database.delete_import(import_id)
    flash("Import deleted.", "success")
    return redirect(url_for("spending"))


@app.route("/spending/filters", methods=("GET", "POST"))
def merchant_filters():
    if request.method == "POST":
        pattern = normalize_merchant(request.form.get("pattern", "")).casefold()
        match_type = request.form.get("match_type", "contains")
        category_id = request.form.get("category_id", type=int)
        excluded = "excluded" in request.form
        category_ids = {row["id"] for row in database.list_categories()}
        valid_category = category_id is None or category_id in category_ids
        if (
            not pattern
            or match_type not in {"exact", "contains"}
            or not valid_category
            or (category_id is None and not excluded)
        ):
            flash("Enter a pattern and choose a category, exclusion, or both.", "error")
        else:
            db = database.get_db()
            try:
                with db:
                    db.execute(
                        """INSERT INTO merchant_rules(
                               pattern, match_type, category_id, excluded
                           ) VALUES (?, ?, ?, ?)
                           ON CONFLICT(pattern, match_type) DO UPDATE SET
                               category_id = excluded.category_id,
                               excluded = excluded.excluded,
                               updated_at = CURRENT_TIMESTAMP""",
                        (pattern, match_type, category_id, excluded),
                    )
                    condition = (
                        "normalized_merchant = ?"
                        if match_type == "exact"
                        else "instr(normalized_merchant, ?) > 0"
                    )
                    matching_transactions = db.execute(
                        f"""SELECT id, merchant, category_id, excluded
                            FROM transactions WHERE {condition}""",
                        (pattern,),
                    ).fetchall()
                    filters = database.list_filters()
                    for transaction in matching_transactions:
                        _normalized, filtered_category, filtered_excluded = (
                            _apply_filters(transaction["merchant"], filters)
                        )
                        db.execute(
                            """UPDATE transactions SET
                                       category_id = COALESCE(?, category_id),
                                       excluded = ?, needs_review = 0,
                                       updated_at = CURRENT_TIMESTAMP
                               WHERE id = ?""",
                            (
                                filtered_category,
                                bool(transaction["excluded"]) or filtered_excluded,
                                transaction["id"],
                            ),
                        )
                flash("Filter saved and applied to matching transactions.", "success")
            except sqlite3.Error:
                flash("The filter could not be saved.", "error")
        return redirect(url_for("merchant_filters"))
    return render_template(
        "spending/filters.html",
        filters=database.list_filters(),
        categories=database.list_categories(),
    )


@app.post("/spending/filters/<int:filter_id>/delete")
def remove_filter(filter_id):
    database.delete_filter(filter_id)
    flash("Filter deleted. Existing transactions were left unchanged.", "success")
    return redirect(url_for("merchant_filters"))


@app.post("/spending/filters/categories")
def add_category():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a category name.", "error")
    else:
        try:
            database.create_category(name)
            flash("Category added.", "success")
        except sqlite3.IntegrityError:
            flash("That category already exists.", "error")
    return redirect(url_for("merchant_filters"))


@app.post("/spending/filters/categories/<int:category_id>/update")
def rename_category(category_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a category name.", "error")
    else:
        try:
            if not database.update_category(category_id, name):
                abort(404)
            flash("Category renamed.", "success")
        except sqlite3.IntegrityError:
            flash("That category already exists.", "error")
    return redirect(url_for("merchant_filters"))


@app.post("/spending/filters/categories/<int:category_id>/delete")
def remove_category(category_id):
    if not database.delete_category(category_id):
        abort(404)
    flash("Category and its filters deleted. Transactions are now uncategorized.", "success")
    return redirect(url_for("merchant_filters"))


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    flash("The combined upload is larger than the 128 MB request limit.", "error")
    return redirect(url_for("import_statement_pdf"))


if __name__ == "__main__":
    app.run(debug=True)
