def clean_versions():
    import shutil

    shutil.rmtree("tests/test_tutorial/test_companies_example/schemas/v2000_01_01")
    shutil.rmtree("tests/test_tutorial/test_companies_example/schemas/v2001_01_01")
    shutil.rmtree("tests/test_tutorial/test_companies_example/schemas/v2002_01_01")
