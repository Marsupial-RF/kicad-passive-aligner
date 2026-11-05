"""Run with KiCad's Python: python tests/test_alignment.py."""
from pathlib import Path
import math
import os
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcbnew as pcb
os.environ['PASSIVE_ALIGNER_HEADLESS'] = '1'
from passive_aligner.core import make_plan, apply_plan, xy, near, on_segment
from passive_aligner.validation import validate


def v(x, y):
    return pcb.VECTOR2I(pcb.FromMM(x), pcb.FromMM(y))


def fixture():
    board = pcb.BOARD()
    nets = []
    for name in ('INPUT', 'OUTPUT', 'OBSTACLE'):
        net = pcb.NETINFO_ITEM(board, name)
        board.Add(net)
        nets.append(net)
    def footprint(ref, position, coords, pad_nets):
        fp = pcb.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetValue('49.9' if ref == 'R1' else 'TEST')
        fp.SetAttributes(pcb.FP_SMD)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        fp.SetPosition(v(*position))
        board.Add(fp)
        for i, (coord, net) in enumerate(zip(coords, pad_nets)):
            pad = pcb.PAD(fp)
            pad.SetNumber(str(i+1))
            pad.SetAttribute(pcb.PAD_ATTRIB_SMD)
            pad.SetShape(pcb.PAD_SHAPE_RECT)
            pad.SetSize(v(1.5, 1.5))
            layers = pcb.LSET()
            for layer in (pcb.F_Cu, pcb.F_Mask, pcb.F_Paste):
                layers.AddLayer(layer)
            pad.SetLayerSet(layers)
            pad.SetPosition(v(*coord))
            pad.SetNet(net)
            fp.Add(pad)
        return fp
    a = footprint('J1', (10,20), [(10,20)], [nets[0]])
    b = footprint('J2', (50,20), [(50,20)], [nets[1]])
    r = footprint('R1', (30,24), [(30,23), (30,25)], nets[:2])
    for start, end, net in [((10,20),(30,23),nets[0]), ((30,25),(50,20),nets[1])]:
        t = pcb.PCB_TRACK(board)
        t.SetStart(v(*start)); t.SetEnd(v(*end)); t.SetWidth(pcb.FromMM(.4))
        t.SetLayer(pcb.F_Cu); t.SetNet(net); board.Add(t)
    for start, end in [((5,10),(55,10)),((55,10),(55,35)),((55,35),(5,35)),((5,35),(5,10))]:
        line = pcb.PCB_SHAPE(board)
        line.SetShape(pcb.SHAPE_T_SEGMENT); line.SetLayer(pcb.Edge_Cuts)
        line.SetStart(v(*start)); line.SetEnd(v(*end)); line.SetWidth(pcb.FromMM(.05)); board.Add(line)
    board.BuildConnectivity()
    return board


