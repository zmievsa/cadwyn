
# Tutorial

This tutorial provides steps for setting up automatic API versioning using Cadwyn. This will be illustrated with an example of a User API with changes to a User's address. The advanced version of the service from this tutorial is available [here](https://github.com/zmievsa/cadwyn/tree/main/tests/tutorial).

Adding a new API version in Cadwyn consists of two main steps:

1. Make the breaking change
2. Use Cadwyn to describe how to revert the breaking change

This tutorial will show how to prepare an environment for working with Cadwyn effectively in a basic usage example, how to make a breaking change, and how Cadwyn can help to keep the old API versions untouched.

## Step 0: Setting up

Here is an initial API setup where a User has a single address. The example implements two routes - one for creating a User and the other for retrieving User details. User ID is implemented as `int` for simplicity. Please note that a `dict` is used in place of a database for simplicity but do not ever do it in real life.

The first API one comes up with usually doesn't require more than one address -- why bother?

```python
{! ./docs_src/quickstart/tutorial/block001.py !}
```

If you visit `/docs`, you will see the following dashboard:

![Dashboard with one version](../img/dashboard_with_one_version.png)

The app is ready for use. To call its endpoints, pass `X-API-VERSION` header with value equal to `2000-01-01`. But this is just one version. For a breaking change, Cadwyn supports multiple versioning styles, including header-based, path-based, and number-based.

## Step 1: Making the breaking change

Assume that during the development you realize that the initial API design is wrong and that `address` should be `addresses` so that the User can have several addresses to choose from. So now you want to change the type of the `addresses` field to a list of strings.

```python hl_lines="2 4 15 20 30 39"
{! ./docs_src/quickstart/tutorial/block002.py !}
```

Now your users will have their API integration broken. To prevent that, introduce API versioning. There aren't many methods of doing that. Most of them force you to either duplicate your schemas, your endpoints, or your entire app instance. And it makes sense, really: duplication is the only way to make sure that you will not break old versions with your new versions; the bigger the piece you are duplicating, the safer. Of course, the safest being duplicating the entire app instance and even having a separate database. But that is expensive and makes it either impossible to make breaking changes often or to support many versions. As a result, either you need infinite resources, very long development cycles, or your users need to migrate often from version to version.

Stripe has come up [with a solution](https://stripe.com/blog/api-versioning): to have one HEAD app version whose responses get migrated to older versions and to describe changes between these versions using migrations. This approach allows Stripe to keep versions for **years** without dropping them. Obviously, each breaking change still hurts and each version still makes the system more complex and expensive, but their approach gives you a chance to minimize this complexity. Additionally, it allows you to backport features and bugfixes to older versions. However, you will also be backporting bugs, which is a sad consequence of eliminating duplication.

Assume you need to know what your code looked like two weeks ago. You may use `git checkout` or `git reset` with an older commit because `git` stores the latest version of your code (which is also called HEAD) and the differences between it and each previous version as a chain of changes. This is exactly how Stripe's approach works. They store the latest version and use the diffs to regenerate the older versions.

Cadwyn is built upon this approach, so let's continue with the tutorial and combine the two versions that were created using versioning.

<details>
  <summary>Note to curious readers</summary>

  Git doesn't actually work this way internally. The description above is closer to how SVN works. It's a simplistic metaphor to explain a concept.
</details>

## Step 2: Describing how to revert the breaking change

To fix the old integrations of your clients, you need to add back the `2000-01-01` version and its state. In Cadwyn, it is done using version changes (or, as Stripe calls them, version gates). You could also think of them as reverse database migrations (database migrations to downgrade to the previous state of the database). Essentially version changes describe the difference between the latest version and the previous version. They are a way to say "Okay, we have applied the breaking changes but here is how we would revert these changes for our old clients".

For every endpoint whose `response_model` is `UserResource`, this migration will convert the list of addresses back to a single address when migrating to the previous version. Your goal is to have an app of [HEAD version](../concepts/version_changes.md#headversion) and to describe what older versions looked like in comparison to it. That way the old versions are frozen in migrations and you can **almost** safely forget about them.

```python hl_lines="8-9 12 14-16 45-66"
{! ./docs_src/quickstart/tutorial/block003.py !}
```

See how the first address is popped from the list? This is guaranteed to be possible if you specified earlier that `min_length` for `addresses` must be `1`. If you didn't, then a user would be able to create a User in a newer version that would be impossible to represent in the older version. I.e. if anyone tried to get that user from the older version, they would get a `ResponseValidationError` because the user wouldn't have data for a mandatory `address` field. You need to always keep in mind that API versioning is only for versioning your **API**, your interface. Your versions must still be completely compatible in terms of data. If they are not, then you are versioning your data and you should go with a separate app instance. Otherwise, your users will have a hard time migrating back and forth between API versions and many unexpected errors.

Also note that the migration was added not only for the response but also for the request. This will allow your business logic to stay completely the same, no matter which version it was called from. Cadwyn will always give your business logic the request model from the HEAD version by wrapping each request in it.

Run the app and take a look at the generated dashboard and OpenAPI schemas:

![Dashboard with two versions](../img/dashboard_with_two_versions.png)
![GET /users/{user_id} endpoint in OpenAPI](../img/get_users_endpoint_from_prior_version.png)

The endpoint above is from the `2000-01-01` version. As you see, your routes and business logic are for the HEAD version but your OpenAPI has all the information about all the API versions which is the main goal of Cadwyn: a large number of long-living API versions without placing any burden on your business logic.

Obviously, this was just a simple example and Cadwyn has a lot more features so if you're interested -- take a look at the [how-to](../how_to/index.md) and [concepts](../concepts/index.md) sections.
