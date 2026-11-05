"""A small CNN with manual backpropagation in plain NumPy.

Used because this project's demo model had to be trained where PyTorch was unavailable, and
because the result has no dependencies and ports directly to JavaScript for on-phone inference.
Layers: [Conv3x3 - BatchNorm - ReLU - MaxPool] x N, global average pool, linear head.
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

F32 = np.float32


class Conv:
    def __init__(self, cin, cout, rng):
        self.w = (rng.normal(0, np.sqrt(2.0 / (cin * 9)), (cin * 9, cout))).astype(F32)
        self.b = np.zeros(cout, F32)
        self.cin, self.cout = cin, cout

    def forward(self, x, train):
        N, C, H, W = x.shape
        p = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
        u = swv(p, (3, 3), axis=(2, 3))                      # N,C,H,W,3,3
        u = u.transpose(0, 2, 3, 1, 4, 5).reshape(N * H * W, C * 9)
        out = (u @ self.w + self.b).reshape(N, H, W, self.cout).transpose(0, 3, 1, 2)
        if train:
            self.u, self.shape = u, x.shape
        return out

    def backward(self, g):
        N, C, H, W = self.shape
        G = g.transpose(0, 2, 3, 1).reshape(N * H * W, self.cout)
        self.gw = (self.u.T @ G).astype(F32)
        self.gb = G.sum(axis=0).astype(F32)
        d = (G @ self.w.T).reshape(N, H, W, C, 3, 3)
        out = np.zeros((N, C, H + 2, W + 2), F32)
        for i in range(3):
            for j in range(3):
                out[:, :, i:i + H, j:j + W] += d[:, :, :, :, i, j].transpose(0, 3, 1, 2)
        self.u = None
        return out[:, :, 1:-1, 1:-1]

    def params(self):
        return [("w", self.w, self.gw, True), ("b", self.b, self.gb, False)]


class BN:
    def __init__(self, c, momentum=0.05):
        self.g = np.ones(c, F32); self.bt = np.zeros(c, F32)
        self.mean = np.zeros(c, F32); self.var = np.ones(c, F32); self.m = momentum

    def forward(self, x, train):
        if train:
            mu = x.mean(axis=(0, 2, 3)); v = x.var(axis=(0, 2, 3))
            self.mean += self.m * (mu - self.mean); self.var += self.m * (v - self.var)
        else:
            mu, v = self.mean, self.var
        s = np.sqrt(v + 1e-5)
        z = (x - mu[None, :, None, None]) / s[None, :, None, None]
        if train:
            self.z, self.s = z, s
        return z * self.g[None, :, None, None] + self.bt[None, :, None, None]

    def backward(self, g):
        # Exact gradient, including through the batch mean and variance.
        self.gg = (g * self.z).sum(axis=(0, 2, 3)).astype(F32)
        self.gbt = g.sum(axis=(0, 2, 3)).astype(F32)
        gbar = g.mean(axis=(0, 2, 3), keepdims=True)
        gz = (g * self.z).mean(axis=(0, 2, 3), keepdims=True)
        out = (g - gbar - self.z * gz) * self.g[None, :, None, None] / self.s[None, :, None, None]
        self.z = None
        return out

    def params(self):
        return [("g", self.g, self.gg, False), ("bt", self.bt, self.gbt, False)]


class ReLU:
    def forward(self, x, train):
        if train:
            self.m = x > 0
        return np.maximum(x, 0)

    def backward(self, g):
        return g * self.m

    def params(self):
        return []


class MaxPool:
    def forward(self, x, train):
        N, C, H, W = x.shape
        b = x.reshape(N, C, H // 2, 2, W // 2, 2).transpose(0, 1, 2, 4, 3, 5).reshape(N, C, H // 2, W // 2, 4)
        i = b.argmax(axis=4)
        if train:
            self.i, self.shape = i, x.shape
        return np.take_along_axis(b, i[..., None], axis=4)[..., 0]

    def backward(self, g):
        N, C, H, W = self.shape
        b = np.zeros((N, C, H // 2, W // 2, 4), F32)
        np.put_along_axis(b, self.i[..., None], g[..., None], axis=4)
        return b.reshape(N, C, H // 2, W // 2, 2, 2).transpose(0, 1, 2, 4, 3, 5).reshape(N, C, H, W)

    def params(self):
        return []


class Net:
    """Outputs: P(grade>=1), P(grade>=2), P(grade>=3), P(grade>=4), P(good quality)."""
    N_OUT = 5

    def __init__(self, channels=(16, 32, 64, 96, 128), seed=0, dropout=0.3):
        rng = np.random.default_rng(seed)
        self.rng, self.channels, self.dropout = rng, tuple(channels), dropout
        self.layers = []
        c0 = 3
        for i, c in enumerate(channels):
            self.layers += [Conv(c0, c, rng), BN(c), ReLU()]
            if i < len(channels) - 1:
                self.layers.append(MaxPool())
            c0 = c
        self.hw = (rng.normal(0, 0.01, (c0, self.N_OUT))).astype(F32)
        self.hb = np.array([-0.3, -1.0, -2.0, -3.0, 1.0], F32)

    def features(self, x, train=False):
        for l in self.layers:
            x = l.forward(x, train)
        return x

    def forward(self, x, train=False):
        f = self.features(x, train)
        self.fshape = f.shape
        z = f.mean(axis=(2, 3))
        if train and self.dropout:
            self.dm = (self.rng.random(z.shape) > self.dropout).astype(F32) / (1 - self.dropout)
            z = z * self.dm
        self.z = z
        return z @ self.hw + self.hb

    def predict(self, x, bs=16):
        out = [self.forward(x[i:i + bs]) for i in range(0, len(x), bs)]
        return 1 / (1 + np.exp(-np.concatenate(out)))

    def backward(self, dlogit):
        self.ghw = (self.z.T @ dlogit).astype(F32)
        self.ghb = dlogit.sum(axis=0).astype(F32)
        gz = dlogit @ self.hw.T
        if self.dropout:
            gz = gz * self.dm
        N, C, H, W = self.fshape
        g = np.broadcast_to((gz / (H * W))[:, :, None, None], self.fshape).astype(F32)
        for l in reversed(self.layers):
            g = l.backward(g)

    def param_list(self):
        out = [("hw", self, "hw", "ghw", True), ("hb", self, "hb", "ghb", False)]
        for i, l in enumerate(self.layers):
            if isinstance(l, Conv):
                out += [(f"{i}w", l, "w", "gw", True), (f"{i}b", l, "b", "gb", False)]
            elif isinstance(l, BN):
                out += [(f"{i}g", l, "g", "gg", False), (f"{i}bt", l, "bt", "gbt", False)]
        return out

    def state(self):
        s = {"channels": np.array(self.channels), "hw": self.hw, "hb": self.hb}
        for i, l in enumerate(self.layers):
            if isinstance(l, Conv):
                s[f"{i}_w"], s[f"{i}_b"] = l.w, l.b
            elif isinstance(l, BN):
                s[f"{i}_g"], s[f"{i}_bt"], s[f"{i}_mean"], s[f"{i}_var"] = l.g, l.bt, l.mean, l.var
        return s

    @classmethod
    def load(cls, path):
        s = np.load(path)
        n = cls(tuple(int(c) for c in s["channels"]), dropout=0)
        n.hw, n.hb = s["hw"], s["hb"]
        for i, l in enumerate(n.layers):
            if isinstance(l, Conv):
                l.w, l.b = s[f"{i}_w"], s[f"{i}_b"]
            elif isinstance(l, BN):
                l.g, l.bt, l.mean, l.var = s[f"{i}_g"], s[f"{i}_bt"], s[f"{i}_mean"], s[f"{i}_var"]
        return n

    def cam(self, x, head=1):
        """Class activation map: head weights applied to the last conv features."""
        f = self.features(x, False)
        return np.einsum("nchw,c->nhw", f, self.hw[:, head])


class Adam:
    def __init__(self, net, lr=1e-3, wd=1e-4, b1=0.9, b2=0.999):
        self.net, self.lr, self.wd, self.b1, self.b2, self.t = net, lr, wd, b1, b2, 0
        self.m, self.v = {}, {}

    def step(self):
        self.t += 1
        for name, obj, a, ga, decay in self.net.param_list():
            p = getattr(obj, a); g = getattr(obj, ga)
            m = self.m.setdefault(name, np.zeros_like(p)); v = self.v.setdefault(name, np.zeros_like(p))
            m += (1 - self.b1) * (g - m); v += (1 - self.b2) * (g * g - v)
            mh = m / (1 - self.b1 ** self.t); vh = v / (1 - self.b2 ** self.t)
            p -= self.lr * mh / (np.sqrt(vh) + 1e-8)
            if decay:
                p -= self.lr * self.wd * p
