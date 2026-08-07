# NestLedger

NestLedger is a local Flask application for IRA allocation planning and
credit-card spending analysis. It imports private strategy CSVs and statement
PDFs, applies reusable merchant filters, and stores strategies and the reviewed
ledger in SQLite.

Supported statement issuers:

- Chase
- Capital One
- Discover
- Apple Card / Goldman Sachs
- American Express

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
pip install -e .
python -m nestledger
```

Open `http://127.0.0.1:5000`

## Docker

Build the image:

```bash
docker build -t nestledger .
```

Run it with a persistent named volume for the SQLite database and uploaded statements:

```bash
docker run --rm \
  --name nestledger \
  -p 127.0.0.1:8000:8000 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v nestledger-data:/app/data \
  nestledger
```

Open `http://127.0.0.1:8000`

## Tests

```bash
python -m unittest discover -v
```

Tests use synthetic statement text and do not contain personal or card data.

## Spending Analyzer

Importing a credit-card statement creates a draft of its transactions for review and categorization. Merchant filters can automatically assign categories, exclude payments, and remember decisions for future statements. Once confirmed, transactions become available in the spending analyzer, where they can be filtered by card, merchant, category, and date to understand spending patterns.

## IRA Strategies

Open **IRA > Strategies**, select **Import CSV**, and upload a UTF-8 CSV with
this format:

```csv
Strategy,Category,Asset,Ticker,Allocation
High risk,US stocks,iShares Core S&P 500 ETF,IVV,27.00%
High risk,US stocks,Vanguard Russell 1000 Growth ETF,VONG,18.00%
High risk,Total,,,100.00%
```

Allocations excluding the optional `Total` row must add up to 100%. The
strategy appears in the strategy switcher automatically. CSV files are
validated in memory and are not retained after import. Strategies can be
edited, replaced from another CSV, or deleted through the UI. Each strategy has
a risk score from 1 (lowest) to 10 (highest); analyzer choices are ordered by
risk score and then alphabetically.

Use **IRA > Analyzer** to switch between imported strategies, review target
allocations, and run the deposit and rebalancing calculators.
