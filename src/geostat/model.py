import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Union
import numpy as np
from scipy.special import expit, logit
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'

# Tensorflow is extraordinarily noisy. Catch warnings during import.
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    import tensorflow as tf
    import tensorflow_probability as tfp
    tfd = tfp.distributions

import tensorflow_probability as tfp

from . import mean as mn
from . import kernel as krn
from .op import Op, SingletonTraceType
from .param import PaperParameter, ParameterSpace, Bound
from .metric import Euclidean, PerAxisDist2
from .param import get_parameter_values, ppp, upp, bpp

MVN = tfp.distributions.MultivariateNormalTriL

__all__ = ['Featurizer', 'GP', 'Mix', 'Model', 'NormalizingFeaturizer']

@dataclass
class GP:
    mean: mn.Trend = None
    kernel: krn.Kernel = None

    def __post_init__(self):
        if self.mean is None or self.mean == 0:
            self.mean = mn.ZeroTrend()
        assert self.kernel is not None

    def __add__(self, other):
        return GP(self.mean + other.mean, self.kernel + other.kernel)

    def __tf_tracing_type__(self, context):
        return SingletonTraceType(self)

    def gather_vars(self):
        return set(self.mean.gather_vars()) | set(self.kernel.gather_vars())

    def reg(self, sp):
        return self.kernel.reg(sp)

def Mix(inputs, weights=None):
    return GP(
        mn.Mix([i.mean for i in inputs], weights), 
        krn.Mix([i.kernel for i in inputs], weights))

class NormalizingFeaturizer:
    """
    Produces featurized locations (F matrix) and remembers normalization
    parameters.  Adds an intercept feature.
    """
    def __init__(self, featurization, locs):
        self.unnorm_featurizer = Featurizer(featurization)
        F_unnorm = self.unnorm_featurizer(locs)
        self.unnorm_mean = tf.reduce_mean(F_unnorm, axis=0)
        self.unnorm_std = tf.math.reduce_std(F_unnorm, axis=0)

    def __call__(self, locs):
        ones = tf.ones([tf.shape(locs)[0], 1], dtype=tf.float32)
        F_unnorm = self.unnorm_featurizer(locs)
        F_norm = (F_unnorm - self.unnorm_mean) / self.unnorm_std
        return tf.concat([ones, F_norm], axis=1)

class Featurizer:
    """
    Produces featurized locations (F matrix).
    """
    def __init__(self, featurization):
        self.featurization = featurization

    def __call__(self, locs):
        locs = tf.cast(locs, tf.float32)
        if self.featurization is None: # No features.
            return tf.ones([tf.shape(locs)[0], 0], dtype=tf.float32)

        feats = self.featurization(*tf.unstack(locs, axis=1))
        if isinstance(feats, tuple): # One or many features.
            if len(feats) == 0:
                return tf.ones([tf.shape(locs)[0], 0], dtype=tf.float32)
            else:
                feats = self.featurization(*tf.unstack(locs, axis=1))
                feats = [tf.broadcast_to(tf.cast(f, tf.float32), [tf.shape(locs)[0]]) for f in feats]
                return tf.stack(feats, axis=1)
        else: # One feature.
            return e(feats)

def e(x, a=-1):
    return tf.expand_dims(x, a)

@tf.function
def gp_covariance(gp, locs, cats, p):
    return gp_covariance2(gp, locs, cats, locs, cats, 0, p)

@tf.function
def gp_covariance2(gp, locs1, cats1, locs2, cats2, offset, p):
    """
    `offset` is i2-i1, where i1 and i2 are the starting indices of locs1
    and locs2.  It is used to create the diagonal non-zero elements
    of a Noise covariance function.  An non-zero offset results in a
    covariance matrix with non-zero entries along an off-center diagonal.
    """

    # assert np.all(cats1 == np.sort(cats1)), '`cats1` must be in non-descending order'
    # assert np.all(cats2 == np.sort(cats2)), '`cats2` must be in non-descending order'

    locs1 = tf.cast(locs1, tf.float32)
    locs2 = tf.cast(locs2, tf.float32)

    cache = {}
    cache['offset'] = offset
    cache['locs1'] = locs1
    cache['locs2'] = locs2
    cache['cats1'] = cats1
    cache['cats2'] = cats2
    cache['per_axis_dist2'] = PerAxisDist2().run(cache, p)
    cache['euclidean'] = Euclidean().run(cache, p)

    M = gp.mean.run(cache, p)
    C = gp.kernel.run(cache, p)
    M = tf.cast(M, tf.float64)
    C = tf.cast(C, tf.float64)
    return M, C

