import os
import time

import pytest

from scripts.cleanup_logs import collect, main


@pytest.fixture
def log_tree(tmp_path, monkeypatch):
    fake_root = tmp_path / "project"
    logs = fake_root / "logs"
    (logs / "jobs").mkdir(parents=True)
    monkeypatch.setattr("scripts.cleanup_logs.ROOT", fake_root)

    old = logs / "run-old.jsonl"
    old.write_text("{}\n")
    old_json = logs / "jobs" / "job-old.json"
    old_json.write_text("{}\n")
    fresh = logs / "run-fresh.jsonl"
    fresh.write_text("{}\n")
    old_time = time.time() - 30 * 24 * 60 * 60
    for path in (old, old_json):
        os.utime(path, (old_time, old_time))
    return logs, old, old_json, fresh


def test_collect_hanya_file_lama(log_tree):
    logs, old, old_json, fresh = log_tree
    found = collect(logs, 14)
    assert old in found and old_json in found
    assert fresh not in found


def test_cleanup_dry_run_tidak_menghapus(log_tree, capsys):
    logs, old, old_json, fresh = log_tree
    assert main(["--days", "14", "--dry-run"]) == 0
    assert old.exists() and old_json.exists() and fresh.exists()
    assert "akan dihapus" in capsys.readouterr().out


def test_cleanup_menghapus_file_lama(log_tree, capsys):
    logs, old, old_json, fresh = log_tree
    assert main(["--days", "14"]) == 0
    assert not old.exists()
    assert not old_json.exists()
    assert fresh.exists()
    assert "2 file dihapus" in capsys.readouterr().out


def test_cleanup_menolak_hari_invalid():
    with pytest.raises(SystemExit):
        main(["--days", "0"])
