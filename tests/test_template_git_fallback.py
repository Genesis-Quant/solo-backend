"""Template discovery falls back to Git when the Gitee REST quota is exhausted."""

import subprocess

import pytest

from core.apps.projects import templates

SCHEME = "genesis-quant/solo-algo-scheme"


@pytest.fixture(autouse=True)
def rest_quota_exhausted(monkeypatch):
    def denied(*args, **kwargs):
        raise templates.TemplateError("rate limited")

    monkeypatch.setattr(templates, "gitee_get", denied)
    templates._cache.clear()
    templates.project_metadata.cache_clear()
    yield
    templates._cache.clear()
    templates.project_metadata.cache_clear()


def test_tags_use_peeled_commits_from_git(monkeypatch):
    lightweight, annotated, peeled = "1" * 40, "2" * 40, "3" * 40
    calls = []

    def git(*arguments):
        calls.append(arguments)
        return (f"{lightweight}\trefs/tags/v1.2.2\n{annotated}\trefs/tags/v1.2.3\n"
                f"{peeled}\trefs/tags/v1.2.3^{{}}\n").encode()

    monkeypatch.setattr(templates, "git_output", git)
    versions = templates.repository_versions(SCHEME, refresh=True)
    assert [(item.tag, item.commit) for item in versions] == [("v1.2.3", peeled), ("v1.2.2", lightweight)]
    assert calls == [("ls-remote", "--tags", f"https://gitee.com/{SCHEME}.git")]


def test_metadata_reads_the_exact_commit_file(monkeypatch):
    commit = "a" * 40
    calls = []

    def git(*arguments):
        calls.append(arguments[2:])
        return b'[project]\nname = "scheme"\nversion = "1.2.3"\n' if arguments[2] == "show" else b""

    monkeypatch.setattr(templates, "git_output", git)
    assert templates.project_metadata(SCHEME, commit, "pyproject.toml")["version"] == "1.2.3"
    assert calls == [("init", "--bare"), ("fetch", "--depth=1", "--no-tags", f"https://gitee.com/{SCHEME}.git", commit),
                     ("show", "FETCH_HEAD:pyproject.toml")]


def test_git_failures_and_invalid_commits_remain_template_errors(monkeypatch):
    def failed(*arguments):
        raise subprocess.CalledProcessError(128, ["git", *arguments])

    monkeypatch.setattr(templates, "git_output", failed)
    with pytest.raises(templates.TemplateError):
        templates.repository_versions(SCHEME, refresh=True)
    with pytest.raises(templates.TemplateError):
        templates.project_metadata(SCHEME, "b" * 40, "pyproject.toml")
    with pytest.raises(templates.TemplateError, match="commit"):
        templates.project_metadata(SCHEME, "not-a-commit", "pyproject.toml")
