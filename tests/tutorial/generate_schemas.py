if __name__ == "__main__":
    from cadwyn.codegen._main import generate_code_for_versioned_packages
    from tests.tutorial.data import head
    from tests.tutorial.versions import version_bundle

    generate_code_for_versioned_packages(head, version_bundle)
