# Pruebas de la compatibilidad Windows / macOS (filesync_core/platform_compat.py).
# Todas corren en cualquier sistema operativo: se le pasa `mac=True/False` a las
# funciones en vez de depender de la plataforma real en que corre pytest.
import hashlib
import os
from pathlib import Path

import pytest

from filesync_core.platform_compat import (
    MAC_TESSERACT_CANDIDATES,
    app_data_root,
    contains_only_ds_store,
    find_bundled_tesseract,
    find_tesseract_cmd,
    is_inside_macos_package,
    is_macos_metadata_file,
    is_macos_package_dir,
    parse_ioreg_uuid,
    scale_font_size,
    walk_files,
)


def _crear_carpeta_tipo_mac(base: Path) -> None:
    """Arma una carpeta con lo que suele haber en un Mac: archivos normales,
    metadatos de Finder, un paquete .app y una carpeta de sistema."""
    (base / "Fotos").mkdir()
    (base / "Fotos" / "a.jpg").write_bytes(b"a")
    (base / "Fotos" / ".DS_Store").write_bytes(b"x")
    (base / "Fotos" / "._a.jpg").write_bytes(b"x")
    (base / "informe.pdf").write_bytes(b"pdf")
    (base / ".DS_Store").write_bytes(b"x")
    (base / "Editor.app" / "Contents" / "MacOS").mkdir(parents=True)
    (base / "Editor.app" / "Contents" / "MacOS" / "Editor").write_bytes(b"bin")
    (base / ".Trashes").mkdir()
    (base / ".Trashes" / "viejo.txt").write_bytes(b"t")


def _archivos(base: Path, mac: bool) -> set[str]:
    return {
        str((Path(root) / name).relative_to(base)).replace(os.sep, "/")
        for root, _dirs, files in walk_files(base, mac=mac)
        for name in files
    }


def test_walk_files_in_mac_skips_packages_and_finder_metadata(tmp_path: Path) -> None:
    _crear_carpeta_tipo_mac(tmp_path)
    assert _archivos(tmp_path, mac=True) == {"informe.pdf", "Fotos/a.jpg"}


def test_walk_files_outside_mac_is_identical_to_os_walk(tmp_path: Path) -> None:
    # En Windows el comportamiento no debe cambiar: entrega todo, igual que os.walk.
    _crear_carpeta_tipo_mac(tmp_path)
    esperado = {
        str((Path(root) / name).relative_to(tmp_path)).replace(os.sep, "/")
        for root, _dirs, files in os.walk(tmp_path)
        for name in files
    }
    assert _archivos(tmp_path, mac=False) == esperado
    assert "Editor.app/Contents/MacOS/Editor" in esperado


