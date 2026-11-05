"""Generate synthetic example boards and capture the real wx preview dialog."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_alignment import fixture
from passive_aligner.core import make_plan, apply_plan
from passive_aligner.plugin import AlignmentDialog
import pcbnew as pcb
import wx

root = Path(__file__).resolve().parents[1]
board = fixture()
before = root/'examples/before.kicad_pcb'
board.SetFileName(str(before))
pcb.SaveBoard(str(before), board)
plan = make_plan(board, ['R1'])
app = wx.App(False)
dialog = AlignmentDialog(board, plan)
dialog.Show()


def capture():
    dialog.check()
    if not dialog.checked:
        raise RuntimeError('Demo DRC check failed')
    dialog.Update()
    wx.YieldIfNeeded()
    wx.CallLater(300, finish)


def finish():
    if not dialog.apply.IsEnabled():
        raise RuntimeError('Apply button must be enabled after DRC passes')
    screen = wx.ScreenDC()
    rect = dialog.GetScreenRect()
    bitmap = wx.Bitmap(rect.width, rect.height)
    memory = wx.MemoryDC(bitmap)
    memory.Blit(0, 0, rect.width, rect.height, screen, rect.x, rect.y)
    memory.SelectObject(wx.NullBitmap)
    bitmap.SaveFile(str(root/'docs/usage.png'), wx.BITMAP_TYPE_PNG)
    apply_plan(board, plan)
    pcb.SaveBoard(str(root/'examples/after.kicad_pcb'), board)
    (root/'docs/demo-plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    dialog.Destroy()
    app.ExitMainLoop()


wx.CallLater(500, capture)
app.MainLoop()
