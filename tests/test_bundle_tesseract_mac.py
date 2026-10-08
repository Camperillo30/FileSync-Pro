# Pruebas de tools/bundle_tesseract_mac.py (empaquetado de Tesseract en el .app de macOS).
# Corren en cualquier sistema: `FakeMac` imita otool / install_name_tool / lipo / codesign sobre
# archivos de mentira, con una instalación tipo Homebrew (Cellar + enlaces `opt`, `@rpath`, etc.).
# Lo que NO se puede probar aquí es el comportamiento real de dyld y codesign; eso lo comprueba
# el paso "Verificar Tesseract empaquetado" del flujo de GitHub Actions en un Mac real.
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "bundle_tesseract_mac.py"
_SPEC = importlib.util.spec_from_file_location("bundle_tesseract_mac", _SCRIPT)
bundle = importlib.util.module_from_spec(_SPEC)
sys.modules["bundle_tesseract_mac"] = bundle
_SPEC.loader.exec_module(bundle)


def _line(dep: str) -> str:
    return f"\t{dep} (compatibility version 1.0.0, current version 1.0.0)"


class FakeMac:
    """Simula las herramientas de línea de comandos de macOS. El "contenido" de cada archivo es un texto
    que identifica su metadatos (id, dependencias, rpaths); `install_name_tool` los modifica por ruta."""

    def __init__(self) -> None:
        self.by_token: dict[bytes, dict] = {}
        self.meta: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.archs: dict[bytes, str] = {}
        self.default_arch = "arm64"
        self.verify_fails = False

    def add(self, path: Path, *, install_id=None, deps=(), rpaths=(), archs=None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = path.name.encode()
        path.write_bytes(token)
        self.by_token[token] = {"id": install_id, "deps": list(deps), "rpaths": list(rpaths)}
        if archs:
            self.archs[token] = archs

    def state(self, path: str | Path) -> dict:
        key = str(path)
        if key not in self.meta:
            self.meta[key] = copy.deepcopy(self.by_token[Path(key).read_bytes()])
        return self.meta[key]

    def __call__(self, args, env=None) -> str:
        args = [str(a) for a in args]
        self.calls.append(args)
        tool = Path(args[0]).name
        if tool == "otool":
            meta = self.state(args[-1])
            if args[1] == "-L":
                lines = [f"{args[-1]}:"]
                if meta["id"]:
                    lines.append(_line(meta["id"]))
                return "\n".join([*lines, *(_line(d) for d in meta["deps"])])
            if args[1] == "-D":
                return f"{args[-1]}:\n{meta['id']}" if meta["id"] else f"{args[-1]}:"
            return "\n".join(
                f"Load command 9\n          cmd LC_RPATH\n      cmdsize 48\n         path {r} (offset 12)"
                for r in meta["rpaths"]
            )
        if tool == "install_name_tool":
            meta = self.state(args[-1])
            it = iter(args[1:-1])
            for flag in it:
                if flag == "-id":
                    meta["id"] = next(it)
                elif flag == "-change":
                    old, new = next(it), next(it)
                    assert old in meta["deps"], f"{old} no es dependencia de {args[-1]}"
                    meta["deps"] = [new if d == old else d for d in meta["deps"]]
                elif flag == "-delete_rpath":
                    meta["rpaths"].remove(next(it))
            return ""
        if tool == "lipo":
            if args[1] == "-archs":
                return self.archs.get(Path(args[2]).read_bytes(), self.default_arch)
            Path(args[-1]).write_bytes(Path(args[1]).read_bytes())  # lipo ORIGEN -thin ARQ -output DESTINO
            return ""
        if tool == "codesign":
            if "--verify" in args and self.verify_fails:
                raise bundle.BundleError("codesign: firma inválida")
            return ""
        if args[-1] == "--list-langs":
            return "List of available languages (2):\neng\nspa\n"
        return "FACTURA 12345\n"  # lectura de la imagen de prueba


@pytest.fixture
def brew(tmp_path: Path):
    """Instalación tipo Homebrew: tesseract -> libtesseract, libleptonica (-> libpng por @rpath) + libs del sistema."""
    fake = FakeMac()
    root = tmp_path / "homebrew"
    opt = root / "opt"
    cellar = root / "Cellar"
    tess_lib = f"{opt}/tesseract/lib/libtesseract.5.dylib"
    lept_lib = f"{opt}/leptonica/lib/libleptonica.6.dylib"
    png_lib = f"{opt}/libpng/lib/libpng16.16.dylib"

    fake.add(
        cellar / "tesseract" / "5.5" / "bin" / "tesseract",
        deps=["/usr/lib/libSystem.B.dylib", tess_lib, lept_lib],
    )
    fake.add(
        cellar / "tesseract" / "5.5" / "lib" / "libtesseract.5.dylib",
        install_id=tess_lib,
        deps=[lept_lib, "/usr/lib/libc++.1.dylib"],
    )
    real_lept = cellar / "leptonica" / "1.85" / "lib" / "libleptonica.6.0.0.dylib"
    fake.add(
        real_lept,
        install_id=lept_lib,
        deps=["@rpath/libpng16.16.dylib", "/usr/lib/libz.1.dylib"],
        rpaths=["@loader_path/../../../../opt/libpng/lib"],
    )
    (real_lept.parent / "libleptonica.6.dylib").symlink_to(real_lept.name)
    fake.add(
        cellar / "libpng" / "1.6" / "lib" / "libpng16.16.dylib",
        install_id=png_lib,
        deps=["/usr/lib/libz.1.dylib"],
    )
    opt.mkdir(parents=True)
    for name, version in (("tesseract", "5.5"), ("leptonica", "1.85"), ("libpng", "1.6")):
        (opt / name).symlink_to(cellar / name / version)
    (root / "bin").mkdir()
    (root / "bin" / "tesseract").symlink_to(cellar / "tesseract" / "5.5" / "bin" / "tesseract")

    app = tmp_path / "dist" / "FileSync Pro.app"
    fake.add(app / "Contents" / "MacOS" / "FileSync Pro")
    (app / "Contents" / "Frameworks").mkdir()
    (app / "Contents" / "Resources").mkdir()
    return fake, root / "bin" / "tesseract", app


def _fake_fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * (bundle.MIN_TRAINEDDATA_BYTES + 1) if "traineddata" in url else b"licencia")


