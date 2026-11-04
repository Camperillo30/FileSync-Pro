# Pruebas de tools/slim_tesseract_win.py (adelgazar vendor/tesseract antes de empaquetarlo en el .exe).
# Los archivos PE se fabrican aquí mismo (`build_pe`): cabeceras, secciones, importaciones, secciones de depuración
# con nombre largo, tabla de símbolos y certificado, para probar la lógica sin depender de DLL reales.
# (Con los .exe/.dll reales de Tesseract se comprobó aparte, ejecutándolos con Wine: mismo texto antes y después.)
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "slim_tesseract_win.py"
_SPEC = importlib.util.spec_from_file_location("slim_tesseract_win", _SCRIPT)
slim = importlib.util.module_from_spec(_SPEC)
sys.modules["slim_tesseract_win"] = slim
_SPEC.loader.exec_module(slim)

FILE_ALIGN = 0x200
SECTION_ALIGN = 0x1000


def build_pe(
    imports=(),
    *,
    debug_sections: int = 2,
    debug_in_middle: bool = False,
    symbols: bool = True,
    certificate: bool = False,
    trailing: bytes = b"",
    checksum: int = 0x1234,
) -> bytes:
    """Un PE64 mínimo pero válido: .text, .idata (con `imports`) y N secciones `.debug_partN` (nombre largo)."""
    # Sin tabla de símbolos no hay tabla de cadenas: los nombres de las secciones deben caber en 8 bytes.
    debug_names = [f".debug_part{i}" if symbols else f".debug{i}" for i in range(debug_sections)]
    order = [".text", ".idata", *debug_names]
    if debug_in_middle and debug_names:
        order = [".text", debug_names[0], ".idata", *debug_names[1:]]

    idata = bytearray(0x200)
    names_offset = 0x100
    name_rvas = []
    cursor = names_offset
    section_va = {name: SECTION_ALIGN * (i + 1) for i, name in enumerate(order)}
    for dll in imports:
        idata[cursor : cursor + len(dll) + 1] = dll.encode() + b"\0"
        name_rvas.append(section_va[".idata"] + cursor)
        cursor += len(dll) + 1
    for i, rva in enumerate(name_rvas):
        struct.pack_into("<IIIII", idata, 20 * i, 0, 0, 0, rva, section_va[".idata"] + 0x180)
    raw = {".text": b"\x90" * 0x200, ".idata": bytes(idata), **{n: b"D" * 0x200 for n in debug_names}}

    string_table = bytearray(4)
    long_names = {}
    for name in debug_names:
        long_names[name] = len(string_table)
        string_table += name.encode() + b"\0"
    struct.pack_into("<I", string_table, 0, len(string_table))

    headers = bytearray(FILE_ALIGN)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x40)
    headers[0x40:0x44] = b"PE\0\0"
    coff, optional = 0x44, 0x58
    section_table = optional + 240
    pointer = FILE_ALIGN
    table = bytearray()
    body = bytearray()
    for name in order:
        label = name.encode() if name in (".text", ".idata") or not symbols else f"/{long_names[name]}".encode()
        table += label.ljust(8, b"\0")
        table += struct.pack("<IIIIIIHHI", 0x200, section_va[name], 0x200, pointer, 0, 0, 0, 0, 0x40000040)
        body += raw[name]
        pointer += 0x200
    symbol_pointer = pointer if symbols else 0
    tail = bytearray()
    if symbols:
        tail += bytes(18 * 3) + string_table
    certificate_offset = 0
    if certificate:
        tail += bytes((-(pointer + len(tail))) % 8)
        certificate_offset = pointer + len(tail)
        tail += b"C" * 40
    tail += trailing

    struct.pack_into(
        "<HHIIIHH", headers, coff, 0x8664, len(order), 0, symbol_pointer, 3 if symbols else 0, 240, 0x2022
    )
    struct.pack_into("<H", headers, optional, 0x20B)
    struct.pack_into("<I", headers, optional + 32, SECTION_ALIGN)
    struct.pack_into("<I", headers, optional + 36, FILE_ALIGN)
    struct.pack_into("<I", headers, optional + 56, SECTION_ALIGN * (len(order) + 1))
    struct.pack_into("<I", headers, optional + 60, FILE_ALIGN)
    struct.pack_into("<I", headers, optional + 64, checksum)
    struct.pack_into("<I", headers, optional + 108, 16)
    struct.pack_into("<II", headers, optional + 112 + 8 * 1, section_va[".idata"], 20 * (len(imports) + 1))
    if certificate:
        struct.pack_into("<II", headers, optional + 112 + 8 * 4, certificate_offset, 40)
    headers[section_table : section_table + len(table)] = table
    return bytes(headers + body + tail)


