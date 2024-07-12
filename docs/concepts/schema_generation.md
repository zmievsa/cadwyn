# Schema generation

Cadwyn automatically generates versioned schemas and everything related to them from HEAD version at runtime -- no actual code is being generated. These versioned schemas will be automatically used in requests and responses for [versioned API routes](./main_app.md#main-app).

## Rendering schemas

When you have many versions and many schemas, it is quite hard to know what validators, fields, and other attributes are defined on each schema in any concrete version. To combat this problem, we have a way to **render** the generated pydantic models and enums to code using the command-line interface.

### Rendering a module

Here's how you would render the entire module with your schemas:

```bash
cadwyn render module data.schemas --app=main:app --version=2024-05-26
```
<!-- TODO: Add an option to use a callable instead of a variable -->
This command will print to stdout what the schemas would look like in version 2024-05-26 if they were written by hand instead of generated at runtime by Cadwyn. This command takes the schemas from `data/schemas.py` module and knows what the schemas would look like based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

### Rendering a single model

Here's how you would render a single pydantic model or enum with your schemas:

```bash
cadwyn render model data.schemas:UserCreateRequest --app=main:app --version=2024-05-26
```

This command will print to stdout what the `UserCreateRequest` schema would look like in version 2024-05-26 if it was written by hand instead of generated at runtime by Cadwyn. This command takes the `UserCreateRequest` schema from `data/schemas.py` module and knows what the schema would look like based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.
