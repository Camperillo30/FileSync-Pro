# Pruebas del backend de licencias (backend_app.py).
from pathlib import Path

import backend_app


def test_code_hash_is_deterministic() -> None:
    # El mismo código siempre debe producir el mismo hash (para poder
    # buscarlo en la base de datos), y códigos distintos deben dar hashes
    # distintos (para que el hash sirva como identificador único).
    assert backend_app.hash_code("FS-example-code") == backend_app.hash_code("FS-example-code")
    assert backend_app.hash_code("FS-example-code") != backend_app.hash_code("FS-another-code")


def test_database_schema_can_be_created(tmp_path: Path, monkeypatch) -> None:
    # Redirige DB_PATH a un archivo temporal (no tocar la base de datos
    # real) y comprueba que init_database() la crea sin errores.
    monkeypatch.setattr(backend_app, "DB_PATH", tmp_path / "licenses.db")
    backend_app.init_database()
    assert backend_app.DB_PATH.exists()
