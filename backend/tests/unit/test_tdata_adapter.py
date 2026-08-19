"""Разбор загрузки tdata (ТЗ §4).

Здесь проверяется всё, что можно проверить без настоящей папки Telegram
Desktop: поиск tdata в загрузке, распаковка архивов, защита от путей наружу и
понятность ошибок. Саму расшифровку проверить автотестом нельзя — для этого
нужен действующий ключ авторизации, а подделать его невозможно: Telegram
разрывает соединение, и opentele не может собрать tdata из выдуманной сессии.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core.errors import InvalidInputError
from app.telegram.adapters import tdata


def make_tdata(root: Path, name: str = "tdata") -> Path:
    """Папка, похожая на tdata по структуре: с файлом-картой аккаунтов."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / tdata.TDATA_MARKER).write_bytes(b"not a real map, structure only")
    return folder


class TestFindTdataDir:
    def test_finds_folder_by_marker_file(self, tmp_path: Path) -> None:
        expected = make_tdata(tmp_path)
        assert tdata.find_tdata_dir(tmp_path) == expected

    def test_finds_folder_nested_deep(self, tmp_path: Path) -> None:
        expected = make_tdata(tmp_path / "Telegram Desktop" / "profile")
        assert tdata.find_tdata_dir(tmp_path) == expected

    def test_accepts_the_folder_itself(self, tmp_path: Path) -> None:
        folder = make_tdata(tmp_path)
        assert tdata.find_tdata_dir(folder) == folder

    def test_falls_back_to_folder_named_tdata(self, tmp_path: Path) -> None:
        """Марк-файла нет, но папка названа правильно — стоит попробовать."""
        folder = tmp_path / "backup" / "tdata"
        folder.mkdir(parents=True)
        (folder / "settings0").write_bytes(b"x")

        assert tdata.find_tdata_dir(tmp_path) == folder

    def test_missing_tdata_reports_what_is_expected(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("ничего похожего")

        with pytest.raises(InvalidInputError) as exc_info:
            tdata.find_tdata_dir(tmp_path)

        assert tdata.TDATA_MARKER in exc_info.value.message


class TestMaterializeUpload:
    def test_keeps_relative_paths(self, tmp_path: Path) -> None:
        files = [
            (f"tdata/{tdata.TDATA_MARKER}", b"map"),
            ("tdata/D877F783D5D3EF8C/maps", b"inner"),
        ]

        result = tdata.materialize_upload(files, tmp_path)

        assert result == tmp_path / "tdata"
        assert (tmp_path / "tdata" / "D877F783D5D3EF8C" / "maps").read_bytes() == b"inner"

    def test_unpacks_zip_archive(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        make_tdata(source)
        archive = tmp_path / "tdata.zip"
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.write(source / "tdata" / tdata.TDATA_MARKER, f"tdata/{tdata.TDATA_MARKER}")

        workdir = tmp_path / "work"
        workdir.mkdir()
        result = tdata.materialize_upload([("tdata.zip", archive.read_bytes())], workdir)

        assert (result / tdata.TDATA_MARKER).exists()

    def test_broken_archive_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidInputError, match="повреждён"):
            tdata.materialize_upload([("tdata.zip", b"PK not really")], tmp_path)

    def test_path_traversal_is_blocked(self, tmp_path: Path) -> None:
        """Имя файла из архива не должно уводить запись за пределы папки."""
        workdir = tmp_path / "work"
        workdir.mkdir()

        with pytest.raises(InvalidInputError, match="Недопустимый путь"):
            tdata.materialize_upload([("../../escaped.txt", b"nope")], workdir)

        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_absolute_paths_are_pulled_inside(self, tmp_path: Path) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir()

        tdata.materialize_upload([(f"/tdata/{tdata.TDATA_MARKER}", b"map")], workdir)

        assert (workdir / "tdata" / tdata.TDATA_MARKER).exists()


class TestReadingErrors:
    def test_garbage_folder_gives_a_readable_error(self, tmp_path: Path) -> None:
        """Оператор должен понять, что не так с папкой, без чтения трейсбека."""
        folder = make_tdata(tmp_path)

        with pytest.raises(InvalidInputError) as exc_info:
            tdata.list_accounts(folder)

        message = exc_info.value.message
        assert "tdata" in message.lower()
        assert "Traceback" not in message

    def test_empty_folder_is_rejected(self, tmp_path: Path) -> None:
        empty = tmp_path / "tdata"
        empty.mkdir()

        with pytest.raises(InvalidInputError):
            tdata.list_accounts(empty)


class TestCleanup:
    def test_removes_unpacked_secrets(self, tmp_path: Path) -> None:
        """В распакованной tdata лежат ключи — она не должна оставаться на диске."""
        folder = make_tdata(tmp_path / "work")

        tdata.cleanup(tmp_path / "work")

        assert not folder.exists()

    def test_cleanup_of_missing_folder_is_safe(self, tmp_path: Path) -> None:
        tdata.cleanup(tmp_path / "never-existed")


def test_opentele_stack_is_available() -> None:
    """Связка opentele + Qt + шим tgcrypto должна быть в образе.

    Если импорт отвалится, импорт tdata сломается только в момент загрузки
    файлов пользователем — этот тест ловит поломку раньше.
    """
    import tgcrypto
    from opentele.api import UseCurrentSession
    from opentele.td import TDesktop

    assert hasattr(tgcrypto, "ige256_decrypt")
    assert TDesktop is not None
    assert UseCurrentSession is not None
