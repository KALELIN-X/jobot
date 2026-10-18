import pytest
from pydantic import ValidationError

from jobradar.models import Profile


def test_approved_work_locations_are_empty_until_explicitly_provided():
    assert Profile(location="Home City").approved_work_locations == []


def test_approved_work_locations_are_trimmed_and_deduplicated():
    profile = Profile(approved_work_locations=[" Target City ", "Target City", "Another City"])
    assert profile.approved_work_locations == ["Target City", "Another City"]


@pytest.mark.parametrize("locations", [[" "], ["x" * 201], ["City"] * 31, [123], "Target City", None])
def test_approved_work_locations_reject_invalid_values(locations):
    with pytest.raises(ValidationError):
        Profile(approved_work_locations=locations)
