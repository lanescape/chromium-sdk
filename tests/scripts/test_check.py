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
                self.assertIn(f'.github/workflows/{workflow}: linux-arm64 must have exactly contents: read',
                              self.problems())

    def test_every_chain_needs_the_checks(self):
        self.edit('.github/workflows/ci.yml', """  macos-arm64:
    name: macos-arm64
    needs: checks""", """  macos-arm64:
    name: macos-arm64""")
        self.assertIn('.github/workflows/ci.yml: macos-arm64 must need exactly checks', self.problems())

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
        self.assertIn(".github/workflows/cd.yml: publish must need version, checks and every target's chain, and run "
                      "only if they all succeed", self.problems())

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
        self.assertIn('.github/workflows/target.yml: test must have exactly contents: read', self.problems())

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

    def test_the_test_runner_is_read_from_the_job_not_a_comment(self):
        self.edit('.github/workflows/target.yml', """    runs-on: ${{ fromJSON(""",
                  """    runs-on: lanescape-16vcpu-linux-amd64
    # runs-on: ${{ fromJSON(""")
        self.assertIn(".github/workflows/target.yml: the test must run on targets.json's test_runner for its target",
                      self.problems())

    def test_publish_runs_only_if_every_chain_succeeded(self):
        self.edit('.github/workflows/cd.yml', """    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]
""", """    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]
    if: always()
""")
        problems = self.problems()
        self.assertIn(".github/workflows/cd.yml: publish must need version, checks and every target's chain, and run "
                      "only if they all succeed", problems)
        self.assertTrue(any('no job or step may run past a failure' in p for p in problems))

    def test_a_chain_job_runs_only_after_its_predecessor_succeeded(self):
        self.edit('.github/workflows/target.yml', """    needs: build
""", """    needs: build
    if: ${{ inputs.target != '' }}
""")
        self.assertIn('.github/workflows/target.yml: embedder must run only if the job it needs succeeded',
                      self.problems())

    def test_nothing_runs_past_a_failure(self):
        for workflow, old, new in (
                ('target.yml', '    needs: embedder\n', '    needs: embedder\n    continue-on-error: true\n'),
                ('target.yml', '    needs: build\n', '    needs: build\n    if: ${{ !cancelled() }}\n'),
                ('checks.yml', '      - name: Script tests\n', '      - name: Script tests\n'
                                                               '        continue-on-error: true\n')):
            with self.subTest(workflow=workflow, new=new):
                self.tearDown()
                self.setUp()
                self.edit(f'.github/workflows/{workflow}', old, new)
                self.assertTrue(any('no job or step may run past a failure' in p for p in self.problems()))

    def test_target_yml_leaves_the_build_and_embedder_their_call_s_grant(self):
        for old, new, problem in (
                ('defaults:\n', 'permissions: {}\n\ndefaults:\n',
                 'target.yml must set no permissions for the whole'),
                ("""    runs-on: lanescape-80vcpu-linux-amd64-spot
""", """    runs-on: lanescape-80vcpu-linux-amd64-spot
    permissions:
      contents: read
""", 'target.yml: build must have no permissions of its own')):
            with self.subTest(problem=problem):
                self.tearDown()
                self.setUp()
                self.edit('.github/workflows/target.yml', old, new)
                self.assertTrue(any(problem in p for p in self.problems()))

    def test_only_publish_writes(self):
        self.edit('.github/workflows/cd.yml', """    runs-on: lanescape-2vcpu-linux-amd64
    permissions:
      contents: read
    outputs:""", """    runs-on: lanescape-2vcpu-linux-amd64
    permissions:
      contents: write
    outputs:""")
        self.assertIn('.github/workflows/cd.yml: version must have exactly contents: read', self.problems())

    def test_an_inline_permission_map_is_refused(self):
        self.edit('.github/workflows/ci.yml', """    permissions:
      contents: read
    with:
      target: linux-amd64""", """    permissions: write-all
    with:
      target: linux-amd64""")
        self.assertIn('.github/workflows/ci.yml: linux-amd64 must have exactly contents: read', self.problems())

    def test_the_workflows_grant_nothing_by_default(self):
        self.edit('.github/workflows/ci.yml', 'permissions: {}\n', 'permissions: read-all\n')
        self.assertIn('.github/workflows/ci.yml must set `permissions: {}` for the whole workflow', self.problems())

    def test_keys_are_plain_and_followed_by_their_colon(self):
        for workflow, old, new in (
                ('cd.yml', '  version:\n', '  "version":\n'),
                ('cd.yml', '    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]\n',
                 '    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]\n'
                 '    "if": ${{ success() || !success() }}\n'),
                ('target.yml', '      - name: Run the test embedder\n',
                 '      - name: Run the test embedder\n        continue-on-error : true\n'),
                ('ci.yml', '      contents: read\n', "      'contents': read\n")):
            with self.subTest(new=new):
                self.tearDown()
                self.setUp()
                self.edit(f'.github/workflows/{workflow}', old, new)
                self.assertTrue(any(f'.github/workflows/{workflow}:' in p and 'plain key' in p
                                    for p in self.problems()))

    def test_the_yaml_has_no_structure_the_check_cannot_read(self):
        for workflow, old, new, problem in (
                ('cd.yml', '  version:\n    name: Version\n', '  version:\n     name: Version\n', 'indent'),
                ('cd.yml', '    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]\n',
                 '    needs: [version, checks, linux-amd64, linux-arm64, macos-arm64]\n    <<: *publish\n',
                 'plain key'),
                ('cd.yml', '    runs-on: lanescape-2vcpu-linux-amd64\n    permissions:\n      contents: write\n',
                 '    runs-on: lanescape-2vcpu-linux-amd64\n    permissions: { contents: write, id-token: write }\n',
                 'flow mappings'),
                ('target.yml', '    name: Test ${{ inputs.target }}\n', '    name: &test Test\n', 'anchors')):
            with self.subTest(new=new):
                self.tearDown()
                self.setUp()
                self.edit(f'.github/workflows/{workflow}', old, new)
                self.assertTrue(any(f'.github/workflows/{workflow}:' in p and problem in p for p in self.problems()))

    def test_a_chain_needs_nothing_but_the_checks(self):
        self.edit('.github/workflows/ci.yml', """  linux-amd64:
    name: linux-amd64
    needs: checks""", """  linux-amd64:
    name: linux-amd64
    needs: [checks, linux-arm64]""")
        self.assertIn('.github/workflows/ci.yml: linux-amd64 must need exactly checks', self.problems())

    def test_a_cd_chain_needs_nothing_but_the_checks_and_version(self):
        self.edit('.github/workflows/cd.yml', """  linux-amd64:
    name: linux-amd64
    needs: [version, checks]""", """  linux-amd64:
    name: linux-amd64
    needs: [version, checks, macos-arm64]""")
        self.assertIn('.github/workflows/cd.yml: linux-amd64 must need exactly checks and version', self.problems())

    def test_ci_runs_nothing_else_for_a_fork(self):
        self.edit('.github/workflows/ci.yml', """jobs:
""", """jobs:
  extra:
    runs-on: lanescape-2vcpu-linux-amd64
    permissions:
      contents: read
    steps:
      - run: true

""")
        self.assertTrue(any("ci.yml must have no jobs but checks and the calls" in p for p in self.problems()))

    def test_artifacts_are_kept_a_day(self):
        for path, old, new in (
                ('.github/actions/build/action.yml', '        retention-days: 1\n', '        retention-days: 7\n'),
                ('.github/actions/embedder/action.yml', '        retention-days: 1\n', ''),
                ('.github/workflows/target.yml', """      - name: Run the test embedder
""", """      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: log-${{ inputs.target }}
          path: log
      - name: Run the test embedder
        env:
          RETENTION: 1
""")):
            with self.subTest(path=path):
                self.tearDown()
                self.setUp()
                self.edit(path, old, new)
                self.assertTrue(any(p.startswith(f'{path}:') and 'retention-days: 1' in p for p in self.problems()))


if __name__ == '__main__':
    unittest.main()
