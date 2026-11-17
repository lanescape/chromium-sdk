import json
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

    def test_the_apple_sdk_step_has_its_own_condition(self):
        self.edit('.github/workflows/target.yml', """      - id: apple-sdk
        if: inputs.target == 'macos-arm64'
        uses: ./.github/actions/apple-sdk
      - uses: ./.github/actions/build""", """      - run: echo preparing
        if: inputs.target == 'macos-arm64'
      - id: apple-sdk
        uses: ./.github/actions/apple-sdk
      - uses: ./.github/actions/build""")
        self.assertTrue(any('only for macos-arm64' in p for p in self.problems()))

    def test_the_test_never_fetches_the_apple_sdk(self):
        self.edit('.github/workflows/target.yml', """      - name: Run the test embedder
""", """      - id: apple-sdk
        if: inputs.target == 'macos-arm64'
        uses: ./.github/actions/apple-sdk
      - name: Run the test embedder
""")
        self.assertTrue(any("only target.yml's build and embedder may fetch the Apple SDK" in p
                            for p in self.problems()))

    def test_a_step_s_keys_are_its_own(self):
        for path, old, new, problem in (
                ('.github/actions/embedder/action.yml', """        retention-days: 1
        if-no-files-found: error
""", """        if-no-files-found: error
      env:
        retention-days: 1
""", 'retention-days: 1'),
                ('.github/workflows/target.yml', """      - id: apple-sdk
        if: inputs.target == 'macos-arm64'
        uses: ./.github/actions/apple-sdk
      - uses: ./.github/actions/embedder""", """      - id: apple-sdk
        env:
          if: inputs.target == 'macos-arm64'
        uses: ./.github/actions/apple-sdk
      - uses: ./.github/actions/embedder""", 'only for macos-arm64')):
            with self.subTest(path=path):
                self.tearDown()
                self.setUp()
                self.edit(path, old, new)
                self.assertTrue(any(p.startswith(f'{path}:') and problem in p for p in self.problems()))

    def test_cd_packages_whole_and_ci_quickly(self):
        for workflow, old, new, problem in (
                ('cd.yml', '      target: macos-arm64\n      version: ${{ needs.version.outputs.version }}\n'
                           '      release: true\n',
                 '      target: macos-arm64\n      version: ${{ needs.version.outputs.version }}\n',
                 'macos-arm64 must pass version: ${{ needs.version.outputs.version }}, release: true'),
                ('ci.yml', '      target: linux-arm64\n', '      target: linux-arm64\n      release: true\n',
                 'linux-arm64 must pass only its target')):
            with self.subTest(workflow=workflow):
                self.tearDown()
                self.setUp()
                self.edit(f'.github/workflows/{workflow}', old, new)
                self.assertIn(f'.github/workflows/{workflow}: {problem}', self.problems())

    def test_only_linux_amd64_saves_the_checkout(self):
        saves = '        uses: lanescape/runners/actions/save-cache@v1\n        with:\n'
        for old, new in (
                ("      - if: inputs.target == 'linux-amd64' && !endsWith(steps.restored.outputs.checkout, '_exact')\n",
                 '      - if: "!endsWith(steps.restored.outputs.checkout, \'_exact\')"\n'),
                ("      - if: inputs.target == 'linux-amd64' && !endsWith(steps.restored.outputs.checkout, '_exact')\n",
                 "      - if: inputs.target != 'macos-arm64' && !endsWith(steps.restored.outputs.checkout, '_exact')\n"),
                (f"""{saves}          name: chromium-checkout
          path: /work
""", f"""{saves}          name: chromium-checkout
          path: /work/chromium
"""),
                ("""            .libchromium-restored
            chromium/src/out
""", """            .libchromium-restored
"""),
                (f"""{saves}          name: chromium-checkout
          path: /work
""", f"""{saves}          name: 'chromium-checkout'
          path: /work
""")):
            with self.subTest(new=new):
                self.tearDown()
                self.setUp()
                self.edit('.github/workflows/target.yml', old, new)
                self.assertTrue(any('only linux-amd64 saves the checkout' in p for p in self.problems()))

    def test_the_build_directory_is_saved_alone(self):
        self.edit('.github/workflows/target.yml', """          name: chromium-out-${{ inputs.target }}
          path: /work/chromium/src/out/${{ inputs.target }}
      - if: inputs.target == 'linux-amd64'""", """          name: chromium-out-${{ inputs.target }}
          path: /work/chromium
      - if: inputs.target == 'linux-amd64'""")
        self.assertTrue(any('save the build directory alone' in p for p in self.problems()))

    def test_the_build_directory_is_saved_every_time(self):
        self.edit('.github/workflows/target.yml', """      - uses: lanescape/runners/actions/save-cache@v1
        with:
          name: chromium-out-${{ inputs.target }}""", """      - if: inputs.target == 'linux-amd64'
        uses: lanescape/runners/actions/save-cache@v1
        with:
          name: chromium-out-${{ inputs.target }}""")
        self.assertTrue(any('save the build directory alone, whole, every time' in p for p in self.problems()))

    def test_the_build_directory_is_saved_whole(self):
        self.edit('.github/workflows/target.yml', """          path: /work/chromium/src/out/${{ inputs.target }}
      - if: inputs.target == 'linux-amd64'""", """          path: /work/chromium/src/out/${{ inputs.target }}
          exclude: .libchromium-built-from.json
      - if: inputs.target == 'linux-amd64'""")
        self.assertTrue(any('save the build directory alone, whole, every time' in p for p in self.problems()))

    def test_a_script_s_text_is_no_step(self):
        self.edit('.github/workflows/target.yml', """      - uses: lanescape/runners/actions/save-cache@v1
        with:
          name: chromium-out-${{ inputs.target }}
          path: /work/chromium/src/out/${{ inputs.target }}
""", """      - run: |
          cat <<'EOF'
          - uses: lanescape/runners/actions/save-cache@v1
            with:
              name: chromium-out-${{ inputs.target }}
              path: /work/chromium/src/out/${{ inputs.target }}
          EOF
""")
        self.assertIn('.github/workflows/target.yml: its build must save the build directory once and the checkout '
                      'once', self.problems())

    def test_a_script_s_text_is_no_upload(self):
        self.edit('.github/workflows/target.yml', """      - name: Run the test embedder
""", """      - run: |
          cat <<'EOF'
          - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
          EOF
      - name: Run the test embedder
""")
        self.assertEqual(self.problems(), [])

    def test_both_caches_are_saved(self):
        self.edit('.github/workflows/target.yml', """      - uses: lanescape/runners/actions/save-cache@v1
        with:
          name: chromium-out-${{ inputs.target }}
          path: /work/chromium/src/out/${{ inputs.target }}
""", '')
        self.assertIn('.github/workflows/target.yml: its build must save the build directory once and the checkout '
                      'once', self.problems())

    def test_the_checkout_s_exclusions_are_a_literal_block_in_its_with(self):
        excludes = """          exclude: |
            lost+found
            .libchromium-restored
            chromium/src/out
"""
        for new in ('          exclude: >\n            lost+found\n            .libchromium-restored\n'
                    '            chromium/src/out\n',
                    '        env:\n' + excludes.replace('          ', '          ', 1)):
            with self.subTest(new=new):
                self.tearDown()
                self.setUp()
                self.edit('.github/workflows/target.yml', excludes, new)
                self.assertTrue(any('only linux-amd64 saves the checkout' in p for p in self.problems()))

    def test_only_the_build_has_a_disk_and_caches(self):
        self.edit('.github/workflows/target.yml', """    needs: build
    runs-on: lanescape-16vcpu-linux-amd64
    steps:
""", """    needs: build
    runs-on: lanescape-16vcpu-linux-amd64
    steps:
      - uses: lanescape/runners/actions/cache@v1
        with:
          name: chromium-checkout
          path: /work
""")
        self.assertTrue(any("only target.yml's build has a work disk and caches" in p for p in self.problems()))

    def test_no_disk_is_saved(self):
        self.edit('.github/workflows/target.yml', """      - uses: lanescape/runners/actions/save-cache@""",
                  """      - uses: lanescape/runners/actions/save-disk@v1
      - uses: lanescape/runners/actions/save-cache@""")
        self.assertTrue(any('the runner service keeps no disks' in p for p in self.problems()))

    def test_the_build_asks_for_its_disk(self):
        self.assertFalse([p for p in self.problems() if 'asks for its work disk' in p])
        for old, new in (('          size-gb: 500\n', '          size-gb: 50\n'),
                         ('          iops: 10000\n', ''),
                         ('          size-gb: 500\n', '          size-gb: 500\n          family: linux-amd64\n')):
            with self.subTest(new=new):
                self.tearDown()
                self.setUp()
                self.edit('.github/workflows/target.yml', old, new)
                self.assertTrue(any('the build asks for its work disk, every time, as iops: 10000, size-gb: 500, '
                                    'throughput-mibps: 2400' in p for p in self.problems()))

    def test_a_disk_in_another_case_or_file(self):
        # A second disk, its owner in another case, counts as the build's second.
        self.edit('.github/workflows/target.yml', """      - uses: lanescape/runners/actions/disk@v1
""", """      - uses: Lanescape/runners/actions/disk@d68a947d79a49569632263432da6a2d3ea16aaeb
      - uses: lanescape/runners/actions/disk@v1
""")
        problems = self.problems()
        self.assertTrue(any('its build must ask for its work disk once' in p for p in problems))
        self.assertTrue(any('the build asks for its work disk, every time' in p for p in problems))
        self.tearDown()
        self.setUp()
        extra = self.repo/'.github/actions/extra/action.yaml'
        extra.parent.mkdir(parents=True)
        extra.write_text("""name: Extra
runs:
  using: composite
  steps:
    - uses: lanescape/runners/actions/disk@v1
""")
        self.assertTrue(any(p.startswith('.github/actions/extra/action.yaml:5: only target.yml') for p in self.problems()))

    def test_the_build_always_asks_for_its_disk(self):
        disk = """      - uses: lanescape/runners/actions/disk@v1
        with:
          size-gb: 500
          iops: 10000
          throughput-mibps: 2400
"""
        self.edit('.github/workflows/target.yml', disk, '')
        self.assertTrue(any('its build must ask for its work disk once' in p for p in self.problems()))
        self.tearDown()
        self.setUp()
        self.edit('.github/workflows/target.yml', disk, disk.replace('      - uses:', "      - if: false\n        uses:"))
        self.assertTrue(any('the build asks for its work disk, every time' in p for p in self.problems()))
        self.tearDown()
        self.setUp()
        self.edit('.github/workflows/target.yml', disk, disk.replace('lanescape/runners/actions/disk@v1',
                                                                  "'lanescape/runners/actions/disk@v1'"))
        self.assertFalse([p for p in self.problems() if 'work disk' in p])


