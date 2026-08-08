import pytest

from app.sources.overpass import OverpassSource


def test_activity_is_allowlisted_before_query_building():
    with pytest.raises(ValueError):
        OverpassSource._safe_activity('office"];out;')


def test_city_rejects_query_control_characters():
    with pytest.raises(ValueError):
        OverpassSource._safe_city('Madrid"];out;')