class AlignmentTests(unittest.TestCase):
    def test_moves_rotates_and_preserves_nets(self):
        board = fixture()
        fp = board.FindFootprintByReference('R1')
        nets = [(p.GetNumber(), p.GetNetname()) for p in fp.Pads()]
        plan = make_plan(board, ['R1'])
        self.assertEqual(len(plan['changes']), 1)
        apply_plan(board, plan)
        self.assertEqual(nets, [(p.GetNumber(), p.GetNetname()) for p in fp.Pads()])
        pads = sorted(fp.Pads(), key=lambda p: p.GetNumber())
        self.assertTrue(near(xy(pads[0].GetPosition()), xy(v(29,20))))
        self.assertTrue(near(xy(pads[1].GetPosition()), xy(v(31,20))))
        self.assertEqual(len(list(board.GetTracks())), 2)
        self.assertTrue(all(t.GetStart().y == pcb.FromMM(20) and t.GetEnd().y == pcb.FromMM(20) for t in board.GetTracks()))

    def test_reversed_numbering(self):
        board = fixture()
        pads = sorted(board.FindFootprintByReference('R1').Pads(), key=lambda p: p.GetNumber())
        pads[0].SetNumber('2'); pads[1].SetNumber('1')
        plan = make_plan(board, ['R1'])
        apply_plan(board, plan)
        self.assertTrue(near(xy(pads[0].GetPosition()), xy(v(29,20))))
        self.assertEqual(pads[0].GetNetname(), 'INPUT')

    def test_arbitrary_board_rotation(self):
        for angle in (17, 90, 180, 263):
            board = fixture()
            centre = v(30,20)
            for item in [*board.GetFootprints(), *board.GetTracks()]:
                item.Rotate(centre, pcb.EDA_ANGLE(angle, pcb.DEGREES_T))
            plan = make_plan(board, ['R1'])
            self.assertEqual(len(plan['changes']), 1)
            apply_plan(board, plan)
            a, b = plan['changes'][0]['anchors']
            for p in board.FindFootprintByReference('R1').Pads():
                self.assertTrue(on_segment(xy(p.GetPosition()), a, b))

    def test_locked(self):
        board = fixture(); board.FindFootprintByReference('R1').SetLocked(True)
        self.assertFalse(make_plan(board, ['R1'])['changes'])

    def test_chain_segments_keep_identity_and_width(self):
        board = fixture()
        t = next(t for t in board.GetTracks() if t.GetNetname() == 'INPUT')
        t.SetEnd(v(20,21.5))
        extension = pcb.PCB_TRACK(board)
        extension.SetStart(v(20,21.5)); extension.SetEnd(v(30,23))
        extension.SetLayer(pcb.F_Cu); extension.SetWidth(t.GetWidth()); extension.SetNet(t.GetNet())
        board.Add(extension)
        identities = {(t.m_Uuid.AsString(), t.GetNetname(), t.GetWidth()) for t in board.GetTracks()}
        plan = make_plan(board, ['R1']); self.assertEqual(len(plan['changes']), 1)
        apply_plan(board, plan)
        self.assertEqual(identities, {(t.m_Uuid.AsString(), t.GetNetname(), t.GetWidth()) for t in board.GetTracks()})
        self.assertTrue(all(t.GetStart().y == pcb.FromMM(20) and t.GetEnd().y == pcb.FromMM(20) for t in board.GetTracks()))

    def test_explicit_branch_anchor_stays_fixed(self):
        board = fixture()
        t = next(t for t in board.GetTracks() if t.GetNetname() == 'OUTPUT')
        t.SetEnd(v(40,20))
        branch_ids = []
        for end in ((50,20),(45,30)):
            branch = pcb.PCB_TRACK(board); branch.SetStart(v(40,20)); branch.SetEnd(v(*end))
            branch.SetLayer(pcb.F_Cu); branch.SetWidth(t.GetWidth()); branch.SetNet(t.GetNet()); board.Add(branch)
            branch_ids.append((branch, xy(branch.GetStart()), xy(branch.GetEnd())))
        plan = make_plan(board, ['R1']); self.assertEqual(len(plan['changes']), 1)
        self.assertTrue(near(plan['changes'][0]['anchors'][1], xy(v(40,20))))
        apply_plan(board, plan)
        for branch, start, end in branch_ids:
            self.assertEqual((xy(branch.GetStart()),xy(branch.GetEnd())), (start,end))

    def test_already_aligned(self):
        board = fixture(); apply_plan(board, make_plan(board, ['R1']))
        self.assertEqual(make_plan(board, ['R1'])['skipped'][0]['reason'], 'Already aligned.')

    def test_ground_is_not_assumed_series(self):
        board = fixture()
        net = pcb.NETINFO_ITEM(board, 'GND'); board.Add(net)
        list(board.FindFootprintByReference('R1').Pads())[1].SetNet(net)
        self.assertFalse(make_plan(board, ['R1'])['changes'])

    def test_interior_branch_refused(self):
        board = fixture()
        existing = list(board.GetTracks())[0]
        t = pcb.PCB_TRACK(board); t.SetStart(v(20,21.5)); t.SetEnd(v(20,30))
        t.SetLayer(pcb.F_Cu); t.SetWidth(pcb.FromMM(.4)); t.SetNet(existing.GetNet()); board.Add(t)
        self.assertFalse(make_plan(board, ['R1'])['changes'])

    def test_bottom_layer(self):
        board = fixture()
        for fp in board.GetFootprints():
            fp.Flip(v(30,20), False)
        for t in board.GetTracks():
            t.Flip(v(30,20), False)
        plan = make_plan(board, ['R1'])
        self.assertEqual(len(plan['changes']), 1)
        apply_plan(board, plan)
        self.assertTrue(all(p.IsOnLayer(pcb.B_Cu) for p in board.FindFootprintByReference('R1').Pads()))

    def test_drc_pass_and_obstacle_rejection(self):
        cli = os.environ.get('PASSIVE_ALIGNER_KICAD_CLI')
        with tempfile.TemporaryDirectory() as temp:
            board = fixture(); board.SetFileName(str(Path(temp)/'board.kicad_pcb'))
            plan = make_plan(board, ['R1'])
            result = validate(board, plan, cli)
            self.assertTrue(result['passed'], result)
            # Foreign copper intersects only the proposed horizontal route.
            board = fixture(); board.SetFileName(str(Path(temp)/'obstacle.kicad_pcb'))
            plan = make_plan(board, ['R1'])
            t = pcb.PCB_TRACK(board); t.SetStart(v(20,19)); t.SetEnd(v(20,20.5))
            t.SetLayer(pcb.F_Cu); t.SetWidth(pcb.FromMM(.4))
            t.SetNet(board.FindNet('OBSTACLE')); board.Add(t)
            result = validate(board, plan, cli)
            self.assertFalse(result['passed'], result)
            self.assertTrue(result['new_violations'])
            # The original object, including its foreign copper net, stays intact.
            self.assertEqual(t.GetNetname(), 'OBSTACLE')
            obstacle_fp = pcb.FOOTPRINT(board); obstacle_fp.SetReference('TP1'); board.Add(obstacle_fp)
            pad = pcb.PAD(obstacle_fp); pad.SetNumber('1'); pad.SetPosition(v(20,19))
            pad.SetSize(v(.5,.5)); pad.SetNet(t.GetNet()); obstacle_fp.Add(pad)
            result = validate(board, plan, cli)
            self.assertFalse(result['passed'], result)
            self.assertTrue(any(v['type'] in ('shorting_items','clearance','tracks_crossing') for v in result['new_violations']), result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
