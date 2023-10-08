from cadwyn.routing import generate_versioned_routers

if __name__ == "__main__":
    from datetime import date
    from pathlib import Path

    import uvicorn
    from fastapi import FastAPI

    from cadwyn import generate_code_for_versioned_packages
    from tests.test_tutorial.test_users_example003.schemas import latest
    from tests.test_tutorial.test_users_example003.users import api_version_var, router, versions
    from tests.test_tutorial.utils import clean_versions

    try:
        generate_code_for_versioned_packages(latest, versions)
        router_versions = generate_versioned_routers(
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
