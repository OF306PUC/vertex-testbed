"""The applied command, reconstructed from the states a node did log.

sec:metrics asks for control effort. Three of the five agents report it
directly -- `muapp_P`, `muapp_Q` are in their STATE tail -- and the two whose
law runs on an nRF behind a relay do not: their tail carries the states and
the adaptive quantity, and the command is dropped before it reaches the host.
The command is not missing, only unrecorded: it is a deterministic function
of quantities that ARE logged:

    sigma[k] = x[k-1] - z[k-1]              the interface runs on the old state
    g[k]     = (z[k] - z[k-1]) / h          see below
    omega[k] = the interface's own term, from sigma[k] and the adaptive
               quantity as it stood BEFORE the step: vartheta[k-1], or
               (S1,S2)[k-1]
    mu[k]    = g[k] - omega[k]
    applied  = sat_{mu_max}(mu[k])

`g` needs no neighbour, which is what makes this work on a node whose
neighbour table is not fully logged: it is a forward difference of the node's
own virtual state. Why that identity holds, how the index conventions above
were established rather than assumed, and the one difference from the agent's
own arithmetic that cannot be undone, are in docs/MICROGRID.md.

`verify()` re-runs the establishing check on any run. The reconstruction
should be trusted exactly as far as it keeps passing.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .load import load_run

__all__ = ["applied_command", "verify", "MU_COLS"]

#: What a node logs when it reports the command itself.
MU_COLS = ("muapp_P", "muapp_Q")


def _blocks(run: Path, node_id: int) -> dict:
    return json.loads((run / f"{node_id}.meta.json").read_text())["controller"]


def _omega_adaptive(sigma, vth_prev, h, _mg, _t):
    """`sign(sigma) min(vartheta, |sigma|/h)`, the clipped correction."""
    return np.sign(sigma) * np.minimum(vth_prev[:, None], np.abs(sigma) / h)


def _omega_lc(sigma, x_prev, S_prev, h, mg, t_prev):
    """The LC interface's term, evaluated through the controller's own code.

    The regressor is a function of the measured state and of time, not of the
    error -- `A_i(t)` carries the known profile and `(P~^s, Q~^s)` is the
    smooth saturation of the state. Handing it `sigma` would be a law that
    happens to run.
    """
    from ..controllers.interface_lc import LCInterface, LCParams

    iface = LCInterface(LCParams(**mg["interface"]), h)
    p = iface.p
    out = np.empty_like(sigma)
    for k in range(len(sigma)):
        t = float(t_prev[k])
        if p.terminal and t >= p.T_c:
            # eq:lc_source_terminal: the command is zero, so omega is g and
            # mu is zero. Marked here and zeroed by the caller, which is the
            # one place that holds g.
            out[k] = np.nan
            continue
        lam = iface.lam(t)
        A, ps, qs = iface.regressor(t, float(x_prev[k, 0]), float(x_prev[k, 1]))
        S1, S2 = float(S_prev[k, 0]), float(S_prev[k, 1])
        out[k] = (lam * sigma[k, 0] + A * (ps * S1 + qs * S2),
                  lam * sigma[k, 1] + A * (qs * S1 - ps * S2))
    return out


def applied_command(run: Path, node_id: int, *,
                    rebuild: bool = False) -> np.ndarray:
    """`(N, 2)` of the applied command, logged where possible, else rebuilt.

    Row 0 has no predecessor and is dropped, so the result is one shorter
    than the log and aligned to rows `1..N-1`. `rebuild` forces the
    reconstruction on a node that logs the answer, which is what `verify`
    needs and nothing else should want.
    """
    run = Path(run)
    d = load_run(run).nodes[node_id].data
    return _from_data(run, node_id, d, rebuild=rebuild)


def _from_data(run: Path, node_id: int, d, *, rebuild: bool) -> np.ndarray:
    col = lambda k: np.asarray(d[k], float)
    if not rebuild and all(c in d for c in MU_COLS):
        return np.column_stack([col(MU_COLS[0]), col(MU_COLS[1])])[1:]

    c = _blocks(run, node_id)
    mg, h = c["microgrid"], float(c["dt_s"])
    x = np.column_stack([col("state_P"), col("state_Q")])
    z = np.column_stack([col("vstate_P"), col("vstate_Q")])

    sigma = x[:-1] - z[:-1]
    g = np.diff(z, axis=0) / h
    # The law's own clock, not the host timestamp. The agent reads
    # `t = counter * h` and the counter has already been incremented when the
    # step runs, so the step whose output lands in row k+1 saw `(k+1) h`.
    # Using the logged timestamp instead costs 1e-2 in the LC command: it
    # drifts up to 28 ms from the nominal grid, and `lam = beta_c / max(T_c -
    # t, delta_c)` turns a 28 ms error near T_c into a large one. The
    # adaptive interface has no time-dependent term and does not notice,
    # which is exactly why it had to be checked on LC.
    t_ctrl = (np.arange(len(sigma)) + 1) * h
    if "vartheta" in d:
        omega = _omega_adaptive(sigma, col("vartheta")[:-1], h, mg, t_ctrl)
    elif "S1" in d and "S2" in d:
        omega = _omega_lc(sigma, x[:-1],
                          np.column_stack([col("S1"), col("S2")])[:-1],
                          h, mg, t_ctrl)
    else:
        raise KeyError(
            f"{run.name} node {node_id}: logs neither the applied command nor "
            f"an adaptive quantity to rebuild it from; has {sorted(d)}")

    mu = g - omega
    # The LC terminal rows: command zero by eq:lc_source_terminal.
    mu[~np.isfinite(mu).all(1)] = 0.0
    m = float(mg["interface"].get("mu_max", 10.0))
    return np.clip(mu, -m, m)


def verify(run: Path, tol: float = 0.0) -> dict[int, float]:
    """`{node: max |rebuilt - logged|}` over the nodes that log both.

    Zero is the expected answer, not a small number: the reconstruction
    evaluates the same expressions on the same values. A nonzero result means
    an index convention moved and every effort figure derived from a relayed
    node is wrong until it is explained.
    """
    run = Path(run)
    out = {}
    for node_id, node in load_run(run).nodes.items():
        if not all(c in node.data for c in MU_COLS):
            continue
        logged = np.column_stack([np.asarray(node.data[c], float)
                                  for c in MU_COLS])[1:]
        rebuilt = _from_data(run, node_id, node.data, rebuild=True)
        out[node_id] = float(np.abs(rebuilt - logged).max())
    return out
