from dataclasses import dataclass
from typing import Callable, Dict

import numpy as np

# Tensorflow is extraordinarily noisy. Catch warnings during import.
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    import tensorflow as tf

from .op import Op
from .param import ppp, upp, bpp

def ed(x, a=-1):
    return tf.expand_dims(x, a)

class PerAxisDist2(Op):
    def __init__(self):
        super().__init__({}, dict(locs1='locs1', locs2='locs2'))

    def __call__(self, e):
        x1 = e['locs1']
        x2 = e['locs2']
        return tf.square(ed(x1, 1) - ed(x2, 0))

class Metric(Op):
    pass

def get_scale_vars(scale):
    if scale is not None:
        return {k: p for s in scale for k, p in ppp(s).items()}
    else:
        return {}

class Euclidean(Metric):
    def __init__(self, scale=None):
        fa = dict(scale=scale)
        super().__init__(fa, dict(pa_d2='per_axis_dist2'))

    def vars(self):
        return get_scale_vars(self.fa['scale'])

    def __call__(self, e):
        if e['scale'] is not None:
            return tf.einsum('abc,c->ab', e['pa_d2'], tf.square(e['scale']))
        else:
            return tf.reduce_sum(e['pa_d2'], axis=-1)

class Poincare(Metric):
    def __init__(self, xform: Callable, zoff='zoff', scale=None):
        fa = dict(zoff=zoff, scale=scale)
        self.xform = xform
        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return ppp(self.fa['zoff']) | get_scale_vars(self.fa['scale'])

    def __call__(self, e):
        xlocs1 = tf.stack(self.xform(*tf.unstack(e['locs1'], axis=1)), axis=1)
        xlocs2 = tf.stack(self.xform(*tf.unstack(e['locs2'], axis=1)), axis=1)
        zoff = e['zoff']

        # Maybe scale locations and zoff.
        if e['scale'] is not None:
            xlocs1 *= e['scale']
            xlocs2 *= e['scale']
            zoff *= e['scale'][0]

        z1 = xlocs1[:, 0] + zoff
        z2 = xlocs2[:, 0] + zoff
        zz = ed(z1, -1) * z2

        d2 = tf.reduce_sum(tf.square(ed(xlocs1, 1) - ed(xlocs2, 0)), axis=-1)
        d2 = tf.asinh(0.5 * tf.sqrt(d2 / zz))
        d2 = tf.square(2.0 * zoff * d2)

        return d2

def is_distance_matrix(m):
    assert len(m.shape) == 2
    assert m.shape[0] == m.shape[1]

    a = np.ones_like(m) * float('inf')
    for i in range(m.shape[0]):
        a = np.minimum(a, m[i:i+1, :] + m[:, i:i+1])

    # print(np.nonzero(~np.equal(a, m)))
    return np.allclose(a, m)
