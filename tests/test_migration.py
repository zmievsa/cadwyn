# from tests._data.companies_schemas_scenario import get_company_endpoint, versions
# from universi.structure.versions import Versions


# def test__migrate__with_no_migrations__should_not_raise_error():
#     assert Versions().data_to_version(
#         get_company_endpoint,
#         {"A": "B"},
#         "2000-01-01",
#     ) == {"A": "B"}


# def test__migrate_simple_data_one_version_down():
#     assert versions.data_to_version(
#         get_company_endpoint,
#         {"name": "HeliCorp", "_related_vat_ids": [{"value": "Foo"}, {"value": "Bar"}]},
#         "2001-01-01",
#     ) == {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}


# def test__migrate_simple_data_two_versions_down():
#     assert versions.data_to_version(
#         get_company_endpoint,
#         {"name": "HeliCorp", "_related_vat_ids": [{"value": "Foo"}, {"value": "Bar"}]},
#         "2000-01-01",
#     ) == {"name": "HeliCorp", "vat_id": "Foo"}