def test_parse_otool_outputs() -> None:
    salida = (
        "/opt/homebrew/bin/tesseract:\n"
        "\t/opt/homebrew/opt/leptonica/lib/libleptonica.6.dylib (compatibility version 7.0.0, current version 7.2.0)\n"
        "\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1900.180.0)\n"
    )
    assert bundle.parse_otool_deps(salida) == [
        "/opt/homebrew/opt/leptonica/lib/libleptonica.6.dylib",
        "/usr/lib/libc++.1.dylib",
    ]
    salida_id = "/x/libA.dylib:\n/opt/homebrew/opt/a/lib/libA.dylib\n"
    assert bundle.parse_otool_id(salida_id) == "/opt/homebrew/opt/a/lib/libA.dylib"
    assert bundle.parse_otool_id("/bin/tesseract:\n") is None
    cargas = (
        "Load command 20\n          cmd LC_RPATH\n      cmdsize 48\n"
        "         path @loader_path/../../../../opt/x/lib (offset 12)\n"
        "Load command 21\n          cmd LC_LOAD_DYLIB\n         name /usr/lib/libz.1.dylib (offset 24)\n"
        "Load command 22\n          cmd LC_RPATH\n      cmdsize 32\n         path /opt/homebrew/lib (offset 12)\n"
    )
    assert bundle.parse_otool_rpaths(cargas) == ["@loader_path/../../../../opt/x/lib", "/opt/homebrew/lib"]
    assert bundle.is_system_dep("/usr/lib/libz.1.dylib")
    assert bundle.is_system_dep("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    assert not bundle.is_system_dep("/opt/homebrew/lib/libpng.dylib")
    assert not bundle.is_system_dep("/usr/local/lib/libpng.dylib")


def test_collect_dependencies_follows_symlinks_and_rpaths(brew) -> None:
    fake, tesseract, _app = brew
    plan = bundle.collect_dependencies(tesseract, fake)
    assert set(plan.libs) == {"libtesseract.5.dylib", "libleptonica.6.dylib", "libpng16.16.dylib"}
    # Los archivos de origen son los reales (no los enlaces): sirve para resolver `@loader_path` sin errores.
    assert plan.libs["libleptonica.6.dylib"].name == "libleptonica.6.0.0.dylib"
    assert plan.libs["libpng16.16.dylib"].parts[-4:] == ("libpng", "1.6", "lib", "libpng16.16.dylib")
    # El ejecutable y cada biblioteca solo referencian bibliotecas de terceros; las del sistema no aparecen.
    assert set(plan.references[bundle.EXE_KEY].values()) == {"libtesseract.5.dylib", "libleptonica.6.dylib"}
    assert plan.references["libleptonica.6.dylib"] == {"@rpath/libpng16.16.dylib": "libpng16.16.dylib"}
    assert plan.references["libpng16.16.dylib"] == {}
    assert plan.rpaths["libleptonica.6.dylib"] == ["@loader_path/../../../../opt/libpng/lib"]


def test_collect_dependencies_uses_search_dirs_and_reports_missing(brew, tmp_path: Path) -> None:
    fake, tesseract, _app = brew
    (tmp_path / "homebrew" / "Cellar" / "libpng" / "1.6" / "lib" / "libpng16.16.dylib").unlink()
    with pytest.raises(bundle.BundleError, match="libpng16.16.dylib"):
        bundle.collect_dependencies(tesseract, fake)
    # Con un directorio de respaldo donde sí está, se resuelve.
    extra = tmp_path / "otras_libs"
    fake.add(extra / "libpng16.16.dylib", install_id="/x/libpng16.16.dylib")
    plan = bundle.collect_dependencies(tesseract, fake, [extra])
    assert plan.libs["libpng16.16.dylib"] == (extra / "libpng16.16.dylib").resolve()


def test_bundle_places_and_relocates_everything(brew) -> None:
    fake, tesseract, app = brew
    bundle.bundle_tesseract(
        app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _msg: None
    )
    code = app / "Contents" / "Frameworks" / "tesseract"
    assert (code / "tesseract").is_file()
    assert {p.name for p in (code / "lib").iterdir()} == {
        "libtesseract.5.dylib",
        "libleptonica.6.dylib",
        "libpng16.16.dylib",
    }
    # Idiomas: en Resources, enlazados desde Frameworks (el .app queda con el mismo esquema que PyInstaller).
    assert (code / "tessdata").is_symlink()
    assert (code / "tessdata" / "spa.traineddata").is_file()
    assert (code / "tessdata" / "eng.traineddata").is_file()
    assert (app / "Contents" / "Resources" / "tesseract" / "tessdata" / "spa.traineddata").is_file()
    assert (app / "Contents" / "Resources" / "tesseract" / "LICENSE-tesseract.txt").is_file()

    # Nada apunta ya a Homebrew: el ejecutable mira en ./lib y las bibliotecas, a sus vecinas.
    exe_state = fake.state(code / "tesseract")
    assert sorted(exe_state["deps"]) == [
        "/usr/lib/libSystem.B.dylib",
        "@loader_path/lib/libleptonica.6.dylib",
        "@loader_path/lib/libtesseract.5.dylib",
    ]
    lept = fake.state(code / "lib" / "libleptonica.6.dylib")
    assert lept["id"] == "@loader_path/libleptonica.6.dylib"
    assert sorted(lept["deps"]) == ["/usr/lib/libz.1.dylib", "@loader_path/libpng16.16.dylib"]
    assert lept["rpaths"] == []  # se eliminó el rpath que apuntaba a Homebrew
    assert fake.state(code / "lib" / "libtesseract.5.dylib")["deps"][0] == "@loader_path/libleptonica.6.dylib"
    # Las copias son ejecutables/escribibles aunque Homebrew las deje de solo lectura.
    assert (code / "lib" / "libpng16.16.dylib").stat().st_mode & 0o200

    assert bundle.verify_bundle(app, fake, ocr_test=False) == []


def test_bundle_signs_libraries_then_executable_then_app(brew) -> None:
    fake, tesseract, app = brew
    bundle.bundle_tesseract(
        app,
        tesseract,
        identity="Developer ID Application: Ejemplo (ABCDE12345)",
        entitlements="entitlements.plist",
        runner=fake,
        fetch=_fake_fetch,
        tessdata_dirs=[],
        log=lambda _msg: None,
    )
    firmas = [c for c in fake.calls if c[0].endswith("codesign") and "--verify" not in c]
    objetivos = [Path(c[-1]).name for c in firmas]
    assert objetivos[-1] == "FileSync Pro.app"
    assert objetivos[-2] == "tesseract"
    assert sorted(objetivos[:-2]) == ["libleptonica.6.dylib", "libpng16.16.dylib", "libtesseract.5.dylib"]
    assert "--deep" in firmas[-1]
    assert all("--options=runtime" in c and "--entitlements" in c for c in firmas)
    # Ninguna reescritura ocurre después de la primera firma (cambiar un binario invalida su firma).
    primera_firma = fake.calls.index(firmas[0])
    assert not any(c[0] == "install_name_tool" for c in fake.calls[primera_firma:])


def test_codesign_is_ad_hoc_without_identity() -> None:
    args = bundle.codesign_args(Path("x"), None)
    assert args[:3] == ["/usr/bin/codesign", "-s", "-"]
    assert "--options=runtime" not in args
    assert "--deep" not in args
    assert bundle.codesign_args(Path("x"), "ID", deep=True)[-2:] == ["--deep", "x"]


def test_build_rewrite_args() -> None:
    assert bundle.build_rewrite_args(Path("f"), new_id=None, changes={}, delete_rpaths=[]) == []
    args = bundle.build_rewrite_args(
        Path("f"), new_id="@loader_path/f", changes={"/a": "@loader_path/a"}, delete_rpaths=["/r"]
    )
    assert args == [
        "install_name_tool",
        "-id",
        "@loader_path/f",
        "-change",
        "/a",
        "@loader_path/a",
        "-delete_rpath",
        "/r",
        "f",
    ]


def test_verify_bundle_detects_dependencies_left_outside_the_package(brew) -> None:
    fake, tesseract, app = brew
    bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    code = app / "Contents" / "Frameworks" / "tesseract"
    fake.state(code / "lib" / "libleptonica.6.dylib")["deps"].append("/opt/homebrew/opt/webp/lib/libwebp.7.dylib")
    fake.state(code / "tesseract")["deps"].append("@loader_path/lib/libfalta.dylib")
    problems = bundle.verify_bundle(app, fake, ocr_test=False)
    assert any("libwebp.7.dylib" in p and "fuera del paquete" in p for p in problems)
    assert any("libfalta.dylib" in p for p in problems)


def test_verify_bundle_requires_languages_and_a_working_executable(brew) -> None:
    fake, tesseract, app = brew
    assert bundle.verify_bundle(app, fake) == [f"Falta {app / 'Contents' / 'Frameworks' / 'tesseract' / 'tesseract'}"]
    bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    (app / "Contents" / "Resources" / "tesseract" / "tessdata" / "spa.traineddata").unlink()
    assert any("spa" in p for p in bundle.verify_bundle(app, fake, ocr_test=False))


def test_verify_runs_tesseract_with_a_clean_environment(brew) -> None:
    fake, tesseract, app = brew
    bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    entornos = []
    original = fake.__call__

    def espia(args, env=None):
        if env is not None:
            entornos.append(env)
        return original(args, env=env)

    assert bundle.verify_bundle(app, espia, ocr_test=False) == []
    assert entornos
    assert all(e["PATH"] == "/usr/bin:/bin" and "DYLD_LIBRARY_PATH" not in e for e in entornos)
    assert entornos[0]["TESSDATA_PREFIX"].endswith("tessdata")


def test_architecture_checks() -> None:
    assert bundle.check_architecture(["arm64"], ["arm64"]) == "arm64"
    assert bundle.check_architecture(["x86_64", "arm64"], ["arm64"]) == "arm64"
    with pytest.raises(bundle.BundleError, match="x86_64"):
        bundle.check_architecture(["x86_64"], ["arm64"])
    with pytest.raises(bundle.BundleError, match="universal2"):
        bundle.check_architecture(["arm64"], ["arm64", "x86_64"])


def test_bundle_refuses_a_tesseract_of_another_architecture(brew) -> None:
    fake, tesseract, app = brew
    fake.archs[b"tesseract"] = "x86_64"
    with pytest.raises(bundle.BundleError, match="x86_64"):
        bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)


