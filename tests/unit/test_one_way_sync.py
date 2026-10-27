"""-r and -l only overwrite a file without asking if the target side did not change it since the last sync."""

import io
import os
import time
import zipfile

import pytest
from click.testing import CliRunner

from olsync import auth as olauth
from olsync import olsync as cli


class FakeOverleaf:
    """In-memory Overleaf project that keeps its files across several `ols` runs."""

    files = {}
    uploads = []

    def __init__(self, cookie=None, csrf=None, base_url="https://www.overleaf.com"):
        self.base_url = base_url

    def get_project(self, name):
        # lastUpdated always "newer" than local files, the case where -r used to overwrite local edits silently
        return {"id": "p1", "name": name, "lastUpdated": "2030-01-01T00:00:00.000Z"}

    def get_project_infos(self, project_id):
        return {"rootFolder": [{"_id": "root", "docs": [], "fileRefs": [], "folders": []}]}

    def download_project(self, project_id):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in FakeOverleaf.files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def upload_file(self, project_id, project_infos, name, size, file):
        FakeOverleaf.files[name] = file.read()
        FakeOverleaf.uploads.append(name)
        return True

    def delete_file(self, project_id, project_infos, name):
        FakeOverleaf.files.pop(name, None)
        return True


@pytest.fixture
def project(tmp_path, monkeypatch):
    project_dir = tmp_path / "Thesis"
    project_dir.mkdir()
    olauth.save_auth(project_dir / ".olauth", {"overleaf_session2": "s"}, "c", "https://www.overleaf.com")
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(cli, "OverleafClient", FakeOverleaf)
    FakeOverleaf.files = {}
    FakeOverleaf.uploads = []
    return project_dir


