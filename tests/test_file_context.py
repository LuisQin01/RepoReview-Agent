from src.file_context import read_file_context


def test_read_file_context_rejects_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do not read me", encoding="utf-8")

    context = read_file_context(
        repo_root=repo,
        file_path="../secret.txt",
        max_chars=100,
    )

    assert context.exists is False
    assert "outside" in context.error

def test_read_file_context_truncates_large_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    target = repo / "app.py"
    target.write_text("abcdef", encoding="utf-8")

    context = read_file_context(repo, "app.py", max_chars=3)

    assert context.exists is True
    assert context.content == "abc"
    assert context.truncated is True
    assert context.chars_read == 3