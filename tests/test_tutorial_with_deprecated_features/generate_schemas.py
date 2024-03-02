if __name__ == "__main__":
    from cadwyn.codegen._main import generate_code_for_versioned_packages
    from tests.test_tutorial_with_deprecated_features.data import latest
    from tests.test_tutorial_with_deprecated_features.versions import version_bundle

    generate_code_for_versioned_packages(latest, version_bundle)
