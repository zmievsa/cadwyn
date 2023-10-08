if __name__ == "__main__":
    from pathlib import Path

    import uvicorn
    from fastapi_header_versioning.fastapi import HeaderRoutingFastAPI

    from cadwyn import generate_code_for_versioned_packages, get_cadwyn_dependency
    from cadwyn.header_routing import get_versioned_router
    from tests.test_tutorial.test_users_example002.schemas import latest
    from tests.test_tutorial.test_users_example002.users import api_version_var, router, versions
    from tests.test_tutorial.utils import clean_versions

    try:
        generate_code_for_versioned_packages(latest, versions)
        VERSION_HEADER = "x-api-version"
        app = HeaderRoutingFastAPI(
            version_header=VERSION_HEADER,
            dependencies=[get_cadwyn_dependency(version_header_name=VERSION_HEADER, api_version_var=api_version_var)],
        )
        router = get_versioned_router(router, versions=versions, latest_schemas_module=latest)
        app.include_router(router)
        uvicorn.run(app)
    finally:
        clean_versions(Path(__file__).parent / "schemas")
