from __future__ import annotations

import pytest


@pytest.mark.soak
def test_soak_scaffold() -> None:
    pytest.skip("Soak tests are hardware-in-the-loop and run from scripts/run_soak_test.ps1")
