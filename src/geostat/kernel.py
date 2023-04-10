from dataclasses import dataclass
from typing import Callable, List, Union
import numpy as np

# Tensorflow is extraordinarily noisy. Catch warnings during import.
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    import tensorflow as tf
    from tensorflow.linalg import LinearOperatorFullMatrix as LOFullMatrix
    from tensorflow.linalg import LinearOperatorBlockDiag as LOBlockDiag

from .op import Op
from .metric import Euclidean, PerAxisDist2, ed
from .param import get_parameter_values, ppp, upp, bpp
from .mean import get_trend_coefs

__all__ = ['Kernel']

def block_diag(blocks):
    """Return a dense block-diagonal matrix."""
    return LOBlockDiag([LOFullMatrix(b) for b in blocks]).to_dense()

class Kernel(Op):
    def __init__(self, fa, autoinputs):
        if 'offset' not in autoinputs: autoinputs['offset'] = 'offset'
        if 'locs1' not in autoinputs: autoinputs['locs1'] = 'locs1'
        if 'locs2' not in autoinputs: autoinputs['locs2'] = 'locs2'
        super().__init__(fa, autoinputs)

    def __add__(self, other):
        if other is None:
            return self
        else:
            return Stack([self]) + other

    def __mul__(self, other):
        return Product([self]) * other

    def call(self, p, e):
        """
        Returns tuple `(mean, covariance)` for locations.
        Return values may be unbroadcasted.
        """
        pass

    def __call__(self, p, e):
        """
        Returns tuple `(mean, covariance)` for locations.
        Return values have correct shapes.
        """
        C = self.call(p, e)
        if C is None: C = 0.
        n1 = tf.shape(e['locs1'])[0]
        n2 = tf.shape(e['locs2'])[0]
        C = tf.broadcast_to(C, [n1, n2])
        return C

    def report(self, p):
        string = ', '.join('%s %4.2f' % (v.name, p[v.name]) for v in self.vars())
        return '[' + string + ']'

    def reg(self, p):
        pass