def test_fat_binaries_are_thinned_to_the_app_architecture(brew) -> None:
    fake, tesseract, app = brew
    fake.archs[b"libpng16.16.dylib"] = "x86_64 arm64"
    bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    thin = [c for c in fake.calls if c[0] == "lipo" and c[1] != "-archs"]
    assert len(thin) == 1
    assert thin[0][2:4] == ["-thin", "arm64"]
    assert (app / "Contents" / "Frameworks" / "tesseract" / "lib" / "libpng16.16.dylib").is_file()


def test_bundle_is_repeatable(brew) -> None:
    fake, tesseract, app = brew
    for _ in range(2):
        fake.meta.clear()  # cada corrida empieza con los archivos recién copiados, como en un Mac real
        bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    assert bundle.verify_bundle(app, fake, ocr_test=False) == []


def test_bundle_copies_notices_and_survives_a_missing_license_download(brew, tmp_path: Path) -> None:
    fake, tesseract, app = brew
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("avisos", encoding="utf-8")

    def fetch(url: str, dest: Path) -> None:
        if "LICENSE" in url:
            raise bundle.BundleError("sin red")
        _fake_fetch(url, dest)

    mensajes: list[str] = []
    bundle.bundle_tesseract(
        app, tesseract, notices=notices, runner=fake, fetch=fetch, tessdata_dirs=[], log=mensajes.append
    )
    assert (app / "Contents" / "Resources" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8") == "avisos"
    assert any("licencia" in m for m in mensajes)


def test_assemble_tessdata_prefers_installed_files_and_downloads_the_rest(tmp_path: Path) -> None:
    origen = tmp_path / "share" / "tessdata"
    (origen / "configs").mkdir(parents=True)
    (origen / "configs" / "pdf").write_text("cfg", encoding="utf-8")
    (origen / "eng.traineddata").write_bytes(b"e" * (bundle.MIN_TRAINEDDATA_BYTES + 5))
    (origen / "spa.traineddata").write_bytes(b"corto")  # incompleto: debe descargarse de nuevo
    descargas: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        descargas.append(url)
        _fake_fetch(url, dest)

    destino = tmp_path / "out"
    bundle.assemble_tessdata(destino, [origen], fetch, lambda _m: None)
    assert (destino / "configs" / "pdf").read_text(encoding="utf-8") == "cfg"
    assert (destino / "eng.traineddata").stat().st_size == bundle.MIN_TRAINEDDATA_BYTES + 5
    assert descargas == [bundle.TESSDATA_FAST_URL.format(lang="spa")]


def test_assemble_tessdata_fails_if_a_download_is_incomplete(tmp_path: Path) -> None:
    def fetch(_url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"<html>404</html>")

    with pytest.raises(bundle.BundleError, match="spa|eng"):
        bundle.assemble_tessdata(tmp_path / "out", [], fetch, lambda _m: None)


def test_find_tessdata_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle, "TESSDATA_CANDIDATES", ())
    exe = tmp_path / "Cellar" / "tesseract" / "5.5" / "bin" / "tesseract"
    propio = exe.parent.parent / "share" / "tessdata"
    propio.mkdir(parents=True)
    assert bundle.find_tessdata_dirs(exe, {}) == []  # sin idiomas no cuenta
    (propio / "eng.traineddata").write_bytes(b"x")
    del_env = tmp_path / "datos"
    del_env.mkdir()
    (del_env / "spa.traineddata").write_bytes(b"x")
    assert bundle.find_tessdata_dirs(exe, {"TESSDATA_PREFIX": str(del_env)}) == [del_env, propio]


