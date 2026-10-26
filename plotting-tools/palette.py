"""One colour scheme for every figure in the paper.

Before this existed there were three schemes and they contradicted each other:
`tab:red` meant BLE in the completeness figures and G1 in the disagreement
figure, while the `turbo` cycle used on the 30-agent panels accidentally gave
BLE the blues and bridge the reds, because node ids happen to run 1-10 BLE,
11-20 Wi-Fi, 21-30 bridge. A reader who learned "red = BLE" from one figure met
two other meanings for red elsewhere in the same set.

MEDIA is the semantic palette and matches the node colours in the architecture
diagram, so a medium is the same colour in the diagram, the per-agent panels,
the completeness figures and the AoI figures.

TOPOLOGY deliberately shares no hue with MEDIA. Topology and medium appear on
different figures but in the same paper, and reusing a hue across the two
invites exactly the misreading above.
"""
from __future__ import annotations

# --- medium -----------------------------------------------------------------
#
# REPLACE THESE with the exact hex used by \definecolor in the paper, so the
# figures and the architecture diagram agree. The names mirror the diagram's:
#   BLE -> acccyan,  Wi-Fi -> accgreen,  bridge -> accamber
MEDIA = {
    "ble":    "#00A3C4",     # acccyan
    "wifi":   "#3FA34D",     # accgreen
    "bridge": "#E0A43C",     # accamber
}

#: Display names, so no figure has to spell them locally.
MEDIA_LABEL = {"ble": "BLE", "wifi": "Wi-Fi", "bridge": "Bridge"}

#: Markers, so the media stay separable in greyscale and for colour-blind
#: readers. Colour alone is not an accessible encoding.
MEDIA_MARKER = {"ble": "o", "wifi": "s", "bridge": "^"}

#: Draw order, so the three are always stacked the same way.
MEDIA_ORDER = ("ble", "wifi", "bridge")


# --- controller (reviewers-exp) ---------------------------------------------
#
# The four arms of the smart-grid comparison. Deliberately disjoint from the
# MEDIA hues: a per-agent panel colours by medium and a controller comparison
# colours by arm, and the two appear in the same paper, so cyan, green and
# amber are reserved and these take blue, purple and crimson.
#
# Two families, because the comparison is two pairs rather than four
# independents: the literature baseline in purples, the adaptive interface in
# crimson and navy. Within each pair, lightness separates the variant.
CONTROLLER = {
    "LC0": "#B39DDB",     # light purple, the terminal-policy variant
    "LC+": "#5E35B1",     # deep purple, persistent feedback
    "AA":  "#C1436D",     # crimson, the zero-threshold ablation
    "DZ":  "#1F4E9C",     # navy, the proposed interface
}
#: Dash carried alongside colour so identity survives greyscale and CVD.
CONTROLLER_DASH = {"LC0": (0, (1.4, 1.6)), "LC+": (0, (6.0, 2.0)),
                   "AA": (0, (4.0, 2.2)), "DZ": "solid"}
#: Thick underneath to thin on top, so a coincident pair still reads as two
#: curves. AA and DZ agree over most of the tracking record, which is itself
#: a result, and it has to stay visible rather than being overdrawn.
CONTROLLER_LW = {"LC0": 2.6, "LC+": 2.0, "AA": 2.6, "DZ": 1.1}
CONTROLLER_Z = {"LC0": 1, "LC+": 2, "AA": 2, "DZ": 3}
CONTROLLER_ORDER = ("LC0", "LC+", "AA", "DZ")


# --- coordinates ------------------------------------------------------------
#
# P and Q drawn on ONE axes, which happens only on the fleet-total figure --
# everywhere else they are separate figures and the medium palette applies.
#
# Neither MEDIA nor CONTROLLER hues: a total is not a medium and not an arm,
# and borrowing either would make a reader decode the wrong dimension on a
# page where all three palettes appear. Slate and brick are free, and the
# dash carries the identity through greyscale.
COORD = {"P": "#1B5FA8", "Q": "#C0392B"}      # blue, red
COORD_DASH = {"P": "solid", "Q": (0, (5.0, 1.8))}
COORD_ORDER = ("P", "Q")


