"""Run real KiCad DRC on isolated before/after copies, preserving project rules."""
import collections
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import pcbnew as pcb
from .core import AlignmentError, apply_plan, uid


def save_board(path, board):
    # pcb.SaveBoard rebuilds connectivity and can silently reassign track nets.
    pcb.PCB_IO_KICAD_SEXPR().SaveBoard(str(path), board)


def load_board(path):
    return pcb.PCB_IO_KICAD_SEXPR().LoadBoard(str(path), None)


def net_identity(board):
    return {uid(item): item.GetNetname() for item in [*board.GetTracks(),
            *(p for f in board.GetFootprints() for p in f.Pads())]}


def find_cli():
    explicit = os.environ.get('PASSIVE_ALIGNER_KICAD_CLI')
    path = explicit or shutil.which('kicad-cli')
    if not path:
        raise AlignmentError('kicad-cli is required. Add it to PATH or set PASSIVE_ALIGNER_KICAD_CLI.')
    return path


def run_drc(path, cli):
    output = path.with_suffix('.drc.json')
    env = os.environ.copy()
    # Embedded Python/AppImage environments must not poison the standalone CLI.
    for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE', 'LD_PRELOAD'):
        env.pop(key, None)
    result = subprocess.run([cli, 'pcb', 'drc', '--format', 'json', '--severity-all',
                             '--refill-zones', '--save-board', '-o', str(output), str(path)],
                            capture_output=True, text=True, env=env, timeout=180)
    if result.returncode or not output.exists():
        raise AlignmentError('KiCad DRC could not run: ' + (result.stderr or result.stdout)[-1800:])
    return json.loads(output.read_text())


def signatures(report):
    entries = []
    for group in ('violations', 'unconnected_items', 'schematic_parity'):
        for item in report.get(group, []):
            # Include item UUIDs: equal counts cannot hide a new short elsewhere.
            ids = tuple(sorted(str(i.get('uuid', i.get('description', ''))) for i in item.get('items', [])))
            entries.append((group, item.get('type'), item.get('severity'), ids))
    return collections.Counter(entries)


def validate(board, plan, cli=None):
    cli = cli or find_cli()
    source = Path(board.GetFileName()) if board.GetFileName() else None
    with tempfile.TemporaryDirectory(prefix='passive-aligner-') as temp:
        root = Path(temp)
        paths = []
        for folder in ('before', 'after'):
            directory = root/folder
            directory.mkdir()
            path = directory / (source.name if source else 'board.kicad_pcb')
            if source:
                for suffix in ('.kicad_pro', '.kicad_dru'):
                    sidecar = source.with_suffix(suffix)
                    if sidecar.exists():
                        shutil.copy2(sidecar, path.with_suffix(suffix))
            filename = board.GetFileName()
            save_board(path, board)
            board.SetFileName(filename)
            paths.append(path)
        before = run_drc(paths[0], cli)
        candidate = load_board(paths[1])
        apply_plan(candidate, plan)
        expected_nets = net_identity(candidate)
        save_board(paths[1], candidate)
        after = run_drc(paths[1], cli)
        added = signatures(after)-signatures(before)
        actual_nets = net_identity(load_board(paths[1]))
        changed_nets = [key for key, net in expected_nets.items() if actual_nets.get(key) != net]
        new_violations = [{'type': k[1], 'severity': k[2], 'items': k[3], 'count': v} for k, v in added.items()]
        if changed_nets:
            new_violations.append({'type': 'net_identity_changed', 'severity': 'error', 'items': changed_nets, 'count': len(changed_nets)})
        return {'passed': not bool(new_violations), 'before_count': sum(signatures(before).values()),
                'after_count': sum(signatures(after).values()),
                'new_violations': new_violations,
                'note': 'No new DRC signatures; existing violations remain the designer\'s responsibility.'}
