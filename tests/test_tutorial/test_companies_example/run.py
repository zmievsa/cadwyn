if __name__ == "__main__":
    from tests.test_tutorial.test_companies_example.companies import router, versions
    from tests.test_tutorial.test_companies_example.utils import clean_versions
    from tests.test_tutorial.test_companies_example.schemas import latest
    from universi import regenerate_dir_to_all_versions, api_version_var
    from datetime import date

    import uvicorn
    from fastapi import FastAPI

    try:
        regenerate_dir_to_all_versions(latest, versions)
        router_versions = router.create_versioned_copies(versions, latest_schemas_module=latest)
        app = FastAPI()
        api_version_var.set(date(2000, 1, 1))
        app.include_router(router_versions[date(2000, 1, 1)])
        uvicorn.run(app)
    finally:
        clean_versions()
