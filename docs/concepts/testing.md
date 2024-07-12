# Testing

As Cadwyn allows you to keep the same business logic, database schemas, etc -- you should have a single set of **common** tests that test your current latest version. These tests are going to work like the regular tests that you would have if you did not have any API versioning.

Here's one of the possible file structures for tests:

```tree
└── tests
    ├── __init__.py
    ├── conftest.py
    ├── head
    │   ├── __init__.py
    │   ├── conftest.py
    │   ├── test_users.py
    │   ├── test_admins.py
    │   └── test_invoices.py
    ├── v2022_11_16
    │   ├── __init__.py
    │   ├── conftest.py
    │   └── test_invoices.py
    └── v2023_03_11
        ├── __init__.py
        ├── conftest.py
        └── test_users.py

```

Each time you create a new version, I advise you to follow the following process:

1. Duplicate the **subset** of the HEAD tests and fixtures that is going to be affected by the new version and add it into its own directory, same as the version name. Run all the tests to validate that they pass.
2. Make the breaking changes you wished to make and write the migrations
3. Change HEAD tests to accomodate the new behavior (note how this should be done in step (1) if you prefer to follow TDD)

This approach will make it easy to keep the old versions covered by tests and will keep the duplication in tests minimal.