# --- topology ---------------------------------------------------------------
#
# The same three hues as MEDIA, by choice: the deck and the paper use one
# colour code throughout, matching the node diagram, and consistency across
# figures was judged worth more than keeping the two dimensions separable.
#
# The consequence is that a hue means "medium" on the per-agent panels and
# "topology" on the three summary figures. Nothing in a figure disambiguates
# that, so the captions have to: say which dimension the colours encode.
TOPOLOGY = {
    "dring":    MEDIA["ble"],      # acccyan
    "ring4":    MEDIA["wifi"],     # accgreen
    "clusters": MEDIA["bridge"],   # accamber
}
TOPOLOGY_LABEL = {
    "dring":    r"$\mathcal{G}_1$ directed ring",
    "ring4":    r"$\mathcal{G}_2$ ring, degree 4",
    "clusters": r"$\mathcal{G}_3(t)$ clusters",
}
TOPOLOGY_ORDER = ("dring", "ring4", "clusters")


def shades_compact(medium: str, n: int, spread: float | None = None):
    """`n` tints centred on the medium's OWN lightness.

    Two failure modes, pulling opposite ways, and the spread has to trade
    between them:

    * too wide at small `n`: `shades(m, 2)` returns the two extremes of the
      0.30-0.66 ramp, so amber (base lightness 0.557) yields a dark brown
      and a pale cream and the medium stops being recognisable;
    * too narrow at large `n`: five agents of one medium within +-0.085 are
      five ambers nobody can tell apart, which is the whole-fleet case.

    So the spread grows with the count and the ramp stays centred on the
    medium's own colour, which keeps both readings available: the hue says
    the medium, the lightness separates the agents.
    """
    import colorsys
    r, g, b = (int(MEDIA[medium][i:i + 2], 16) / 255 for i in (1, 3, 5))
    base = colorsys.rgb_to_hls(r, g, b)[1]
    if n <= 1:
        return [MEDIA[medium]]
    if spread is None:
        spread = min(0.26, 0.055 * n)
    lo, hi = max(0.10, base - spread), min(0.90, base + spread)
    return shades(medium, n, l_lo=lo, l_hi=hi)


def shades(medium: str, n: int, l_lo: float = 0.30, l_hi: float = 0.66):
    """`n` tints of one medium's colour, for drawing its agents individually.

    Used on the 30-agent panels so a trace still says which medium it belongs
    to while remaining distinguishable from its neighbours. Lightness varies,
    hue and saturation do not, which is the opposite of the `turbo` cycle this
    replaces: that varied hue across all thirty and so encoded node index, a
    quantity no reader decodes, while overwriting the one that matters.

    The ramp is in HLS, not a multiplication of RGB. Scaling RGB pushes a
    channel past 1.0 for any colour with a bright component -- amber saturates
    its red channel and turns yellow rather than getting lighter -- and drives
    the dark end so far down that the hue is no longer recognisable. Setting
    lightness directly cannot clip and cannot shift hue, and the fixed range
    means the three media span the same lightness, so none of them reads as
    systematically fainter than the others.
    """
    import colorsys

    import matplotlib.colors as mcolors

    r, g, b = mcolors.to_rgb(MEDIA[medium])
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    out = []
    for k in range(n):
        f = k / max(1, n - 1)
        out.append(colorsys.hls_to_rgb(h, l_lo + (l_hi - l_lo) * f, s))
    return out


def medium_of(node_id: int, kinds: dict[int, str] | None = None) -> str:
    """Medium for a node id, from the run's metadata when available.

    Falls back to the 1-10 / 11-20 / 21-30 banding only if no metadata is
    given. The fallback is a convenience, not a rule: the band assignment lives
    in the manifest and a future manifest may not follow it.
    """
    if kinds and node_id in kinds:
        return kinds[node_id]
    return "ble" if node_id <= 10 else ("wifi" if node_id <= 20 else "bridge")
