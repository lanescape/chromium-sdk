import json
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import link_trace


def trace(*events):
    return {'traceEvents': [{'ph': 'M', 'name': 'thread_name', 'tid': 1, 'pid': 1, 'args': {'name': 'ld64.lld'}},
                            *events]}


def event(name, ts, dur, tid, detail=None):
    e = {'ph': 'X', 'name': name, 'ts': ts*1e6, 'dur': dur*1e6, 'tid': tid, 'pid': 1}
    if detail:
        e['args'] = {'detail': detail}
    return e


# One module's codegen runs on long after the rest: what a single straggler looks like.
# The names are lld's own.
STRAGGLER = trace(event('ExecuteLinker', 0, 700, 1), event('LTO', 10, 680, 1),
                  event('Thin backend', 20, 100, 2, 'obj/a.o'), event('Thin backend', 20, 670, 3, 'obj/huge.o'),
                  event('Total Thin backend', 0, 770, 9))
# The backends finish together, and a serial phase on the main thread follows.
SERIAL = trace(event('ExecuteLinker', 0, 700, 1), event('LTO', 10, 100, 1),
               event('Thin backend', 20, 80, 2, 'obj/a.o'), event('Thin backend', 20, 85, 3, 'obj/b.o'),
               event('Write output file', 120, 570, 1))


class LinkTrace(unittest.TestCase):
    def test_a_straggler_is_at_the_end_and_longest(self):
        text = '\n'.join(link_trace.summarize(STRAGGLER))
        self.assertIn('The link took 700.0 s, on 3 threads.', text)
        tail = text.split('What ran in its last 175.0 s:')[1].split('The threads that finished last')[0]
        self.assertIn('`Thin backend obj/huge.o`', tail)
        self.assertNotIn('obj/a.o', tail)
        threads = text.split('The threads that finished last')[1].split('The longest events:')[0]
        self.assertIn('| 3 | 20.0 s | 690.0 s | 670.0 s |', threads)
        self.assertIn('| `Thin backend` | 770.0 s |', text)

    def test_a_serial_phase_is_at_the_end_and_no_backend_is(self):
        text = '\n'.join(link_trace.summarize(SERIAL))
        tail = text.split('What ran in its last 175.0 s:')[1].split('The threads that finished last')[0]
        self.assertIn('`Write output file`', tail)
        self.assertNotIn('Thin backend', tail)

    def test_nested_events_count_once_for_their_thread(self):
        threads = link_trace.threads(link_trace.events(STRAGGLER))
        self.assertEqual(threads[1]['covered'], 700e6)

    def test_totals_arent_events(self):
        self.assertNotIn('Total Thin backend', [e['name'] for e in link_trace.events(STRAGGLER)])

    def test_a_long_detail_keeps_its_end(self):
        text = '\n'.join(link_trace.summarize(trace(event('Thin backend', 0, 1, 2, 'x'*300 + 'end.o'))))
        self.assertIn('…' + 'x'*95 + 'end.o', text)

    def test_what_it_cant_read_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad, summary = Path(tmp)/'bad.json', Path(tmp)/'summary.md'
            for content in ('not json', '[]', json.dumps({'traceEvents': [{'ph': 'X', 'ts': 'x'}]})):
                bad.write_text(content)
                self.assertEqual(link_trace.main([str(bad), '--summary', str(summary)]), 0)
            self.assertEqual(link_trace.main([str(Path(tmp)/'missing.json')]), 0)
            self.assertIn("## The macOS link", summary.read_text())

    def test_the_summary_is_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, summary = Path(tmp)/'trace.json', Path(tmp)/'summary.md'
            path.write_text(json.dumps(STRAGGLER))
            summary.write_text('before\n')
            self.assertEqual(link_trace.main([str(path), '--summary', str(summary)]), 0)
            self.assertTrue(summary.read_text().startswith('before\n## The macOS link'))


if __name__ == '__main__':
    unittest.main()
