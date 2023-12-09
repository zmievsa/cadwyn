if __name__ == "__main__":
    from pathlib import Path

    import uvicorn

    from tests.test_tutorial.routes import app, router
    from tests.test_tutorial.utils import clean_versions

    try:
        app.generate_and_include_versioned_routers(router)

        uvicorn.run(app)
    finally:
        clean_versions(Path(__file__).parent / "data")
