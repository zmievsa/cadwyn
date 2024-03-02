
# Making the breaking change

During our development, we have realized that the initial API design was wrong and that addresses should have always been a list because the user wants to have multiple addresses to choose from so now we have to change the type of the "address" field to the list of strings.

```python
# data/latest/users.py
from pydantic import BaseModel, Field
import uuid


class BaseUser(BaseModel):
    addresses: list[str] = Field(min_items=1)


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID
```

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
        "addresses": payload.addresses,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)

uvicorn.run(app)
```

But every user of ours will now have their API integration broken. To prevent that, we have to introduce API versioning. There aren't many methods of doing that. Most of them force you to either duplicate your schemas, your endpoints, or your entire app instance. And it makes sense, really: duplication is the only way to make sure that you will not break old versions with your new versions; the bigger the piece you duplicating -- the safer. Of course, the safest being duplicating the entire app instance and even having a separate database. But that is expensive and makes it either impossible to make breaking changes often or to support many versions. As a result, either you need infinite resources, very long development cycles, or your users will need to often migrate from version to version.

Stripe has come up [with a solution](https://stripe.com/blog/api-versioning): let's have one HEAD app version whose responses get migrated to older versions and let's describe changes between these versions using migrations. This approach allows them to keep versions for **years** without dropping them. Obviously, each breaking change is still bad and each version still makes our system more complex and expensive, but their approach gives us a chance to minimize this complexity. Additionally, it allows us backport features and bugfixes to older versions. However, you will also be backporting bugs, which is a sad consequence of eliminating duplication.

Cadwyn builds upon approach so let's continue our tutorial and now try to combine the two versions we created using versioning.
