#!/usr/bin/env python3
"""Run with KiCad's Python. Plan by default; explicit output required to apply."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import pcbnew as pcb
os.environ['PASSIVE_ALIGNER_HEADLESS'] = '1'
from passive_aligner.core import make_plan, apply_plan
from passive_aligner.validation import validate, save_board, load_board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('--reference', action='append', required=True, help='Repeat for independent series passives.')
    parser.add_argument('--output', type=Path, help='Apply to a NEW board file after passing DRC. Never overwrites a file.')
    parser.add_argument('--kicad-cli', help='Path to a matching KiCad CLI, otherwise discover on PATH.')
    args = parser.parse_args()
    try:
        if not args.board.is_file():
            raise ValueError('Input board does not exist.')
        if args.output and (args.output.exists() or any(args.output.with_suffix(s).exists() for s in ('.kicad_pro', '.kicad_dru'))):
            raise ValueError('Output or its project rules already exist; choose a new name.')
        board = load_board(args.board.resolve())
        board.SetFileName(str(args.board.resolve()))
        plan = make_plan(board, args.reference)
        if args.output:
            if not plan['changes']:
                raise ValueError('No eligible changes: ' + json.dumps(plan['skipped']))
            plan['drc'] = validate(board, plan, args.kicad_cli)
            if not plan['drc']['passed']:
                print(json.dumps(plan, indent=2))
                return 2
            apply_plan(board, plan)
            if board.GetAreaCount():
                pcb.ZONE_FILLER(board).Fill(board.Zones())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_board(args.output, board)
            for suffix in ('.kicad_pro', '.kicad_dru'):
                sidecar = args.board.with_suffix(suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, args.output.with_suffix(suffix))
        print(json.dumps(plan, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
