# Code generation

Cadwyn generates versioned schemas and everything related to them from latest version. These versioned schemas will be automatically used in requests and responses for [versioned API routes](./main_app.md#main-app). There are two methods of generating code: using a function and using the CLI.

## Warning

Cadwyn no longer requires you to generate your schemas by hand. It will generate everything for you on your app's startup. Manual code generation is only reserved for when the system does not function as we expect it to. If you need to use manual code generation -- please, leave an issue in our github repository.

## Command-line interface

You can use `cadwyn codegen` which accepts a python path to your version bundle.

**NOTE** that it is not a regular system path. It's the **python-style** path -- the same one you would use when running `uvicorn` through command-line. Imagine that you are importing the module and then appending `":" + version_bundle_variable_name` at the end.

```bash
cadwyn codegen path.to.version.bundle:version_bundle_variable
```

**Note that:**

* You don't use the system path style for both arguments. Instead, imagine that you are importing these modules in python -- that's the way you want to write down the paths.
* Take a look at how we point to our version bundle. We use ":" to say that it's a variable within the specified module

## Function interface

You can use `cadwyn.generate_code_for_versioned_packages` which accepts a `template_module` (a directory which contains the latest versions) and `versions` which is the `VersionBundle` from which to generate versions.
