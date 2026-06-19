"""Analytic Hierarchy Process (AHP) + eigen-analysis helpers.

Two complementary eigenvalue methods, both operating on the six strategic
dimensions defined in src/prioritize/subscores.py:

1. PRESCRIPTIVE (true AHP / Saaty):
   Given a pairwise-comparison matrix of human judgments ("how much more
   important is financial_impact than time_sensitivity?"), the PRINCIPAL
   EIGENVECTOR yields the priority weights, and the principal EIGENVALUE
   (lambda_max) yields a Consistency Ratio that flags self-contradictory
   judgments. This is how you DERIVE defensible weights.

2. DESCRIPTIVE (PCA-style):
   Given the actual sub-scores of many real articles, the eigen-decomposition
   of their correlation matrix shows which dimensions DRIVE the variance in
   what the system is actually surfacing. This is how you SEE what's really
   moving the rankings.
"""
import numpy as np

# Saaty Random Index by matrix size n (for the consistency ratio).
RANDOM_INDEX = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12,
                6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}


def ahp_weights(matrix: np.ndarray) -> dict:
    """Principal-eigenvector weights + consistency from a pairwise matrix.

    Returns {weights, lambda_max, consistency_index, consistency_ratio, consistent}.
    """
    n = matrix.shape[0]
    eigvals, eigvecs = np.linalg.eig(matrix)
    k = int(np.argmax(eigvals.real))            # principal eigenvalue index
    lambda_max = float(eigvals[k].real)
    vec = np.abs(eigvecs[:, k].real)
    weights = vec / vec.sum()                   # normalize to sum 1

    ci = (lambda_max - n) / (n - 1) if n > 1 else 0.0
    ri = RANDOM_INDEX.get(n, 1.49)
    cr = ci / ri if ri else 0.0
    return {
        "weights": weights,
        "lambda_max": lambda_max,
        "consistency_index": ci,
        "consistency_ratio": cr,
        "consistent": cr < 0.10,   # Saaty's acceptability threshold
    }


def matrix_from_pairwise(labels: list[str], pairwise: dict) -> np.ndarray:
    """Build a reciprocal comparison matrix.

    `pairwise` maps "A_vs_B" -> Saaty value (1-9): how many times more important
    A is than B. Reciprocal (B vs A) is filled automatically; diagonal = 1.
    Missing pairs default to 1 (equal).
    """
    n = len(labels)
    idx = {lab: i for i, lab in enumerate(labels)}
    m = np.ones((n, n))
    for key, val in pairwise.items():
        a, b = key.split("_vs_")
        i, j = idx[a.strip()], idx[b.strip()]
        m[i, j] = float(val)
        m[j, i] = 1.0 / float(val)
    return m


def eigen_analysis(data: np.ndarray) -> dict:
    """PCA-style eigen-decomposition of the correlation matrix of `data`
    (rows = articles, cols = dimensions).

    Returns {eigenvalues, variance_explained, loadings (PC1 weights), corr}.
    """
    # Standardize columns; guard against zero-variance dimensions
    std = data.std(axis=0)
    std[std == 0] = 1.0
    z = (data - data.mean(axis=0)) / std
    corr = np.corrcoef(z, rowvar=False)
    corr = np.nan_to_num(corr)

    eigvals, eigvecs = np.linalg.eigh(corr)     # symmetric -> real, sorted asc
    order = np.argsort(eigvals)[::-1]
    eigvals = eigvals[order].real
    eigvecs = eigvecs[:, order].real

    variance_explained = eigvals / eigvals.sum()
    pc1 = np.abs(eigvecs[:, 0])
    loadings = pc1 / pc1.sum()
    return {
        "eigenvalues": eigvals,
        "variance_explained": variance_explained,
        "loadings": loadings,        # PC1 contribution per dimension (sums to 1)
        "corr": corr,
    }
