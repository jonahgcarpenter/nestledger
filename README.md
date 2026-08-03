# Personal Finance Lab

A local Flask application for IRA allocation planning and credit-card spending
analysis. It imports statement PDFs, extracts transactions locally, applies
reusable merchant filters, and stores the reviewed ledger in SQLite.

Supported statement issuers:

- Chase
- Capital One
- Discover
- Apple Card / Goldman Sachs
- American Express

Issuer PDF layouts change over time. Every import opens as a draft so extracted
dates, merchants, categories, and amounts can be checked before confirmation.

## System Requirements

- Python 3.10 or newer
- Poppler (`pdftotext` and `pdftoppm`)
- Tesseract with English language data for scanned PDFs

On Arch Linux:

```bash
sudo pacman -S --needed poppler tesseract tesseract-data-eng
```

On Debian or Ubuntu:

```bash
sudo apt install poppler-utils tesseract-ocr tesseract-ocr-eng
```

PDFs with embedded text only require Poppler. Tesseract is invoked only when
embedded text does not contain recognizable transaction rows. No statement
content is sent to an external service.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. Choose **Spending & statements**, then **Import**.

The SQLite database is created automatically at
`instance/spending.sqlite3`. Successfully imported originals are retained under
`instance/statements/` using their SHA-256 hashes and can be viewed from the
review and import-history screens. Both locations are excluded from Git and
restricted to the current user. Temporary OCR images are always deleted, and
PDFs from failed imports are not retained. Exact duplicate PDFs are detected by
SHA-256 hash.

## Statement Workflow

1. Upload one or more PDFs, up to 16 MB each and 128 MB combined.
2. Review the detected issuer, statement period, warnings, and transactions.
3. Correct uncertain rows highlighted in coral.
4. Optionally select **Remember merchant** to create an exact category and/or
   exclusion filter.
5. Confirm the import to include it in spending summaries.

Deleting an import also deletes its archived original PDF. Imports created
before PDF archiving was enabled continue to work but cannot display an
original.

Card payments remain visible in the ledger when an exclusion filter matches
them. Refunds and credits reduce the assigned category total.

## Categories And Filters

Categories and filters are managed from the **Filters** page. The initial
database is seeded with a standard category set and editable default filters.
After that one-time seed, deleting or renaming them is persistent.

Fresh databases start with Groceries, Dining, Subscriptions,
Bills & Utilities, Shopping, and Other. Payment filters are exclusion-only:
they retain matching rows for reconciliation without assigning a category or
including them in spending views.

A filter can assign a category, exclude matching transactions from spending,
or do both. Exact matches take priority, followed by longer `contains` matches.
Creating a filter immediately applies it to matching stored transactions. The
PDF parser only extracts statement facts and contains no categorization policy.

## Tests

```bash
python -m unittest discover -v
```

Tests use synthetic statement text and do not contain personal or card data.

## Add An IRA Portfolio

Place a `.csv` file in `strategies/` with this format:

```csv
Strategy,Category,Asset,Ticker,Allocation
High risk,US stocks,iShares Core S&P 500 ETF,IVV,27.00%
High risk,US stocks,Vanguard Russell 1000 Growth ETF,VONG,18.00%
High risk,Total,,,100.00%
```

Allocations excluding the optional `Total` row must add up to 100%. The
strategy appears in the strategy switcher automatically.
