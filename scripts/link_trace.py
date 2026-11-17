#!/usr/bin/env python3
"""Summarizes a linker's time trace: where the link spent its time, and what ran at its end.

    link_trace.py <trace.json> [--summary <file>]

lld writes the trace with `--time-trace=<file>`, in Chrome's trace format: one complete event ("ph": "X") for each
phase it times, on the thread that ran it, with the module or file it worked on as `args.detail`, and a "Total
<phase>" event for each phase's sum. The summary is printed, and with --summary appended as Markdown to that file,
such as $GITHUB_STEP_SUMMARY. It's a measurement for the maintainer (specs/0003-ci-cd.md, Timing the macOS link),
and never fails a build: a trace it can't read is said to be so, and it exits 0.
"""

import argparse
import json
import sys
from pathlib import Path

TOP = 15
DETAIL = 100


def seconds(microseconds):
    return f'{microseconds/1e6:.1f} s'


def events(trace):
    """The trace's timed events, without its totals."""
    out = []
    for e in trace.get('traceEvents', []):
        if e.get('ph') != 'X' or not isinstance(e.get('dur'), (int, float)) or not isinstance(e.get('ts'), (int, float)):
            continue
        if str(e.get('name', '')).startswith('Total '):
            continue
        out.append(e)
    return out


def totals(trace):
    """Each phase's sum, as lld adds it up, largest first."""
    out = [(e['dur'], e['name'][len('Total '):]) for e in trace.get('traceEvents', [])
           if e.get('ph') == 'X' and str(e.get('name', '')).startswith('Total ') and isinstance(e.get('dur'), (int, float))]
    return sorted(out, reverse=True)


def threads(evs):
    """Each thread's first start, last end, and the wall time its events cover: an event inside another on its thread
    is part of that one. That's not work done: an outer phase, such as the whole link, covers its waits too."""
    out = {}
    for e in sorted(evs, key=lambda e: (e['ts'], -e['dur'])):
        t = out.setdefault(e.get('tid'), {'first': e['ts'], 'last': e['ts'], 'covered': 0})
        end = e['ts'] + e['dur']
        if end <= t['last']:
            continue
        t['covered'] += end - max(e['ts'], t['last'])
        t['last'] = end
    return out


def label(e):
    detail = str((e.get('args') or {}).get('detail', ''))
    if len(detail) > DETAIL:
        detail = '…' + detail[-DETAIL:]
    return f"{e.get('name', '')}{' ' + detail if detail else ''}"


def summarize(trace):
    evs = events(trace)
    if not evs:
        return ['The trace has no timed events.']
    start = min(e['ts'] for e in evs)
    end = max(e['ts'] + e['dur'] for e in evs)
    per_thread = threads(evs)
    lines = [f'The link took {seconds(end - start)}, on {len(per_thread)} threads.', '']
    # The link's last stretch: what ran then, longest first. Outer phases, such as the whole link, are there too.
    window = min(300e6, (end - start) / 4)
    tail = [e for e in evs if e['ts'] + e['dur'] > end - window]
    lines += [f'What ran in its last {seconds(window)}:', '', '| From | Took | Thread | What |', '|---|---|---|---|']
    for e in sorted(tail, key=lambda e: -e['dur'])[:TOP]:
        lines.append(f"| {seconds(e['ts'] - start)} | {seconds(e['dur'])} | {e.get('tid')} | `{label(e)}` |")
    lines += ['', 'The threads that finished last, with the wall time their events cover, waits included:', '', '| Thread | From | To | Covered |', '|---|---|---|---|']
    for tid, t in sorted(per_thread.items(), key=lambda x: -x[1]['last'])[:5]:
        lines.append(f"| {tid} | {seconds(t['first'] - start)} | {seconds(t['last'] - start)} | {seconds(t['covered'])} |")
    lines.append('')
    lines += ['The longest events:', '', '| From | Took | Thread | What |', '|---|---|---|---|']
    for e in sorted(evs, key=lambda e: -e['dur'])[:TOP]:
        lines.append(f"| {seconds(e['ts'] - start)} | {seconds(e['dur'])} | {e.get('tid')} | `{label(e)}` |")
    lines.append('')
    sums = totals(trace)
    if sums:
        lines += ['Each phase in all, over every thread:', '', '| Phase | In all |', '|---|---|']
        lines += [f'| `{name}` | {seconds(dur)} |' for dur, name in sums[:TOP]]
        lines.append('')
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('trace', type=Path)
    parser.add_argument('--summary', type=Path)
    args = parser.parse_args(argv)
    try:
        trace = json.loads(args.trace.read_text())
        lines = ['## The macOS link', ''] + summarize(trace)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        lines = ['## The macOS link', '', f'Its time trace couldn\'t be read: {type(e).__name__}.']
    text = '\n'.join(lines) + '\n'
    sys.stdout.write(text)
    if args.summary:
        try:
            with args.summary.open('a') as f:
                f.write(text + '\n')
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
