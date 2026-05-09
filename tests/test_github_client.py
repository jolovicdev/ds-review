from src.github_client import is_reviewable_source_path, is_skipped_repo_path


def test_reviewable_source_path_includes_common_code_and_diff_files():
    assert is_reviewable_source_path("src/plugin.lua")
    assert is_reviewable_source_path("patches/fix-cache.patch")
    assert is_reviewable_source_path("Dockerfile.prod")
    assert is_reviewable_source_path("infra/main.tf")


def test_reviewable_source_path_skips_generated_dependency_noise():
    assert is_skipped_repo_path("node_modules/lib/index.js")
    assert is_skipped_repo_path("dist/app.js")
    assert is_skipped_repo_path("src/app.min.js")
    assert is_skipped_repo_path("uv.lock")
    assert not is_reviewable_source_path("node_modules/lib/index.js")
    assert not is_reviewable_source_path("package-lock.json")