def test_walk_files_lets_the_caller_sort_and_prune_dirnames_in_place(tmp_path: Path) -> None:
    for name in ("b", "a", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.txt").write_bytes(b"1")
    visitadas = []
    for root, dirnames, _files in walk_files(tmp_path, mac=True):
        dirnames.sort(key=str.lower)
        if "b" in dirnames:
            dirnames.remove("b")
        visitadas.append(Path(root).name)
    assert visitadas[1:] == ["a", "c"]


def test_metadata_and_package_detectors() -> None:
    assert is_macos_metadata_file(".DS_Store")
    assert is_macos_metadata_file(".ds_store")
    assert is_macos_metadata_file("._foto.jpg")
    assert not is_macos_metadata_file("foto.jpg")
    assert not is_macos_metadata_file(".gitignore")
    assert is_macos_package_dir("Editor.app")
    assert is_macos_package_dir("Mis Fotos.photoslibrary")
    assert not is_macos_package_dir("Facturas")
    assert not is_macos_package_dir("app")


def test_is_inside_macos_package(tmp_path: Path) -> None:
    assert is_inside_macos_package(tmp_path / "X.app" / "Contents", tmp_path)
    assert is_inside_macos_package(tmp_path / "X.app", tmp_path)
    assert not is_inside_macos_package(tmp_path / "Facturas" / "2026", tmp_path)
    assert not is_inside_macos_package(Path("/otra/ruta"), tmp_path)


def test_contains_only_ds_store(tmp_path: Path) -> None:
    solo_ds = tmp_path / "solo_ds"
    solo_ds.mkdir()
    (solo_ds / ".DS_Store").write_bytes(b"x")
    con_archivo = tmp_path / "con_archivo"
    con_archivo.mkdir()
    (con_archivo / ".DS_Store").write_bytes(b"x")
    (con_archivo / "nota.txt").write_bytes(b"x")
    vacia = tmp_path / "vacia"
    vacia.mkdir()
    assert contains_only_ds_store(solo_ds)
    assert not contains_only_ds_store(con_archivo)
    assert not contains_only_ds_store(vacia)
    assert not contains_only_ds_store(tmp_path / "no_existe")


def test_parse_ioreg_uuid() -> None:
    salida = (
        "  | {\n"
        '  |   "IOPlatformUUID" = "8f14e45f-ceea-467a-9575-0ff0e2b4a1c9"\n'
        '  |   "IOPlatformSerialNumber" = "X"\n'
        "  | }"
    )
    assert parse_ioreg_uuid(salida) == "8F14E45F-CEEA-467A-9575-0FF0E2B4A1C9"
    assert parse_ioreg_uuid("sin uuid") == ""
    assert parse_ioreg_uuid("") == ""


def test_app_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert app_data_root(mac=True) == Path.home() / "Library" / "Application Support"
    # Fuera de macOS se conserva el comportamiento original: %APPDATA% si existe...
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert app_data_root(mac=False) == tmp_path / "AppData"
    # ...y la carpeta personal si no.
    monkeypatch.delenv("APPDATA")
    assert app_data_root(mac=False) == Path.home()


def test_scale_font_size() -> None:
    assert scale_font_size(10, 1.0) == 10
    assert [scale_font_size(n, 4 / 3) for n in (8, 9, 10, 11, 26)] == [11, 12, 13, 15, 35]
    assert scale_font_size(1, 0.1) == 1


def test_find_tesseract_cmd() -> None:
    homebrew_arm, homebrew_intel, _macports = MAC_TESSERACT_CANDIDATES
    nunca = lambda _name: None  # noqa: E731
    en_path = lambda _name: "/usr/bin/tesseract"  # noqa: E731
    # Fuera de macOS no se toca nada.
    assert find_tesseract_cmd(mac=False, which=nunca, exists=lambda _p: True) is None
    # En macOS, si ya está en el PATH no hace falta fijar ruta.
    assert find_tesseract_cmd(mac=True, which=en_path, exists=lambda _p: True) is None
    # Sin PATH (app abierta desde Finder): prueba Homebrew Apple Silicon primero, luego Intel.
    assert find_tesseract_cmd(mac=True, which=nunca, exists=lambda p: True) == homebrew_arm
    assert find_tesseract_cmd(mac=True, which=nunca, exists=lambda p: p == homebrew_intel) == homebrew_intel
    assert find_tesseract_cmd(mac=True, which=nunca, exists=lambda _p: False) is None


def test_device_id_is_stable_on_mac_and_unchanged_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tkinter")
    import Organizador

    manager = Organizador.LicenseManager
    # Windows/otros: la fórmula original no cambia (no se invalidan licencias ya activadas).
    monkeypatch.setattr(Organizador, "IS_MAC", False)
    monkeypatch.setattr(Organizador.platform, "system", lambda: "Windows")
    monkeypatch.setattr(Organizador.platform, "node", lambda: "PC-DE-SANTY")
    monkeypatch.setattr(Organizador.uuid, "getnode", lambda: 123456789)
    esperado = hashlib.sha256(b"Windows|PC-DE-SANTY|123456789").hexdigest()
    assert manager.get_device_id() == esperado

    # macOS: el id depende del UUID de hardware, no del nombre de red ni de la MAC.
    monkeypatch.setattr(Organizador, "IS_MAC", True)
    monkeypatch.setattr(Organizador, "mac_hardware_uuid", lambda: "8F14E45F-CEEA-467A-9575-0FF0E2B4A1C9")
    monkeypatch.setattr(Organizador.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Organizador.platform, "node", lambda: "MacBook-Air.local")
    id_casa = manager.get_device_id()
    monkeypatch.setattr(Organizador.platform, "node", lambda: "192-168-1-23.dynamic.cafe.net")
    monkeypatch.setattr(Organizador.uuid, "getnode", lambda: 987654321)
    assert manager.get_device_id() == id_casa
    # Si no se puede leer el UUID, se cae a la fórmula original en vez de fallar.
    monkeypatch.setattr(Organizador, "mac_hardware_uuid", lambda: "")
    assert manager.get_device_id() != id_casa


def _resolver_en(base: Path):
    """Imita `resolve_app_resource`: devuelve la ruta si existe bajo `base`, o None."""

    def resolver(*parts: str) -> Path | None:
        candidate = base.joinpath(*parts)
        return candidate if candidate.exists() else None

    return resolver


def test_find_bundled_tesseract_inside_the_app_package(tmp_path: Path) -> None:
    carpeta = tmp_path / "tesseract"
    (carpeta / "tessdata").mkdir(parents=True)
    (carpeta / "tesseract.exe").write_bytes(b"MZ")
    comando, tessdata = find_bundled_tesseract(_resolver_en(tmp_path), windows=True)
    assert comando == str(carpeta / "tesseract.exe")
    assert tessdata == str(carpeta / "tessdata")


def test_find_bundled_tesseract_falls_back_to_vendor_folder(tmp_path: Path) -> None:
    # Al correr desde el código, build.ps1 deja Tesseract en vendor/tesseract.
    carpeta = tmp_path / "vendor" / "tesseract"
    carpeta.mkdir(parents=True)
    (carpeta / "tesseract").write_bytes(b"\x7fELF")
    comando, tessdata = find_bundled_tesseract(_resolver_en(tmp_path), windows=False)
    assert comando == str(carpeta / "tesseract")
    assert tessdata is None  # sin carpeta tessdata: no se fuerza TESSDATA_PREFIX


def test_find_bundled_tesseract_returns_none_when_not_bundled(tmp_path: Path) -> None:
    assert find_bundled_tesseract(_resolver_en(tmp_path), windows=True) is None
    # El ejecutable de otra plataforma no cuenta: en Windows se busca tesseract.exe.
    carpeta = tmp_path / "tesseract"
    carpeta.mkdir()
    (carpeta / "tesseract").write_bytes(b"\x7fELF")
    assert find_bundled_tesseract(_resolver_en(tmp_path), windows=True) is None