@tf.function
def mvn_log_pdf(u, m, cov):
    """Log PDF of a multivariate gaussian."""
    u_adj = u - m
    logdet = tf.linalg.logdet(2 * np.pi * cov)
    quad = tf.matmul(e(u_adj, 0), tf.linalg.solve(cov, e(u_adj, -1)))[0, 0]
    return tf.cast(-0.5 * (logdet + quad), tf.float32)

@tf.function
def gp_log_likelihood(data, surf_params, gp):
    m, S = gp_covariance(gp, data['locs'], data['cats'], surf_params)
    u = tf.cast(data['vals'], tf.float64)
    return mvn_log_pdf(u, m, S)

def gp_train_step(optimizer, data, parameters, parameter_space, gp, reg=None):
    with tf.GradientTape() as tape:
        sp = parameter_space.get_surface(parameters)

        ll = gp_log_likelihood(data, sp, gp)

        if reg:
            reg_penalty = reg * gp.reg(sp)
        else:
            reg_penalty = 0.

        loss = -ll + reg_penalty

    gradients = tape.gradient(loss, parameters.values())
    optimizer.apply_gradients(zip(gradients, parameters.values()))
    return sp, ll, reg_penalty

def check_parameters(pps: List[PaperParameter], values: Dict[str, float]) -> Dict[str, Bound]:
    d = defaultdict(list)
    for pp in pps:
        d[pp.name].append(pp)
    out = {}
    for name, pps in d.items():
        lo = np.max([pp.lo for pp in pps])
        hi = np.min([pp.hi for pp in pps])
        assert lo < hi, 'Conflicting bounds for parameter `%s`' % name
        assert name in values, 'Parameter `%s` is missing' % name
        assert np.all(lo <= values[name]) and np.all(values[name] <= hi), 'Parameter `%s` is out of bounds' % name
        out[name] = Bound(lo, hi)
    return out

