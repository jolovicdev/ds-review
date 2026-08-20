import threading

from src import persistent_state as ps


def test_corrupt_state_file_does_not_crash_readers(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text('{"owner/repo#1": {"pending": Tr')

    assert ps.get_pending_reviews() == []
    assert ps.get_last_commit("owner/repo", 1) == ""


def test_concurrent_writers_do_not_lose_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "STATE_PATH", tmp_path / "state.json")

    def writer(repo, pr_number):
        for _ in range(25):
            ps.save_review(repo, pr_number, pr_number, "sha")

    threads = [
        threading.Thread(target=writer, args=("owner/repo", 1)),
        threading.Thread(target=writer, args=("owner/web", 2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = ps._load()
    assert set(state) == {"owner/repo#1", "owner/web#2"}


def test_save_is_atomic_and_leaves_no_temp_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "STATE_PATH", tmp_path / "state.json")

    ps.save_pending_review("owner/repo", 5, 9)

    assert ps.get_pending_reviews() == [{"repo": "owner/repo", "pr_number": 5, "installation_id": 9}]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.lock"]


def test_requeue_counter_increments_and_clears(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "STATE_PATH", tmp_path / "state.json")

    assert ps.note_requeue("owner/repo", 3) == 1
    assert ps.note_requeue("owner/repo", 3) == 2

    ps.clear_pending("owner/repo", 3)

    assert ps.note_requeue("owner/repo", 3) == 1
