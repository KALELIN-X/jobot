import pytest
from pydantic import ValidationError

from jobradar.autofill import plan_form
from jobradar.models import Profile


def control(label, field_id="field", name=""):
    return {"id": field_id, "label": label, "name": name, "type": "text", "tag": "input", "required": True, "visible": True, "value": ""}


def test_name_components_default_to_empty_and_are_not_parsed_from_full_name():
    profile = Profile(name="Alexandra Example")
    assert profile.first_name == profile.last_name == profile.preferred_first_name == ""
    fields = [control("First name", "first"), control("Last name", "last"), control("Preferred first name", "preferred")]
    plan = plan_form(fields, profile.model_dump())
    assert not plan.actions
    assert len(plan.blockers) == 3


def test_explicit_legal_and_preferred_name_fields_are_filled_separately():
    profile = Profile(name="Alexandra Example", first_name="Alexandra", last_name="Example", preferred_first_name="Alex")
    fields = [control("Full name", "full"), control("First name", "first"), control("Last name", "last"), control("Preferred first name", "preferred")]
    plan = plan_form(fields, profile.model_dump())
    assert not plan.blockers
    assert {action.field_id: action.value for action in plan.actions} == {"full": "Alexandra Example", "first": "Alexandra", "last": "Example", "preferred": "Alex"}


@pytest.mark.parametrize("label,name,key", [("Given name", "", "first_name"), ("", "first_name", "first_name"), ("Family name", "", "last_name"), ("Surname", "", "last_name"), ("", "lastName", "last_name"), ("", "preferred_first_name", "preferred_first_name"), ("", "preferredFirstName", "preferred_first_name")])
def test_explicit_name_aliases_use_the_corresponding_supplied_field(label, name, key):
    plan = plan_form([control(label, name=name)], {key: "Approved value"})
    assert plan.actions[0].value == "Approved value"
    assert not plan.blockers


def test_preferred_first_name_label_wins_over_generic_first_name_identifier():
    plan = plan_form([control("Preferred first name", name="first_name")], {"first_name": "Alexandra", "preferred_first_name": "Alex"})
    assert plan.actions[0].value == "Alex"
    missing = plan_form([control("Preferred first name", name="first_name")], {"first_name": "Alexandra"})
    assert not missing.actions
    assert len(missing.blockers) == 1


def test_full_name_is_not_constructed_from_components_and_preferred_name_is_not_inferred():
    plan = plan_form([control("Full name", "full"), control("Preferred first name", "preferred")], {"first_name": "Alexandra", "last_name": "Example"})
    assert not plan.actions
    assert len(plan.blockers) == 2


@pytest.mark.parametrize("key", ["first_name", "last_name", "preferred_first_name"])
def test_name_fields_trim_and_enforce_length_limit(key):
    assert getattr(Profile(**{key: " Alex "}), key) == "Alex"
    with pytest.raises(ValidationError):
        Profile(**{key: "x" * 161})
