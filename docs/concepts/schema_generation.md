# Schema generation

Cadwyn automatically generates versioned schemas and everything related to them from HEAD version at runtime — no code is actually generated. These versioned schemas will be automatically used in requests to and responses from [versioned API routes](./main_app.md#main-app).

## Rendering schemas

If you have many versions and schemas, it is quite challenging to know what validators, fields, and other attributes are defined in each schema in a specific version. To address this issue, there is a way to **render** the generated Pydantic models and enums as code using the command-line interface.

**NOTICE** that `cadwyn render` does not guarantee rendering correct schemas. It provides a close enough approximation for manual validation, but it is not yet ready for production code generation. For example, it doesn't handle schema renaming in a class's `__bases__` yet.

### Rendering a module

Here's a way to render the entire module with the schemas:

```bash
{! ./docs_src/concepts/schema_generation/block003.sh !}
```
<!-- TODO: Add an option to use a callable instead of a variable -->
This command prints to stdout how the schemas would look in version 2024-05-26 if they were written by hand rather than generated at runtime by Cadwyn. This command takes the schemas from `data/schemas.py` module and knows what the schemas would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`. Run it from the directory that contains `main.py` and `data/`: `cadwyn` imports the modules from there.

### Rendering a single model

Here's a way to render a single Pydantic model or enum with the schemas:

```bash
{! ./docs_src/concepts/schema_generation/block004.sh !}
```

This command prints to stdout how the `UserCreateRequest` schema would look in version 2024-05-26 if it was written by hand rather than generated at runtime by Cadwyn. This command takes the `UserCreateRequest` schema from `data/schemas.py` module and knows what the schema would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

The rendered fields are the ones that the schema has in that version. When a schema has instructions for a field that it inherits, it is rendered with its own version of that field, and its parents and siblings are rendered with theirs (see [Fields that a schema inherits](./schema_migrations.md#fields-that-a-schema-inherits)).

## Generating schemas without FastAPI

Cadwyn can generate versioned schemas from its version changes even without FastAPI:

```python
{! ./docs_src/concepts/schema_generation/block001.py !}
```

Cadwyn generates a copy of every model for every version, except for the enums without enum instructions and the models that refer to themselves (directly or through other models, e.g. `children: "list[Category]"`) when neither they nor the models they refer to have instructions in any version (including `HeadVersion`) and they are not parents of models with versioned copies. Cadwyn uses such enums and models as they are: `schema_generators["2025-11-16"][Category] is Category`. Cadwyn makes this decision when it first needs a model, so the models defined after that (e.g. new subclasses) do not change it.

### Generic models

Parametrized generic models such as `Page[Item]` use the versioned copies of their type args in every version. For example, take the app from [issue #416](https://github.com/zmievsa/cadwyn/issues/416):

```python
{! ./docs_src/concepts/schema_generation/block002.py !}
```

Version 2000-01-01 uses the old `Item` with its `name` field inside `Page[Item]`, generic subclasses such as `PageWithTotal[Item]`, `class ItemPage(Page[Item])`, `class Items(RootModel[list[Item]])`, and nested generics such as `Page[Page[Item]]`, and its OpenAPI has one `Item` schema.

Only the generics whose type args refer to models or enums that have schema or enum instructions in any version, including `HeadVersion`, are affected. All other generics keep their type args, their OpenAPI names, and their behavior. Cadwyn 7 kept the latest versions of the type args in every version, so apps that have such generics change when they upgrade:

* Older versions validate and serialize the args with their old models, so clients of older versions that relied on the latest models inside generics may get validation errors. Converters for `Item` do not run for the items of `Page[Item]`, so add converters for the generics too where older clients need them, e.g. `convert_response_to_previous_version_for(Page[Item])` and `convert_request_to_next_version_for(Page[Item])` that rename `full_name` to `name` and back in every item.
* Changes from `HeadVersion` also apply to these args in every version, including the latest one.
* The OpenAPI names of these generics can change in every version because their quoted type args are evaluated, e.g. `Page[list["Item"]]` is named `Page_list_Item__` instead of `Page_list_str__`.
* `migrate_response_body` rejects instances of the latest models inside these generics, e.g. `Page[Item](...)` or `{"items": [Item(...)]}` for `Page[Item]`, with a `ValidationError` in every version, including the latest one, as it already does for other versioned models nested in the response model. Pass plain data, such as the result of `model_dump()`, instead.
* The models in these args are copied for every version like other versioned models, so the ones that cannot be copied (e.g. with an `Annotated` validator that holds a `threading.Lock`, which cannot be deep-copied) fail even if no route uses them directly.
* Instructions for a specific parametrization, such as `schema(Pair[str, int])`, also apply where pydantic builds that parametrization in a field of another generic, such as `pair: Pair[T, int]` in `Box[T]` used as `Box[str]` ([issue #420](https://github.com/zmievsa/cadwyn/issues/420)). Cadwyn 7 kept `Pair[str, int]` without its instructions there in older versions of `Box[str]`. Now request bodies with `Box[str]` need the same request migrations as `Pair[str, int]` itself. Fields keep their previous behavior when `Box`, its bases, or `Box[str]` have instructions of their own, when pydantic refers to the parametrization recursively inside itself, or when such parametrizations refer to each other.
* Instructions for a field that a model inherits from a generic base through its type args keep the field's type in older versions. For example, `schema(Ints).field("root").had(description=...)` for `class Ints(RootModel[list[int]])`, or `schema(StrIntPair).field("second").had(...)` for `class StrIntPair(Pair[str, int])`. In Cadwyn 7, such a field became the bare type var (`~RootModelRootType`, `~U`) in older versions, so they accepted any value for it, and invalid input failed with a 500 instead of a 422. Now the field behaves in older versions like any other field of its type, so requests that only worked because it accepted anything can change:
    * its validators and coercion run in the older version as well as in the latest one, as for other fields, so validators that are not idempotent, e.g. `AfterValidator(lambda v: v + 1)` in the type args, apply more than once;
    * serializers that change its value, e.g. `PlainSerializer(lambda v: f"{v:,}", return_type=str)`, also change the request body that reaches the latest version, so such requests need request migrations;
    * models in its type args validate their older shape, e.g. after `schema(Item).field("n").had(name="old_n")` the older version requires `old_n`;
    * request migrations run after the older version validates the field, so they no longer see values that its type rejects.
    * as for other fields, `had()` does not change the constraints that the field's type declares through `Annotated[..., Field(...)]`, e.g. in its type arg or in the annotation of the parent that defines it. So on Pydantic 2.12 and newer, `.had(ge=5)` for a field whose type carries `Annotated[int, Field(ge=0)]` no longer applies, and older versions accept values such as `1` that they rejected before.

Some type args keep their latest models:

* the args that lead back to the generic itself, e.g. `subcategories: Page["Category"]` within `Category` (see [Models that refer to each other](#models-that-refer-to-each-other));
* the args whose models refer to names that are not defined yet when Cadwyn first needs the generic;
* quoted args, such as `Page["Item"]`, whose models are not defined in the module of the generic.

### Computed fields that return models

Pydantic resolves the return types of computed fields on its own, so Cadwyn cannot version them: in every version, a computed field that returns a versioned model (a model with instructions, or one that refers to such models) keeps the latest version of that model. In older versions, this has two consequences:

* The computed field serializes the data that it returns with the latest model. If it returns the versioned data of the model, e.g. `self.items[0]` in `def best(self) -> Item`, the fields that the older version renamed or changed are left out or appear in their latest shape.
* OpenAPI describes the computed field with the latest model too.

If older versions need the older shape of such data, expose it as a regular field instead of a computed field, e.g. `best: Item` filled in by a validator or by the endpoint. Cadwyn versions regular fields and migrates their data like any other.

### Models that refer to each other

Models can refer to themselves, directly or through other models, e.g. `children: "list[Category]"` or `Author.books: list[Book]` with `Book.author: Author`. When such models need versioning, Cadwyn copies all of them together, and their copies refer to each other. So if `Category` had a `title` field instead of `name` in an older version, that version uses `title` at every depth of the tree.

[Parametrized generic models](#generic-models) such as `Page[Category]` use these copies as their type args when the models with instructions or the routes of the version refer to such models. Otherwise, e.g. when such models only need versioning because they refer to a model with instructions and only `migrate_response_body` uses them directly, the type args of generics keep the latest models, as they always did. If a model with instructions refers to such models only through the type args of a generic, while a route uses them directly, Cadwyn raises a `SchemaGenerationError` instead of using both the latest and the older versions of them in one version.

Cadwyn raises a `SchemaGenerationError` when it generates the versions if it cannot copy such models:

* when they refer to each other in places that need the classes themselves rather than annotations, e.g. in default factories, in the metadata of `Annotated`, in type aliases, or in the type args of generics that are not from `typing` or `collections.abc`;
* when their copies, or the other copies of the version that use them, would still use the latest versions of such models, which Cadwyn cannot version there: e.g. in the return types of computed fields and serializers, in names quoted inside other annotations (`list["Book"]`, which you can write as `"list[Book]"` instead), in generic fields whose type args lead back to the generic itself, such as `children: Page["Category"]` in a `Category` that also refers to itself in another way, or in a generic that keeps the latest models next to a field that uses their copies, such as `category: Category` along with `related: Page[Category]` when only `migrate_response_body` uses them;
* when a field of such a model that uses models that need versioning, or a field of another model that uses such models, has a default that contains an instance of the latest version of a model that needs versioning (e.g. `settings: Settings = Settings()`, also within other models and containers), or a default factory that Cadwyn cannot verify. Cadwyn only accepts `list`, `dict`, `set`, `tuple`, and `frozenset`, the classes of the models themselves (e.g. `Field(default_factory=Settings)`, which Cadwyn versions), pydantic's constructors of the models that do not need versioning (e.g. `Unchanged.model_construct`), and `functools.partial` of these with arguments that pass the same check. The defaults of all other fields are used as Cadwyn always did, except that their values must not contain the latest versions of such models (e.g. `raw: dict = {"category": Category(...)}`). Enum members are serialized by their values so they are used as Cadwyn always did;
* when their annotations, or the annotations of the functions that they use, refer to names that are not defined yet.

Some other things to keep in mind about such models:

* Cadwyn cannot see the latest models that validators create, e.g. `AfterValidator(lambda value: Settings())`. As with all other models, their older versions keep such latest models.
* Their copies are built when Cadwyn generates them, even if they use `defer_build=True`.
* Their hooks that Pydantic calls while it builds them, such as `__pydantic_init_subclass__`, must not ask Cadwyn for their own versioned copies (e.g. through `migrate_response_body`): Cadwyn raises a `CadwynError` if they do.
* The schema hooks of any model (e.g. `__get_pydantic_core_schema__`) must not ask Cadwyn for versioned models while another thread generates the versioned copies of such models: Pydantic 2.14 holds its lock while it runs these hooks, and Cadwyn rebuilds such copies while it holds its own lock.
