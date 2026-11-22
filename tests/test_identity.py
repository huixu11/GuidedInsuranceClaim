from insurance_claims import identity
from insurance_claims.identity import MATCHED, MISMATCHED, NOT_PROVIDED


def test_dob_formats_normalise_to_iso():
    for spelling in ("1985-03-15", "03/15/1985", "3/15/1985", "March 15, 1985", "15 March 1985",
                     "Mar 15th 1985", "15.03.1985"):
        assert identity.normalize_dob(spelling) == "1985-03-15", spelling
    assert identity.normalize_dob("the fifteenth") is None
    assert identity.normalize_dob("1985-02-31") is None


def test_phone_email_last4_policy_normalisation():
    assert identity.normalize_phone("+1 (650) 521-2836") == "6505212836"
    assert identity.normalize_phone("650.521.2836") == "6505212836"
    assert identity.normalize_email(" Margaret@Email.com. ") == "margaret@email.com"
    assert identity.normalize_last4("123-45-4472") == "4472"  # a full SSN is reduced to its last four
    assert identity.normalize_last4("44") is None
    assert identity.normalize_policy("pol 9921") == identity.normalize_policy("POL-9921")


def test_name_matching_tolerates_case_order_and_spacing():
    assert identity.names_match("margaret chen", "Margaret Chen")
    assert identity.names_match("Chen, Margaret", "Margaret Chen")
    assert identity.names_match("Yawen Li", "Ya Wen Li")
    assert not identity.names_match("Margaret Chan", "Margaret Chen")
    assert not identity.names_match("Margaret", "Margaret Chen")


def test_three_matching_identifiers_verify(repo):
    result = identity.evaluate(
        {"full_name": "Margaret Chen", "dob": "March 15, 1985", "id_last4": "4472"}, repo
    )
    assert result.party_id == "P9"
    assert result.matched_count == 3
    assert result.field_status["phone"] == NOT_PROVIDED


def test_policy_number_locates_but_does_not_count(repo):
    result = identity.evaluate({"full_name": "Margaret Chen", "dob": "1985-03-15",
                                "policy_number": "POL-9921"}, repo)
    assert result.policy_status == MATCHED
    assert result.matched_count == 2  # still one short of the required three


def test_aliases_and_alternate_id_type(repo):
    result = identity.evaluate(
        {"full_name": "Yaven Li", "email": "yawen.li@example.com", "id_last4": "5317"}, repo
    )
    assert result.party_id == "P13" and result.matched_count == 3
    ma = identity.evaluate({"full_name": "Ma Tian", "id_last4": "6688", "phone": "650 208 8799"}, repo)
    assert ma.party_id == "P12" and ma.matched_count == 3  # national ID last 4 works like SSN last 4


def test_identifiers_from_two_people_never_add_up(repo):
    # Margaret's name + Ava's date of birth and SSN digits: no single record has three matches.
    result = identity.evaluate(
        {"full_name": "Margaret Chen", "dob": "1990-08-21", "id_last4": "9180"}, repo
    )
    assert result.matched_count < 3
    assert MISMATCHED in result.field_status.values()


def test_unknown_person_matches_nothing(repo):
    result = identity.evaluate({"full_name": "John Smith", "dob": "1970-01-01"}, repo)
    assert not result.record_found and result.party_id is None
    assert result.mismatched == ["full_name", "dob"]


def test_email_is_masked_for_display():
    assert identity.mask_email("margaret@email.com") == "m•••@email.com"
