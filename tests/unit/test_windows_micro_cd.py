"""Unit checks for the Windows updater; no Docker or GitHub traffic."""
import json
import os
import stat
import subprocess
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infra.deploy.windows import update_mjobs_micro as cd


SHA = "a" * 40


def check(name, *, ident=1, status="completed", conclusion="success", app="github-actions", head=SHA):
    return {"name": name, "id": ident, "status": status, "conclusion": conclusion,
            "app": {"slug": app}, "head_sha": head, "started_at": "2026-10-24T00:00:00Z"}


class WindowsCDTests(unittest.TestCase):
    def test_all_verified_exact_named_gates_required(self):
        rows = [check(name, ident=i) for i, name in enumerate(cd.REQUIRED, start=1)]
        with patch.object(cd, "command", return_value=json.dumps(
                [{"check_runs": rows, "total_count": len(rows)}])):
            self.assertTrue(cd.checks_pass("a" * 40))

    def test_missing_pending_skipped_or_fake_app_rejected(self):
        cases = [[], [check(cd.REQUIRED[0], status="in_progress")],
                 [check(cd.REQUIRED[0], conclusion="skipped")],
                 [check(cd.REQUIRED[0], app="unknown")]]
        for rows in cases:
            with self.subTest(rows=rows), patch.object(cd, "command", return_value=json.dumps(
                    [{"check_runs": rows, "total_count": len(rows)}])):
                self.assertFalse(cd.checks_pass("a" * 40))

    def test_newer_failed_check_overrides_earlier_green(self):
        rows = [check(name, ident=i) for i, name in enumerate(cd.REQUIRED, start=1)]
        rows.append(check(cd.REQUIRED[0], ident=100, conclusion="failure"))
        with patch.object(cd, "command", return_value=json.dumps(
                [{"check_runs": rows, "total_count": len(rows)}])):
            self.assertFalse(cd.checks_pass("a" * 40))

    def test_ambiguous_check_page_blocks(self):
        with patch.object(cd, "command", return_value=json.dumps(
                [{"check_runs": [], "total_count": 101}])):
            with self.assertRaises(RuntimeError):
                cd.checks_pass("a" * 40)

    def test_migration_revision_change_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "migrations").mkdir()
            (root / "migrations" / "a.py").write_text("revision='1'\n", encoding="utf-8")
            (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
            before = cd.migration_digest(root)
            (root / "migrations" / "a.py").write_text("revision='2'\n", encoding="utf-8")
            self.assertNotEqual(before, cd.migration_digest(root))

    def test_container_mode_is_personal_and_public_host_is_explicit(self):
        with patch.object(cd, "command") as executor:
            cd.compose(Path("foo"), "b" * 40, "mjobs-micro:sha-" + "b" * 40, "ps")
            args, kwargs = executor.call_args
            self.assertEqual(kwargs["env"]["MJOBS_COMPOSE_MOCK"], "0")
            self.assertEqual(kwargs["env"]["MJOBS_OPERATION_MODE"], "PERSONAL")
            self.assertEqual(kwargs["env"]["MJOBS_PUBLIC_HOST"], cd.PUBLIC_HOST)
            self.assertEqual(kwargs["env"]["MJOBS_PORT"], "18801")
            self.assertEqual(args[0][-1], "ps")

    def test_complete_pagination_can_find_gates_after_first_page(self):
        rows = [check("unrelated", ident=i) for i in range(1, 101)]
        rows += [check(name, ident=i) for i, name in enumerate(cd.REQUIRED, start=101)]
        pages = [{"total_count": len(rows), "check_runs": rows[:100]},
                 {"total_count": len(rows), "check_runs": rows[100:]}]
        with patch.object(cd, "command", return_value=json.dumps(pages)) as executor:
            self.assertTrue(cd.checks_pass(SHA))
        args = executor.call_args.args[0]
        self.assertIn("--paginate", args)
        self.assertIn("--slurp", args)
        self.assertIn("filter=all&per_page=100", args[-1])

    def test_inconsistent_duplicate_or_other_head_pages_are_rejected(self):
        row = check(cd.REQUIRED[0])
        cases = [
            [{"total_count": 2, "check_runs": [row]}, {"total_count": 3, "check_runs": []}],
            [{"total_count": 2, "check_runs": [row]}, {"total_count": 2, "check_runs": [row]}],
            [{"total_count": 1, "check_runs": [check(cd.REQUIRED[0], head="b" * 40)]}],
            [{"total_count": 1, "check_runs": [check(cd.REQUIRED[0], ident=True)]}],
            {"total_count": 1, "check_runs": [row]},
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(cd, "command", return_value=json.dumps(pages)):
                with self.assertRaises(RuntimeError):
                    cd.checks_pass(SHA)

    def test_newer_queued_run_without_start_time_supersedes_green(self):
        rows = [check(name, ident=i) for i, name in enumerate(cd.REQUIRED, start=1)]
        queued = check(cd.REQUIRED[0], ident=100, status="queued", conclusion=None)
        queued["started_at"] = None
        rows.append(queued)
        with patch.object(cd, "command", return_value=json.dumps(
                [{"total_count": len(rows), "check_runs": rows}])):
            self.assertFalse(cd.checks_pass(SHA))

    def test_bad_sha_is_rejected_before_github_or_clone(self):
        with patch.object(cd, "command") as executor:
            for value in ("../release", "abc", None):
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    cd.check_runs(value)
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    cd.prepare_candidate(value)
            executor.assert_not_called()


class CandidateReuseTests(unittest.TestCase):
    """Exercise actual Git metadata and files, without a network or Docker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.source = self.home / "releases" / "seed"
        self.source.mkdir(parents=True)
        self.git("init", "-b", "main")
        (self.source / ".gitignore").write_text(".env\n*.cache\n", encoding="utf-8")
        (self.source / "compose.yml").write_text("MJOBS_PUBLIC_HOST MJOBS_IMAGE\n", encoding="utf-8")
        (self.source / "migrations").mkdir()
        (self.source / "migrations/a.py").write_text("revision='1'\n", encoding="utf-8")
        (self.source / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Updater Test", "-c", "user.email=updater@example.test",
                 "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        self.git("remote", "add", "origin", f"https://github.com/{cd.REPOSITORY}.git")
        self.sha = self.git("rev-parse", "HEAD")
        target = self.source.with_name(self.sha)
        self.source.rename(target)
        self.source = target
        home_patch = patch.object(cd, "HOME", self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", *args], cwd=self.source, env=env,
                                       text=True, stderr=subprocess.PIPE).strip()

    def assert_blocked(self):
        with self.assertRaises(RuntimeError):
            cd.prepare_candidate(self.sha)
        self.assertTrue(self.source.exists(), "Rejected candidates must be retained")

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"Host cannot create test symlink: {error}")

    def test_clean_clone_with_only_known_overlays_is_reused_without_clone(self):
        (self.source / ".env").write_text("PRIVATE_TEST_VALUE=fixture\n", encoding="utf-8")
        (self.source / "compose.micro.yml").write_text("networks: {}\n", encoding="utf-8")
        with patch.object(cd, "command", wraps=cd.command) as executor:
            self.assertEqual(cd.prepare_candidate(self.sha), self.source)
            self.assertEqual(cd.prepare_candidate(self.sha), self.source)
        self.assertFalse(any("clone" in call.args[0] or "docker" in call.args[0]
                             for call in executor.call_args_list))

    def test_fresh_clone_uses_verifier_line_endings_despite_host_autocrlf(self):
        (self.source / ".gitattributes").write_bytes(b"*.py text eol=lf\n*.yml text eol=lf\n")
        (self.source / "frontend.tsx").write_bytes(b"export const value = 1;\nexport const next = 2;\n")
        self.git("add", ".gitattributes", "frontend.tsx")
        self.git("add", "--renormalize", ".")
        self.git("-c", "user.name=Updater Test", "-c", "user.email=updater@example.test",
                 "-c", "commit.gpgsign=false", "commit", "-m", "line ending fixture")
        sha = self.git("rev-parse", "HEAD")
        clone_from = self.source.as_uri()
        real_command = cd.command
        clone_calls = []

        def local_clone(args, **kwargs):
            if args[0] == "git" and "clone" in args:
                self.assertEqual(args[-2], f"https://github.com/{cd.REPOSITORY}.git")
                clone_calls.append(kwargs.get("env"))
                local_args = [*args[:-2], clone_from, args[-1]]
                result = real_command(local_args, **kwargs)
                real_command(["git", "remote", "set-url", "origin", args[-2]],
                             cwd=args[-1], env=kwargs.get("env"))
                return result
            return real_command(args, **kwargs)

        for kind in ("global", "system"):
            with self.subTest(config=kind):
                config = Path(self.temp.name) / (kind + ".gitconfig")
                config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
                env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                env["GIT_CONFIG_GLOBAL"] = str(config) if kind == "global" else os.devnull
                if kind == "system":
                    env["GIT_CONFIG_SYSTEM"] = str(config)
                else:
                    env["GIT_CONFIG_NOSYSTEM"] = "1"
                home = Path(self.temp.name) / ("fresh-" + kind)
                with patch.dict(os.environ, env, clear=True), patch.object(cd, "HOME", home), \
                        patch.object(cd, "log"), \
                        patch.object(cd, "command", side_effect=local_clone):
                    candidate = cd.prepare_candidate(sha)
                    self.assertEqual((candidate / "frontend.tsx").read_bytes(),
                                     b"export const value = 1;\nexport const next = 2;\n")
                    self.assertEqual(clone_calls[-1]["GIT_CONFIG_NOSYSTEM"], "1")
                    self.assertEqual(clone_calls[-1]["GIT_CONFIG_GLOBAL"], os.devnull)
                    self.assertNotIn("GIT_CONFIG_SYSTEM", clone_calls[-1])
                    (candidate / "frontend.tsx").write_bytes(b"edited\n")
                    with self.assertRaisesRegex(RuntimeError, "tracked files changed"):
                        cd.verify_candidate(candidate, sha)

    def test_wrong_origin_is_rejected(self):
        self.git("remote", "set-url", "origin", "https://github.com/other/MJobs.git")
        self.assert_blocked()

    def test_wrong_commit_is_rejected_even_when_tracked_tree_is_clean(self):
        self.git("-c", "user.name=Updater Test", "-c", "user.email=updater@example.test",
                 "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "other")
        self.assert_blocked()

    def test_tracked_changes_are_rejected(self):
        (self.source / "compose.yml").write_text("changed\n", encoding="utf-8")
        self.assert_blocked()
        self.git("add", "compose.yml")
        self.assert_blocked()

    def test_index_flags_cannot_hide_changed_tracked_files(self):
        self.git("update-index", "--assume-unchanged", "compose.yml")
        (self.source / "compose.yml").write_text("hidden change\n", encoding="utf-8")
        self.assert_blocked()

    def test_unknown_untracked_and_ignored_files_are_rejected_and_preserved(self):
        for name in ("unknown.txt", "build.cache", " .env"):
            with self.subTest(name=name):
                item = self.source / name
                item.write_text("preserve this\n", encoding="utf-8")
                self.assert_blocked()
                self.assertEqual(item.read_text(encoding="utf-8"), "preserve this\n")
                item.unlink()

    def test_git_directory_redirection_is_rejected(self):
        metadata = self.source / ".git"
        relocated = Path(self.temp.name) / "git-metadata"
        metadata.rename(relocated)
        metadata.write_text(f"gitdir: {relocated.as_posix()}\n", encoding="utf-8")
        self.assert_blocked()

    def test_unknown_git_config_and_external_objects_are_rejected(self):
        self.git("config", "include.path", str(Path(self.temp.name) / "external-config"))
        self.assert_blocked()
        self.git("config", "--unset", "include.path")
        (self.source / ".git/objects/info/alternates").write_text("/outside/objects\n", encoding="utf-8")
        self.assert_blocked()

    def test_overlay_symlink_is_rejected_without_touching_target(self):
        target = Path(self.temp.name) / "private.txt"
        target.write_text("untouched\n", encoding="utf-8")
        self.symlink(self.source / ".env", target)
        self.assert_blocked()
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched\n")

    def test_symlinked_release_ancestor_is_rejected(self):
        releases = self.source.parent
        actual = self.home / "actual-releases"
        releases.rename(actual)
        self.symlink(releases, actual, directory=True)
        self.assert_blocked()

    def test_windows_junction_attribute_is_rejected(self):
        real_lstat = Path.lstat
        redirected = self.source.parent

        def lstat(path, *args, **kwargs):
            if path == redirected:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            with self.assertRaises(RuntimeError):
                cd.prepare_candidate(self.sha)

    def test_reused_candidate_does_not_bypass_changed_migrations(self):
        baseline = Path(self.temp.name) / "baseline"
        (baseline / "migrations").mkdir(parents=True)
        (baseline / "migrations/a.py").write_text("revision='old'\n", encoding="utf-8")
        (baseline / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        state = self.home / "state.json"
        state.write_text(json.dumps({"sha": "b" * 40,
                                    "image": "mjobs-micro:sha-" + "b" * 40,
                                    "source": str(baseline)}), encoding="utf-8")
        python = Path(self.temp.name) / "python.exe"
        python.touch()
        with patch.object(cd, "STATE", state), patch.object(cd, "BASE", baseline), \
                patch.object(cd, "OPS_PYTHON", python), patch.object(cd, "main_sha", return_value=self.sha), \
                patch.object(cd, "checks_pass", return_value=True), \
                patch.object(cd, "compose") as compose, patch.object(cd, "command", wraps=cd.command) as executor:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "New migrations require offline guarded review"):
                    cd.run_update()
            compose.assert_not_called()
            self.assertFalse(any("docker" in call.args[0] for call in executor.call_args_list))
        self.assertTrue(self.source.exists())

    def test_fresh_main_and_checks_are_still_required_before_stopping_writers(self):
        baseline = Path(self.temp.name) / "baseline"
        (baseline / "migrations").mkdir(parents=True)
        (baseline / "migrations/a.py").write_text("revision='1'\n", encoding="utf-8")
        (baseline / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        (baseline / ".env").write_text("TEST_VALUE=fixture\n", encoding="utf-8")
        (baseline / "compose.micro.yml").write_text("networks: {}\n", encoding="utf-8")
        state = self.home / "state.json"
        state.write_text(json.dumps({"sha": "b" * 40,
                                    "image": "mjobs-micro:sha-" + "b" * 40,
                                    "source": str(baseline)}), encoding="utf-8")
        python = Path(self.temp.name) / "python.exe"
        python.touch()
        real_command = cd.command

        def command(args, **kwargs):
            if args[:2] == ["docker", "build"]:
                return ""
            return real_command(args, **kwargs)

        for heads, checks in (([self.sha, "c" * 40], [True]),
                              ([self.sha, self.sha], [True, False])):
            with self.subTest(heads=heads, checks=checks), \
                    patch.object(cd, "STATE", state), patch.object(cd, "BASE", baseline), \
                    patch.object(cd, "OPS_PYTHON", python), \
                    patch.object(cd, "main_sha", side_effect=heads), \
                    patch.object(cd, "checks_pass", side_effect=checks), \
                    patch.object(cd, "compose") as compose, \
                    patch.object(cd, "log"), patch.object(cd, "command", side_effect=command):
                with self.assertRaisesRegex(RuntimeError, "superseded or checks changed"):
                    cd.run_update()
                compose.assert_not_called()
        self.assertEqual(json.loads(state.read_text())["sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