class Uses(unittest.TestCase):
    """scripts/check.py's rule for how a workflow names an action."""

    def test_the_runner_services_actions_by_their_tag(self):
        self.assertEqual(check.uses_problems('w.yml', 1, '      - uses: lanescape/runners/actions/disk@v1'), [])
        for ref in ('d68a947d79a49569632263432da6a2d3ea16aaeb # v1', 'main', 'v1.2'):
            with self.subTest(ref=ref):
                self.assertEqual(check.uses_problems('w.yml', 3, f'      - uses: lanescape/runners/actions/cache@{ref}'),
                                 ["w.yml:3: use lanescape/runners/actions/cache@v1, lanescape/runners' tag"])

    def test_not_the_old_actions_repository(self):
        for ref in ('v1', 'd68a947d79a49569632263432da6a2d3ea16aaeb'):
            with self.subTest(ref=ref):
                self.assertEqual(check.uses_problems('w.yml', 2, f'      - uses: lanescape/actions/disk@{ref}'),
                                 ['w.yml:2: use lanescape/runners/actions/disk@v1; the actions moved to lanescape/runners'])
        self.assertEqual(check.uses_problems('w.yml', 3, '      - uses: Lanescape/Actions/save-cache@v1'),
                         ['w.yml:3: use lanescape/runners/actions/save-cache@v1; the actions moved to lanescape/runners'])
        for used in ('lanescape/actions@v1', 'Lanescape/Actions@0123456789abcdef0123456789abcdef01234567'):
            with self.subTest(used=used):
                self.assertEqual(check.uses_problems('w.yml', 4, f'      - uses: {used}'),
                                 ['w.yml:4: use lanescape/runners/actions/<name>@v1; the actions moved to lanescape/runners'])

    def test_only_the_runner_services_actions_from_its_repository(self):
        for used in ('lanescape/runners/actions/internal/gcs@v1', 'lanescape/runners/.github/workflows/ci.yml@v1',
                     'lanescape/runners/actions/disk@main', 'lanescape/runners@v1',
                     'lanescape/runners@0123456789abcdef0123456789abcdef01234567',
                     'Lanescape/Runners@0123456789abcdef0123456789abcdef01234567'):
            with self.subTest(used=used):
                self.assertEqual(check.uses_problems('w.yml', 4, f'      - uses: {used}'),
                                 [f"w.yml:4: use {used.split('@')[0]}@v1, lanescape/runners' tag"])

    def test_quoted_references(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.assertEqual(check.uses_problems('w.yml', 1, f'      - uses: {quote}lanescape/runners/actions/disk@v1{quote}'),
                                 [])
                self.assertEqual(check.uses_problems(
                    'w.yml', 2, f'      - uses: {quote}lanescape/runners/actions/disk@d68a947d79a49569632263432da6a2d3ea16aaeb'
                                f'{quote}'), ["w.yml:2: use lanescape/runners/actions/disk@v1, lanescape/runners' tag"])
                self.assertEqual(check.uses_problems(
                    'w.yml', 3, f'      - uses: {quote}actions/checkout@0123456789abcdef0123456789abcdef01234567{quote}'),
                    [])
                self.assertEqual(check.uses_problems('w.yml', 4, f'      - uses: {quote}actions/checkout@v6{quote}'),
                                 ['w.yml:4: pin actions/checkout@v6 to a commit'])

    def test_references_in_another_case(self):
        line = '      - uses: Lanescape/Runners/actions/disk@d68a947d79a49569632263432da6a2d3ea16aaeb'
        self.assertEqual(check.uses_problems('w.yml', 6, line), ["w.yml:6: use Lanescape/Runners/actions/disk@v1, lanescape/runners' tag"])
        self.assertEqual(check.uses_problems('w.yml', 7, '      - uses: Lanescape/runners/actions/disk@v1'),
                         ["w.yml:7: use Lanescape/runners/actions/disk@v1, lanescape/runners' tag"])

    def test_local_actions_under_github(self):
        self.assertEqual(check.uses_problems('w.yml', 1, '      - uses: ./.github/actions/apple-sdk'), [])
        self.assertEqual(check.uses_problems('w.yml', 2, '    uses: ./.github/workflows/target.yml'), [])
        self.assertEqual(check.uses_problems('w.yml', 3, '      - uses: ./ci/extra'), ['w.yml:3: keep ./ci/extra under .github/'])
        self.assertEqual(check.uses_problems('w.yml', 4, '      - uses: ./.github/../ci/extra'),
                         ['w.yml:4: keep ./.github/../ci/extra under .github/'])

    def test_escaped_references(self):
        line = '      - uses: "\\x6canescape/actions/disk@d68a947d79a49569632263432da6a2d3ea16aaeb"'
        self.assertEqual(check.uses_problems('w.yml', 5, line), ["w.yml:5: write the action's reference without escapes"])

    def test_others_by_a_commit(self):
        pinned = '      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v6'
        self.assertEqual(check.uses_problems('w.yml', 1, pinned), [])
        self.assertEqual(check.uses_problems('w.yml', 2, '      - uses: actions/checkout@v6'),
                         ['w.yml:2: pin actions/checkout@v6 to a commit'])
        self.assertEqual(check.uses_problems('w.yml', 3, '      - uses: ./.github/actions/apple-sdk'), [])


class Caches(unittest.TestCase):
    """scripts/check.py's rules for .github/runners.json's caches (specs/0003-ci-cd.md, Caches)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for path in ('.github/runners.json', 'targets.json', 'scripts/sdk.py', 'chromium'):
            source, copy = REPO/path, self.repo/path
            copy.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, copy)
            else:
                shutil.copy(source, copy)
        self.runners = json.loads((self.repo/'.github/runners.json').read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def problems(self):
        (self.repo/'.github/runners.json').write_text(json.dumps(self.runners))
        return check.cache_problems(self.repo)

    def test_the_repository_passes(self):
        self.assertEqual(self.problems(), [])

    def test_disks_are_refused(self):
        self.runners['disks'] = {'linux-amd64': {}}
        self.assertIn('.github/runners.json: the runner service keeps no disks; declare "caches"', self.problems())

    def test_unknown_fields_are_refused(self):
        self.runners['images'] = {}
        self.assertIn('.github/runners.json has fields the runner service refuses', self.problems())

    def test_every_target_has_a_build_directory_cache(self):
        del self.runners['caches']['chromium-out-linux-arm64']
        self.assertIn('.github/runners.json must declare chromium-checkout and one chromium-out-<target> for each '
                      'target in targets.json, and no other caches', self.problems())

    def test_no_other_caches(self):
        self.runners['caches']['debian'] = {}
        self.assertTrue(any('and no other caches' in p for p in self.problems()))

    def test_names_are_the_service_s(self):
        for name in ('Chromium', '1chromium', 'chromium-', 'c'*33):
            with self.subTest(name=name):
                self.runners['caches'] = {name: {}}
                self.assertTrue(any(f'{name} is not a cache name' in p for p in self.problems()))

    def test_the_checkout_is_keyed_by_its_files_alone(self):
        for change in ('chromium/args.gn', None):
            with self.subTest(change=change):
                listed = self.runners['caches']['chromium-checkout']['from']
                if change:
                    listed.append(change)
                else:
                    listed.remove('chromium/patches/v8.patch')
                self.assertTrue(any(p.startswith('.github/runners.json: chromium-checkout must be keyed by exactly '
                                                 'chromium/gclient, chromium/patches/src.patch')
                                    for p in self.problems()))
                self.tearDown()
                self.setUp()

    def test_a_new_patch_keys_both(self):
        (self.repo/'chromium/patches/third_party/skia.patch').write_text('')
        found = self.problems()
        for name in ('chromium-checkout', 'chromium-out-macos-arm64'):
            self.assertTrue(any(p.startswith(f'.github/runners.json: {name} must be keyed by exactly')
                                and 'chromium/patches/third_party/skia.patch' in p for p in found))

    def test_a_build_directory_is_keyed_by_everything_its_build_compiles(self):
        self.runners['caches']['chromium-out-linux-amd64']['from'].remove('scripts/sdk.py')
        self.assertTrue(any(p.startswith('.github/runners.json: chromium-out-linux-amd64 must be keyed by exactly')
                            for p in self.problems()))

    def test_every_listed_path_exists(self):
        (self.repo/'chromium/gn/embedder_probe_mac.mm').unlink()
        self.assertIn('.github/runners.json: chromium-out-linux-amd64 names chromium/gn/embedder_probe_mac.mm, which '
                      'does not exist', self.problems())


if __name__ == '__main__':
    unittest.main()
