from datetime import date
from typing import Any

import pytest

from universi import api_version_var
from universi.structure import (
    AbstractVersionChange,
    Version,
    Versions,
    convert_response_to_previous_version_for,
)


async def test_endpoint():
    pass


class VersionChange1(AbstractVersionChange):
    description = "Change vat id to list"
    instructions_to_migrate_to_previous_version = ()

    @convert_response_to_previous_version_for(test_endpoint)
    def change_vat_ids_to_list(cls, data: dict[str, Any]) -> None:
        data["vat_ids"] = [id["value"] for id in data.pop("_prefetched_vat_ids")]


class VersionChange2(AbstractVersionChange):
    description = "Change vat ids to str"
    instructions_to_migrate_to_previous_version = ()

    @convert_response_to_previous_version_for(test_endpoint)
    def change_vat_ids_to_single_item(cls, data: dict[str, Any]) -> None:
        data["vat_id"] = data.pop("vat_ids")[0]


def test__migrate__with_no_migrations__should_not_raise_error():
    assert Versions().data_to_version(
        test_endpoint,
        {"A": "B"},
        date(2000, 1, 1),
    ) == {"A": "B"}


def test__migrate_simple_data_one_version_down():
    versions = Versions(
        Version(date(2002, 1, 1), VersionChange1),
        Version(date(2001, 1, 1)),
    )
    assert versions.data_to_version(
        test_endpoint,
        {
            "name": "HeliCorp",
            "_prefetched_vat_ids": [{"value": "Foo"}, {"value": "Bar"}],
        },
        date(2001, 1, 1),
    ) == {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}


def test__migrate_simple_data_one_version_down__with_some_inapplicable_migrations__result_is_unaffected():
    async def test_endpoint2():
        raise NotImplementedError

    class VersionChange3(AbstractVersionChange):
        description = "Change vat ids to str"
        instructions_to_migrate_to_previous_version = ()

        @convert_response_to_previous_version_for(test_endpoint2)
        def break_vat_ids(cls, data: dict[str, Any]) -> None:
            raise NotImplementedError

    versions = Versions(
        Version(date(2002, 1, 1), VersionChange1, VersionChange3),
        Version(date(2001, 1, 1)),
    )
    assert versions.data_to_version(
        test_endpoint,
        {
            "name": "HeliCorp",
            "_prefetched_vat_ids": [{"value": "Foo"}, {"value": "Bar"}],
        },
        date(2001, 1, 1),
    ) == {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}


def test__migrate_simple_data_two_versions_down():
    versions = Versions(
        Version(date(2002, 1, 1), VersionChange1),
        Version(date(2001, 1, 1), VersionChange2),
        Version(date(2000, 1, 1)),
    )
    assert versions.data_to_version(
        test_endpoint,
        {
            "name": "HeliCorp",
            "_prefetched_vat_ids": [{"value": "Foo"}, {"value": "Bar"}],
        },
        date(2000, 1, 1),
    ) == {"name": "HeliCorp", "vat_id": "Foo"}


@pytest.mark.parametrize("api_version", [None, date(2001, 1, 1)])
async def test__versioned_decorator__with_latest_version__response_is_unchanged(
    api_version: date | None,
):
    versions = Versions(
        Version(date(2001, 1, 1), VersionChange2),
        Version(date(2000, 1, 1)),
    )

    @versions.versioned(test_endpoint)
    async def test():
        return {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}

    api_version_var.set(api_version)
    assert await test() == {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}


async def test__versioned_decorator__with_earlier_version__response_is_migrated():
    versions = Versions(
        Version(date(2001, 1, 1), VersionChange2),
        Version(date(2000, 1, 1)),
    )

    @versions.versioned(test_endpoint)
    async def test():
        return {"name": "HeliCorp", "vat_ids": ["Foo", "Bar"]}

    api_version_var.set(date(2000, 1, 1))
    assert await test() == {"name": "HeliCorp", "vat_id": "Foo"}