def test_parse_resolves_long_section_names_and_imports() -> None:
    pe = slim.parse_pe(build_pe(["libfoo.dll", "KERNEL32.dll"]))
    assert [s.name for s in pe.sections] == [".text", ".idata", ".debug_part0", ".debug_part1"]
    assert slim.imported_dlls(pe) == ["libfoo.dll", "KERNEL32.dll"]
    assert slim.imported_dlls(slim.parse_pe(build_pe())) == []


def test_parse_rejects_files_that_are_not_pe() -> None:
    with pytest.raises(slim.SlimError):
        slim.parse_pe(b"esto no es un ejecutable" * 10)
    with pytest.raises(slim.SlimError):
        slim.parse_pe(b"MZ" + bytes(0x100))


def _carpeta_tesseract(tmp_path: Path) -> Path:
    """tesseract.exe -> A, B(en mayúsculas distinta) ; A -> C ; D nadie la usa ; KERNEL32 es del sistema."""
    (tmp_path / "tessdata").mkdir()
    (tmp_path / "tessdata" / "spa.traineddata").write_bytes(b"datos")
    (tmp_path / "LICENSE-tesseract.txt").write_text("licencia", encoding="utf-8")
    (tmp_path / "tesseract.exe").write_bytes(build_pe(["libA.dll", "LIBB.DLL", "KERNEL32.dll"]))
    (tmp_path / "libA.dll").write_bytes(build_pe(["libC.dll", "msvcrt.dll"]))
    (tmp_path / "libB.dll").write_bytes(build_pe([]))
    (tmp_path / "libC.dll").write_bytes(build_pe([]))
    (tmp_path / "libD.dll").write_bytes(build_pe(["libA.dll"]))  # importa una necesaria, pero nadie la importa a ella
    return tmp_path


def test_find_reachable_follows_imports_case_insensitively(tmp_path: Path) -> None:
    carpeta = _carpeta_tesseract(tmp_path)
    assert slim.find_reachable(carpeta) == {"tesseract.exe", "liba.dll", "libb.dll", "libc.dll"}


def test_prune_unreachable_removes_only_unneeded_dlls(tmp_path: Path) -> None:
    carpeta = _carpeta_tesseract(tmp_path)
    borradas = slim.prune_unreachable(carpeta)
    assert [p.name for p, _size in borradas] == ["libD.dll"]
    assert sorted(p.name for p in carpeta.iterdir()) == [
        "LICENSE-tesseract.txt",
        "libA.dll",
        "libB.dll",
        "libC.dll",
        "tessdata",
        "tesseract.exe",
    ]
    assert (carpeta / "tessdata" / "spa.traineddata").is_file()


def test_prune_needs_the_entry_executable(tmp_path: Path) -> None:
    (tmp_path / "libA.dll").write_bytes(build_pe([]))
    with pytest.raises(slim.SlimError, match=r"tesseract\.exe"):
        slim.prune_unreachable(tmp_path)


def test_strip_debug_removes_debug_sections_and_symbols() -> None:
    original = build_pe(["libfoo.dll"])
    stripped = slim.strip_debug(original)
    assert stripped is not None
    assert len(stripped) == 0x600  # cabeceras + .text + .idata, sin depuración ni símbolos
    pe = slim.parse_pe(stripped)
    assert [s.name for s in pe.sections] == [".text", ".idata"]
    assert (pe.symbol_pointer, pe.symbol_count) == (0, 0)
    assert pe.data_directory(slim.DIR_IMPORT) == slim.parse_pe(original).data_directory(slim.DIR_IMPORT)
    assert slim.imported_dlls(pe) == ["libfoo.dll"]
    assert struct.unpack_from("<I", stripped, pe.optional_offset + 56)[0] == 3 * SECTION_ALIGN  # SizeOfImage
    assert struct.unpack_from("<I", stripped, pe.optional_offset + 64)[0] == 0  # CheckSum
    characteristics = struct.unpack_from("<H", stripped, pe.coff_offset + 18)[0]
    assert characteristics & slim.IMAGE_FILE_DEBUG_STRIPPED
    assert original[0x200:0x600] == stripped[0x200:0x600]  # el código y las importaciones no se tocan
    slim.verify_stripped(original, stripped)
    assert slim.strip_debug(stripped) is None  # ya está limpio


def test_strip_debug_drops_the_authenticode_certificate() -> None:
    original = build_pe(certificate=True)
    assert slim.parse_pe(original).data_directory(slim.DIR_SECURITY)[0] > 0
    stripped = slim.strip_debug(original)
    assert stripped is not None
    assert slim.parse_pe(stripped).data_directory(slim.DIR_SECURITY) == (0, 0)
    assert len(stripped) == 0x600  # la firma ya no vale: se elimina con el resto


