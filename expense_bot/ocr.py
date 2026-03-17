from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Optional, Sequence

import httpx

from .parser import format_date_id, format_idr, infer_category, parse_amount_token, parse_date_input


DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
DATE_WORD_RE = re.compile(
    r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|mei|may|jun|jul|agu|aug|sep|okt|oct|nov|des|dec)[a-z]*\s+\d{2,4})\b",
    re.I,
)
MONEY_TOKEN_RE = re.compile(
    r"(?i)\b(?:rp\.?\s*|idr\s*)?\d[\d.,]*(?:\s*(?:rb|ribu|k|jt|juta)\b)?\b"
)
TOTAL_KEYWORDS = (
    "grand total",
    "total bayar",
    "amount due",
    "netto",
    "jumlah",
    "total",
)
BANK_TOTAL_KEYWORDS = (
    "total amount",
    "jumlah transfer",
    "nominal transfer",
    "transfer amount",
    "debit amount",
    "nominal",
    "total debit",
)
IGNORE_TOTAL_HINTS = (
    "ppn",
    "tax",
    "pajak",
    "service",
    "change",
    "kembalian",
    "payment",
    "paid",
    "debit",
    "credit",
    "cash",
    "tunai",
    "diskon",
    "discount",
    "admin",
    "subtotal",
    "sub total",
    "saldo",
    "balance",
    "available",
    "fee",
    "pan",
    "terminal id",
    "reference no",
    "reference",
    "ref",
)
MERCHANT_SKIP_HINTS = (
    "struk",
    "receipt",
    "invoice",
    "tanggal",
    "date",
    "table",
    "cashier",
    "no.",
    "telp",
    "phone",
)
BANK_HINTS = (
    "bank",
    "rekening",
    "no rek",
    "account",
    "transfer",
    "recipient",
    "va ",
    "virtual account",
    "m-banking",
    "mobile banking",
    "internet banking",
    "debit",
    "kredit",
    "qris",
    "trx",
    "ref",
)
BANK_NAMES = (
    "bca",
    "bri",
    "bni",
    "mandiri",
    "permata",
    "cimb",
    "danamon",
    "ocbc",
    "dbs",
    "jago",
    "blu",
    "seabank",
    "maybank",
)

MERCHANT_CATEGORY_OVERRIDES = {
    "alfamart": "Belanja Bulanan",
    "indomaret": "Belanja Bulanan",
    "superindo": "Belanja Bulanan",
    "hypermart": "Belanja Bulanan",
    "starbucks": "Kopi/Snack",
    "kopi kenangan": "Kopi/Snack",
    "janji jiwa": "Kopi/Snack",
    "fore": "Kopi/Snack",
    "mcd": "Makanan & Minuman",
    "kfc": "Makanan & Minuman",
    "burger king": "Makanan & Minuman",
}


@dataclass
class ReceiptExtraction:
    item: str
    total: int
    tanggal: str
    kategori: str
    merchant: str
    expense_date: Optional[date]
    used_fallback_total: bool
    is_bank_transaction: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "total": self.total,
            "tanggal": self.tanggal,
            "kategori": self.kategori,
        }


@dataclass
class OCRResult:
    raw_text: str
    receipt: Optional[ReceiptExtraction]
    structured_data: Optional[dict[str, Any]]
    needs_manual_total_confirmation: bool
    reply_text: str


def _normalize_lines(ocr_input: str | Sequence[str]) -> list[str]:
    if isinstance(ocr_input, str):
        raw_lines = ocr_input.splitlines()
    else:
        raw_lines = list(ocr_input)
    return [re.sub(r"\s+", " ", line).strip() for line in raw_lines if str(line).strip()]


def _extract_amounts(text: str) -> list[int]:
    candidates = [match.group(0) for match in MONEY_TOKEN_RE.finditer(text)]
    amounts: list[int] = []
    for token in candidates:
        if not _is_plausible_money_token(token):
            continue
        parsed = parse_amount_token(token)
        if parsed and 100 <= parsed <= 2_000_000_000:
            amounts.append(parsed)
    return amounts


def _is_plausible_money_token(token: str) -> bool:
    token_low = token.lower().strip()
    clean = token_low.replace(" ", "")
    has_currency = ("rp" in token_low) or ("idr" in token_low)
    clean = clean.replace("rp.", "").replace("rp", "").replace("idr", "")
    if not clean:
        return False

    has_suffix = clean.endswith(("rb", "ribu", "k", "jt", "juta"))
    digits_only = re.sub(r"\D", "", clean)
    if not digits_only:
        return False

    if not has_suffix and len(digits_only) > 12:
        return False

    if (
        not has_suffix
        and not has_currency
        and "." not in clean
        and "," not in clean
        and len(digits_only) >= 8
    ):
        return False

    if not has_suffix and ("." in clean or "," in clean):
        normalized = clean.replace(",", ".")
        groups = normalized.split(".")
        if len(groups) > 5:
            return False
        if len(groups) > 1 and any(len(part) != 3 for part in groups[1:]):
            return False

    return True


