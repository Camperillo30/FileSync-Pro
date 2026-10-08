# Pruebas del diagnóstico `FileSync Pro --ocr-selftest` (Organizador.ocr_selftest / main).
import shutil
from pathlib import Path

import pytest

Organizador = pytest.importorskip("Organizador")  # necesita tkinter, igual que la app


def test_main_runs_the_selftest_without_opening_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas = []
    monkeypatch.setattr(Organizador, "ocr_selftest", lambda ruta=None: llamadas.append(ruta) or 0)
    monkeypatch.setattr(Organizador, "ModernOrganizadorGUI", lambda: pytest.fail("no debe abrir la ventana"))
    with pytest.raises(SystemExit) as salida:
        Organizador.main(["--ocr-selftest", "informe.txt"])
    assert salida.value.code == 0
    assert llamadas == ["informe.txt"]
    with pytest.raises(SystemExit) as salida:
        Organizador.main(["--ocr-selftest"])
    assert llamadas[-1] is None


def test_selftest_reports_a_missing_ocr_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Organizador, "pytesseract", None)
    informe = tmp_path / "informe.txt"
    assert Organizador.ocr_selftest(str(informe)) == 1
    texto = informe.read_text(encoding="utf-8")
    assert "error=" in texto
    assert texto.strip().endswith("resultado=FALLO")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="no hay Tesseract instalado en este equipo")
def test_selftest_reads_a_test_image_with_the_installed_tesseract(tmp_path: Path) -> None:
    informe = tmp_path / "informe.txt"
    codigo = Organizador.ocr_selftest(str(informe))
    texto = informe.read_text(encoding="utf-8")
    if "faltan los idiomas" in texto:
        pytest.skip("este Tesseract no tiene spa y eng")
    assert codigo == 0, texto
    assert "resultado=OK" in texto
    assert "idiomas=" in texto
