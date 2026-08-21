from bot.titles import display_title


def test_never_shows_provider_hash() -> None:
    assert display_title("webdav_e40bd8be54970f5a") == "Unknown track"
    assert display_title("archive_1aaf737a461bc8b7") == "Unknown track"


def test_prettifies_drive_stem() -> None:
    assert display_title("volume-ii-consciousness-and-addiction") == (
        "Volume II Consciousness and Addiction"
    )


def test_prettifies_path() -> None:
    assert display_title("VolumeSeries/volume-i-power-vs-force.mp4") == (
        "Volume I Power vs Force"
    )


def test_empty_and_none() -> None:
    assert display_title("") == "Unknown track"
    assert display_title(None) == "Unknown track"