def _pick_merchant(lines: list[str]) -> str:
    first_lines = lines[:3] if lines else []
    merchant = "Merchant tidak diketahui"
    best_score = -999

    for line in first_lines:
        low = line.lower()
        if any(hint in low for hint in MERCHANT_SKIP_HINTS):
            continue

        letters = sum(1 for char in line if char.isalpha())
        digits = sum(1 for char in line if char.isdigit())
        score = letters - (digits * 2)
        if "rp" in low:
            score -= 8
        if score > best_score and letters >= 3:
            best_score = score
            merchant = line.title()
    return merchant


def _detect_bank_transaction(lines: list[str]) -> bool:
    joined = " ".join(lines).lower()
    has_bank_name = any(bank in joined for bank in BANK_NAMES)
    has_bank_context = any(
        hint in joined
        for hint in (
            "transfer",
            "penerima",
            "recipient",
            "beneficiary",
            "receiver",
            "rekening",
            "account",
            "nominal",
            "debit",
            "saldo",
            "ref",
            "trx",
            "qris",
        )
    )
    if has_bank_name and has_bank_context:
        return True

    score = sum(1 for hint in BANK_HINTS if hint in joined)
    return score >= 2


def _pick_bank_merchant(lines: list[str]) -> str:
    recipient_labels = (
        "penerima",
        "recipient",
        "receiver",
        "beneficiary",
        "tujuan",
        "kepada",
        "nama penerima",
        "nama tujuan",
    )
    invalid_hints = ("pan", "ref", "terminal", "id", "rekening", "account")

    for idx, line in enumerate(lines):
        low = line.lower()
        if any(label in low for label in recipient_labels):
            candidate = ""
            inline_match = re.search(
                r"(?i)(?:penerima|recipient|receiver|beneficiary|tujuan|kepada)\s*[:=-]\s*(.+)$",
                line,
            )
            if inline_match:
                candidate = inline_match.group(1).strip()
            elif idx + 1 < len(lines):
                candidate = lines[idx + 1].strip()

            candidate_low = candidate.lower()
            digit_ratio = sum(char.isdigit() for char in candidate) / max(len(candidate), 1)
            if (
                len(candidate) >= 3
                and not any(hint in candidate_low for hint in invalid_hints)
                and digit_ratio < 0.4
            ):
                return candidate.title()

    for line in lines[:6]:
        low = line.lower()
        for bank in BANK_NAMES:
            if bank in low:
                return f"Transfer {bank.upper()}"

    return "Transaksi Bank"


def _pick_category(merchant: str, lines: Iterable[str]) -> str:
    merchant_low = merchant.lower()
    for key, category in MERCHANT_CATEGORY_OVERRIDES.items():
        if key in merchant_low:
            return category

    inferred = infer_category(f"{merchant} {' '.join(lines)}")
    if inferred != "Lainnya":
        return inferred
    return "Belanja Lainnya"


