"""KiCad ActionPlugin with before/after preview and a DRC-gated Apply button."""
from datetime import datetime
import os
from pathlib import Path
import shutil
import wx
import pcbnew as pcb
from .core import make_plan, apply_plan, xy
from .validation import validate, save_board


class Preview(wx.Panel):
    def __init__(self, parent, board, plan):
        super().__init__(parent, size=(860, 340))
        self.SetMinSize((700, 300))
        self.board, self.plan = board, plan
        self.Bind(wx.EVT_PAINT, self.paint)

    def paint(self, event):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush('#101f2a'))
        dc.Clear()
        w, h = self.GetClientSize()
        points = [p for c in self.plan['changes'] for p in (*c['anchors'], *c['old_pads'], *c['new_pads'])]
        if not points:
            return
        x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
        y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
        padding = pcb.FromMM(4)
        scale = min((w/2-55)/(x1-x0+padding*2), (h-100)/(y1-y0+padding*2))
        def pos(p, panel):
            return (round(panel*w/2+28+(p[0]-x0+padding)*scale), round(h/2+20+(p[1]-(y0+y1)/2)*scale))
        dc.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        dc.SetTextForeground('#ffffff')
        dc.DrawText('EXISTING', 25, 20)
        dc.DrawText('PROPOSED · same nets, straight path', round(w/2+25), 20)
        dc.SetPen(wx.Pen('#334955', 1))
        dc.DrawLine(round(w/2), 12, round(w/2), h-12)
        for panel in (0, 1):
            for c in self.plan['changes']:
                pads = c['new_pads'] if panel else c['old_pads']
                dc.SetPen(wx.Pen('#62d6c3', 5))
                for pad, anchor, chain in zip(pads, c['anchors'], c['chains']):
                    if panel:
                        dc.DrawLine(*pos(pad, panel), *pos(anchor, panel))
                    else:
                        for t in chain:
                            dc.DrawLine(*pos(t['start'], panel), *pos(t['end'], panel))
                dc.SetPen(wx.Pen('#f6bf54', 11))
                dc.DrawLine(*pos(pads[0], panel), *pos(pads[1], panel))
                dc.SetPen(wx.Pen('#e8edf1', 1))
                dc.SetBrush(wx.Brush('#e8edf1'))
                for p in pads:
                    dc.DrawCircle(*pos(p, panel), 5)
                for i, a in enumerate(c['anchors']):
                    dc.SetBrush(wx.Brush('#62d6c3'))
                    dc.DrawCircle(*pos(a, panel), 6)
                    px, py = pos(a, panel)
                    dc.DrawText('A' if i == 0 else 'B', px-5, py+14)
                middle = ((pads[0][0]+pads[1][0])/2, (pads[0][1]+pads[1][1])/2)
                px, py = pos(middle, panel)
                dc.DrawText(c['reference'], px-12, py-35)


class AlignmentDialog(wx.Dialog):
    def __init__(self, board, plan):
        super().__init__(None, title='Passive Aligner · Marsupial RF', size=(980, 710),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board, self.plan, self.checked = board, plan, False
        self.SetMinSize((820, 660))
        layout = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(self, label='Align series passives with their connection path')
        title.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        layout.Add(title, 0, wx.ALL, 16)
        layout.Add(wx.StaticText(self, label='1  Select R / L / C footprints    →    2  Review geometry    →    3  Check DRC & apply'), 0, wx.LEFT | wx.RIGHT, 16)
        layout.Add(Preview(self, board, plan), 1, wx.EXPAND | wx.ALL, 16)
        lines = [f"{c['reference']}: rotate {c['angle_screen_deg']:+.2f}° on screen; move {c['translation_mm']:.3f} mm; preserve {', '.join(c['pad_nets'])}" for c in plan['changes']]
        lines += [f"Skipped {c['reference']}: {c['reason']}" for c in plan['skipped']]
        box = wx.TextCtrl(self, value='\n'.join(lines), style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 85))
        layout.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)
        self.status = wx.StaticText(self, label='Preview shows connection geometry. DRC checks the full board; RF performance is not evaluated.')
        layout.Add(self.status, 0, wx.ALL, 16)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        check = wx.Button(self, label='Check DRC')
        self.apply = wx.Button(self, wx.ID_OK, label='Apply alignment')
        self.apply.Enable(False)
        cancel = wx.Button(self, wx.ID_CANCEL, label='Cancel')
        buttons.Add(check, 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer()
        buttons.Add(cancel, 0, wx.RIGHT, 8)
        buttons.Add(self.apply)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 16)
        self.SetSizer(layout)
        check.Bind(wx.EVT_BUTTON, self.check)

    def check(self, event=None):
        self.status.SetLabel('Checking isolated copies with KiCad DRC…')
        self.Update()
        try:
            with wx.BusyCursor():
                result = validate(self.board, self.plan)
            self.checked = result['passed']
            self.apply.Enable(self.checked)
            if self.checked:
                self.status.SetLabel(f"DRC: {result['before_count']} → {result['after_count']} findings; no new violations. Ready to apply.")
            else:
                kinds = ', '.join(sorted({v['type'] for v in result['new_violations']}))
                self.status.SetLabel('Apply blocked: new DRC violations — ' + kinds)
        except Exception as exc:
            self.checked = False
            self.apply.Enable(False)
            self.status.SetLabel('DRC check failed. No changes applied.')
            wx.MessageBox(str(exc), 'Passive Aligner', wx.OK | wx.ICON_ERROR, self)


class PassiveAligner(pcb.ActionPlugin):
    def defaults(self):
        self.name = 'Passive Aligner'
        self.category = 'Routing'
        self.description = 'Align selected two-pad series passives with their local routed path; preview and check DRC.'
        self.show_toolbar_button = True
        self.icon_file_name = str(Path(__file__).with_name('icon.png'))

    def Run(self):
        board = pcb.GetBoard()
        try:
            refs = [f.GetReference() for f in board.GetFootprints() if f.IsSelected() or any(p.IsSelected() for p in f.Pads())]
            if not refs:
                raise ValueError('Select one or more routed R, C, or L footprints (or their pads), then run Passive Aligner.')
            if not board.GetFileName():
                raise ValueError('Save the board once so that a backup can be stored beside it.')
            plan = make_plan(board, refs)
            if not plan['changes']:
                raise ValueError('\n'.join(f"{s['reference']}: {s['reason']}" for s in plan['skipped']))
            dialog = AlignmentDialog(board, plan)
            try:
                if dialog.ShowModal() != wx.ID_OK or not dialog.checked:
                    return
            finally:
                dialog.Destroy()
            source = Path(board.GetFileName())
            backup_dir = source.parent/'.passive-aligner-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup_dir.mkdir(parents=True)
            save_board(backup_dir/source.name, board)
            board.SetFileName(str(source))
            for suffix in ('.kicad_pro', '.kicad_dru'):
                if source.with_suffix(suffix).exists():
                    shutil.copy2(source.with_suffix(suffix), backup_dir/source.with_suffix(suffix).name)
            apply_plan(board, plan)
            if board.GetAreaCount():
                pcb.ZONE_FILLER(board).Fill(board.Zones())
            pcb.Refresh()
            wx.MessageBox('Alignment applied. Review the board and save when ready.\nBackup: ' + str(backup_dir),
                          'Passive Aligner', wx.OK | wx.ICON_INFORMATION)
        except Exception as exc:
            wx.MessageBox(str(exc), 'Passive Aligner', wx.OK | wx.ICON_ERROR)
