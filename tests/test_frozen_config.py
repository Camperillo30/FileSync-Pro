"""El ejecutable (.exe / .app) solo debe usar la configuración embebida.

Cada prueba arranca un Python aparte que simula estar congelado por PyInstaller
(`sys.frozen` y `sys._MEIPASS`) antes de importar la app, porque la configuración
se carga al importar el módulo.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

RAIZ = Path(__file__).resolve().parents[1]


def _correr(
    tmp_path: Path, frozen: bool, env_extra: dict, archivos_sueltos: bool, embebido_extra: dict | None = None
) -> dict:
    meipass = tmp_path / "meipass"
    meipass.mkdir(parents=True)
    embebido = {
        "FILESYNC_PRO_LICENSE_REDEEM_URL": "https://embebida.example/canje",
        "FILESYNC_PRO_LICENSE_SIGNING_SECRET": "secreto-embebido",
        **(embebido_extra or {}),
    }
    (meipass / "desktop_runtime_config.json").write_text(json.dumps(embebido), encoding="utf-8")
    trabajo = tmp_path / "trabajo"
    trabajo.mkdir()
    if archivos_sueltos:
        # Lo que un cliente podría dejar junto al .exe para saltarse la licencia.
        (trabajo / ".env").write_text("FILESYNC_PRO_DEV_MODE=1\n", encoding="utf-8")
        (trabajo / "desktop_runtime_config.local.json").write_text(
            json.dumps(
                {
                    "FILESYNC_PRO_LICENSE_REDEEM_URL": "https://atacante.example/canje",
                    "FILESYNC_PRO_LICENSE_SIGNING_SECRET": "secreto-del-atacante",
                }
            ),
            encoding="utf-8",
        )
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(RAIZ)!r})
        if {frozen!r}:
            sys.frozen = True
            sys._MEIPASS = {str(meipass)!r}
        import Organizador as O
        lm = O.LicenseManager.__new__(O.LicenseManager)
        lm.get_machine_name = lambda: "MI-PC-DEV"
        # Identificador fijo: en un contenedor sin MAC, uuid.getnode() cambia en cada proceso.
        O.LicenseManager.get_demo_device_id = staticmethod(lambda: "equipo-de-prueba")
        print(json.dumps({{
            "dev": lm.is_developer_access_enabled(),
            "huella": O.LicenseManager.get_dev_device_hash(),
            "url": O.LICENSE_REDEEM_URL,
            "secret": O.LICENSE_SIGNING_SECRET,
        }}))
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FILESYNC_PRO_", "WOMPI_"))}
    env.update(env_extra)
    env["HOME"] = str(tmp_path / "home")
    salida = subprocess.run(
        [sys.executable, "-c", script], cwd=trabajo, env=env, capture_output=True, text=True, timeout=120
    )
    assert salida.returncode == 0, salida.stderr
    return json.loads(salida.stdout.strip().splitlines()[-1])


def test_exe_ignores_env_vars_and_loose_files(tmp_path: Path) -> None:
    datos = _correr(
        tmp_path,
        frozen=True,
        env_extra={"FILESYNC_PRO_DEV_MODE": "1", "FILESYNC_PRO_DEV_HOSTNAMES": "MI-PC-DEV"},
        archivos_sueltos=True,
    )
    assert datos["dev"] is False
    assert datos["url"] == "https://embebida.example/canje"
    assert datos["secret"] == "secreto-embebido"


def test_dev_mode_from_source_needs_explicit_opt_in(tmp_path: Path) -> None:
    assert _correr(tmp_path / "a", frozen=False, env_extra={}, archivos_sueltos=False)["dev"] is False
    hostnames = {"FILESYNC_PRO_DEV_HOSTNAMES": "otro-pc, mi-pc-dev"}
    con_equipo = _correr(tmp_path / "b", frozen=False, env_extra=hostnames, archivos_sueltos=False)
    assert con_equipo["dev"] is True
    variable = {"FILESYNC_PRO_DEV_MODE": "1"}
    con_variable = _correr(tmp_path / "c", frozen=False, env_extra=variable, archivos_sueltos=False)
    assert con_variable["dev"] is True


def test_dev_exe_only_works_on_the_machine_it_was_built_for(tmp_path: Path) -> None:
    # La huella de este equipo, como la calcula build.ps1 -Dev.
    huella = _correr(tmp_path / "a", frozen=False, env_extra={}, archivos_sueltos=False)["huella"]
    assert len(huella) == 64

    # .exe de desarrollador hecho para este equipo: da acceso.
    propio = {"FILESYNC_PRO_DEV_DEVICE_HASHES": huella}
    assert _correr(tmp_path / "b", frozen=True, env_extra={}, archivos_sueltos=False, embebido_extra=propio)["dev"]

    # El mismo tipo de .exe hecho para OTRO equipo: no da acceso aquí.
    ajeno = {"FILESYNC_PRO_DEV_DEVICE_HASHES": "0" * 64}
    assert not _correr(tmp_path / "c", frozen=True, env_extra={}, archivos_sueltos=False, embebido_extra=ajeno)["dev"]

    # La huella no se puede meter desde afuera (variable de entorno) en un .exe normal.
    desde_afuera = {"FILESYNC_PRO_DEV_DEVICE_HASHES": huella}
    assert not _correr(tmp_path / "d", frozen=True, env_extra=desde_afuera, archivos_sueltos=True)["dev"]
