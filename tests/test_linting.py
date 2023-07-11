# import re

# import pytest
# from pydantic import BaseModel, Field

# from tests._data.companies_schemas_scenario import versions
# from universi.exceptions import LintingError
# from universi.linting import lint_all_schemas
# from universi.structure.schemas import field, schema
# from universi.structure.versions import AbstractVersionChange, Version, Versions


# def test__linting__correct_schemas():
#     lint_all_schemas(versions)


# async def companies2_endpoint():
#     pass


# def make_schema_version_change(*alter_instructions):
#     class VersionChange(
#         AbstractVersionChange,
#         description="No description",
#         alter_instructions=alter_instructions,
#     ):
#         pass

#     return VersionChange


# def make_simple_versions(*alter_instructions):
#     return Versions(
#         Version("2001-01-01", make_schema_version_change(*alter_instructions)),
#         Version("2000-01-01"),
#     )


# class Company_2000(BaseModel):
#     name: str


# class Company_2001(BaseModel):
#     name: str


# def test__linting_field_existed_with__invalid__error():
#     with pytest.raises(
#         LintingError,
#         match=re.escape(
#             "Field vat_id not found in old model <class 'tests.test_linting.Company_2000'>",
#         ),
#     ):
#         lint_all_schemas(
#             make_simple_versions(
#                 schema(
#                     Company_2001,
#                     Company_2000,
#                     field("vat_id").existed_with(type=int, info=Field()),
#                 ),
#             ),
#         )


# def test__linting_field_didnt_exist__invalid__error():
#     with pytest.raises(
#         LintingError,
#         match=re.escape(
#             "Unexpected field name found in old model <class 'tests.test_linting.Company_2000'>",
#         ),
#     ):
#         lint_all_schemas(
#             make_simple_versions(
#                 schema(Company_2001, Company_2000, field("name").didnt_exist),
#             ),
#         )


# def test__linting_field_had__invalid__error():
#     with pytest.raises(
#         LintingError,
#         match=re.escape(
#             "Expected value PydanticUndefined doesn't match the field value Foo for attribute default in model <class 'tests.test_linting.Company_2000'>",
#         ),
#     ):
#         lint_all_schemas(
#             make_simple_versions(
#                 schema(Company_2001, Company_2000, field("name").had(default="Foo")),
#             ),
#         )
