"""Evaluation helpers. Confidence intervals resample whole patients, not images."""
import numpy as np


def auc(y, s):
    y = np.asarray(y).astype(bool); s = np.asarray(s, dtype=float)
    n1, n0 = y.sum(), (~y).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = s.argsort(); ss = s[order]
    ranks = np.empty(len(s), float)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and ss[j + 1] == ss[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def sens_spec(y, s, t):
    y = np.asarray(y).astype(bool); p = np.asarray(s) >= t
    tp, fn, tn, fp = (p & y).sum(), (~p & y).sum(), (~p & ~y).sum(), (p & ~y).sum()
    return {"sensitivity": float(tp / max(1, tp + fn)), "specificity": float(tn / max(1, tn + fp)),
            "tp": int(tp), "fn": int(fn), "tn": int(tn), "fp": int(fp)}


def threshold_for_sensitivity(y, s, target=0.90):
    """Highest threshold that still reaches the target sensitivity. Pick this on validation only."""
    y = np.asarray(y).astype(bool); pos = np.sort(np.asarray(s)[y])[::-1]
    k = int(np.ceil(target * len(pos))) - 1
    return float(pos[max(0, k)])


def patient_bootstrap(fn, y, s, groups, n=2000, seed=0):
    """95% CI for fn(y, s), resampling patients with replacement."""
    rng = np.random.default_rng(seed)
    y = np.asarray(y); s = np.asarray(s); groups = np.asarray(groups)
    ids = np.unique(groups); idx = {g: np.where(groups == g)[0] for g in ids}
    vals = []
    for _ in range(n):
        take = np.concatenate([idx[g] for g in rng.choice(ids, len(ids))])
        v = fn(y[take], s[take])
        if v == v:
            vals.append(v)
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def quadratic_kappa(a, b, k=5):
    a = np.asarray(a, int); b = np.asarray(b, int)
    O = np.zeros((k, k)); np.add.at(O, (a, b), 1)
    E = np.outer(O.sum(1), O.sum(0)) / O.sum()
    W = (np.arange(k)[:, None] - np.arange(k)) ** 2 / (k - 1) ** 2
    return float(1 - (W * O).sum() / (W * E).sum())
