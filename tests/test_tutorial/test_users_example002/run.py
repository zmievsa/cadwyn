from universi.routing import generate_all_router_versions


if __name__ == "__main__":
    from datetime import date
    from pathlib import Path

    import uvicorn
    from fastapi import FastAPI

    from tests.test_tutorial.test_users_example002.schemas import latest
    from tests.test_tutorial.test_users_example002.users import api_version_var, router, versions
    from tests.test_tutorial.utils import clean_versions
    from universi import regenerate_dir_to_all_versions

    try:
        regenerate_dir_to_all_versions(latest, versions)
        router_versions = generate_all_router_versions(
            router,
            versions=versions,
            latest_schemas_module=latest,
        )
        app = FastAPI()
        api_version_var.set(date(2000, 1, 1))
        app.include_router(router_versions[date(2000, 1, 1)])
        uvicorn.run(app)
    finally:
        clean_versions(Path(__file__).parent / "schemas")
