# Pruebas de las operaciones seguras de archivos (filesync_core/safety.py).
import datetime as dt
from pathlib import Path

import pytest

from filesync_core.safety import (
    PathSafetyError,
    atomic_write_json,
    is_within,
    purge_quarantine,
    quarantine_summary,
    safe_unlink,
    validate_source_destination,
)


def test_source_and_destination_can_be_the_same_directory(tmp_path: Path) -> None:
    # Organizar "en el mismo lugar" (crear subcarpetas dentro del origen)
    # debe estar permitido.
    source = tmp_path / "source"
    source.mkdir()
    source_result, destination_result = validate_source_destination(source, source)
    assert source_result == source.resolve()
    assert destination_result == source.resolve()


def test_source_and_destination_must_not_be_nested(tmp_path: Path) -> None:
    # Un destino DENTRO del origen (pero no el propio origen) debe
    # rechazarse, porque los archivos movidos volverían a aparecer en el
    # escaneo del origen.
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(PathSafetyError):
        validate_source_destination(source, source / "organized")


def test_source_and_destination_can_be_siblings(tmp_path: Path) -> None:
    # Dos carpetas independientes (ninguna contiene a la otra) deben
    # aceptarse sin problema.
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source_result, destination_result = validate_source_destination(source, destination)
    assert source_result == source.resolve()
    assert destination_result == destination.resolve()


def test_atomic_json_and_safe_unlink_stay_in_storage(tmp_path: Path) -> None:
    # Escribir un JSON de forma atómica y luego borrarlo con safe_unlink
    # debe funcionar mientras el archivo esté dentro de `storage`...
    storage = tmp_path / "storage"
    state_file = storage / "state.json"
    atomic_write_json(state_file, {"status": "running"})
    assert state_file.exists()
    assert is_within(state_file, storage)
    assert safe_unlink(state_file, storage)
    assert not state_file.exists()
    # ...pero safe_unlink debe negarse a borrar algo fuera de esa carpeta,
    # aunque la ruta sea válida.
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(PathSafetyError):
        safe_unlink(outside, storage)
    assert outside.exists()


def _make_quarantine(tmp_path: Path) -> Path:
    # Cuarentena de prueba con carpetas por fecha, como la crea la app.
    root = tmp_path / "duplicate_quarantine"
    (root / "2026-07-01").mkdir(parents=True)
    (root / "2026-07-01" / "viejo.pdf").write_bytes(b"a" * 10)
    (root / "2026-09-15").mkdir()
    (root / "2026-09-15" / "reciente.pdf").write_bytes(b"b" * 20)
    (root / "carpeta-rara").mkdir()
    (root / "carpeta-rara" / "no_tocar.txt").write_bytes(b"c" * 5)
    return root


def test_purge_quarantine_only_deletes_entries_older_than_the_limit(tmp_path: Path) -> None:
    # Con un límite de 30 días, solo se borra lo que entró hace MÁS de 30
    # días (según el nombre de la carpeta), no lo reciente ni lo que no
    # tiene un nombre de fecha válido.
    root = _make_quarantine(tmp_path)
    today = dt.date(2026, 9, 19)
    assert quarantine_summary(root, older_than_days=30, today=today) == (1, 10)
    deleted, freed = purge_quarantine(root, older_than_days=30, today=today)
    assert (deleted, freed) == (1, 10)
    assert not (root / "2026-07-01").exists()  # la carpeta vacía también se quita
    assert (root / "2026-09-15" / "reciente.pdf").exists()
    assert (root / "carpeta-rara" / "no_tocar.txt").exists()
    assert root.is_dir()


def test_purge_quarantine_without_limit_empties_everything_but_keeps_the_root(tmp_path: Path) -> None:
    root = _make_quarantine(tmp_path)
    outside = tmp_path / "fuera.txt"
    outside.write_text("keep", encoding="utf-8")
    assert quarantine_summary(root) == (3, 35)
    assert purge_quarantine(root) == (3, 35)
    assert root.is_dir()
    assert list(root.iterdir()) == []
    assert outside.exists()
    # Vaciar una cuarentena inexistente o ya vacía no es un error.
    assert purge_quarantine(root) == (0, 0)
    assert purge_quarantine(tmp_path / "no_existe") == (0, 0)
