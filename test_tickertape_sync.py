"""tickertape_sync with a fake GitHub: no network."""
import pandas as pd
import pytest

import tickertape_sync as S


def csv(asof, fscores=True, rows=500):
    t = pd.DataFrame({"symbol": [f"S{i}" for i in range(rows)], "incTrev": 1.0,
                      "pitroskif": 5 if fscores else None, "asof": asof})
    return t.to_csv(index=False).encode()


@pytest.fixture
def world(tmp_path, monkeypatch):
    local = tmp_path / "inputs" / "fundamentals_tickertape.csv"
    monkeypatch.setattr(S, "LOCAL", str(local))
    monkeypatch.setattr(S, "token", lambda: "tok")
    gh = {repo: None for repo, _, _ in S.COPIES}
    uploads = []
    monkeypatch.setattr(S, "remote", lambda repo, path, branch, tok: gh[repo])
    monkeypatch.setattr(S, "upload", lambda repo, path, branch, tok, raw, sha, when: (
        uploads.append((repo, when, sha)), gh.__setitem__(repo, (raw, "new"))))
    return local, gh, uploads


def test_a_newer_file_here_goes_to_both_repositories(world):
    local, gh, uploads = world
    local.parent.mkdir(parents=True)
    local.write_bytes(csv("2026-12-01"))
    gh["dr-azygos/my-formula"] = (csv("2026-11-01"), "a")
    gh["dr-azygos/my_bots"] = (csv("2026-11-01"), "b")
    done = S.sync(lambda m: None)
    assert [(r, w) for r, w, _ in uploads] == [("dr-azygos/my-formula", "2026-12-01"), ("dr-azygos/my_bots", "2026-12-01")]
    assert uploads[0][2] == "a" and len(done) == 2
    assert S.sync(lambda m: None) == []  # all the same now


def test_a_newer_file_on_github_comes_here_and_to_the_other_repo(world):
    local, gh, uploads = world
    local.parent.mkdir(parents=True)
    local.write_bytes(csv("2026-11-01"))
    gh["dr-azygos/my-formula"] = (csv("2026-11-01"), "a")
    gh["dr-azygos/my_bots"] = (csv("2026-12-01"), "b")
    S.sync(lambda m: None)
    assert S.asof(local.read_bytes()) == "2026-12-01"
    assert [r for r, _, _ in uploads] == ["dr-azygos/my-formula"]


def test_a_broken_file_never_spreads(world):
    local, gh, uploads = world
    local.parent.mkdir(parents=True)
    local.write_bytes(csv("2026-12-01", fscores=False))  # the 1 Nov problem: F-scores missing
    gh["dr-azygos/my-formula"] = (csv("2026-11-01"), "a")
    gh["dr-azygos/my_bots"] = (csv("2026-11-01"), "b")
    said = []
    S.sync(said.append)
    assert uploads == [] and any("F-scores" in s for s in said)
    assert S.asof(local.read_bytes()) == "2026-11-01"  # replaced by the last good copy…
    assert (local.parent / "fundamentals_tickertape.rejected.csv").exists()  # …the broken one kept aside


def test_without_a_key_it_only_reads(world, monkeypatch):
    local, gh, uploads = world
    monkeypatch.setattr(S, "token", lambda: "")
    local.parent.mkdir(parents=True)
    local.write_bytes(csv("2026-12-01"))
    gh["dr-azygos/my-formula"] = (csv("2026-11-01"), "a")
    said = []
    S.sync(said.append)
    assert uploads == [] and any("github_token.txt" in s for s in said)


def test_the_background_run_never_breaks_the_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "LOG", str(tmp_path / "log.txt"))
    monkeypatch.setattr(S, "sync", lambda log: 1 / 0)
    S.start_in_background().join(5)
    assert "sync failed: ZeroDivisionError" in (tmp_path / "log.txt").read_text()