@dataclass
class Model():
    gp: GP
    parameters: Dict[str, np.ndarray] = None
    parameter_sample_size: Optional[int] = None
    locs: np.ndarray = None
    vals: np.ndarray = None
    cats: np.ndarray = None
    report: Callable = None
    verbose: bool = True

    def __post_init__(self):

        '''
        Parameters:
                x : Pandas DataFrame with columns for locations.

                u : A Pandas Series containing observations.

                featurization : function, optional
                    Should be a function that takes x1 (n-dim array of input data)
                    and returns the coordinates, i.e., x, y, x**2, y**2.
                    Example: def featurization(x1):
                                return x1[:, 0], x1[:, 1], x1[:, 0]**2, x1[:, 1]**2.
                    Default is None.

                latent : List[GP]
                     Name of the covariance function to use in the GP.
                     Should be 'squared-exp' or 'gamma-exp'.
                     Default is 'squared-exp'.

                parameters : dict, optional
                    The starting point for the parameters.
                    Example: parameters=dict(range=2.0, sill=5.0, nugget=1.0).
                    Default is None.

                verbose : boolean, optional
                    Whether or not to print parameters.
                    Default is True.

        Performs Gaussian process training and prediction.
        '''

        # Default reporting function.
        def default_report(p, prefix=None):
            if prefix: print(prefix, end=' ')

            def fmt(x):
                if isinstance(x, tf.Tensor):
                    x = x.numpy()

                if isinstance(x, (int, np.int32, np.int64)):
                    return '{:5d}'.format(x)
                if isinstance(x, (float, np.float32, np.float64)):
                    return '{:5.2f}'.format(x)
                else:
                    with np.printoptions(precision=2, formatter={'floatkind': '{:5.2f}'.format}):
                        return str(x)

            print('[%s]' % (' '.join('%s %s' % (k, fmt(v)) for k, v in p.items())))

        if self.report == None: self.report = default_report

        if self.locs is not None: self.locs = np.array(self.locs)
        if self.vals is not None: self.vals = np.array(self.vals)
        if self.cats is not None: self.cats = np.array(self.cats)

        # Collect paraameters.
        if self.parameters is None: self.parameters = {}

        self.parameter_space = ParameterSpace(check_parameters(self.gp.gather_vars(), self.parameters))

    def fit(self, locs, vals, cats=None,
        step_size=0.01, iters=100, reg=None):

        # Permute datapoints if cats is given.
        if cats is not None:
            cats = np.array(cats)
            perm = np.argsort(cats)
            locs, vals, cats = locs[perm], vals[perm], cats[perm]

        # Data dict.
        self.data = {
            'locs': tf.constant(locs, dtype=tf.float32),
            'vals': tf.constant(vals, dtype=tf.float32),
            'cats': None if cats is None else tf.constant(cats, dtype=tf.int32)}

        up = self.parameter_space.get_underlying(self.parameters)

        optimizer = tf.keras.optimizers.Adam(learning_rate=step_size)

        j = 0 # Iteration count.
        for i in range(10):
            t0 = time.time()
            while j < (i + 1) * iters / 10:
                p, ll, reg_penalty = gp_train_step(optimizer, self.data, up, self.parameter_space,
                    self.gp, reg)
                j += 1

            time_elapsed = time.time() - t0
            if self.verbose == True:
                self.report(dict(iter=j, ll=ll, time=time_elapsed, reg=reg_penalty, **p))

        new_parameters = self.parameter_space.get_surface(up, numpy=True)

        # Restore order if things were permuted.
        if cats is not None:
            revperm = np.argsort(perm)
            locs, vals, cats = locs[revperm], vals[revperm], cats[revperm]

        return replace(self, parameters = new_parameters, locs=locs, vals=vals, cats=cats)

    def mcmc(self, locs, vals, cats=None,
            chains=4, step_size=0.1, move_prob=0.5,
            samples=1000, burnin=500, report_interval=100):

        assert samples % report_interval == 0, '`samples` must be a multiple of `report_interval`'
        assert burnin % report_interval == 0, '`burnin` must be a multiple of `report_interval`'

        # Permute datapoints if cats is given.
        if cats is not None:
            cats = np.array(cats)
            perm = np.argsort(cats)
            locs, vals, cats = locs[perm], vals[perm], cats[perm]

        # Data dict.
        self.data = {
            'locs': tf.constant(locs, dtype=tf.float32),
            'vals': tf.constant(vals, dtype=tf.float32),
            'cats': None if cats is None else tf.constant(cats, dtype=tf.int32)}

        # Initial MCMC state.
        initial_up = self.parameter_space.get_underlying(self.parameters)

        # Unnormalized log posterior distribution.
        def g(up):
            sp = self.parameter_space.get_surface(up)
            return gp_log_likelihood(self.data, sp, self.gp)

        def f(*up_flat):
            up = tf.nest.pack_sequence_as(initial_up, up_flat)
            ll = tf.map_fn(g, up, fn_output_signature=tf.float32)
            # log_prior = -tf.reduce_sum(tf.math.log(1. + tf.square(up_flat)), axis=0)
            return ll # + log_prior

        # Run the chain for a burst.
        @tf.function
        def run_chain(current_state, final_results, kernel, iters):
            samples, results, final_results = tfp.mcmc.sample_chain(
                num_results=iters,
                current_state=current_state,
                kernel=kernel,
                return_final_kernel_results=True,
                trace_fn=lambda _, results: results)

            return samples, results, final_results
        
        def new_state_fn(scale, dtype):
          direction_dist = tfd.Normal(loc=dtype(0), scale=dtype(1))
          scale_dist = tfd.Exponential(rate=dtype(1/scale))
          pick_dist = tfd.Bernoulli(probs=move_prob)

          def _fn(state_parts, seed):
            next_state_parts = []
            part_seeds = tfp.random.split_seed(
                seed, n=len(state_parts), salt='rwmcauchy')
            for sp, ps in zip(state_parts, part_seeds):
                pick = tf.cast(pick_dist.sample(sample_shape=sp.shape, seed=ps), tf.float32)
                direction = direction_dist.sample(sample_shape=sp.shape, seed=ps)
                scale_val = scale_dist.sample(seed=ps)
                next_state_parts.append(sp + tf.einsum('a...,a->a...', pick * direction, scale_val))
            return next_state_parts
          return _fn

        inv_temps = 0.5**np.arange(chains, dtype=np.float32)

        def make_kernel_fn(target_log_prob_fn):
            return tfp.mcmc.RandomWalkMetropolis(
                target_log_prob_fn=target_log_prob_fn,
                new_state_fn=new_state_fn(scale=step_size / np.sqrt(inv_temps), dtype=np.float32))

        kernel = tfp.mcmc.ReplicaExchangeMC(
            target_log_prob_fn=f,
            inverse_temperatures=inv_temps,
            make_kernel_fn=make_kernel_fn)

        # Do bursts.
        current_state = tf.nest.flatten(initial_up)
        final_results = None
        acc_states = []
        num_bursts = (samples + burnin) // report_interval
        burnin_bursts = burnin // report_interval
        for i in range(num_bursts):
            is_burnin = i < burnin_bursts

            if self.verbose and (i == 0 or i == burnin_bursts):
                print('BURNIN\n' if is_burnin else '\nSAMPLING')
            
            t0 = time.time()
            states, results, final_results = run_chain(current_state, final_results, kernel, report_interval)

            if self.verbose == True:
                if not is_burnin: print()
                accept_rates = results.post_swap_replica_results.is_accepted.numpy().mean(axis=0)
                print('[iter {:4d}] [time {:.1f}] [accept rates {}]'.format(
                    ((i if is_burnin else i - burnin_bursts) + 1) * report_interval,
                    time.time() - t0,
                    ' '.join([f'{x:.2f}' for x in accept_rates.tolist()])))

            if not is_burnin:
                acc_states.append(tf.nest.map_structure(lambda x: x.numpy(), states))
                all_states = [np.concatenate(x, 0) for x in zip(*acc_states)]
                up = tf.nest.pack_sequence_as(initial_up, all_states)
                sp = self.parameter_space.get_surface(up, numpy=True) 

                # Reporting
                if self.verbose == True:
                    for p in [5, 50, 95]:
                        x = tf.nest.map_structure(lambda x: np.percentile(x, p, axis=0), sp)
                        self.report(x, prefix=f'{p:02d}%ile')

            current_state = [s[-1] for s in states]

        posterior = self.parameter_space.get_surface(up, numpy=True)

        # Restore order if things were permuted.
        if cats is not None:
            revperm = np.argsort(perm)
            locs, vals, cats = locs[revperm], vals[revperm], cats[revperm]

        return replace(self, 
            parameters=posterior,
            parameter_sample_size=samples,
            locs=locs, vals=vals, cats=cats)

    def generate(self, locs, cats=None):
        assert self.locs is None and self.vals is None, 'Conditional generation not yet supported'
        assert self.parameter_sample_size is None, 'Generation from a distribution not yet supported'

        locs = np.array(locs)

        # Permute datapoints if cats is given.
        if cats is not None:
            cats = np.array(cats)
            perm = np.argsort(cats)
            locs, cats = locs[perm], cats[perm]

        up = self.parameter_space.get_underlying(self.parameters)

        p = self.parameter_space.get_surface(up)

        m, S = gp_covariance(
            self.gp,
            tf.constant(locs, dtype=tf.float32),
            None if cats is None else tf.constant(cats, dtype=tf.int32),
            p)

        vals = MVN(m, tf.linalg.cholesky(S)).sample().numpy()

        # Restore order if things were permuted.
        if cats is not None:
            revperm = np.argsort(perm)
            locs, vals, cats = locs[revperm], vals[revperm], cats[revperm]

        return replace(self, locs=locs, vals=vals, cats=cats)

    def predict(self, locs2, cats2=None, *, subsample=None, reduce=None, tracker=None, pair=False):
        '''
        Performs GP predictions of the mean and variance.
        Has support for batch predictions for large data sets.
        '''

        assert subsample is None or self.parameter_sample_size is not None, \
            '`subsample` is only valid with sampled parameters'

        assert reduce is None or self.parameter_sample_size is not None, \
            '`reduce` is only valid with sampled parameters'

        assert subsample is None or reduce is None, \
            '`subsample` and `reduce` cannot both be given'

        if tracker is None: tracker = lambda x: x

        assert self.locs.shape[-1] == locs2.shape[-1], 'Mismatch in location dimentions'
        if cats2 is not None:
            assert cats2.shape == locs2.shape[:1], 'Mismatched shapes in cats and locs'

        def interpolate_batch(A11i, locs1, vals1diff, cats1, locs2, cats2, parameters):
            """
            Inputs:
              locs1.shape = [N1, K]
              vals1diff.shape = [N1]
              cats1.shape = [N1]
              locs2.shape = [N2, K]
              cats2.shape = [N2]

            Outputs:
              u2_mean.shape = [N2]
              u2_var.shape = [N2]
            """

            N1 = len(locs1) # Number of measurements.

            # Permute datapoints if cats is given.
            if cats2 is not None:
                perm = np.argsort(cats2)
                locs2, cats2 = locs2[perm], cats2[perm]

            _, A12 = gp_covariance2(
                self.gp,
                tf.constant(locs1, dtype=tf.float32),
                tf.constant(cats1, dtype=tf.int32),
                tf.constant(locs2, dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                N1,
                parameters)

            m2, A22 = gp_covariance(
                self.gp,
                tf.constant(locs2, dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                parameters)

            # Restore order if things were permuted.
            if cats2 is not None:
                revperm = np.argsort(perm)
                m2 = tf.gather(m2, revperm)
                A12 = tf.gather(A12, revperm, axis=-1)
                A22 = tf.gather(tf.gather(A22, revperm), revperm, axis=-1)

            u2_mean = m2 + tf.einsum('ab,a->b', A12, tf.einsum('ab,b->a', A11i, vals1diff))
            u2_var = tf.linalg.diag_part(A22) -  tf.einsum('ab,ab->b', A12, tf.matmul(A11i, A12))

            return u2_mean, u2_var

        def interpolate_pair_batch(A11i, locs1, vals1diff, cats1, locs2, cats2, parameters):
            """
            Inputs:
              locs1.shape = [N1, K]
              vals1diff.shape = [N1]
              cats1.shape = [N1]
              locs2.shape = [N2, 2, K]
              cats2.shape = [N2]

            Outputs:
              u2_mean.shape = [N2, 2]
              u2_var.shape = [N2, 2, 2]
            """

            N1 = len(locs1) # Number of measurements.
            N2 = len(locs2) # Number of prediction pairs.

            # Permute datapoints if cats is given.
            perm = np.argsort(cats2)
            locs2, cats2 = locs2[perm], cats2[perm]

            _, A12 = gp_covariance2(
                self.gp,
                tf.constant(locs1, dtype=tf.float32),
                tf.constant(cats1, dtype=tf.int32),
                tf.constant(locs2[:, 0, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                N1,
                parameters)

            _, A13 = gp_covariance2(
                self.gp,
                tf.constant(locs1, dtype=tf.float32),
                tf.constant(cats1, dtype=tf.int32),
                tf.constant(locs2[:, 1, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                N1,
                parameters)

            m2, A22 = gp_covariance(
                self.gp,
                tf.constant(locs2[:, 0, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                parameters)

            m3, A33 = gp_covariance(
                self.gp,
                tf.constant(locs2[:, 1, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                parameters)

            _, A23 = gp_covariance2(
                self.gp,
                tf.constant(locs2[:, 0, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                tf.constant(locs2[:, 1, :], dtype=tf.float32),
                tf.constant(cats2, dtype=tf.int32),
                N2,
                parameters)

            # Reassemble into more useful shapes.

            A12 = tf.stack([A12, A13], axis=-1) # [N1, N2, 2]

            m2 = tf.stack([m2, m3], axis=-1) # [N2, 2]

            A22 = tf.linalg.diag_part(A22)
            A33 = tf.linalg.diag_part(A33)
            A23 = tf.linalg.diag_part(A23)
            A22 = tf.stack([tf.stack([A22, A23], axis=-1), tf.stack([A23, A33], axis=-1)], axis=-2) # [N2, 2, 2]

            # Restore order if things were permuted.
            revperm = np.argsort(perm)
            m2 = tf.gather(m2, revperm)
            A12 = tf.gather(A12, revperm, axis=1)
            A22 = tf.gather(A22, revperm)

            u2_mean = m2 + tf.einsum('abc,a->bc', A12, tf.einsum('ab,b->a', A11i, vals1diff))
            u2_var = A22 - tf.einsum('abc,abd->bcd', A12, tf.einsum('ae,ebd->abd', A11i, A12))

            return u2_mean, u2_var

        def interpolate(locs1, vals1, cats1, locs2, cats2, parameters, pair=False):
            # Interpolate in batches.
            batch_size = self.locs.shape[0] // 2

            for_gp = []

            if cats2 is None:
                cats2 = np.zeros(locs2.shape[:1], np.int32)

            for start in np.arange(0, len(locs2), batch_size):
                stop = start + batch_size
                subset = locs2[start:stop], cats2[start:stop]
                for_gp.append(subset)

            # Permute datapoints if cats is given.
            if cats1 is not None:
                perm = np.argsort(cats1)
                locs1, vals1, cats1 = locs1[perm], vals1[perm], cats1[perm]
            else:
                perm = None
                cats1 = np.zeros_like(locs1[..., 0], np.int32)

            m1, A11 = gp_covariance(
                self.gp,
                tf.constant(locs1, dtype=tf.float32),
                tf.constant(cats1, dtype=tf.int32),
                parameters)

            A11i = tf.linalg.inv(A11)

            u2_mean_s = []
            u2_var_s = []

            f = interpolate_pair_batch if pair else interpolate_batch

            for locs_subset, cats_subset in for_gp:
                u2_mean, u2_var = f(A11i, locs1, vals1 - m1, cats1, locs_subset, cats_subset, parameters)
                u2_mean = u2_mean.numpy()
                u2_var = u2_var.numpy()
                u2_mean_s.append(u2_mean)
                u2_var_s.append(u2_var)

            u2_mean = np.concatenate(u2_mean_s)
            u2_var = np.concatenate(u2_var_s)

            return u2_mean, u2_var

        if self.parameter_sample_size is None:
            m, v = interpolate(self.locs, self.vals, self.cats, locs2, cats2, self.parameters, pair)
        elif reduce == 'median':
            p = tf.nest.map_structure(lambda x: np.quantile(x, 0.5, axis=0).astype(np.float32), self.parameters)
            m, v = interpolate(self.locs, self.vals, self.cats, locs2, cats2, p, pair)
        elif reduce == 'mean':
            p = tf.nest.map_structure(lambda x: x.mean(axis=0).astype(np.float32), self.parameters)
            m, v = interpolate(self.locs, self.vals, self.cats, locs2, cats2, p, pair)
        else:
            samples = self.parameter_sample_size

            if subsample is not None:
                assert subsample <= samples, '`subsample` may not exceed sample size'
            else:
                subsample = samples

            # Thin by picking roughly equally-spaced samples.
            a = np.arange(samples) * subsample / samples % 1
            pick = np.concatenate([[True], a[1:] >= a[:-1]])
            parameters = tf.nest.map_structure(lambda x: x[pick], self.parameters)

            # Make a prediction for each sample.
            results = []
            for i in tracker(range(subsample)):
                p = tf.nest.map_structure(lambda x: x[i], parameters)
                results.append(interpolate(self.locs, self.vals, self.cats, locs2, cats2, p, pair))

            mm, vv = [np.stack(x) for x in zip(*results)]
            m = mm.mean(axis=0)
            v = (np.square(mm) + vv).mean(axis=0) - np.square(m)

        return m, v
