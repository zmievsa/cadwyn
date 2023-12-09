if __name__ == "__main__":
    from cadwyn import generate_code_for_versioned_packages
    from tests.test_tutorial.data import latest
    from tests.test_tutorial.versions import version_bundle

    generate_code_for_versioned_packages(latest, version_bundle)
