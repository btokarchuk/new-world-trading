from pathlib import Path

import pytest

from fixture_db import build_fixture


@pytest.fixture
def fixture_db(tmp_path: Path) -> tuple[Path, Path]:
    return build_fixture(tmp_path)
