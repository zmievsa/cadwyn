if __name__ == "__main__":
    from pathlib import Path

    import uvicorn

    from tests.test_tutorial_with_latest.routes import app, router
    from tests.test_tutorial_with_latest.utils import clean_versions

    try:
        app.generate_and_include_versioned_routers(router)

        uvicorn.run(app)
    finally:
        # This is only here for testing purposes. In reality, you wouldn't do that.
        clean_versions(Path(__file__).parent / "data")