def ols(*args, input=None):
    result = CliRunner().invoke(cli.main, list(args), input=input, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def edit_locally(path, content):
    path.write_bytes(content)
    old = time.time() - 3600  # older than the project's lastUpdated
    os.utime(path, (old, old))


# --- -r: remote -> local ----------------------------------------------------------------------------------------


def test_remote_only_overwrites_files_changed_only_on_overleaf(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    FakeOverleaf.files["main.tex"] = b"v2 from Overleaf"
    output = ols("-r")
    assert (project / "main.tex").read_bytes() == b"v2 from Overleaf"
    assert "Warning" not in output


def test_remote_only_asks_before_overwriting_a_local_edit(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"local edit")
    FakeOverleaf.files["main.tex"] = b"remote edit"
    output = ols("-r", input="n\n")
    assert "main.tex" in output and "changed" in output
    assert (project / "main.tex").read_bytes() == b"local edit"


def test_remote_only_overwrites_a_local_edit_when_confirmed(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"local edit")
    ols("-r", input="y\n")
    assert (project / "main.tex").read_bytes() == b"v1"


def test_remote_only_asks_without_sync_history(project):
    FakeOverleaf.files = {"main.tex": b"remote"}
    edit_locally(project / "main.tex", b"local")
    ols("-r", input="n\n")
    assert (project / "main.tex").read_bytes() == b"local"


# --- -l: local -> remote ----------------------------------------------------------------------------------------


def test_local_only_uploads_files_changed_only_locally(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"v2 local")
    output = ols("-l")
    assert FakeOverleaf.files["main.tex"] == b"v2 local"
    assert "Warning" not in output


def test_local_only_asks_before_overwriting_an_edit_on_overleaf(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    (project / "main.tex").write_bytes(b"local edit")  # local file is *newer*, -l used to upload without asking
    FakeOverleaf.files["main.tex"] = b"remote edit"
    output = ols("-l", input="n\n")
    assert "main.tex" in output and "changed" in output
    assert FakeOverleaf.files["main.tex"] == b"remote edit"


def test_local_only_asks_without_sync_history(project):
    FakeOverleaf.files = {"main.tex": b"remote"}
    (project / "main.tex").write_bytes(b"local")
    ols("-l", input="n\n")
    assert FakeOverleaf.files["main.tex"] == b"remote"


# --- state ------------------------------------------------------------------------------------------------------


def test_one_way_syncs_record_the_state_for_later_syncs(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols("-r")
    # after -r the files are known to be in sync, so a later local edit is uploaded by a two-way sync without asking
    edit_locally(project / "main.tex", b"v2 local")
    output = ols()
    assert FakeOverleaf.files["main.tex"] == b"v2 local"
    assert "Conflict" not in output


def test_skipped_file_stays_unsynced(project):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"local edit")
    FakeOverleaf.files["main.tex"] = b"remote edit"
    ols("-r", input="n\n")
    output = ols("-r", input="n\n")
    assert "main.tex" in output and "changed" in output


# --- failed uploads ---------------------------------------------------------------------------------------------


def test_local_only_reports_a_rejected_upload_instead_of_success(project, monkeypatch):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"v2 local")
    # upload_file signals a rejected upload (e.g. HTTP 422) by returning False
    monkeypatch.setattr(FakeOverleaf, "upload_file", lambda self, *args: False)
    result = CliRunner().invoke(cli.main, ["-l"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "Synced files from [local] to [remote]" not in result.output
    assert FakeOverleaf.files["main.tex"] == b"v1"


def test_rejected_upload_is_not_recorded_as_synced(project, monkeypatch):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    edit_locally(project / "main.tex", b"v2 local")
    real_upload = FakeOverleaf.upload_file
    monkeypatch.setattr(FakeOverleaf, "upload_file", lambda self, *args: False)
    CliRunner().invoke(cli.main, ["-l"], catch_exceptions=False)
    monkeypatch.setattr(FakeOverleaf, "upload_file", real_upload)
    # the local edit is still pending, so the next two-way sync uploads it without a conflict
    output = ols()
    assert FakeOverleaf.files["main.tex"] == b"v2 local"
    assert "Conflict" not in output


def test_rejected_remote_deletion_fails_without_reporting_success(project, monkeypatch):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    (project / "main.tex").unlink()
    monkeypatch.setattr(FakeOverleaf, "delete_file", lambda self, *args: False)
    result = CliRunner().invoke(cli.main, ["-l"], input="d\n", catch_exceptions=False)
    assert result.exit_code != 0
    assert "deleting" in result.output
    assert "Synced files from [local] to [remote]" not in result.output
    assert FakeOverleaf.files["main.tex"] == b"v1"


@pytest.mark.parametrize("restore", [False, True])
def test_failed_post_upload_refresh_does_not_create_false_conflict(project, monkeypatch, restore):
    if restore:
        FakeOverleaf.files = {}
        (project / "main.tex").write_bytes(b"v2")
        args, answer = ["-r"], "r\n"
    else:
        FakeOverleaf.files = {"main.tex": b"v1"}
        ols()
        (project / "main.tex").write_bytes(b"v2")
        args, answer = ["-l"], None
    real_download = FakeOverleaf.download_project
    calls = 0

    def fail_refresh(self, project_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("temporary refresh failure")
        return real_download(self, project_id)

    with monkeypatch.context() as patch:
        patch.setattr(FakeOverleaf, "download_project", fail_refresh)
        output = ols(*args, input=answer)
    assert "Warning" in output and "refresh" in output
    assert FakeOverleaf.files["main.tex"] == b"v2"
    (project / "main.tex").write_bytes(b"v3")
    output = ols(input="s\n")
    assert "Conflict" not in output
    assert FakeOverleaf.files["main.tex"] == b"v3"


@pytest.mark.parametrize("mode", ["two-way", "local-only", "restore"])
def test_edit_during_upload_is_synced_on_the_next_run(project, monkeypatch, mode):
    if mode == "restore":
        FakeOverleaf.files = {}
        (project / "main.tex").write_bytes(b"v2")
        args, answer = ["-r"], "r\n"
    else:
        FakeOverleaf.files = {"main.tex": b"v1"}
        ols()
        (project / "main.tex").write_bytes(b"v2")
        args, answer = (["-l"] if mode == "local-only" else []), None
    real_upload = FakeOverleaf.upload_file

    def upload_then_edit(self, *args):
        result = real_upload(self, *args)
        (project / "main.tex").write_bytes(b"v3 saved during sync")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(FakeOverleaf, "upload_file", upload_then_edit)
        ols(*args, input=answer)
    assert FakeOverleaf.files["main.tex"] == b"v2"
    output = ols(input="s\n")
    assert "Conflict" not in output
    assert FakeOverleaf.files["main.tex"] == b"v3 saved during sync"


@pytest.mark.parametrize("args", [[], ["-r"]])
def test_edit_after_download_is_synced_on_the_next_run(project, monkeypatch, args):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    FakeOverleaf.files["main.tex"] = b"v2 remote"
    real_write = cli.write_file

    def write_then_edit(name, content):
        real_write(name, content)
        (project / name).write_bytes(b"v3 saved during sync")

    with monkeypatch.context() as patch:
        patch.setattr(cli, "write_file", write_then_edit)
        ols(*args)
    output = ols(input="s\n")
    assert "Conflict" not in output
    assert FakeOverleaf.files["main.tex"] == b"v3 saved during sync"


@pytest.mark.parametrize("edited_side", ["local", "remote"])
def test_edit_to_an_unrelated_file_during_upload_remains_pending(project, monkeypatch, edited_side):
    FakeOverleaf.files = {"main.tex": b"v1"}
    ols()
    (project / "note.txt").write_bytes(b"new file")
    real_upload = FakeOverleaf.upload_file

    def upload_then_edit(self, *args):
        result = real_upload(self, *args)
        if edited_side == "local":
            (project / "main.tex").write_bytes(b"v2 edited during sync")
        else:
            FakeOverleaf.files["main.tex"] = b"v2 edited during sync"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(FakeOverleaf, "upload_file", upload_then_edit)
        ols()
    output = ols(input="s\n")
    assert "Conflict" not in output
    assert FakeOverleaf.files["main.tex"] == b"v2 edited during sync"
    assert (project / "main.tex").read_bytes() == b"v2 edited during sync"


def test_edit_mid_transfer_does_not_change_the_uploaded_snapshot(project, monkeypatch):
    original = b"a" * 131072
    edited = b"b" * 131072
    (project / "main.tex").write_bytes(original)

    def edit_mid_upload(self, project_id, infos, name, size, file):
        first_chunk = file.read(16384)
        (project / name).write_bytes(edited)
        FakeOverleaf.files[name] = first_chunk + file.read()
        assert size == len(FakeOverleaf.files[name])
        return True

    with monkeypatch.context() as patch:
        patch.setattr(FakeOverleaf, "upload_file", edit_mid_upload)
        ols()
    assert FakeOverleaf.files["main.tex"] == original
    output = ols(input="s\n")
    assert "Conflict" not in output
    assert FakeOverleaf.files["main.tex"] == edited


@pytest.mark.parametrize("args", [["-l"], ["-r"]])
def test_one_way_overwrite_shows_a_conflict_preview(project, args):
    FakeOverleaf.files = {"main.tex": b"remote edit\n"}
    (project / "main.tex").write_bytes(b"local edit\n")
    output = ols(*args, input="n\n")
    assert "--- local/main.tex" in output and "+++ remote/main.tex" in output
    assert output.index("+remote edit") < output.index("Overwrite it")
    assert FakeOverleaf.files["main.tex"] == b"remote edit\n"
    assert (project / "main.tex").read_bytes() == b"local edit\n"
