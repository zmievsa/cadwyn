# Contribution Guide

## Setting up the environment

* The lowest currently supported version is Python 3.10. You can use
[pyenv](https://github.com/pyenv/pyenv) to manage multiple Python
versions on your system.
* We maintain a Makefile with several commands to help with common tasks.

1. Install [Poetry](https://python-poetry.org/)
2. Run `poetry install --all-extras` to create a [virtual environment](https://docs.python.org/3/tutorial/venv.html) and install the required development dependencies
3. Install [pre-commit](https://pre-commit.com/)
4. Run `pre-commit install --install-hooks` to install pre-commit hooks

Many modern IDEs like PyCharm or VS Code will enable the Poetry-managed virtualenv that is created in step 2 for you automatically. If your IDE/editor does not offer this functionality, then you will need to manually activate the virtualenv yourself. Otherwise you may encounter errors or unexpected behaviour when trying to run the commands referenced within this document.

To activate the virtualenv, please consult Poetry's documentation on [working with virtual environments](https://python-poetry.org/docs/basic-usage#using-your-virtual-environment).

The rest of this document will assume this environment is active wherever commands are referenced.

## Code contributions

### Workflow

1. [Fork](https://github.com/zmievsa/cadwyn/fork) the [Cadwyn repository](https://github.com/zmievsa/cadwyn)
2. Clone your fork locally with git
3. [Set up the environment](#setting-up-the-environment)
4. Make your changes
5. Commit your changes to git.
6. Open a [pull request](https://docs.github.com/en/pull-requests). Give the pull request a descriptive title indicating what it changes.

## Guidelines for writing code

* Code should be [Pythonic and zen](https://peps.python.org/pep-0020/)
* All code should be fully [typed](https://peps.python.org/pep-0484/). This is enforced via [ruff](https://github.com/astral-sh/ruff) but the type hinting itself will be enforced by [pyright](https://github.com/microsoft/pyright/) in the future.
  * When requiring complex types, use a [type alias](https://docs.python.org/3/library/typing.html#type-aliases).
  * If something cannot be typed correctly due to a limitation of the type checkers, you may use [typing.cast](https://docs.python.org/3/library/typing.html#typing.cast) to rectify the situation. However, you should only use as a last resort if you've exhausted all other options of [type narrowing](https://mypy.readthedocs.io/en/stable/type_narrowing.html), such as [isinstance()](https://docs.python.org/3/library/functions.html#isinstance) checks and [type guards](https://docs.python.org/3/library/typing.html#typing.TypeGuard)
  * You may use `pyright: ignore` if you ensured that a line is correct, but pyright has issues with it.
* If you are adding or modifying existing code, ensure that it's fully tested. 100% test coverage is mandatory, and will be checked on the PR using [Github Actions](https://github.com/features/actions)
* When adding a new public interface, it has to be included in the reference documentation located in `docs/reference.md`. If applicable, add or modify examples in the docs related to the new functionality implemented, following the guidelines established in [Adding examples](#adding-examples)

### Writing and running tests

Tests are contained within the `tests` directory, and follow roughly the same
directory structure as the `cadwyn` module. If you are adding a test
case, it should be located within the correct submodule of `tests`. E.g.
tests for `cadwyn/codegen.py` reside in `tests/codegen`.

The `Makefile` includes several commands for running tests:

* `make test` to run tests located in `tests`
* `make supertest` will run tests on both pydantic 1 and pydantic 2

### Running type checkers

We use [pyright](https://github.com/microsoft/pyright/) to enforce type safety.
You can run it with:

`pyright .`

## Project documentation

The documentation is located in the `/docs` directory and is all in
[Markdown](https://www.markdownguide.org/).

### Docs theme and appearance

We welcome contributions that enhance / improve the appearance and usability of the docs. We use [mkdocs-material](https://squidfunk.github.io/mkdocs-material/) If you wish to contribute to the docs style / setup, or static site generation, you should consult the theme docs as a first step.

### Running the docs locally

Then you can serve the documentation with `make docs`

### Writing and editing docs

We welcome contributions that enhance / improve the content of the docs. Feel free to add examples, clarify text, restructure the docs, etc., but make sure to follow these guidelines:

* Write text in idiomatic english, using simple language
* Opt for [Oxford commas](https://en.wikipedia.org/wiki/Serial_comma) when listing a series of terms
* Keep examples simple and self contained
* Provide links where applicable
