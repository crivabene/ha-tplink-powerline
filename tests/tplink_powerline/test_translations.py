"""Every translation has the same keys and placeholders as English."""

import json
from pathlib import Path
import re

import pytest

TRANSLATIONS = Path(__file__).parents[2] / "custom_components" / "tplink_powerline" / "translations"


def flatten(data: dict, prefix: str = "") -> dict[str, str]:
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out |= flatten(value, f"{prefix}{key}.")
        else:
            out[f"{prefix}{key}"] = value
    return out


ENGLISH = flatten(json.loads((TRANSLATIONS / "en.json").read_text()))


@pytest.mark.parametrize("path", sorted(TRANSLATIONS.glob("*.json")), ids=lambda p: p.stem)
def test_same_keys_and_placeholders(path):
    other = flatten(json.loads(path.read_text()))
    assert other.keys() == ENGLISH.keys()
    for key, text in ENGLISH.items():
        assert set(re.findall(r"{\w+}", other[key])) == set(re.findall(r"{\w+}", text)), key
