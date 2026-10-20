"""A process killed mid-write (power cut) loses only the item in flight; resume finishes the job;
the console commands still work on top of the snapshot-based workbook build."""

import subprocess
import sys

import respx
from typer.testing import CliRunner

from qexport.cli import app as cli_app
from qexport.store.checkpoint import CheckpointStore
from tests.conftest import ADMIN_EMAIL, ADMIN_TOKEN, APP, SDK_TOKEN
from tests.integration.test_pipeline import _tenant, build

KILL_MID_COMMIT = """
import os, sys
from pathlib import Path
from qexport.store.checkpoint import CheckpointStore
s = CheckpointStore(Path(sys.argv[1]), sys.argv[2])
db = s._db
db.execute("BEGIN IMMEDIATE")
db.execute("DELETE FROM messages WHERE room_id = '491000001'")
db.execute("UPDATE rooms SET fetch_state='done' WHERE room_id = '491000002'")
os._exit(9)          # no COMMIT, no cleanup: what a power cut looks like
"""


@respx.mock
def test_kill_mid_write_then_resume(settings, client, norm, tmp_path):
    path = tmp_path / "e2e.sqlite3"
    store = CheckpointStore(path, APP)
    _tenant()
    orch = build(settings, client, store, norm)
    orch.run(write_xlsx=False, limit_rooms=2)                       # 2 of 5 rooms downloaded
    before = store.progress()
    store.close()
    assert before.done == 2

    rc = subprocess.run([sys.executable, "-c", KILL_MID_COMMIT, str(path), APP]).returncode
    assert rc == 9

    store = CheckpointStore(path, APP)
    assert store.integrity_check() == ["ok"]
    after = store.progress()
    assert (after.done, after.messages) == (before.done, before.messages)   # half-write rolled back
    result = build(settings, client, store, norm).run()
    assert result.exit_code == 0 and store.progress().done == 5 and result.report.complete
    store.close()


def test_cli_write_xlsx_and_status_use_the_snapshot_build(tmp_path, monkeypatch, norm):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"QISCUS_APP_CODE={APP}\nQISCUS_ADMIN_TOKEN={ADMIN_TOKEN}\nQISCUS_SDK_USER_ID={ADMIN_EMAIL}\n"
        f"QISCUS_SDK_TOKEN={SDK_TOKEN}\n", encoding="utf-8")
    from qexport.api.models import RawRoom
    from tests.conftest import raw_room
    s = CheckpointStore(tmp_path / "exports" / f"qexport_{APP}.sqlite3", APP)
    s.upsert_rooms_page([norm.room(RawRoom.model_validate(raw_room(1)))], None)
    s.close()

    runner = CliRunner()
    r = runner.invoke(cli_app, ["status"])
    assert r.exit_code == 0 and "Conversations" in r.output
    r = runner.invoke(cli_app, ["write-xlsx"])
    assert r.exit_code == 0, r.output
    files = list((tmp_path / "exports").glob("*.xlsx"))
    assert len(files) == 1 and files[0].stem.endswith("_PARTIAL")      # 1 room not downloaded
    assert list((tmp_path / "exports").glob(".snapshot-*")) == []
