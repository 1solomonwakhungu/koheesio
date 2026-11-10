"""Local source-policy tests. These never dispatch or upload anything."""

import ast
import itertools
import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = "Nike-Inc/koheesio"
FORKS = ("1solomonwakhungu/koheesio", "other/koheesio", "Nike-Inc/koheesio-copy")
OUTCOMES = ("success", "failure", "cancelled", "skipped")
EVENTS = (
    ("pull_request", "refs/pull/42/merge"),
    ("push", "refs/heads/main"),
    ("push", "refs/tags/koheesio-v1.2.3"),
    ("push", "refs/tags/unrelated"),
    ("workflow_dispatch", "refs/heads/main"),
    ("schedule", "refs/heads/main"),
)


def expression_matches(expression, repository, event, ref):
    """Evaluate the supported guard subset without Python eval()."""
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    tree = ast.parse(expression.replace("&&", " and ").replace("||", " or "), mode="eval")
    context = {"repository": repository, "event_name": event, "ref": ref}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in ("true", "false"):
            return node.id == "true"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "github":
                return context[node.attr]
        if isinstance(node, ast.BoolOp):
            values = [visit(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "startsWith" and len(node.args) == 2:
                left, right = (visit(arg) for arg in node.args)
                return left.startswith(right)
            if node.func.id == "always" and not node.args:
                return True
        raise AssertionError("Unsupported workflow guard: " + ast.dump(node))

    return bool(visit(tree))


def dependencies(job):
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


def runnable(job, repository, event, ref, outcomes=None):
    """Model the source guard and GitHub's default successful-needs condition."""
    outcomes = outcomes or {}
    if job.get("if", "").strip() != "always()":
        if any(outcomes.get(name) != "success" for name in dependencies(job)):
            return False
    return expression_matches(job.get("if", "true"), repository, event, ref)


class WorkflowPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {
            path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
        }
        cls.pins = json.loads((ROOT / ".github/workflow-policy/action-pins.json").read_text())

    def test_fork_jobs_cannot_upload_or_publish(self):
        for name in ("release.yml", "codeql.yml"):
            for job_id, job in self.workflows[name]["jobs"].items():
                outcomes = {need: "success" for need in dependencies(job)}
                for repository, (event, ref) in itertools.product(FORKS, EVENTS):
                    with self.subTest(workflow=name, job=job_id, repository=repository, event=event, ref=ref):
                        self.assertFalse(runnable(job, repository, event, ref, outcomes))

    def test_upstream_publisher_requires_release_tag_and_successful_tests_and_build(self):
        job = self.workflows["release.yml"]["jobs"]["publish-pypi"]
        self.assertEqual(set(dependencies(job)), {"tests", "python-artifacts"})
        for test_result, build_result, (event, ref) in itertools.product(OUTCOMES, OUTCOMES, EVENTS):
            expected = (
                test_result == build_result == "success"
                and event == "push"
                and ref.startswith("refs/tags/koheesio-v")
            )
            self.assertEqual(
                runnable(job, UPSTREAM, event, ref, {"tests": test_result, "python-artifacts": build_result}),
                expected,
            )
        self.assertEqual(job["environment"], "release")
        self.assertEqual(job["permissions"], {"id-token": "write"})

    def test_build_requires_tests_and_artifact_is_bound_to_current_run_and_revision(self):
        release = self.workflows["release.yml"]
        test_job = release["jobs"]["tests"]
        self.assertEqual(test_job["uses"], "./.github/workflows/test.yml")
        self.assertNotIn("secrets", test_job)
        build = release["jobs"]["python-artifacts"]
        self.assertEqual(dependencies(build), ["tests"])
        for result in OUTCOMES:
            self.assertEqual(
                runnable(build, UPSTREAM, "push", "refs/tags/koheesio-v1.2.3", {"tests": result}),
                result == "success",
            )
        upload = next(step for step in build["steps"] if "actions/upload-artifact@" in step.get("uses", ""))
        download = next(
            step for step in release["jobs"]["publish-pypi"]["steps"]
            if "actions/download-artifact@" in step.get("uses", "")
        )
        self.assertEqual(upload["with"]["name"], "python-artifacts-${{ github.sha }}")
        self.assertEqual(upload["with"]["name"], download["with"]["name"])
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(set(download["with"]), {"name", "path"})
        for repository in FORKS:
            self.assertFalse(expression_matches(upload["if"], repository, "push", "refs/heads/main"))
        self.assertTrue(release["concurrency"]["group"].startswith("release-"))
        self.assertTrue(self.workflows["test.yml"]["concurrency"]["group"].startswith("test-"))

    def test_events_run_the_full_matrix_without_change_scanning(self):
        workflow = self.workflows["test.yml"]
        self.assertEqual(set(workflow["on"]), {"pull_request", "workflow_dispatch", "workflow_call"})
        self.assertEqual(set(workflow["on"]["pull_request"]), {"branches"})
        self.assertEqual(set(workflow["jobs"]), {"workflow-policy", "tests", "final_check"})
        for job_id in ("workflow-policy", "tests"):
            job = workflow["jobs"][job_id]
            self.assertNotIn("if", job)
            self.assertNotIn("needs", job)
            self.assertNotIn("continue-on-error", job)
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
            for event, ref in EVENTS:
                self.assertTrue(runnable(job, FORKS[0], event, ref))
        self.assertEqual(workflow["jobs"]["tests"]["strategy"]["fail-fast"], "false")

    def test_required_final_check_fails_for_every_unsuccessful_prerequisite(self):
        job = self.workflows["test.yml"]["jobs"]["final_check"]
        self.assertEqual(job["if"], "always()")
        self.assertEqual(set(dependencies(job)), {"tests", "workflow-policy"})
        self.assertEqual(job["permissions"], {})
        self.assertNotIn("continue-on-error", job)
        self.assertEqual(len(job["steps"]), 1)
        step = job["steps"][0]
        self.assertEqual(step["env"], {
            "POLICY_RESULT": "${{ needs.workflow-policy.result }}",
            "TEST_RESULT": "${{ needs.tests.result }}",
        })
        self.assertNotIn("continue-on-error", step)
        for policy_result, test_result in itertools.product(OUTCOMES, repeat=2):
            with self.subTest(policy=policy_result, tests=test_result):
                completed = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                    env={**os.environ, "POLICY_RESULT": policy_result, "TEST_RESULT": test_result},
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(completed.returncode == 0, policy_result == test_result == "success")

    def test_exact_verified_action_paths_and_commits(self):
        seen = set()
        for workflow in self.workflows.values():
            for job in workflow["jobs"].values():
                for entry in [job] + job.get("steps", []):
                    if "uses" not in entry:
                        continue
                    reference = entry["uses"]
                    if reference.startswith("./"):
                        self.assertEqual(reference, "./.github/workflows/test.yml")
                        continue
                    action, sha = reference.rsplit("@", 1)
                    self.assertRegex(sha, r"^[0-9a-f]{40}$")
                    self.assertIn(action, self.pins)
                    pin = self.pins[action]
                    self.assertEqual(sha, pin["sha"])
                    self.assertRegex(pin["action_blob_sha"], r"^[0-9a-f]{40}$")
                    parts = action.split("/")
                    expected_path = "/".join(parts[2:] + ["action.yml"])
                    self.assertEqual(pin["action_path"], expected_path)
                    self.assertEqual(pin["action_url"], "https://github.com/" + "/".join(parts[:2]) +
                                     "/blob/" + sha + "/" + expected_path)
                    seen.add(action)
        self.assertEqual(seen, set(self.pins))

    def test_least_permissions_and_unpersisted_checkout(self):
        permitted_writes = {
            ("release.yml", "publish-pypi"): {"id-token": "write"},
            ("codeql.yml", "analyze"): {"security-events": "write"},
        }
        for name, workflow in self.workflows.items():
            self.assertEqual(workflow["permissions"], {"contents": "read"})
            self.assertNotIn("pull_request_target", workflow["on"])
            self.assertNotIn("workflow_run", workflow["on"])
            for job_id, job in workflow["jobs"].items():
                effective = job.get("permissions", workflow["permissions"])
                self.assertIsInstance(effective, dict)
                writes = {key: value for key, value in effective.items() if value == "write"}
                self.assertEqual(writes, permitted_writes.get((name, job_id), {}))
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertEqual(step["with"]["persist-credentials"], "false")
                        self.assertEqual(step["with"]["ref"], "${{ github.sha }}")
                        self.assertNotIn("repository", step["with"])
                    self.assertNotRegex(step.get("run", ""), r"gh\s+pr\s+merge|--auto\b")
        analyze = self.workflows["codeql.yml"]["jobs"]["analyze"]
        action = next(step for step in analyze["steps"] if "codeql-action/analyze@" in step.get("uses", ""))
        self.assertEqual(action["with"]["upload-database"], "false")

    def test_dependabot_covers_python_and_actions_without_automatic_merging(self):
        self.assertNotIn("auto-merge-dependabot.yml", self.workflows)
        self.assertEqual(set(self.workflows), {"test.yml", "release.yml", "codeql.yml"})
        config = yaml.load((ROOT / ".github/dependabot.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(config["version"], "2")
        self.assertEqual({update["package-ecosystem"] for update in config["updates"]}, {"pip", "github-actions"})
        for update in config["updates"]:
            self.assertEqual(update["directory"], "/")
            self.assertIn(update["schedule"]["interval"], {"weekly", "monthly"})


if __name__ == "__main__":
    unittest.main()
