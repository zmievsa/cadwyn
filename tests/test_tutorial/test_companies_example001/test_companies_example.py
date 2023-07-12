from datetime import date
from typing import Any

from tests.test_tutorial.test_companies_example001.schemas.latest.companies import (
    BaseCompany,
    CompanyCreateRequest,
    CompanyResource,
    CompanyVATIDResourceList,
)
from universi import Field
from universi.routing import VersionedAPIRouter
from universi.structure.endpoints import endpoint
from universi.structure.responses import convert_response_to_previous_version_for
from universi.structure.schemas import field, schema
from universi.structure.versions import AbstractVersionChange, Version, Versions

router = VersionedAPIRouter()


@router.post("/companies", response_model=CompanyResource)
async def create_company(company: CompanyCreateRequest):
    return {
        "id": 83,
        "name": "Company 1",
        "vat_id": company.vat_id,
    }


@router.get("/companies/{company_id}", response_model=CompanyResource)
async def get_company(company_id: int):
    return {
        "id": company_id,
        "name": "Company 1",
    }


# Please, never do this in any code. This is just for the demo.
COMPANY_DB = {}


class CompaniesScenario:
    def create_company(self, company: CompanyCreateRequest):
        pass

    def get_company(self):
        pass


# TODO: NEed to validate that the user doesn't use versioned schemas instead of the latest ones
@router.get("/companies/{company_id}/vat_ids", response_model=CompanyVATIDResourceList)
async def get_company_vat_ids(company_id: int):
    return [{"id": 83, "value": "First VAT ID"}, {"id": 91, "value": "Second VAT ID"}]


class ChangeVatIDToList(AbstractVersionChange):
    description = "Change vat id to list"
    instructions_to_migrate_to_previous_version = (
        schema(
            BaseCompany,
            field("vat_ids").didnt_exist,
            field("vat_id").existed_with(type=str, info=Field()),
        ),
    )

    @convert_response_to_previous_version_for(get_company, create_company)
    def change_vat_ids_to_single_item(cls, data: dict[str, Any]) -> None:
        data["vat_id"] = data.pop("vat_ids")[0]


class ChangeVatIDsToSubresource(AbstractVersionChange):
    description = "Change vat ids to subresource"
    instructions_to_migrate_to_previous_version = (
        schema(
            BaseCompany,
            field("vat_ids").existed_with(type=list[str], info=Field()),
        ),
        schema(
            CompanyCreateRequest,
            field("default_vat_id").didnt_exist,
        ),
        endpoint(get_company_vat_ids).didnt_exist,
    )

    @convert_response_to_previous_version_for(get_company, create_company)
    def change_vat_ids_to_list(cls, data: dict[str, Any]) -> None:
        data["vat_ids"] = [id["value"] for id in data.pop("_prefetched_vat_ids")]


versions = Versions(
    Version(date(2002, 1, 1), ChangeVatIDsToSubresource),
    Version(date(2001, 1, 1), ChangeVatIDToList),
    Version(date(2000, 1, 1)),
)


# regenerate_dir_to_all_versions(latest, versions)


# routers = router.create_versioned_copies(versions, latest_schemas_module=latest)
# app = FastAPI()
# version = date(2002, 1, 1)
# api_version_var.set(version)
# app.include_router(routers[version])
# print(version)
# uvicorn.run(app, port=8000)
