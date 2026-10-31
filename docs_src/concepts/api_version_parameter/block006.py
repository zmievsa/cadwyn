from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_format="date",
    versions=VersionBundle(Version("2022-01-02")),
)
