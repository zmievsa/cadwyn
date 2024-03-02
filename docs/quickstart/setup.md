
# Setup

## Installation

```bash
pip install cadwyn
```

## Project structure

The recommended directory structure for cadwyn is as follows:

```tree
├── data
│   ├── __init__.py
│   ├── unversioned
│   │   ├── __init__.py
│   │   └── users.py
│   └── latest          # The latest version of your schemas goes here
│       ├── __init__.py
│       └── users.py
└── versions
    ├── __init__.py     # Your version bundle goes here
    └── v2001_01_01.py  # Your version changes go here
```

Here is an initial API setup where the User has a single address. We will be implementing two routes - one for creating a user and another for retrieving user details. We'll be using "int" for ID for simplicity. Please note that we will use a dict in place of a database for simplicity of our examples but do not ever do it in real life.

The first API you come up with usually doesn't require more than one address -- why bother?

So we create our file with schemas:

```python
# data/latest/users.py
from pydantic import BaseModel
import uuid


class BaseUser(BaseModel):
    address: str


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID
```

Then we create our version bundle which will keep track of our API versions:

```python
# versions/__init__.py
from cadwyn.structure import Version, VersionBundle
from datetime import date
from data import latest


version_bundle = VersionBundle(
    Version(date(2001, 1, 1)),
    latest_schemas_package=latest,
)
```

## Generating versioned schemas

Now let's generate the only version of our schemas -- the one we created in the [first step](./setup.md#setup):

```bash
cadwyn codegen versions:version_bundle
```

**WARNING** Cadwyn doesn't edit your imports when generating schemas so if you make any imports from versioned code to versioned code, I would suggest using [relative imports](https://docs.python.org/3/reference/import.html#package-relative-imports) to make sure that they will still work as expected after code generation.

## Generating versioned routes

```python
# routes.py
from data.latest.users import UserCreateRequest, UserResource
from versions import version_bundle
from cadwyn import VersionedAPIRouter, Cadwyn
import uuid
import uvicorn

database_parody = {}
router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "address": payload.address,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)

uvicorn.run(app)
```

That's it! Our app is ready to run.

Cadwyn has just generated a separate directory with the versioned schemas for us: one for each API version defined in our `VersionBundle`. If we run the app, we will see the following dashboard:

![Dashboard with one version](../img/dashboard_with_one_version.png)