class TrendPrior(Kernel):
    def __init__(self, featurizer, alpha='alpha'):
        fa = dict(alpha=alpha)
        self.featurizer = featurizer
        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return ppp(self.fa['alpha'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        F1 = tf.cast(self.featurizer(e['locs1']), tf.float32)
        F2 = tf.cast(self.featurizer(e['locs2']), tf.float32)
        return v['alpha'] * tf.einsum('ba,ca->bc', F1, F2)

    def reg(self, p):
        return 0.

def scale_to_metric(scale, metric):
    assert scale is None or metric is None
    if metric is None:
        if scale is None:
            metric = 'euclidean'
        else:
            metric = Euclidean(scale)
    return metric

class SquaredExponential(Kernel):
    def __init__(self, sill='sill', range='range', scale=None, metric=None):
        fa = dict(sill=sill, range=range)
        autoinputs = scale_to_metric(scale, metric)
        super().__init__(fa, dict(d2=autoinputs))

    def vars(self):
        return ppp(self.fa['sill']) + ppp(self.fa['range'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        return v['sill'] * tf.exp(-0.5 * e['d2'] / tf.square(v['range']))

    def reg(self, p):
        v = get_parameter_values(self.fa, p)
        return v['range']

class GammaExponential(Kernel):
    def __init__(self, range='range', sill='sill', gamma='gamma', scale=None, metric=None):
        fa = dict(sill=sill, range=range, gamma=gamma, scale=scale)
        autoinputs = scale_to_metric(scale, metric)
        super().__init__(fa, dict(d2=autoinputs))

    def vars(self):
        return ppp(self.fa['sill']) + ppp(self.fa['range']) + bpp(self.fa['gamma'], 0., 2.)

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        return v['sill'] * gamma_exp(e['d2'] / tf.square(v['range']), v['gamma'])

    def reg(self, p):
        v = get_parameter_values(self.fa, p)
        return v['range']

class Wiener(Kernel):
    def __init__(self, axis, start):

        self.axis = axis
        self.start = start

        # Include the element of scale corresponding to the axis of
        # integration as an explicit formal argument.
        fa = dict()

        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return []

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        x1 = e['locs1'][..., self.axis]
        x2 = e['locs2'][..., self.axis]
        k = tf.minimum(ed(x1, 1), ed(x2, 0)) - self.start
        return k

    def reg(self, p):
        return 0.

class IntSquaredExponential(Kernel):
    def __init__(self, axis, start, range='range'):

        self.axis = axis
        self.start = start

        # Include the element of scale corresponding to the axis of
        # integration as an explicit formal argument.
        fa = dict(range=range)

        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return ppp(self.fa['range'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        x1 = tf.pad(e['locs1'][..., self.axis] - self.start, [[1, 0]])
        x2 = tf.pad(e['locs2'][..., self.axis] - self.start, [[1, 0]])

        r = v['range']
        sdiff = (ed(x1, 1) - ed(x2, 0)) / (r * np.sqrt(2.))
        k = -tf.square(r) * (np.sqrt(np.pi) * sdiff * tf.math.erf(sdiff) + tf.exp(-tf.square(sdiff)))
        k -= k[0:1, :]
        k -= k[:, 0:1]
        k = k[1:, 1:]

        return k

    def reg(self, p):
        return 0.

class IntExponential(Kernel):
    def __init__(self, axis, start, range='range'):

        self.axis = axis
        self.start = start

        # Include the element of scale corresponding to the axis of
        # integration as an explicit formal argument.
        fa = dict(range=range)

        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return ppp(self.fa['range'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        x1 = tf.pad(e['locs1'][..., self.axis] - self.start, [[1, 0]])
        x2 = tf.pad(e['locs2'][..., self.axis] - self.start, [[1, 0]])

        r = v['range']
        sdiff = tf.abs(ed(x1, 1) - ed(x2, 0)) / r
        k = -tf.square(r) * (sdiff + tf.exp(-sdiff))
        k -= k[0:1, :]
        k -= k[:, 0:1]
        k = k[1:, 1:]

        return k

    def reg(self, p):
        return 0.

class Noise(Kernel):
    def __init__(self, nugget='nugget'):
        fa = dict(nugget=nugget)
        super().__init__(fa, dict(locs1='locs1', locs2='locs2'))

    def vars(self):
        return ppp(self.fa['nugget'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)

        indices1 = tf.range(tf.shape(e['locs1'])[0])
        indices2 = tf.range(tf.shape(e['locs2'])[0]) + e['offset']
        C = tf.where(tf.equal(tf.expand_dims(indices1, -1), indices2), v['nugget'], 0.)
        return C

    def reg(self, p):
        return 0.

class Delta(Kernel):
    def __init__(self, dsill='dsill', axes=None):
        fa = dict(dsill=dsill)
        self.axes = axes
        super().__init__(fa, dict(pa_d2='per_axis_dist2'))

    def vars(self):
        return ppp(self.fa['dsill'])

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)

        if self.axes is not None:
            n = tf.shape(e['pa_d2'])[-1]
            mask = tf.math.bincount(self.axes, minlength=n, maxlength=n, dtype=tf.float32)
            d2 = tf.einsum('abc,c->ab', e['pa_d2'], mask)
        else:
            d2 = tf.reduce_sum(e['pa_d2'], axis=-1)

        return v['dsill'] * tf.cast(tf.equal(d2, 0.), tf.float32)

    def reg(self, p):
        return 0.

class Mix(Kernel):
    def __init__(self, inputs, weights):
        super().__init__(
            dict(weights=weights),
            dict(inputs=inputs, cats1='cats1', cats2='cats2'))

    def vars(self):
        return [p for row in self.fa['weights']
                  for p in get_trend_coefs(row)]

    def call(self, p, e):
        v = get_parameter_values(self.fa, p)
        weights = []
        for row in v['weights']:
            if isinstance(row, (tuple, list)):
                row = tf.stack(row)
                weights.append(row)
        weights = tf.stack(weights)
        C = tf.stack(e['inputs'], axis=-1) # [locs, locs, numinputs].
        Aaug1 = tf.gather(weights, e['cats1']) # [locs, numinputs].
        Aaug2 = tf.gather(weights, e['cats2']) # [locs, numinputs].
        outer = tf.einsum('ac,bc->abc', Aaug1, Aaug2) # [locs, locs, numinputs].
        C = tf.einsum('abc,abc->ab', C, outer) # [locs, locs].
        return C

class Mux(Kernel):
    def __init__(self, inputs):
        self.inputs = inputs
        super().__init__(
            dict(),
            dict(cats1='cats1', cats2='cats2'))

    def vars(self):
        return []

    def gather_vars(self, cache=None):
        """Make a special version of gather_vars because
           we can want to gather variables from `inputs`,
           even though it's not in autoinputs"""
        vv = super().gather_vars(cache)
        for iput in self.inputs:
            cache[id(self)] |= iput.gather_vars(cache)
        return cache[id(self)]

    def call(self, p, e):
        N = len(self.inputs)
        catcounts1 = tf.math.bincount(e['cats1'], minlength=N, maxlength=N)
        catcounts2 = tf.math.bincount(e['cats2'], minlength=N, maxlength=N)
        catindices1 = tf.math.cumsum(catcounts1, exclusive=True)
        catindices2 = tf.math.cumsum(catcounts2, exclusive=True)
        catdiffs = tf.unstack(catindices2 - catindices1, num=N)
        locsegs1 = tf.split(e['locs1'], catcounts1, num=N)
        locsegs2 = tf.split(e['locs2'], catcounts2, num=N)

        CC = [] # Observation noise submatrices.
        for sublocs1, sublocs2, catdiff, iput in zip(locsegs1, locsegs2, catdiffs, self.inputs):
            cache = dict(
                offset = e['offset'] + catdiff,
                locs1 = sublocs1,
                locs2 = sublocs2)
            cache['per_axis_dist2'] = PerAxisDist2().run(cache, p)
            cache['euclidean'] = Euclidean().run(cache, p)
            Csub = iput.run(cache, p)
            CC.append(Csub)

        return block_diag(CC)

class Stack(Kernel):
    def __init__(self, parts: List[Kernel]):
        self.parts = parts
        super().__init__({}, dict(locs1='locs1', locs2='locs2', parts=parts))

    def vars(self):
        return [p for part in self.parts for p in part.vars()]

    def __add__(self, other):
        if isinstance(other, Kernel):
            return Stack(self.parts + [other])
    
    def call(self, p, e):
        return tf.reduce_sum(e['parts'], axis=0)

    def report(self, p):
        return ' '.join(part.report(p) for part in self.parts)

    def reg(self, p):
        return tf.reduce_sum([part.reg(p) for part in self.parts], axis=0)

class Product(Kernel):
    def __init__(self, parts: List[Kernel]):
        self.parts = parts
        super().__init__({}, dict(locs1='locs1', locs2='locs2', parts=parts))

    def vars(self):
        return [p for part in self.parts for p in part.vars()]

    def __mul__(self, other):
        if isinstance(other, Kernel):
            return Product(self.parts + [other])
    
    def call(self, p, e):
        return tf.reduce_sum(e['parts'], axis=0)

    def report(self, p):
        return ' '.join(part.report(p) for part in self.parts)

    def reg(self, p):
        return tf.reduce_sum([part.reg(p) for part in self.parts], axis=0)

# Gamma exponential covariance function.
@tf.custom_gradient
def safepow(x, a):
    y = tf.pow(x, a)
    def grad(dy):
        dx = tf.where(x <= 0.0, tf.zeros_like(x), dy * tf.pow(x, a-1))
        dx = unbroadcast(dx, x.shape)
        da = tf.where(x <= 0.0, tf.zeros_like(a), dy * y * tf.math.log(x))
        da = unbroadcast(da, a.shape)
        return dx, da
    return y, grad

def unbroadcast(x, shape):
    excess_rank = tf.maximum(0, len(x.shape) - len(shape))
    x = tf.reduce_sum(x, axis=tf.range(excess_rank))
    axes_that_are_one = tf.where(tf.equal(shape, 1))[:, 0]
    x = tf.reduce_sum(x, axis=axes_that_are_one, keepdims=True)
    return x

def gamma_exp(d2, gamma):
    return tf.exp(-safepow(tf.maximum(d2, 0.0), 0.5 * gamma))

class Observation(Op):

    def __init__(self,
        coefs: List,
        noise: Kernel
    ):
        self.coefs = coefs
        self.noise = noise
        super().__init__({}, self.noise)

    def vars(self):
        vv = [p for c in self.coefs for p in upp(c)]
        vv += self.noise.vars()
        return vv

    def __call__(self, p, e):
        """
        Dummy.
        """
        return 0.
