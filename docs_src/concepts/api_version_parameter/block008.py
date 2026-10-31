from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    versions=VersionBundle(Version("2022-01-02")),
    api_version_title="My Great API version parameter",
    api_version_description="Description of my great API version parameter",
)