def test_strip_debug_declines_when_it_is_not_safe() -> None:
    assert slim.strip_debug(build_pe(debug_sections=0)) is None  # nada que quitar
    assert slim.strip_debug(build_pe(debug_in_middle=True)) is None  # una sección de depuración no es de las últimas
    assert slim.strip_debug(build_pe(trailing=b"datos extra desconocidos")) is None  # no se sabe qué hay al final
    assert slim.strip_debug(build_pe(symbols=False, trailing=b"x" * 16)) is None
    limpio_sin_simbolos = slim.strip_debug(build_pe(symbols=False))
    assert limpio_sin_simbolos is not None and len(limpio_sin_simbolos) == 0x600


def test_verify_stripped_detects_corruption() -> None:
    original = build_pe(["libfoo.dll"])
    stripped = bytearray(slim.strip_debug(original))
    stripped[0x210] ^= 0xFF  # altera el código
    with pytest.raises(slim.SlimError, match="contenido"):
        slim.verify_stripped(original, bytes(stripped))
    with pytest.raises(slim.SlimError, match="secciones"):
        slim.verify_stripped(original, original)  # sin quitar nada: siguen las secciones de depuración
    con_simbolos = build_pe(["libfoo.dll"], debug_sections=0)
    with pytest.raises(slim.SlimError, match="símbolos"):
        slim.verify_stripped(con_simbolos, con_simbolos)  # sin depuración pero con tabla de símbolos


def test_strip_folder_replaces_files_and_leaves_no_temporaries(tmp_path: Path) -> None:
    carpeta = _carpeta_tesseract(tmp_path)
    (carpeta / "libB.dll").write_bytes(build_pe([], debug_sections=0))  # ya limpia: se salta
    (carpeta / "libB.dll").chmod(0o444)
    resultados = slim.strip_folder(carpeta)
    assert sorted(p.name for p, _old, _new in resultados) == ["libA.dll", "libC.dll", "libD.dll", "tesseract.exe"]
    assert all(new < old for _p, old, new in resultados)
    assert not list(carpeta.glob("*.slim"))
    assert slim.strip_debug((carpeta / "tesseract.exe").read_bytes()) is None
    assert slim.imported_dlls(slim.parse_pe((carpeta / "tesseract.exe").read_bytes())) == [
        "libA.dll",
        "LIBB.DLL",
        "KERNEL32.dll",
    ]


class FakeTesseract:
    """Sustituye a tesseract.exe en la prueba de funcionamiento."""

    def __init__(self, langs: str = "List of available languages (2):\neng\nspa\n", text: str = "FACTURA 12345\n"):
        self.langs, self.text, self.calls = langs, text, []

    def __call__(self, args, env, cwd=None) -> str:
        self.calls.append((list(args), env, cwd))
        return self.langs if args[-1] == "--list-langs" else self.text


def test_selftest_passes_with_a_working_tesseract(tmp_path: Path) -> None:
    fake = FakeTesseract()
    informe = slim.selftest(tmp_path, ["wine64"], fake)
    assert informe[0] == "idiomas spa y eng: OK"
    primera_llamada = fake.calls[0]
    assert primera_llamada[0][:2] == ["wine64", str(tmp_path / "tesseract.exe")]
    assert primera_llamada[1]["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")  # no depende de otra instalación
    if len(informe) > 1 and "OK" in informe[1]:
        assert fake.calls[1][2] is not None  # la imagen se lee con cwd = carpeta temporal


def test_selftest_fails_when_a_language_is_missing_or_reading_is_wrong(tmp_path: Path) -> None:
    with pytest.raises(slim.SlimError, match="spa"):
        slim.selftest(tmp_path, (), FakeTesseract(langs="eng\n"))
    pytest.importorskip("PIL")
    with pytest.raises(slim.SlimError, match="12345"):
        slim.selftest(tmp_path, (), FakeTesseract(text="algo distinto"))


def test_main_prunes_and_strips_but_can_skip_each_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "completa").mkdir()
    carpeta = _carpeta_tesseract(tmp_path / "completa")
    assert slim.main([str(carpeta), "--no-strip"]) == 0
    assert not (carpeta / "libD.dll").exists()
    assert slim.strip_debug((carpeta / "libA.dll").read_bytes()) is not None  # no se tocó la depuración
    assert slim.main([str(carpeta), "--no-prune"]) == 0
    assert slim.strip_debug((carpeta / "libA.dll").read_bytes()) is None
    salida = capsys.readouterr().out
    assert "1 borradas" in salida and "sin depuración" in salida

    vacia = tmp_path / "vacia"
    vacia.mkdir()
    assert slim.main([str(vacia)]) == 1
    assert "tesseract.exe" in capsys.readouterr().err
