if __name__ == "__main__":
    import uvicorn

    from tests.tutorial.routes import app, router

    app.generate_and_include_versioned_routers(router)
    uvicorn.run(app)
