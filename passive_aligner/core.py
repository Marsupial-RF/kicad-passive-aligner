"""Deterministic, conservative alignment of routed two-terminal series passives.

Geometry is in KiCad internal units. No libraries, inference, or network access.
"""
import math
import re
import pcbnew as pcb

TOL = pcb.FromMM(0.001)


class AlignmentError(ValueError):
    pass


def xy(p):
    return (p.x, p.y)


def vec(p):
    return pcb.VECTOR2I(round(p[0]), round(p[1]))


def dist(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def uid(item):
    return item.m_Uuid.AsString()


def copper_layer(pad):
    layers = [layer for layer in (pcb.F_Cu, pcb.B_Cu) if pad.IsOnLayer(layer)]
    if len(layers) != 1:
        raise AlignmentError('Pad must be on exactly one outer copper layer.')
    return layers[0]


def near(a, b):
    return dist(a, b) <= TOL


def on_segment(p, a, b):
    d = dist(a, b)
    if d <= TOL:
        return near(p, a)
    t = ((p[0]-a[0])*(b[0]-a[0]) + (p[1]-a[1])*(b[1]-a[1])) / d**2
    q = (a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1]))
    return 0 <= t <= 1 and near(p, q)


def trace_chain(board, fp, pad):
    """Walk centre-terminated straight tracks to a pad, via, or explicit junction."""
    tracks = list(board.GetTracks())
    pads = [p for f in board.GetFootprints() for p in f.Pads()]
    layer = copper_layer(pad)
    current = xy(pad.GetPosition())
    seen = set()
    chain = []
    width = None
    for _ in range(len(tracks)+1):
        touching = [t for t in tracks if t.IsOnLayer(layer) and t.HitTest(vec(current))]
        foreign_pads = [p for p in pads if uid(p) != uid(pad) and p.IsOnLayer(layer) and p.HitTest(vec(current))]
        vias = [t for t in touching if isinstance(t, pcb.PCB_VIA)]
        if chain and (foreign_pads or vias):
            # Anchors remain in place; never silently move another selected component.
            return chain, current
        candidates = [t for t in touching if uid(t) not in seen]
        if chain and len(candidates) >= 2:
            if all(not isinstance(t, (pcb.PCB_ARC, pcb.PCB_VIA)) and
                   (near(current, xy(t.GetStart())) or near(current, xy(t.GetEnd()))) for t in candidates):
                return chain, current
            raise AlignmentError('Branch is not an explicit track endpoint junction.')
        if len(candidates) != 1 or (not chain and (foreign_pads or vias)):
            raise AlignmentError('Each pad needs one unambiguous routed connection to an anchor.')
        t = candidates[0]
        if isinstance(t, (pcb.PCB_VIA, pcb.PCB_ARC)) or t.IsLocked():
            raise AlignmentError('Locked tracks, arcs, and layer changes inside a connection are unsupported.')
        if t.GetNetCode() != pad.GetNetCode():
            raise AlignmentError('Copper touching the connection belongs to a different net.')
        a, b = xy(t.GetStart()), xy(t.GetEnd())
        if near(current, a):
            end, reverse = b, False
        elif near(current, b):
            end, reverse = a, True
        else:
            raise AlignmentError('Connection must terminate at the pad centre or at a track endpoint.')
        if near(current, end):
            raise AlignmentError('Zero-length track in connection.')
        if width is None:
            width = t.GetWidth()
        if width != t.GetWidth():
            raise AlignmentError('Width transitions are preserved by skipping this connection.')
        # Do not flatten across any intermediate copper branch/pad/via.
        for other in tracks:
            if uid(other) == uid(t) or not other.IsOnLayer(layer):
                continue
            points = [xy(other.GetStart()), xy(other.GetEnd())]
            if any(on_segment(q, current, end) and not near(q, current) and not near(q, end) for q in points):
                raise AlignmentError('A connection has an interior branch; split it at an explicit junction first.')
        for other in pads:
            q = xy(other.GetPosition())
            if other.IsOnLayer(layer) and on_segment(q, current, end) and not near(q, current) and not near(q, end):
                raise AlignmentError('A connection passes through another pad.')
        chain.append({'uuid': uid(t), 'reverse': reverse, 'start': current, 'end': end,
                      'width': width, 'layer': layer, 'net': pad.GetNetCode()})
        seen.add(uid(t))
        current = end
    raise AlignmentError('Connection forms a loop.')


