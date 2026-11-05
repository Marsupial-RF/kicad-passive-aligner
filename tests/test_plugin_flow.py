"""Exercise the actual wx modal/Apply/backup flow on a synthetic board.

Run with KiCad Python and a display (Xvfb is sufficient). Only the KiCad host
board getter/refresh and final message box are substituted; no human GUI is used.
"""
from pathlib import Path
import hashlib
import tempfile
from unittest.mock import patch
from test_alignment import fixture
from passive_aligner.plugin import PassiveAligner, AlignmentDialog
from passive_aligner.core import xy
from passive_aligner.validation import save_board
import pcbnew as pcb
import wx

app = wx.App(False)
with tempfile.TemporaryDirectory(prefix='aligner-gui-test-') as temp:
    path = Path(temp)/'example.kicad_pcb'
    board = fixture()
    board.SetFileName(str(path))
    board.FindFootprintByReference('R1').SetSelected()
    save_board(path, board)
    original = path.read_bytes()
    messages = []

    def click_check_and_apply():
        dialog = next(w for w in wx.GetTopLevelWindows() if isinstance(w, AlignmentDialog))
        dialog.check()
        assert dialog.checked and dialog.apply.IsEnabled()
        wx.PostEvent(dialog.apply, wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK))

    wx.CallLater(300, click_check_and_apply)
    with patch.object(pcb, 'GetBoard', return_value=board), patch.object(pcb, 'Refresh'), \
         patch.object(wx, 'MessageBox', side_effect=lambda *args, **kwargs: messages.append(args[0])):
        PassiveAligner().Run()
    assert messages and messages[-1].startswith('Alignment applied.'), messages
    assert path.read_bytes() == original, 'Plugin overwrote the source file'
    backups = list(Path(temp).glob('.passive-aligner-backups/*/example.kicad_pcb'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert all(p.GetPosition().y == pcb.FromMM(20) for p in board.FindFootprintByReference('R1').Pads())
    print('PASS: wx modal Check DRC → Apply; source untouched; backup preserved; live board aligned.')
