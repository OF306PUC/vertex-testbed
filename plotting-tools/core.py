import numpy as np
from numpy.typing import NDArray


# ---------------------- Core: Dynamics ----------------------
def f_dyn(t, x, Lsym, Lsb, parameters):
    A = parameters["A"]                  # scalar damping alpha_0
    c = parameters["F"]                  # scalar coupling factor
    n_nodes = parameters["N"]
    Kz = parameters["K"]                 # modal gain matrix
    U = dft_matrix(n_nodes)              # DFT matrix for modal transformation

    dxdt = np.zeros_like(x)
    theta = x[:n_nodes].T

    # Transform modal gain back to theta-coordinates
    Ktheta = U @ Kz @ U.conj().T
    Ktheta = np.real_if_close(Ktheta).real
    u = -Ktheta @ theta

    dxdt[:n_nodes] = (-A * theta - c * (Lsym + Lsb) @ theta + u).T
    return dxdt


def rk4_step(f, t, x, dt, *args):
    """
    One step of fixed-step RK4 integration.

    f : function(t, x, *args) -> dxdt
    t : current time
    x : current state vector
    dt: time step
    *args: extra arguments passed to f
    """
    k1 = f(t, x, *args)
    k2 = f(t + dt/2, x + dt/2 * k1, *args)
    k3 = f(t + dt/2, x + dt/2 * k2, *args)
    k4 = f(t + dt,   x + dt   * k3, *args)
    return x + (dt/6) * (k1 + 2*k2 + 2*k3 + k4)

# --------------------- Core: Utilities ---------------------
def save_description(path, scenario_name, model_name, N, T, dt, vareps_values, chords=None):
    with open(path, "w") as f:
        f.write(f"scenario_name : {scenario_name}\n")
        f.write(f"model_name    : {model_name}\n")
        f.write(f"N             : {N}\n")
        f.write(f"T             : {T}\n")
        f.write(f"dt            : {dt}\n")
        f.write(f"epsilon_values: {list(vareps_values)}\n")
        if chords is not None:
            f.write(f"chords        : {chords}\n")


# --------------------- Core: Metrics ------------------------
def disagreement_projector(N: int) -> NDArray[np.float64]:
    """
    P = I - (1/N) 11^T
    Projects onto the disagreement subspace for scalar agents.
    """
    one = np.ones((N, 1), dtype=float)
    return np.eye(N, dtype=float) - (one @ one.T) / N


def disagreement_projector_vector(N: int, n: int) -> NDArray[np.float64]:
    """
    P = (I_N - (1/N)11^T) ⊗ I_n
    Projects onto the disagreement subspace for vector agents.
    """
    Pn = disagreement_projector(N)
    return np.kron(Pn, np.eye(n, dtype=float))


def compute_disagreement_scalar(x_t: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    x_t shape: (N, n_t)
    Returns dis(t) shape: (n_t,)
    """
    N, n_t = x_t.shape
    P = disagreement_projector(N)  # shape (N, N)
    x_dis = P @ x_t
    return np.linalg.norm(x_dis, axis=0)


def compute_disagreement_vector(x_t: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    x_t shape: (n_t, N, n)
    Returns dis(t) shape: (n_t,)
    """
    n_t, N, n = x_t.shape
    P = disagreement_projector_vector(N, n)  # shape (Nn, Nn)
    x_flat = x_t.reshape(n_t, N * n)
    x_dis = x_flat @ P.T
    return np.linalg.norm(x_dis, axis=1)


def spectral_norm(M: NDArray[np.float64]) -> float:
    """
    ||M||_2 = largest singular value
    """
    return float(np.linalg.norm(M, ord=2))


def sup_symmetry_breaking_norm(L_sb_list: list[NDArray[np.float64]]) -> float:
    """
    Approximates sup_t ||L_sb(t)||_2 over a sampled trajectory.
    For static cases, pass a list with one matrix.
    """
    vals = [spectral_norm(L_sb) for L_sb in L_sb_list]
    return float(np.max(vals))


def analytic_margin_gap(alpha0: float,
                        K_inf: float,
                        kappa: float,
                        sup_norm_Lsb: float) -> float:
    """
    gap = alpha0 - ||K||_inf - kappa * sup_t ||L_sb(t)||_2
    """
    return float(alpha0 - K_inf - kappa * sup_norm_Lsb)

def closed_loop_matrix_scalar(a: float,
                              c: float,
                              L: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    A_cl = -a I_N - c L
    """
    N = L.shape[0]
    return -a * np.eye(N) - c * L


def eig_metrics(A_cl: NDArray[np.complex128 | np.float64]) -> dict:
    """
    Returns eigenvalue-based stability metrics.
    """
    eigvals = np.linalg.eigvals(A_cl)
    spectral_abscissa = float(np.max(np.real(eigvals)))
    min_real_part = float(np.min(np.real(eigvals)))
    return {
        "eigvals": eigvals,
        "spectral_abscissa": spectral_abscissa,
        "min_real_part": min_real_part,
    }


# --------------------- Core: Helper ------------------------
def dft_matrix(N):
    j = np.arange(N)[:, None]
    k = np.arange(N)[None, :]
    return (1.0 / np.sqrt(N)) * np.exp(-2j * np.pi * j * k / N)

def closed_loop(N, A_sc, F_sc, Lsym, Lsb, Kz): 
    L_t = Lsym + Lsb
    Ktheta = dft_matrix(N) @ Kz @ dft_matrix(N).conj().T
    Ktheta = np.real_if_close(Ktheta).real
    A_cl = -(A_sc * np.eye(N) + F_sc * L_t + Ktheta)
    return A_cl, L_t

def sa_disagreement(A_cl):
    """Second-largest real eigenvalue — slowest disagreement mode."""
    return sorted(np.linalg.eigvals(A_cl).real, reverse=True)[1]

def analytic_bound(K_inf, sup_Lsb, alpha0=0.02, c=1.0):
    """
    eq. (15): alpha0 - ||K||_inf - c*sup||DeltaL||_2
    Positive → guarantee holds; negative → outside sufficient condition.
    """
    return float(alpha0 - K_inf - c * sup_Lsb)
