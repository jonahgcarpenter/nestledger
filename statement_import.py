"""Local-only credit-card statement extraction and parsing.

The module intentionally uses command-line Poppler and Tesseract tools rather
than uploading financial documents to a third party.
"""

from __future__ import annotations

import calendar
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


class StatementImportError(Exception):
    """Base exception for statement imports."""


class InvalidPDFError(StatementImportError):
    """The supplied document is not a PDF."""


class ExtractionError(StatementImportError):
    """PDF text extraction or OCR failed."""


class MissingToolError(ExtractionError):
    """A required local extraction executable is unavailable."""


class ExtractionTimeoutError(ExtractionError):
    """A local extraction executable exceeded its time limit."""


class UnsupportedStatementError(StatementImportError):
    """The statement issuer or its transaction layout is unsupported."""


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    method: str  # "text" or "ocr"

    def __iter__(self):
        # Allows the convenient ``text, method = extract_pdf_text(...)`` API.
        yield self.text
        yield self.method


@dataclass
class ParsedTransaction:
    transaction_date: date | None
    description: str
    amount: Decimal | None
    posting_date: date | None = None
    merchant: str = ""
    category: str | None = None
    needs_review: bool = False
    warnings: list[str] = field(default_factory=list)
    raw_line: str = field(default="", repr=False)

    @property
    def date(self) -> date | None:
        """Compatibility alias for consumers that call this simply a date."""
        return self.transaction_date


@dataclass
class ParsedStatement:
    issuer: str
    transactions: list[ParsedTransaction]
    period_start: date | None = None
    period_end: date | None = None
    closing_date: date | None = None
    extraction_method: str | None = None
    warnings: list[str] = field(default_factory=list)


_DATE_TOKEN = r"(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{2,4})?)"
_AMOUNT_TOKEN = r"(?:(?:-\s*)?\$?\s*\d[\d,]*\.\d{2}-?|\(\s*\$?\s*\d[\d,]*\.\d{2}\s*\))(?:\s*(?:CR|CREDIT))?"
_FOOTNOTE_TOKEN = r"(?:\s*[*†‡⧫]+)?"
_ROW_RE = re.compile(
    rf"^\s*(?P<date>{_DATE_TOKEN}){_FOOTNOTE_TOKEN}"
    rf"(?:\s+(?P<post>{_DATE_TOKEN}){_FOOTNOTE_TOKEN})?\s+"
    rf"(?P<description>.+?)\s+(?P<amount>{_AMOUNT_TOKEN}){_FOOTNOTE_TOKEN}\s*$",
    re.IGNORECASE,
)
_DATE_FIND_RE = re.compile(_DATE_TOKEN, re.IGNORECASE)
_AMOUNT_FIND_RE = re.compile(
    rf"(?P<amount>{_AMOUNT_TOKEN}){_FOOTNOTE_TOKEN}\s*$", re.IGNORECASE
)
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}

_SUMMARY_WORDS = (
    "new balance", "previous balance", "minimum payment", "credit limit",
    "available credit", "total fees", "total interest", "total payments",
    "total credits", "total purchases", "account summary", "payment due",
    "cash advance limit", "year-to-date", "year to date",
)
MAX_OCR_PAGES = 40


def _safe_detail(value: str) -> str:
    """Redact card-like digit runs before including tool output in errors."""
    value = re.sub(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)", "[REDACTED]", value)
    return value.strip()[:500]


def validate_pdf(source: str | Path | bytes | bytearray) -> None:
    """Validate a document using its PDF magic bytes, independently of parsing."""
    try:
        if isinstance(source, (bytes, bytearray)):
            magic = bytes(source[:5])
        else:
            with Path(source).open("rb") as document:
                magic = document.read(5)
    except OSError as exc:
        raise InvalidPDFError(f"Unable to read PDF: {_safe_detail(str(exc))}") from exc
    if magic != b"%PDF-":
        raise InvalidPDFError("Document does not have a valid %PDF header")