def test_find_tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle.shutil, "which", lambda _name: None)
    monkeypatch.setattr(bundle, "TESSERACT_CANDIDATES", ())
    with pytest.raises(bundle.BundleError, match="brew install tesseract"):
        bundle.find_tesseract(None, {})
    binario = tmp_path / "bin" / "tesseract"
    binario.parent.mkdir()
    binario.write_bytes(b"x")
    assert bundle.find_tesseract(str(binario), {}) == binario.resolve()
    assert bundle.find_tesseract(None, {"TESSERACT_BIN": str(binario)}) == binario.resolve()


def test_run_command_returns_output_and_reports_failures() -> None:
    assert bundle.run_command([sys.executable, "-c", "print('hola')"]).strip() == "hola"
    with pytest.raises(bundle.BundleError, match="Falló el comando"):
        bundle.run_command([sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(bundle.BundleError):
        bundle.run_command(["/no/existe/nada"])


@pytest.mark.skipif(sys.platform == "darwin", reason="en un Mac el script sí funciona")
def test_main_refuses_to_run_outside_macos(capsys: pytest.CaptureFixture[str]) -> None:
    assert bundle.main(["--check"]) == 1
    assert "macOS" in capsys.readouterr().err


def test_layout_matches_what_the_app_looks_for(brew) -> None:
    """Contrato entre el script y `Organizador.configure_ocr`: en un .app de PyInstaller `sys._MEIPASS` es
    `Contents/Frameworks`, y desde ahí la app busca `tesseract/tesseract` y `tesseract/tessdata`."""
    from filesync_core.platform_compat import find_bundled_tesseract

    fake, tesseract, app = brew
    bundle.bundle_tesseract(app, tesseract, runner=fake, fetch=_fake_fetch, tessdata_dirs=[], log=lambda _m: None)
    meipass = app / "Contents" / "Frameworks"

    def resolver(*parts: str):
        candidate = meipass.joinpath(*parts)
        return candidate if candidate.exists() else None

    comando, tessdata = find_bundled_tesseract(resolver, windows=False)
    assert Path(comando) == meipass / "tesseract" / "tesseract"
    assert Path(tessdata) == meipass / "tesseract" / "tessdata"
    assert (Path(tessdata) / "spa.traineddata").is_file()  # a través del enlace hacia Resources
