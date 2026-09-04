"""The trusted validator must work without an authenticated `gh`.

CI runs the validator with only the workflow's ephemeral token, and an operator
may run it with none. Tag resolution, annotated-tag peeling, release-asset
downloads and source clones therefore fall back to the public GitHub REST and
HTTPS endpoints whenever `gh` is not authenticated, and keep using `gh` exactly
as before when a token is present.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "validate_real_installed.py"


def load_helper():
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location(
            "validate_real_installed_public_fetch", HELPER
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", hdrs=None, fp=None)  # type: ignore[arg-type]


class PublicFetchFallbackTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper()
        # Force the unauthenticated decision for every test in this class.
        self.helper._gh_authenticated_cache[""] = False
        self.fetch_env = {"PATH": "/bin"}

    def test_lightweight_tag_resolves_over_public_rest(self):
        commit = "a" * 40
        seen: list[tuple[str, str, str]] = []

        def fake_urlopen(request, timeout=0):
            seen.append(
                (
                    request.full_url,
                    request.get_header("Accept"),
                    request.get_header("User-agent"),
                )
            )
            return _FakeResponse(
                json.dumps({"object": {"type": "commit", "sha": commit}}).encode()
            )

        with (
            mock.patch.object(self.helper.urllib.request, "urlopen", fake_urlopen),
            mock.patch.object(self.helper.subprocess, "run") as run,
        ):
            resolved = self.helper.resolve_release_tag_commit(
                "brandyn-s/code-search", "v0.4.0", self.fetch_env
            )

        self.assertEqual(resolved, commit)
        run.assert_not_called()
        self.assertEqual(len(seen), 1)
        url, accept, agent = seen[0]
        self.assertEqual(
            url, "https://api.github.com/repos/brandyn-s/code-search/git/ref/tags/v0.4.0"
        )
        self.assertEqual(accept, "application/vnd.github+json")
        self.assertTrue(agent)

    def test_annotated_tag_is_peeled_over_public_rest(self):
        tag_object = "b" * 40
        commit = "a" * 40
        responses = {
            "https://api.github.com/repos/brandyn-s/code-graph/git/ref/tags/v0.9.0": {
                "object": {"type": "tag", "sha": tag_object}
            },
            f"https://api.github.com/repos/brandyn-s/code-graph/git/tags/{tag_object}": {
                "object": {"type": "commit", "sha": commit}
            },
        }
        urls: list[str] = []

        def fake_urlopen(request, timeout=0):
            urls.append(request.full_url)
            return _FakeResponse(json.dumps(responses[request.full_url]).encode())

        with mock.patch.object(self.helper.urllib.request, "urlopen", fake_urlopen):
            resolved = self.helper.resolve_release_tag_commit(
                "brandyn-s/code-graph", "v0.9.0", self.fetch_env
            )

        self.assertEqual(resolved, commit)
        self.assertEqual(urls, list(responses))

    def test_missing_tag_is_a_clear_error(self):
        def fake_urlopen(request, timeout=0):
            raise _http_error(request.full_url, 404)

        with mock.patch.object(self.helper.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.helper.RealInstallError) as raised:
                self.helper.resolve_release_tag_commit(
                    "brandyn-s/code-search", "v9.9.9", self.fetch_env
                )
        self.assertIn("not found", str(raised.exception))
        self.assertIn("git/ref/tags/v9.9.9", str(raised.exception))

    def test_rate_limit_error_names_the_remedy(self):
        def fake_urlopen(request, timeout=0):
            raise _http_error(request.full_url, 403)

        with mock.patch.object(self.helper.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.helper.RealInstallError) as raised:
                self.helper.github_api_json("repos/x/y/git/ref/tags/v1", self.fetch_env)
        self.assertIn("GH_TOKEN", str(raised.exception))

    def test_release_assets_download_from_public_release_urls(self):
        payloads = {
            "https://github.com/brandyn-s/code-search/releases/download/v0.4.0/a.whl": b"wheel",
            "https://github.com/brandyn-s/code-search/releases/download/v0.4.0/SHA256SUMS": b"sums",
        }
        accepts: list[str] = []

        def fake_urlopen(request, timeout=0):
            accepts.append(request.get_header("Accept"))
            return _FakeResponse(payloads[request.full_url])

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            with (
                mock.patch.object(self.helper.urllib.request, "urlopen", fake_urlopen),
                mock.patch.object(self.helper, "run") as run,
            ):
                self.helper.download_release_assets(
                    "brandyn-s/code-search",
                    "v0.4.0",
                    ["a.whl", "SHA256SUMS"],
                    destination,
                    self.fetch_env,
                )
            run.assert_not_called()
            self.assertEqual((destination / "a.whl").read_bytes(), b"wheel")
            self.assertEqual((destination / "SHA256SUMS").read_bytes(), b"sums")
        self.assertEqual(set(accepts), {"application/octet-stream"})

    def test_clone_uses_plain_git_over_https(self):
        with mock.patch.object(self.helper, "run") as run:
            self.helper.clone_repository(
                "brandyn-s/code-search", Path("/tmp/x"), self.fetch_env
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["git", "clone", "--no-checkout"])
        self.assertIn("https://github.com/brandyn-s/code-search.git", command)


class AuthenticatedPathTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper()
        self.helper._gh_authenticated_cache.clear()

    def test_token_selects_gh_without_probing(self):
        fetch_env = {"PATH": "/bin", "GH_TOKEN": "private-token"}
        with mock.patch.object(self.helper.subprocess, "run") as run:
            self.assertTrue(self.helper.gh_is_authenticated(fetch_env))
        run.assert_not_called()

    def test_token_keeps_gh_api_and_gh_release_download(self):
        fetch_env = {"PATH": "/bin", "GH_TOKEN": "private-token"}
        response = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"object": {"type": "commit", "sha": "a" * 40}})
        )
        with (
            mock.patch.object(self.helper.subprocess, "run", return_value=response) as run,
            mock.patch.object(self.helper.urllib.request, "urlopen") as urlopen,
        ):
            resolved = self.helper.resolve_release_tag_commit(
                "brandyn-s/code-search", "v0.4.0", fetch_env
            )
        self.assertEqual(resolved, "a" * 40)
        self.assertEqual(run.call_args.args[0][:3], ["gh", "api", "--method"])
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "private-token")
        urlopen.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.helper, "run") as run_cmd:
                self.helper.download_release_assets(
                    "brandyn-s/code-search", "v0.4.0", ["a.whl"], Path(tmp), fetch_env
                )
            command = run_cmd.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "release", "download"])
        self.assertIn("--pattern", command)

    def test_unauthenticated_gh_is_detected_from_auth_status(self):
        fetch_env = {"PATH": "/bin"}
        not_logged_in = subprocess.CompletedProcess(
            [], 4, stdout="", stderr="You are not logged into any GitHub hosts."
        )
        with (
            mock.patch.object(self.helper.shutil, "which", return_value="/usr/bin/gh"),
            mock.patch.object(self.helper.subprocess, "run", return_value=not_logged_in),
        ):
            self.assertFalse(self.helper.gh_is_authenticated(fetch_env))
        self.helper._gh_authenticated_cache.clear()
        logged_in = subprocess.CompletedProcess(
            [], 0, stdout="github.com\n  ✓ Logged in to github.com account x", stderr=""
        )
        with (
            mock.patch.object(self.helper.shutil, "which", return_value="/usr/bin/gh"),
            mock.patch.object(self.helper.subprocess, "run", return_value=logged_in),
        ):
            self.assertTrue(self.helper.gh_is_authenticated(fetch_env))


if __name__ == "__main__":
    unittest.main()
