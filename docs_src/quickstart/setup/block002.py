from cadwyn import Cadwyn, HeadVersion, Version, VersionBundle

app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2000-01-01")))


@app.get("/")
async def root():
    return {"message": "Hello World"}