def _extract_total(lines: list[str], is_bank_transaction: bool) -> tuple[Optional[int], bool]:
    keywords = BANK_TOTAL_KEYWORDS + TOTAL_KEYWORDS if is_bank_transaction else TOTAL_KEYWORDS
    keyword_amounts: list[int] = []
    cut_labels_re = re.compile(
        r"(?i)\b("
        r"source of fund|qris reference|reference|ref no|merchant pan|customer pan|"
        r"terminal id|acquirer|saldo|balance|available|fee|admin"
        r")\b"
    )

    for idx, line in enumerate(lines):
        low = line.lower()
        if "subtotal" in low or "sub total" in low:
            continue
        if not any(keyword in low for keyword in keywords):
            continue

        found_inline = False
        for keyword in keywords:
            if keyword in low:
                segment = line[low.find(keyword) + len(keyword) :]
                cut_match = cut_labels_re.search(segment)
                if cut_match:
                    segment = segment[: cut_match.start()]
                amounts = _extract_amounts(segment)
                if amounts:
                    keyword_amounts.append(amounts[0])
                    found_inline = True

        if not found_inline and idx + 1 < len(lines):
            next_line = lines[idx + 1].lower()
            if not any(hint in next_line for hint in IGNORE_TOTAL_HINTS):
                next_amounts = _extract_amounts(lines[idx + 1])
                if next_amounts:
                    keyword_amounts.append(next_amounts[0])

    if keyword_amounts:
        sorted_amounts = sorted(keyword_amounts)
        return sorted_amounts[len(sorted_amounts) // 2], False

    all_amounts: list[int] = []
    for line in lines:
        low = line.lower()
        if any(hint in low for hint in IGNORE_TOTAL_HINTS):
            continue
        all_amounts.extend([amount for amount in _extract_amounts(line) if amount >= 1000])

    if all_amounts:
        return max(all_amounts), True
    return None, False


def _is_noisy(lines: list[str], total: Optional[int], used_fallback_total: bool) -> bool:
    if not lines:
        return True

    joined = " ".join(lines)
    alnum_count = sum(1 for char in joined if char.isalnum())
    printable_count = sum(1 for char in joined if char.strip())
    ratio = (alnum_count / printable_count) if printable_count else 0

    very_short = len(lines) <= 2
    no_total = total is None or (total is not None and total < 1000)
    weak_text = ratio < 0.45
    weak_fallback = used_fallback_total and (very_short or weak_text)
    return no_total or weak_fallback


def extract_receipt_data(ocr_input: str | Sequence[str]) -> OCRResult:
    lines = _normalize_lines(ocr_input)
    raw_text = "\n".join(lines)

    is_bank_transaction = _detect_bank_transaction(lines)
    total, used_fallback = _extract_total(lines, is_bank_transaction=is_bank_transaction)
    noisy = _is_noisy(lines, total, used_fallback)

    if noisy:
        return OCRResult(
            raw_text=raw_text,
            receipt=None,
            structured_data=None,
            needs_manual_total_confirmation=True,
            reply_text=(
                "Sepertinya hasil scan struknya belum cukup jelas. "
                "Boleh konfirmasi total belanjanya dulu?"
            ),
        )

    merchant = _pick_bank_merchant(lines) if is_bank_transaction else _pick_merchant(lines)
    date_match = DATE_RE.search(raw_text)
    if date_match:
        tanggal = date_match.group(1)
    else:
        date_word_match = DATE_WORD_RE.search(raw_text)
        tanggal = date_word_match.group(1) if date_word_match else "-"
    expense_date = parse_date_input(tanggal) if tanggal != "-" else None
    kategori = "Transfer/Bank" if is_bank_transaction else _pick_category(merchant, lines)
    item = f"Transfer ke {merchant}" if is_bank_transaction else f"Belanja {merchant}"

    receipt = ReceiptExtraction(
        item=item,
        total=total or 0,
        tanggal=format_date_id(expense_date) if expense_date else tanggal,
        kategori=kategori,
        merchant=merchant,
        expense_date=expense_date,
        used_fallback_total=used_fallback,
        is_bank_transaction=is_bank_transaction,
    )
    source_label = "bukti transaksi bank" if is_bank_transaction else "struk"
    confirmation = (
        f"Wah, {source_label} dari {receipt.merchant} kebaca nih:\n\n"
        f"Item: {receipt.item}\n"
        f"Total: {format_idr(receipt.total)}\n"
        f"Kategori: {receipt.kategori}\n"
        f"Tanggal: {receipt.tanggal}\n\n"
        "Balas `simpan` untuk catat, atau `ubah total/kategori/merchant/tanggal ...`."
    )
    return OCRResult(
        raw_text=raw_text,
        receipt=receipt,
        structured_data=receipt.to_json(),
        needs_manual_total_confirmation=False,
        reply_text=confirmation,
    )


# ---------------------------------------------------------------------------
# OCR_BACKEND options:
#   "moondream"  – vikhyatk/moondream2 via HF Serverless (free, recommended)
#   "gemini"     – Google Gemini Flash Vision API (free 15 RPM tier)
#   "hf_custom"  – Custom HF Dedicated Endpoint (your own endpoint URL)
# ---------------------------------------------------------------------------

_HF_SERVERLESS_BASE = "https://api-inference.huggingface.co/models"
_MOONDREAM_MODEL = "vikhyatk/moondream2"
_MOONDREAM_OCR_QUESTION = (
    "Please transcribe ALL text visible in this receipt or transaction proof image. "
    "Include every line, number, and label exactly as it appears. "
    "Output only the raw text, no commentary."
)


class ReceiptOCR:
    """Multi-backend receipt OCR.

    Backend selection (``ocr_backend`` parameter / ``OCR_BACKEND`` env var):
    - ``moondream`` – vikhyatk/moondream2 via HF Serverless API (free, default)
    - ``gemini``    – Google Gemini Flash Vision API (free 15 RPM)
    - ``hf_custom`` – Custom/Dedicated HF Inference Endpoint
    """

    def __init__(
        self,
        api_token: str,
        ocr_backend: str = "moondream",
        # legacy / hf_custom options
        endpoint_url: str = "",
        model_id: str = "",
        # gemini
        gemini_api_key: str = "",
    ) -> None:
        self.api_token = api_token.strip()
        self.ocr_backend = ocr_backend.strip().lower() or "moondream"
        self.endpoint_url = endpoint_url.strip()
        self.model_id = model_id.strip()
        self.gemini_api_key = gemini_api_key.strip()

    @property
    def enabled(self) -> bool:
        if self.ocr_backend == "moondream":
            return bool(self.api_token)
        if self.ocr_backend == "gemini":
            return bool(self.gemini_api_key)
        if self.ocr_backend == "hf_custom":
            return bool(self.endpoint_url)
        return False

    async def scan_receipt(self, image_bytes: bytes) -> Optional[OCRResult]:
        if not self.enabled:
            return None

        try:
            raw_text = await self._extract_text(image_bytes)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"OCR backend '{self.ocr_backend}' gagal: {exc}") from exc

        if not raw_text:
            return None
        return extract_receipt_data(raw_text)

    async def _extract_text(self, image_bytes: bytes) -> str:
        if self.ocr_backend == "moondream":
            return await self._extract_moondream(image_bytes)
        if self.ocr_backend == "gemini":
            return await self._extract_gemini(image_bytes)
        if self.ocr_backend == "hf_custom":
            return await self._extract_hf_custom(image_bytes)
        raise ValueError(f"OCR backend tidak dikenal: '{self.ocr_backend}'")

    # ------------------------------------------------------------------
    # Backend 1: Moondream2 via HF Serverless (FREE, recommended)
    # Model: vikhyatk/moondream2
    # HF Serverless format: multipart/form-data image OR data:{mime};base64
    # ------------------------------------------------------------------
    async def _extract_moondream(self, image_bytes: bytes) -> str:
        url = f"{_HF_SERVERLESS_BASE}/{_MOONDREAM_MODEL}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "x-wait-for-model": "true",
        }
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "inputs": {
                "image": f"data:image/jpeg;base64,{b64}",
                "question": _MOONDREAM_OCR_QUESTION,
            },
            "parameters": {"max_new_tokens": 1024},
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return self._parse_hf_response(data)

    # ------------------------------------------------------------------
    # Backend 2: Google Gemini Flash Vision (FREE 15 RPM)
    # Set env: GEMINI_API_KEY=your_key, OCR_BACKEND=gemini
    # ------------------------------------------------------------------
    async def _extract_gemini(self, image_bytes: bytes) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent?key={self.gemini_api_key}"
        )
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": b64,
                            }
                        },
                        {
                            "text": (
                                "Ini adalah gambar struk belanja atau bukti transaksi. "
                                "Tolong salin SEMUA teks yang terlihat persis seperti aslinya, "
                                "baris per baris. Jangan tambahkan komentar, hanya teks saja."
                            )
                        },
                    ]
                }
            ],
            "generationConfig": {"maxOutputTokens": 1024, "temperature": 0},
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            return ""

    # ------------------------------------------------------------------
    # Backend 3: Custom / Dedicated HF Inference Endpoint
    # For paid HF Endpoints or self-hosted models
    # ------------------------------------------------------------------
    async def _extract_hf_custom(self, image_bytes: bytes) -> str:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload: dict[str, Any] = {
            "inputs": f"data:image/jpeg;base64,{b64}",
            "parameters": {"max_new_tokens": 1024},
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(self.endpoint_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return self._parse_hf_response(data)

    # ------------------------------------------------------------------
    # Shared response parser for HF-style outputs
    # ------------------------------------------------------------------
    def _parse_hf_response(self, payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()

        if isinstance(payload, list):
            for item in payload:
                text = self._parse_hf_response(item)
                if text:
                    return text
            return ""

        if not isinstance(payload, dict):
            return ""

        error = payload.get("error")
        if error:
            raise RuntimeError(str(error))

        for key in ("generated_text", "answer", "text", "ocr_text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for nested_key in ("result", "output", "data"):
            if nested_key in payload:
                text = self._parse_hf_response(payload[nested_key])
                if text:
                    return text

        return ""
