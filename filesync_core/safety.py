"""Safe, testable filesystem primitives used by the desktop UI.

Este módulo agrupa las operaciones de disco que la interfaz de escritorio
(Organizador.py) necesita para mover, escribir y borrar archivos sin
arriesgarse a tocar algo fuera de las carpetas que el usuario autorizó.
La idea central es "defensa en profundidad": antes de mover o borrar
cualquier archivo se valida su ruta, y todas las escrituras se hacen de
forma atómica (archivo temporal + reemplazo) para no dejar datos a medio
escribir si la app se cierra o falla a mitad de camino.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path


class PathSafetyError(ValueError):
    """Se lanza cuando una operación intentaría salirse de la carpeta permitida.

    Por ejemplo: borrar un archivo que no está dentro del almacenamiento de
    la app, o usar como destino una carpeta que está dentro (o contiene) la
    carpeta de origen.
    """


def canonical(path: str | Path) -> Path:
    """Convierte una ruta a su forma "canónica": absoluta, sin `~` y sin
    enlaces simbólicos/`..` ambiguos. Esto es lo que permite comparar rutas
    de forma confiable (ver `is_within`).
    `strict=False` evita que falle si la ruta todavía no existe en disco.
    """
    return Path(path).expanduser().resolve(strict=False)


def is_within(path: str | Path, parent: str | Path) -> bool:
    """Indica si `path` está dentro de la carpeta `parent` (o es la misma).

    Se apoya en `Path.relative_to`, que lanza `ValueError` si `path` no
    desciende de `parent`; ese error se traduce simplemente en `False`.
    """
    try:
        canonical(path).relative_to(canonical(parent))
        return True
    except ValueError:
        return False


def validate_source_destination(source: str | Path, destination: str | Path) -> tuple[Path, Path]:
    """Valida el par origen/destino antes de organizar archivos.

    Reglas de negocio:
    - El origen debe existir y ser una carpeta real.
    - Origen y destino pueden ser exactamente la misma carpeta (la app
      organiza en subcarpetas dentro de ella).
    - Si son carpetas distintas, ninguna puede estar contenida dentro de la
      otra, porque si el destino estuviera dentro del origen, los archivos
      recién movidos podrían volver a aparecer en el escaneo del origen y
      procesarse de nuevo (o al revés).

    Devuelve las rutas ya normalizadas (canónicas) para que el resto del
    código no tenga que volver a resolverlas.
    """
    source_path, destination_path = canonical(source), canonical(destination)
    if not source_path.is_dir():
        raise PathSafetyError("El directorio de origen no existe o no es accesible.")
    # A shared root is safe: the UI builds its list of files before creating
    # organized subfolders, so those new files are not processed again.
    # Nested-but-different directories are still unsafe because they may be
    # discovered during the source scan.
    if source_path != destination_path and (
        is_within(destination_path, source_path) or is_within(source_path, destination_path)
    ):
        raise PathSafetyError("El origen y el destino no pueden contenerse entre sí, salvo que sean la misma carpeta.")
    return source_path, destination_path


def atomic_write_json(path: str | Path, payload: object) -> None:
    """Escribe un JSON de forma atómica (todo o nada).

    En vez de escribir directamente sobre `path`, se escribe primero en un
    archivo temporal en la misma carpeta, se fuerza a disco con `fsync` y
    recién entonces se reemplaza el archivo final con `os.replace` (que en
    la mayoría de sistemas es una operación atómica). Así, si la app se
    cierra a mitad de la escritura, el archivo original queda intacto en
    vez de quedar corrupto o truncado.
    """
    target = canonical(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        # Si algo falló antes del replace, no dejamos basura temporal.
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Igual que `atomic_write_json` pero para contenido binario crudo
    (por ejemplo, cuando se guarda un archivo ya procesado en cuarentena
    o algún artefacto que no es JSON).
    """
    target = canonical(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def safe_unlink(path: str | Path, allowed_parent: str | Path) -> bool:
    """Borra un archivo, pero solo si está dentro de `allowed_parent`.

    Esto evita que un bug (o una ruta manipulada) termine borrando un
    archivo fuera del almacenamiento propio de la aplicación. `missing_ok`
    hace que borrar algo que ya no existe no sea un error.
    """
    candidate = canonical(path)
    if not is_within(candidate, allowed_parent) or candidate == canonical(allowed_parent):
        raise PathSafetyError("Se rechazó una ruta fuera del almacenamiento de la aplicación.")
    candidate.unlink(missing_ok=True)
    return True


def quarantine_file(source: str | Path, quarantine_root: str | Path) -> Path:
    """Mueve un archivo a la carpeta de "cuarentena" en vez de borrarlo.

    Se usa, por ejemplo, con archivos duplicados: en lugar de eliminarlos
    directamente, se guardan aparte para que el usuario pueda revisarlos o
    recuperarlos después. Si ya existe un archivo con el mismo nombre en la
    cuarentena, se le agrega un sufijo numérico (`_1`, `_2`, ...) hasta
    encontrar un nombre libre, para no sobrescribir nada.
    """
    source_path = canonical(source)
    root = canonical(quarantine_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / source_path.name
    counter = 1
    while destination.exists():
        destination = root / f"{source_path.stem}_{counter}{source_path.suffix}"
        counter += 1
    return Path(shutil.move(str(source_path), str(destination)))


def _quarantine_files(
    quarantine_root: str | Path,
    older_than_days: int | None = None,
    today: dt.date | None = None,
) -> Iterator[Path]:
    """Recorre la cuarentena y va entregando los archivos que se pueden borrar.

    La cuarentena se organiza en subcarpetas con la FECHA en que cada
    duplicado entró (`AAAA-MM-DD`, ver `Organizador.py`). Por eso la
    antigüedad se calcula con el nombre de esa carpeta y NO con la fecha de
    modificación del archivo: un archivo movido conserva su fecha original
    (que puede ser de hace años) aunque haya entrado a la cuarentena hoy.

    - `older_than_days=None`: entrega TODO lo que haya en la cuarentena.
    - `older_than_days=N`: entrega solo los archivos de subcarpetas con más
      de N días de antigüedad. Una subcarpeta cuyo nombre no es una fecha
      válida se deja intacta (ante la duda, no se borra).
    - Nunca sigue enlaces simbólicos ni entrega nada fuera de la raíz.
    """
    root = canonical(quarantine_root)
    if not root.is_dir():
        return
    reference_day = today or dt.date.today()
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        if older_than_days is not None:
            if not entry.is_dir():
                continue
            try:
                entered_on = dt.datetime.strptime(entry.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (reference_day - entered_on).days <= older_than_days:
                continue
        if entry.is_file():
            yield entry
            continue
        for current, dirnames, filenames in os.walk(entry):
            # `os.walk` no entra a enlaces simbólicos a carpetas; además los
            # sacamos de la lista para no tratarlos como archivos.
            dirnames[:] = [name for name in dirnames if not (Path(current) / name).is_symlink()]
            for filename in filenames:
                candidate = Path(current) / filename
                if not candidate.is_symlink():
                    yield candidate


def quarantine_summary(
    quarantine_root: str | Path,
    older_than_days: int | None = None,
    today: dt.date | None = None,
) -> tuple[int, int]:
    """Cuenta cuántos archivos y cuántos bytes hay en la cuarentena que
    `purge_quarantine` borraría con los mismos argumentos (no borra nada).
    Sirve para mostrar el detalle en el diálogo de confirmación."""
    count = 0
    total_bytes = 0
    for candidate in _quarantine_files(quarantine_root, older_than_days, today):
        try:
            total_bytes += candidate.stat().st_size
        except OSError:
            continue
        count += 1
    return count, total_bytes


def purge_quarantine(
    quarantine_root: str | Path,
    older_than_days: int | None = None,
    today: dt.date | None = None,
) -> tuple[int, int]:
    """Borra DEFINITIVAMENTE archivos de la cuarentena y devuelve
    `(archivos_borrados, bytes_liberados)`.

    Cada borrado pasa por `safe_unlink`, que se niega a tocar cualquier ruta
    fuera de `quarantine_root`. Al final se quitan las subcarpetas de fecha
    que hayan quedado vacías (con `os.rmdir`, que solo borra carpetas vacías);
    la carpeta raíz de la cuarentena nunca se borra. Un archivo que no se
    pueda borrar (en uso, sin permisos) se omite y se sigue con el resto.
    """
    root = canonical(quarantine_root)
    deleted = 0
    freed = 0
    for candidate in list(_quarantine_files(root, older_than_days, today)):
        try:
            size = candidate.stat().st_size
            safe_unlink(candidate, root)
        except OSError:
            continue
        deleted += 1
        freed += size
    if root.is_dir():
        for current, _dirnames, _filenames in os.walk(root, topdown=False):
            folder = Path(current)
            if folder == root or folder.is_symlink():
                continue
            try:
                os.rmdir(folder)
            except OSError:
                continue
    return deleted, freed
