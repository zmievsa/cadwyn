from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_format="string",
    versions=VersionBundle(
        Version("v2"),
        Version("anything_can_be_a_version"),
        Version("v1"),
    ),
)
