# Service structure

The service structure with Cadwyn is fairly straighforward. See the [example service](https://github.com/zmievsa/cadwyn/tree/main/tests/tutorial) or follow the steps above:

1. Define a [VersionBundle](./version_changes.md#versionbundle) where you add your first version.
2. Create a `data/latest` directory and add your latest version of schemas there. This will serve as a template directory for future code generation.
3. Run [code generation](./code_generation.md#code-generation) that will create generated versions of your `latest` directory next to it.
4. Create a [Cadwyn app](./main_app.md) that you will use instead of `FastAPI`. Pass imported `data/latest` and your `VersonBundle` to it.
5. Create a [VersionedAPIRouter](./main_app.md#versionedapirouter) that you will use for defining your versioned routes.
6. [Include this router](./main_app.md) and any other versioned routers into your `Cadwyn` app. It will duplicate your router in runtime for each API version.

The recommended directory structure for cadwyn is as follows:
<!--- Find a better name for "data" dir. "Schemas" doesn't work because enums can also be there. "Versioned" doesn't work because unversioned stuff can also be there as long as it's not in latest.-->

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

Schemas, enums, and any other versioned data are inside the `data.latest` package, version changes are inside the `versions.vXXXX_XX_XX` modules, and version bundle is inside the `versions.__init__` module. It includes all versions with all version changes -- including the ones you add in the recipes.

You can assume for the purpose of our guides that we already have a version **2000-01-01** and we are making a new version **2001-01-01** with the changes from our scenarios.

You can structure your business logic, database, and all other parts of your application in any way you like.

That's it! Your service is ready to be versioned. We can now use the most powerful feature of Cadwyn: [version changes](./version_changes.md#version-changes).
