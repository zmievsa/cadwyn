if __name__ == "__main__":
    # Please, note that this approach is not recommended. You should never generate code and run app at the same time.
    # You should generate it first and then run it in a separate process.
    from pathlib import Path

    import uvicorn

    from cadwyn import generate_code_for_versioned_packages
    from tests.test_tutorial.test_users_example002.data import latest
    from tests.test_tutorial.test_users_example002.versions import version_bundle
    from tests.test_tutorial.utils import clean_versions

    try:
        generate_code_for_versioned_packages(latest, version_bundle)
        from tests.test_tutorial.test_users_example002.routes import app, router

        app.generate_and_include_versioned_routers(router)

        uvicorn.run(app)
    finally:
        clean_versions(Path(__file__).parent / "data")