def _run(command: list[str], timeout: int, tool: str) -> subprocess.CompletedProcess[str]:
    if shutil.which(command[0]) is None:
        raise MissingToolError(f"Required local tool is not installed: {tool}")
    try:
        return subprocess.run(
            command, shell=False, check=False, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionTimeoutError(f"{tool} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise ExtractionError(f"Could not run {tool}: {_safe_detail(str(exc))}") from exc


def _is_transaction_like(text: str, minimum_rows: int = 1) -> bool:
    rows = 0
    for line in text.splitlines():
        if _ROW_RE.match(re.sub(r"\s+", " ", line).strip()):
            rows += 1
            if rows >= minimum_rows:
                return True
    return False


def extract_pdf_text(
    source: str | Path | bytes | bytearray,
    *,
    text_timeout: int = 30,
    render_timeout: int = 60,
    ocr_timeout_per_page: int = 45,
) -> ExtractionResult:
    """Extract statement text locally, falling back from Poppler text to OCR.

    Byte inputs, rendered pages, and OCR intermediates live only inside a
    TemporaryDirectory. The caller's path is read but never copied or changed.
    """
    validate_pdf(source)
    with tempfile.TemporaryDirectory(prefix="statement-import-") as workspace:
        root = Path(workspace)
        if isinstance(source, (bytes, bytearray)):
            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(bytes(source))
        else:
            pdf_path = Path(source)

        extracted_path = root / "embedded.txt"
        result = _run(
            ["pdftotext", "-layout", str(pdf_path), str(extracted_path)],
            text_timeout,
            "pdftotext",
        )
        if result.returncode != 0:
            raise ExtractionError(
                f"pdftotext failed: {_safe_detail(result.stderr) or 'unknown error'}"
            )
        try:
            embedded = extracted_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ExtractionError("pdftotext did not produce readable output") from exc
        if _is_transaction_like(embedded):
            return ExtractionResult(embedded, "text")

        info = _run(["pdfinfo", str(pdf_path)], text_timeout, "pdfinfo")
        if info.returncode != 0:
            raise ExtractionError(
                f"pdfinfo failed: {_safe_detail(info.stderr) or 'unknown error'}"
            )
        page_match = re.search(r"^Pages:\s+(\d+)\s*$", info.stdout, re.MULTILINE)
        if page_match is None:
            raise ExtractionError("Could not determine the PDF page count")
        page_count = int(page_match.group(1))
        if page_count > MAX_OCR_PAGES:
            raise ExtractionError(
                f"Scanned PDFs are limited to {MAX_OCR_PAGES} pages; this file has {page_count}"
            )

        prefix = root / "page"
        rendered = _run(
            ["pdftoppm", "-png", "-r", "300", str(pdf_path), str(prefix)],
            render_timeout,
            "pdftoppm",
        )
        if rendered.returncode != 0:
            raise ExtractionError(
                f"pdftoppm failed: {_safe_detail(rendered.stderr) or 'unknown error'}"
            )
        pages = sorted(root.glob("page-*.png"))
        if not pages:
            raise ExtractionError("pdftoppm rendered no pages")
        ocr_pages: list[str] = []
        for page in pages:
            ocr = _run(
                ["tesseract", str(page), "stdout", "-l", "eng"],
                ocr_timeout_per_page,
                "tesseract",
            )
            if ocr.returncode != 0:
                raise ExtractionError(
                    f"tesseract failed: {_safe_detail(ocr.stderr) or 'unknown error'}"
                )
            ocr_pages.append(ocr.stdout)
        text = "\n\f\n".join(ocr_pages)
        if not text.strip():
            raise ExtractionError("OCR produced no text")
        return ExtractionResult(text, "ocr")


def detect_issuer(text: str) -> str:
    """Return a canonical issuer name based on statement text."""
    lowered = text.lower()
    signatures = (
        ("Apple Card/Goldman Sachs", ("apple card", "goldman sachs")),
        ("American Express", ("american express", "americanexpress", "amex")),
        ("Capital One", ("capital one", "capitalone.com")),
        ("Discover", ("discover",)),
        ("Chase", ("jpmorgan chase", "chase.com", "chase card", "chase bank")),
    )
    for issuer, needles in signatures:
        if any(needle in lowered for needle in needles):
            return issuer
    raise UnsupportedStatementError("Statement issuer could not be detected")


def parse_us_amount(value: str, description: str = "") -> Decimal:
    """Parse a US currency amount using charges-positive sign semantics."""
    raw = value.strip()
    negative = (
        raw.startswith("-") or raw.endswith("-") or
        (raw.startswith("(") and raw.endswith(")")) or
        bool(re.search(r"\b(?:CR|CREDIT)\s*$", raw, re.IGNORECASE))
    )
    number = re.sub(r"[^\d.]", "", raw)
    if not number or number.count(".") != 1:
        raise ValueError(f"Malformed US amount: {_safe_detail(value)}")
    try:
        amount = Decimal(number)
    except InvalidOperation as exc:
        raise ValueError(f"Malformed US amount: {_safe_detail(value)}") from exc
    if not negative and re.search(
        r"^(?:online payment|automatic payment|autopay|payment thank you|"
        r"payment received|refund(?: from)?|cashback bonus|cash back reward|"
        r"statement credit|credit adjustment|reversal)\b|\b(?:refund|credit|reversal)$",
        description,
        re.IGNORECASE,
    ):
        negative = True
    return -amount if negative else amount


def _parse_date_token(token: str, closing: date | None) -> tuple[date | None, str | None]:
    cleaned = re.sub(r"\s+", " ", token.replace(",", "").strip())
    month: int
    day: int
    year: int | None = None
    if "/" in cleaned:
        pieces = cleaned.split("/")
        try:
            month, day = int(pieces[0]), int(pieces[1])
            if len(pieces) == 3:
                year = int(pieces[2])
        except ValueError:
            return None, f"Invalid date token: {cleaned}"
    else:
        pieces = cleaned.split()
        month = _MONTHS.get(pieces[0][:3].lower(), 0) if pieces else 0
        try:
            day = int(pieces[1])
            if len(pieces) > 2:
                year = int(pieces[2])
        except (IndexError, ValueError):
            return None, f"Invalid date token: {cleaned}"
    if year is not None and year < 100:
        year += 2000
    if year is None:
        if closing is None:
            return None, f"Year is ambiguous for date: {cleaned}"
        year = closing.year - 1 if month > closing.month + 6 else closing.year
    try:
        return date(year, month, day), None
    except ValueError:
        return None, f"Invalid date token: {cleaned}"


def _find_statement_dates(text: str) -> tuple[date | None, date | None, date | None, list[str]]:
    warnings: list[str] = []
    explicit: list[date] = []
    for token in _DATE_FIND_RE.findall(text):
        if re.search(r"(?:/\d{2,4}|\b\d{4}\b)", token):
            parsed, _ = _parse_date_token(token, None)
            if parsed:
                explicit.append(parsed)

    close_match = re.search(
        rf"(?:closing date|statement (?:closing )?date|billing period ending|through)\s*:?\s*({_DATE_TOKEN})",
        text,
        re.IGNORECASE,
    )
    closing = _parse_date_token(close_match.group(1), None)[0] if close_match else None

    period_match = re.search(
        rf"(?:billing period|statement period|opening/closing date|account period)?\s*:?[ \t]*"
        rf"({_DATE_TOKEN})\s*(?:-|–|—|to|through)\s*({_DATE_TOKEN})",
        text,
        re.IGNORECASE,
    )
    start = end = None
    if period_match:
        end, end_warning = _parse_date_token(period_match.group(2), closing)
        if end:
            # A labeled period range is more specific than a closing-date
            # substring (notably Chase's "Opening/Closing Date" heading).
            closing = end
        start, start_warning = _parse_date_token(period_match.group(1), closing)
        warnings.extend(w for w in (start_warning, end_warning) if w)
    if closing is None and explicit:
        # Header dates are normally statement dates; transaction dates are often
        # yearless and therefore excluded from this candidate list.
        closing = max(explicit)
    if end is None:
        end = closing
    if start is None and closing is not None:
        days_match = re.search(
            r"Days in Billing Period\s*:\s*(\d{1,3})", text, re.IGNORECASE
        )
        if days_match:
            billing_days = int(days_match.group(1))
            if 1 <= billing_days <= 366:
                start = closing - timedelta(days=billing_days - 1)
    if closing is None:
        warnings.append("Statement closing date was not found; yearless rows need review")
    return start, end, closing, warnings


def normalize_merchant(description: str) -> str:
    """Conservatively remove common references and location suffixes."""
    merchant = re.sub(r"\s+", " ", description).strip(" -")
    merchant = re.sub(
        r"\s+\d+(?:\.\d+)?%\s+\$\d[\d,]*\.\d{2}$", "", merchant
    )
    merchant = re.sub(
        r"\s+ORDER\s+NUMBER\s+[A-Z0-9-]+.*$", "", merchant,
        flags=re.IGNORECASE,
    )
    merchant = re.sub(r"^AplPay\s+", "", merchant, flags=re.IGNORECASE)
    merchant = re.sub(r"^BT\*DD\s+\*", "", merchant, flags=re.IGNORECASE)
    merchant = re.sub(
        r"\s+(?:REF|REFERENCE|TRACE|TRANSACTION|ORDER|AUTH|ID)\s*[#:]?\s*[A-Z0-9-]{5,}\s*$",
        "",
        merchant,
        flags=re.IGNORECASE,
    )
    merchant = re.sub(r"\s+#\d{3,}\s*$", "", merchant)
    merchant = re.sub(
        r"\s+[A-Z][A-Z.'-]{1,20}\s+[A-Z]{2}(?:\s+USA)?\s*$", "", merchant
    )
    merchant = re.sub(r"\s+\d{7,}\s*$", "", merchant)

    return merchant.strip(" -*")


class StatementAdapter:
    """Common interface for issuer-specific statement adapters."""

    issuer = ""
    skip_phrases: tuple[str, ...] = ()
    section_start: re.Pattern[str] | None = None
    section_end: re.Pattern[str] | None = None
    layout_merchant_index = 1

    def merchant_from_row(self, raw: str, description: str) -> str:
        columns = [column.strip() for column in re.split(r"\s{2,}", raw.strip())]
        index = self.layout_merchant_index
        if (
            len(columns) > index + 1
            and _DATE_FIND_RE.fullmatch(columns[0].rstrip("*†‡⧫"))
            and _AMOUNT_FIND_RE.search(columns[-1])
        ):
            return normalize_merchant(columns[index])
        return normalize_merchant(description)

    def transaction_lines(self, text: str) -> list[tuple[int, str]]:
        lines = list(enumerate(text.splitlines(), 1))
        if self.section_start is None:
            return lines
        start = next(
            (index for index, (_, line) in enumerate(lines) if self.section_start.match(line)),
            None,
        )
        if start is None:
            return lines
        end = len(lines)
        if self.section_end is not None:
            end = next(
                (
                    index
                    for index, (_, line) in enumerate(lines[start + 1 :], start + 1)
                    if self.section_end.match(line)
                ),
                len(lines),
            )
        return lines[start + 1 : end]

    def parse(self, text: str, extraction_method: str | None = None) -> ParsedStatement:
        start, end, closing, warnings = _find_statement_dates(text)
        transactions: list[ParsedTransaction] = []
        pending: ParsedTransaction | None = None
        for line_number, raw in self.transaction_lines(text):
            line = re.sub(r"\s+", " ", raw).strip()
            lowered = line.lower()
            if not line:
                pending = None
                continue
            match = _ROW_RE.match(line)
            if match:
                description = match.group("description").strip()
                tx_date, date_warning = _parse_date_token(match.group("date"), closing)
                posting_date = None
                post_warning = None
                if match.group("post"):
                    posting_date, post_warning = _parse_date_token(match.group("post"), closing)
                row_warnings = [w for w in (date_warning, post_warning) if w]
                try:
                    amount = parse_us_amount(match.group("amount"), description)
                except ValueError as exc:
                    amount = None
                    row_warnings.append(str(exc))
                transaction = ParsedTransaction(
                    transaction_date=tx_date,
                    posting_date=posting_date,
                    description=description,
                    merchant=self.merchant_from_row(raw, description),
                    amount=amount,
                    needs_review=bool(row_warnings),
                    warnings=row_warnings,
                    raw_line=raw,
                )
                transactions.append(transaction)
                pending = transaction
                warnings.extend(f"Line {line_number}: {warning}" for warning in row_warnings)
                continue
            if any(word in lowered for word in _SUMMARY_WORDS + self.skip_phrases):
                continue

            # Wrapped descriptions generally contain letters but no date or
            # terminal amount. Attach only immediately adjacent indented text.
            if pending and raw[:1].isspace() and re.search(r"[A-Za-z]", line):
                if not _DATE_FIND_RE.match(line) and not _AMOUNT_FIND_RE.search(line):
                    if re.match(r"^(?:Order Number\b|\d{6,}\b)", line, re.IGNORECASE):
                        continue
                    pending.description = f"{pending.description} {line}"
                    pending.merchant = normalize_merchant(f"{pending.merchant} {line}")
                    continue
            date_match = _DATE_FIND_RE.match(line)
            if date_match and not _AMOUNT_FIND_RE.search(line):
                warning = "date-like row has no unambiguous amount"
                remainder = line[date_match.end():].strip()
                # Retain clearly transaction-shaped malformed rows, while not
                # manufacturing amounts for ordinary dated headings.
                if remainder and re.search(r"(?:\$|\d[,.]\d|\bCR\b)", remainder, re.IGNORECASE):
                    warnings.append(f"Line {line_number}: {warning}")
                    tx_date, date_warning = _parse_date_token(date_match.group(), closing)
                    row_warnings = [warning]
                    if date_warning:
                        row_warnings.append(date_warning)
                    transaction = ParsedTransaction(
                        transaction_date=tx_date,
                        description=remainder,
                        amount=None,
                        merchant=normalize_merchant(remainder),
                        needs_review=True,
                        warnings=row_warnings,
                        raw_line=raw,
                    )
                    transactions.append(transaction)
                    pending = transaction

        if not transactions:
            raise UnsupportedStatementError(
                f"{self.issuer} statement detected, but no supported transaction rows were found"
            )
        return ParsedStatement(
            issuer=self.issuer,
            period_start=start,
            period_end=end,
            closing_date=closing,
            transactions=transactions,
            extraction_method=extraction_method,
            warnings=warnings,
        )


class ChaseAdapter(StatementAdapter):
    issuer = "Chase"
    skip_phrases = ("purchases and adjustments", "payments and other credits")
    section_start = re.compile(r"^\s*ACCOUNT ACTIVITY\s*$", re.IGNORECASE)
    section_end = re.compile(r"^\s*INTEREST CHARGES\s*$", re.IGNORECASE)


class CapitalOneAdapter(StatementAdapter):
    issuer = "Capital One"
    skip_phrases = ("transactions", "payments, credits and adjustments")
    section_start = re.compile(
        r"^\s*.+:\s*Payments, Credits and Adjustments\s*$", re.IGNORECASE
    )
    section_end = re.compile(
        r"^\s*Total Transactions for This Period\b", re.IGNORECASE
    )
    layout_merchant_index = 2


class DiscoverAdapter(StatementAdapter):
    issuer = "Discover"
    skip_phrases = ("purchases", "payments and credits")


class AppleCardAdapter(StatementAdapter):
    issuer = "Apple Card/Goldman Sachs"
    skip_phrases = ("transactions", "daily cash adjustment")
    section_start = re.compile(r"^\s*Payments\s*$", re.IGNORECASE)
    section_end = re.compile(
        r"^\s*Total charges, credits and returns\b", re.IGNORECASE
    )


class AmericanExpressAdapter(StatementAdapter):
    issuer = "American Express"
    skip_phrases = ("detail continued", "payments and credits")
    section_start = re.compile(
        r"^\s*(?:Payments and Credits|New Charges)\s*$", re.IGNORECASE
    )
    section_end = re.compile(r"^\s*Fees\s*$", re.IGNORECASE)


_ADAPTERS = {
    adapter.issuer: adapter
    for adapter in (
        ChaseAdapter(), CapitalOneAdapter(), DiscoverAdapter(),
        AppleCardAdapter(), AmericanExpressAdapter(),
    )
}


def parse_statement(text: str, extraction_method: str | None = None) -> ParsedStatement:
    """Detect an issuer and parse statement dates and transaction rows."""
    issuer = detect_issuer(text)
    return _ADAPTERS[issuer].parse(text, extraction_method)


def import_statement(source: str | Path | bytes | bytearray) -> ParsedStatement:
    """Validate, locally extract, detect, and parse one PDF statement."""
    extraction = extract_pdf_text(source)
    return parse_statement(extraction.text, extraction.method)


__all__ = [
    "AmericanExpressAdapter", "AppleCardAdapter", "CapitalOneAdapter",
    "ChaseAdapter", "DiscoverAdapter", "ExtractionError", "ExtractionResult",
    "ExtractionTimeoutError", "InvalidPDFError", "MissingToolError",
    "ParsedStatement", "ParsedTransaction", "StatementAdapter",
    "StatementImportError", "UnsupportedStatementError",
    "detect_issuer", "extract_pdf_text", "import_statement", "normalize_merchant",
    "parse_statement", "parse_us_amount", "validate_pdf",
]