def plan_one(board, ref):
    fp = board.FindFootprintByReference(ref)
    if not fp or not re.fullmatch(r'[RCL][0-9]+', ref, re.I):
        raise AlignmentError('Select an R, C, or L reference with exactly two SMD pads.')
    if fp.IsLocked():
        raise AlignmentError('Footprint is locked.')
    pads = sorted(list(fp.Pads()), key=lambda p: p.GetNumber())
    if len(pads) != 2 or len({p.GetNumber() for p in pads}) != 2:
        raise AlignmentError('Exactly two distinct pads are required.')
    if any(p.GetAttribute() != pcb.PAD_ATTRIB_SMD for p in pads) or copper_layer(pads[0]) != copper_layer(pads[1]):
        raise AlignmentError('Only two SMD pads on the same copper layer are supported.')
    if not all(p.GetNetCode() for p in pads) or pads[0].GetNetCode() == pads[1].GetNetCode():
        raise AlignmentError('Pads must belong to two different assigned nets.')
    if any(re.search(r'(^|[/_])(GND|GROUND|VSS|AGND|DGND|PGND)([/_]|$)', p.GetNetname(), re.I) for p in pads):
        raise AlignmentError('Ground-connected passives are skipped: automatic series alignment is inappropriate for shunts.')
    if any(z.HitTestFilledArea(copper_layer(p), p.GetPosition()) for z in board.Zones() for p in pads):
        raise AlignmentError('Pad touches a filled zone; its series connection is ambiguous.')
    chains, anchors = zip(*(trace_chain(board, fp, p) for p in pads))
    p, q = [xy(pad.GetPosition()) for pad in pads]
    a, b = anchors
    length, spacing = dist(a, b), dist(p, q)
    if length <= spacing + 2*TOL or spacing <= TOL:
        raise AlignmentError('Anchors are too close together.')
    ux, uy = (b[0]-a[0])/length, (b[1]-a[1])/length
    mid = ((p[0]+q[0])/2, (p[1]+q[1])/2)
    projection = (mid[0]-a[0])*ux + (mid[1]-a[1])*uy
    if not spacing/2 + TOL < projection < length-spacing/2-TOL:
        raise AlignmentError('The component projection falls outside its connection anchors.')
    target_mid = (a[0]+projection*ux, a[1]+projection*uy)
    target_pads = [(target_mid[0]+s*spacing/2*ux, target_mid[1]+s*spacing/2*uy) for s in (-1, 1)]
    angle = math.degrees(math.atan2(uy, ux)-math.atan2(q[1]-p[1], q[0]-p[0]))
    angle = (angle+180) % 360-180
    if abs(angle) < 0.001 and dist(mid, target_mid) < TOL:
        raise AlignmentError('Already aligned.')
    return {'reference': ref, 'footprint': uid(fp), 'pad_numbers': [p.GetNumber() for p in pads],
            'pad_nets': [p.GetNetname() for p in pads], 'old_pads': [p, q], 'new_pads': target_pads,
            'anchors': anchors, 'chains': chains, 'angle_screen_deg': angle,
            'translation_mm': dist(mid, target_mid)/pcb.FromMM(1)}


def make_plan(board, refs):
    plans, skipped = [], []
    for ref in refs:
        try:
            plans.append(plan_one(board, ref))
        except AlignmentError as exc:
            skipped.append({'reference': ref, 'reason': str(exc)})
    # A batch must not move an anchor of another correction or share a track.
    for i, plan in enumerate(plans):
        ids = {t['uuid'] for c in plan['chains'] for t in c}
        for other in plans[i+1:]:
            other_ids = {t['uuid'] for c in other['chains'] for t in c}
            if ids & other_ids or any(near(a, p) for a in plan['anchors'] for p in other['old_pads']) or any(near(a, p) for a in other['anchors'] for p in plan['old_pads']):
                raise AlignmentError('Selected components share a connection. Align them individually and review each result.')
    return {'changes': plans, 'skipped': skipped}


def apply_plan(board, plan):
    tracks = {uid(t): t for t in board.GetTracks()}
    for change in plan['changes']:
        fp = board.FindFootprintByReference(change['reference'])
        pads = {p.GetNumber(): p for p in fp.Pads()}
        p, q = change['old_pads']
        mid = vec(((p[0]+q[0])/2, (p[1]+q[1])/2))
        # KiCad positive rotation is counterclockwise; screen Y grows downward.
        fp.Rotate(mid, pcb.EDA_ANGLE(-change['angle_screen_deg'], pcb.DEGREES_T))
        first = pads[change['pad_numbers'][0]]
        fp.Move(vec(change['new_pads'][0])-first.GetPosition())
        for number, target, net in zip(change['pad_numbers'], change['new_pads'], change['pad_nets']):
            if not near(xy(pads[number].GetPosition()), target) or pads[number].GetNetname() != net:
                raise AlignmentError('Pad transformation failed; do not apply this plan.')
        for number, anchor, chain in zip(change['pad_numbers'], change['anchors'], change['chains']):
            start = xy(pads[number].GetPosition())
            total = sum(dist(t['start'], t['end']) for t in chain)
            offset = 0
            for t in chain:
                lo, hi = offset/total, (offset+dist(t['start'], t['end']))/total
                ends = [vec((start[0]+f*(anchor[0]-start[0]), start[1]+f*(anchor[1]-start[1]))) for f in (lo, hi)]
                if t['reverse']:
                    ends.reverse()
                tracks[t['uuid']].SetStart(ends[0])
                tracks[t['uuid']].SetEnd(ends[1])
                offset += dist(t['start'], t['end'])
    # Do not call BuildConnectivity here: KiCad may propagate a touching track's
    # net into another track, masking a newly introduced short before DRC.
    # DRC reads the preserved explicit net assignments from the candidate file.
