"""Unit tests for the page capability manifest."""

from __future__ import annotations

import pytest

from octop.infra.apps import capabilities as caps
from octop.infra.errors import ErrorCode, OctopError


def test_empty_value_is_the_legacy_all_on_set() -> None:
    assert caps.parse("") == caps.LEGACY_CAPABILITIES
    assert caps.parse(None) == caps.LEGACY_CAPABILITIES


def test_new_apps_get_the_safe_default() -> None:
    assert caps.CAP_DATA_READ in caps.NEW_APP_CAPABILITIES
    assert caps.CAP_DATA_WRITE in caps.NEW_APP_CAPABILITIES
    assert caps.CAP_AI_GENERATE not in caps.NEW_APP_CAPABILITIES
    assert caps.CAP_GAME_LAUNCH not in caps.NEW_APP_CAPABILITIES
    assert caps.CAP_EXTERNAL_FETCH not in caps.NEW_APP_CAPABILITIES
    assert caps.CAP_EVENTS_EMIT not in caps.NEW_APP_CAPABILITIES


def test_parse_drops_unknown_names_and_denies_malformed_values() -> None:
    assert caps.parse('["data.read","future.capability"]') == {caps.CAP_DATA_READ}
    assert caps.parse("not json") == frozenset()
    assert caps.parse('{"data.read": true}') == frozenset()
    assert caps.parse("[]") == frozenset()


def test_dump_is_stable_and_validates() -> None:
    assert caps.dump([caps.CAP_DATA_WRITE, caps.CAP_DATA_READ]) == ('["data.read","data.write"]')
    with pytest.raises(OctopError) as exc:
        caps.validate(["data.read", "nope"])
    assert exc.value.code is ErrorCode.APP_CAPABILITY_INVALID
