from __future__ import annotations

import pytest

from sitemon import SigningKey


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()
