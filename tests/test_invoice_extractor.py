"""Pruebas del extractor de facturas: tarifas de IVA y PDFs escaneados."""

import io
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import Organizador  # noqa: E402


def _extractor():
    for value in vars(Organizador).values():
        if isinstance(value, type) and hasattr(value, "extract_many"):
            return value()
    raise AssertionError("No se encontró la clase del extractor de facturas")


def test_porcentaje_de_iva_no_se_toma_como_monto():
    extractor = _extractor()
    lines = ["Subtotal: $116.500", "IVA 19%: $22.135", "TOTAL: $138.635"]
    assert extractor._find_labeled_amount(lines, extractor.TAX_LABELS) == "22,135.00"
    assert extractor._extract_amounts("IVA 19 %: $22.135") == ["22,135.00"]


def test_monto_sin_porcentaje_sigue_igual():
    extractor = _extractor()
    assert extractor._extract_amounts("TOTAL: $138.635") == ["138,635.00"]


@pytest.mark.skipif(
    Organizador.Image is None
    or Organizador.pytesseract is None
    or Organizador.PdfReader is None
    or shutil.which("tesseract") is None,
    reason="Requiere Pillow, pypdf y Tesseract",
)
def test_pdf_escaneado_se_lee_con_ocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    image = Image.new("RGB", (900, 700), "white")
    draw = ImageDraw.Draw(image)
    y = 60
    for line in (
        "Ferreteria La Tuerca",
        "NIT 900.000.000-0",
        "FACTURA DE VENTA No. FV-2210",
        "Fecha: 10/10/2026",
        "Subtotal: $359.000",
        "IVA 19%: $68.210",
        "TOTAL: $427.210",
    ):
        draw.text((60, y), line, fill="black", font=font)
        y += 70
    pdf_path = tmp_path / "factura_escaneada.pdf"
    image.save(pdf_path, "PDF", resolution=100)

    result = _extractor().extract(pdf_path)

    assert result["estado"] == "ok"
    assert result["numero"] == "FV-2210"
    assert result["iva"] == "68,210.00"
    assert result["total"] == "427,210.00"
