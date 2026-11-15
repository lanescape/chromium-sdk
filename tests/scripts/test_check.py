import shutil
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO
import check


class WorkflowShape(unittest.TestCase):
    """scripts/check.py's rules for the workflows' shape (specs/0003-ci-cd.md)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        shutil.copytree(REPO/'.github', self.repo/'.github')
        shutil.copy(REPO/'targets.json', self.repo/'targets.json')

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, path, old, new):
        file = self.repo/path
        text = file.read_text()
        self.assertIn(old, text)
        file.write_text(text.replace(old, new, 1))

    def problems(self):
        return check.workflow_problems(self.repo)

    def test_the_repository_passes(self):
        self.assertEqual(self.problems(), [])

    def test_a_linux_call_must_not_grant_id_token(self):
        for workflow in ('ci.yml', 'cd.yml'):
            with self.subTest(workflow=workflow):
                self.tearDown()
                self.setUp()
                self.edit(f'.github/workflows/{workflow}', """    permissions:
      contents: read
    with:
      target: linux-arm64""", """    permissions:
      contents: read
      id-token: write
    with:
      target: linux-arm64""")
                self.assertTrue(any('linux-arm64 must grant exactly contents: read' in p for p in self.problems()))

    def test_every_chain_needs_the_checks(self):
        self.edit('.github/workflows/ci.yml', """  macos-arm64:
    name: macos-arm64
    needs: checks""", """  macos-arm64:
    name: macos-arm64""")
        self.assertIn('.github/workflows/ci.yml: macos-arm64 must need checks', self.problems())

    def test_ci_chains_skip_forks(self):
        self.edit('.github/workflows/ci.yml', """  linux-amd64:
    name: linux-amd64
    needs: checks
    if: github.event.pull_request.head.repo.full_name == github.repository
""", """  linux-amd64:
    name: linux-amd64
    needs: checks
""")
        self.assertTrue(any('linux-amd64 must run only for this repository' in p for p in self.problems()))

    def test_every_target_has_a_chain(self):
        self.edit('.github/workflows/cd.yml', 'target: linux-arm64', 'target: linux-amd64')
        self.assertIn('.github/workflows/cd.yml must call target.yml once for each target in targets.json',
                      self.problems())

    def test_publish_needs_every_chain(self):
        self.edit('.github/workflows/cd.yml', 'needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]',
                  'needs: [version, checks, linux-amd64, linux-arm64]')
        self.assertIn(".github/workflows/cd.yml: publish must need version, checks and every target's chain",
                      self.problems())

    def test_the_chain_is_build_embedder_test(self):
        self.edit('.github/workflows/target.yml', '    needs: embedder\n', '    needs: build\n')
        self.assertTrue(any('target.yml: its jobs must be' in p for p in self.problems()))

    def test_the_chain_never_asks_for_id_token_itself(self):
        self.edit('.github/workflows/target.yml', """    permissions:
      contents: read
""", """    permissions:
      contents: read
      id-token: write
""")
        self.assertTrue(any('target.yml' in p and 'id-token' in p for p in self.problems()))

    def test_the_test_sets_its_own_permissions(self):
        self.edit('.github/workflows/target.yml', """    permissions:
      contents: read
""", '')
        self.assertIn('.github/workflows/target.yml: test must set its permissions, without id-token',
                      self.problems())

    def test_the_apple_sdk_is_fetched_only_for_macos(self):
        self.edit('.github/workflows/target.yml', """      - id: apple-sdk
        if: inputs.target == 'macos-arm64'
        uses: ./.github/actions/apple-sdk""", """      - id: apple-sdk
        uses: ./.github/actions/apple-sdk""")
        self.assertTrue(any('only for macos-arm64' in p for p in self.problems()))

    def test_the_test_runs_on_targets_json_runner(self):
        self.edit('targets.json', 'blacksmith-6vcpu-macos-26', 'blacksmith-12vcpu-macos-26')
        self.assertIn(".github/workflows/target.yml: the test must run on targets.json's test_runner for its target",
                      self.problems())

    def test_only_the_build_uses_the_large_machine(self):
        self.edit('.github/workflows/target.yml', """    needs: build
    runs-on: lanescape-16vcpu-linux-amd64""", """    needs: build
    runs-on: lanescape-80vcpu-linux-amd64-spot""")
        self.assertTrue(any("only target.yml's build runs on" in p for p in self.problems()))


if __name__ == '__main__':
    unittest.main()
