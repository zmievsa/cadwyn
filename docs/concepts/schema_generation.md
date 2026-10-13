# Schema generation

Cadwyn automatically generates versioned schemas and everything related to them from HEAD version at runtime — no code is actually generated. These versioned schemas will be automatically used in requests to and responses from [versioned API routes](./main_app.md#main-app).

## Rendering schemas

If you have many versions and schemas, it is quite challenging to know what validators, fields, and other attributes are defined in each schema in a specific version. To address this issue, there is a way to **render** the generated Pydantic models and enums as code using the command-line interface.

**NOTICE** that `cadwyn render` does not guarantee rendering correct schemas. It provides a close enough approximation for manual validation, but it is not yet ready for production code generation. For example, it doesn't handle schema renaming in a class's `__bases__` yet.

### Rendering a module

Here's a way to render the entire module with the schemas:

```bash
cadwyn render module data.schemas --app=main:app --version=2024-05-26
```
<!-- TODO: Add an option to use a callable instead of a variable -->
This command prints to stdout how the schemas would look in version 2024-05-26 if they were written by hand rather than generated at runtime by Cadwyn. This command takes the schemas from `data/schemas.py` module and knows what the schemas would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

### Rendering a single model

Here's a way to render a single Pydantic model or enum with the schemas:

```bash
cadwyn render model data.schemas:UserCreateRequest --app=main:app --version=2024-05-26
```

This command prints to stdout how the `UserCreateRequest` schema would look in version 2024-05-26 if it was written by hand rather than generated at runtime by Cadwyn. This command takes the `UserCreateRequest` schema from `data/schemas.py` module and knows what the schema would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

## Generating schemas without FastAPI

Cadwyn can generate versioned schemas from its version changes even without FastAPI:

```python
import cadwyn
from cadwyn.schema_generation import generate_versioned_models
from my_versions import MyVersionedSchema, version_bundle

schema_generators = generate_versioned_models(version_bundle)
MyVersionedSchemaFrom2025 = schema_generators["2025-11-16"][MyVersionedSchema]
```

Cadwyn generates a copy of every model for every version, except for the enums without enum instructions and the models that refer to themselves (directly or through other models, e.g. `children: "list[Category]"`) when neither they nor the models they refer to have instructions in any version (including `HeadVersion`) and they are not parents of models with versioned copies. Cadwyn uses such enums and models as they are: `schema_generators["2025-11-16"][Category] is Category`. Cadwyn makes this decision when it first needs a model, so the models defined after that (e.g. new subclasses) do not change it.

### Models that refer to each other

Models can refer to themselves, directly or through other models, e.g. `children: "list[Category]"` or `Author.books: list[Book]` with `Book.author: Author`. When such models need versioning, Cadwyn copies all of them together, and their copies refer to each other. So if `Category` had a `title` field instead of `name` in an older version, that version uses `title` at every depth of the tree. Parametrized generic models such as `Page[Category]` use these copies as their type args when the models with instructions or the routes of the version refer to such models. This requires Pydantic 2.11.5 or newer: with older Pydantic versions, Cadwyn raises a `SchemaGenerationError` for such models.

Otherwise, e.g. when such models only need versioning because they refer to a model with instructions and only `migrate_response_body` uses them directly, the type args of generics keep the latest models, as they always did. If a model with instructions refers to such models only through the type args of a generic, while a route uses them directly, Cadwyn raises a `SchemaGenerationError` instead of using both the latest and the older versions of them in one version.

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
