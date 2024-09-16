
# Setup

Cadwyn is built around FastAPI and supports all of its functionality out of the box. One difference is that Cadwyn requires you to define API versions and extends your routing and swagger to support header-based API versioning.

## Installation

```bash
pip install 'cadwyn[cli]'
```

## The basics

First, let's set up the most basic versioned app possible:

```python
from datetime import date
from cadwyn import Cadwyn, VersionBundle, HeadVersion, Version


app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2000-01-01")))


@app.get("/")
async def root():
    return {"message": "Hello World"}
```

and run it using:

```bash
fastapi dev main.py
```

That's it. That's the main difference between setting up FastAPI and Cadwyn: you have to specify your versions. Everything you specify at app level (such as using `include_router` or `app.get(...)`) will end up unversioned and essentially function like a regular FastAPI route.

## Docs

If you visit `/docs`, instead of the regular swagger, you will see a version dashboard:

![Version dashboard](../img/unversioned_dashboard.png)

Clicking a card will take you to the card's regular swagger page.
