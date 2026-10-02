"""Proves the import-linter contracts actually bite, not just that they pass today."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LINT_IMPORTS = str(Path(sys.executable).parent / "lint-imports")


def run_lint_imports(config: Path, src: Path) -> subprocess.CompletedProcess[str]:
    # PYTHONPATH precedes the editable install, so grimp analyses the copy under `src`.
    env = {**os.environ, "PYTHONPATH": str(src)}
    return subprocess.run(
        [LINT_IMPORTS, "--config", str(config), "--no-cache"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def source_copy(tmp_path: Path, api_root: Path) -> Path:
    src = tmp_path / "src"
    shutil.copytree(api_root / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
    return src


def test_the_committed_contracts_match_the_generator(repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "gen_import_contracts.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_current_codebase_honours_every_contract(api_root: Path, source_copy: Path) -> None:
    result = run_lint_imports(api_root / ".importlinter", source_copy)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("offending_file", "offending_import", "broken_contract"),
    [
        pytest.param(
            "modules/identity/application/leak.py",
            "from smarthome.modules.devices.domain import *",
            "identity reaches other modules only through their public.py",
            id="module-reaches-into-another-module",
        ),
        pytest.param(
            "modules/devices/domain/leak.py",
            "import sqlalchemy",
            "Domain layers import no frameworks and no shared infrastructure",
            id="domain-imports-a-framework",
        ),
        pytest.param(
            "modules/commands/domain/leak.py",
            "from smarthome.modules.commands.infrastructure import *",
            "Each module is layered api -> infrastructure -> application -> domain",
            id="domain-depends-on-its-own-infrastructure",
        ),
        pytest.param(
            "shared/leak.py",
            "from smarthome.modules.energy import public",
            "The shared kernel never depends on a module",
            id="shared-kernel-depends-on-a-module",
        ),
    ],
)
def test_a_forbidden_import_fails_the_build(
    api_root: Path,
    source_copy: Path,
    offending_file: str,
    offending_import: str,
    broken_contract: str,
) -> None:
    (source_copy / "smarthome" / offending_file).write_text(f"{offending_import}\n")

    result = run_lint_imports(api_root / ".importlinter", source_copy)

    assert result.returncode != 0
    assert f"{broken_contract} BROKEN" in result.stdout


def test_importing_another_modules_public_interface_is_allowed(
    api_root: Path, source_copy: Path
) -> None:
    (
        source_copy / "smarthome" / "modules" / "identity" / "application" / "uses_devices.py"
    ).write_text("from smarthome.modules.devices import public\n")

    result = run_lint_imports(api_root / ".importlinter", source_copy)

    assert result.returncode == 0, result.stdout
