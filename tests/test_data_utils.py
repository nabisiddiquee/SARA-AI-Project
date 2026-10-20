from my_python_project.data_utils import normalize_customer_data


def test_moves_company_name_to_org_name():
    out = normalize_customer_data({"id": 1, "companyName": "Acme"})
    assert out == {"id": 1, "orgName": "Acme"}


def test_prefers_existing_org_name():
    out = normalize_customer_data({"companyName": "Acme", "orgName": "Acme Inc"})
    assert out == {"orgName": "Acme Inc"}


def test_passthrough_when_already_normalized():
    src = {"id": 2, "orgName": "FinAgg"}
    assert normalize_customer_data(src) == src


def test_does_not_mutate_input():
    src = {"companyName": "Acme"}
    normalize_customer_data(src)
    assert src == {"companyName": "Acme"}
