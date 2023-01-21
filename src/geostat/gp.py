import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Union
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
    from tensorflow.linalg import LinearOperatorFullMatrix as LOFullMatrix
    from tensorflow.linalg import LinearOperatorBlockDiag as LOBlockDiag

import tensorflow_probability as tfp

from .spatialinterpolator import SpatialInterpolator
from . import covfunc as cf
from .param import PaperParameter, ParameterSpace, Bound
from .metric import Euclidean, PerAxisDist2

MVN = tfp.distributions.MultivariateNormalTriL

__all__ = ['GP', 'NormalizingFeaturizer']

# Produces featurized locations (F matrix) and remembers normalization parameters.
class NormalizingFeaturizer:
    def __init__(self, featurization, locs):
        self.featurization = featurization
        F_unnorm = self.get_unnorm_features(locs)
        self.unnorm_mean = tf.reduce_mean(F_unnorm, axis=0)
        self.unnorm_std = tf.math.reduce_std(F_unnorm, axis=0)

    def get_unnorm_features(self, locs):
        locs = tf.cast(locs, tf.float32)
        if self.featurization is None: # No features.
            return tf.ones([tf.shape(locs)[0], 0], dtype=tf.float32)

        feats = self.featurization(*tf.unstack(locs, axis=1))
        if isinstance(feats, tuple): # One or many features.
            if len(feats) == 0:
                return tf.ones([tf.shape(locs)[0], 0], dtype=tf.float32)
            else:
                return tf.stack(self.featurization(*tf.unstack(locs, axis=1)), axis=1)
        else: # One feature.
            return e(feats)

    def __call__(self, locs):
        ones = tf.ones([tf.shape(locs)[0], 1], dtype=tf.float32)
        F_unnorm = self.get_unnorm_features(locs)
        F_norm = (F_unnorm - self.unnorm_mean) / self.unnorm_std
        return tf.concat([ones, F_norm], axis=1)

def e(x, a=-1):
    return tf.expand_dims(x, a)

def block_diag(blocks):
    """Return a dense block-diagonal matrix."""
    return LOBlockDiag([LOFullMatrix(b) for b in blocks]).to_dense()

@tf.function
def gp_covariance(covariance, observation, locs, cats, p):
    # assert np.all(cats == np.sort(cats)), '`cats` must be in non-descending order'
    locs = tf.cast(locs, tf.float32)
    cache = {}
    cache['locs'] = locs
    cache['per_axis_dist2'] = PerAxisDist2().run(cache, p)
    cache['euclidean'] = Euclidean().run(cache, p)
    C = tf.stack([c.run(cache, p) for c in covariance], axis=-1) # [locs, locs, hidden].

    numobs = len(observation)

    if numobs == 0:
        C = tf.cast(C[..., 0], tf.float64)
        m = tf.zeros_like(C[0, :])
        return m, C

    A = tf.convert_to_tensor(cf.get_parameter_values([o.coefs for o in observation], p)) # [surface, hidden].
    Aaug = tf.gather(A, cats) # [locs, hidden].

    outer = tf.einsum('ac,bc->abc', Aaug, Aaug) # [locs, locs, hidden].
    S = tf.einsum('abc,abc->ab', C, outer) # [locs, locs].

    locsegs = tf.split(locs, tf.math.bincount(cats, minlength=numobs, maxlength=numobs), num=numobs)

    NN = [] # Observation noise submatrices.
    for sublocs, o in zip(locsegs, observation):
        cache['locs'] = sublocs
        cache['per_axis_dist2'] = PerAxisDist2().run(cache, p)
        cache['euclidean'] = Euclidean().run(cache, p)
        N = o.noise.run(cache, p)
        NN.append(N)
    S += block_diag(NN)
    S = tf.cast(S, tf.float64)

    m = tf.concat([o.mu(sublocs) for sublocs, o in zip(locsegs, observation)], 0)
    m = tf.cast(m, tf.float64)

    return m, S

@tf.function
def mvn_log_pdf(u, m, cov):
    """Log PDF of a multivariate gaussian."""
    u_adj = u - m
    logdet = tf.linalg.logdet(2 * np.pi * cov)
    quad = tf.matmul(e(u_adj, 0), tf.linalg.solve(cov, e(u_adj, -1)))[0, 0]
    return tf.cast(-0.5 * (logdet + quad), tf.float32)

@tf.function
def gp_log_likelihood(data, parameters, parameter_space, covariance, observation):
    p = parameter_space.get_surface(parameters)

    m, S = gp_covariance(covariance, observation, data['locs'], data['cats'], p)

    u = tf.cast(data['vals'], tf.float64)

    return mvn_log_pdf(u, m, S)

def gp_train_step(optimizer, data, parameters, parameter_space, hyperparameters, covariance, observation):
    with tf.GradientTape() as tape:
        ll = gp_log_likelihood(data, parameters, parameter_space, covariance, observation)

        if hyperparameters['reg'] != None:
            reg = hyperparameters['reg'] * tf.reduce_sum([c.reg(p) for c in covariance])
        else:
            reg = 0.

        loss = -ll + reg

    gradients = tape.gradient(loss, parameters.values())
    optimizer.apply_gradients(zip(gradients, parameters.values()))
    return p, ll, reg

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
        assert lo <= values[name] <= hi, 'Parameter `%s` is out of bounds' % name
        out[name] = Bound(lo, hi)
    return out

@dataclass
class GP(SpatialInterpolator):
    covariance: Union[cf.CovarianceFunction, List[cf.CovarianceFunction]]
    observation: Union[cf.Observation, List[cf.Observation]] = None
    parameters: Dict[str, float] = None
    hyperparameters: object = None
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

                covariance : cf.CovarianceFunction
                     Name of the covariance function to use in the GP.
                     Should be 'squared-exp' or 'gamma-exp'.
                     Default is 'squared-exp'.

                parameters : dict, optional
                    The starting point for the parameters.
                    Example: parameters=dict(range=2.0, sill=5.0, nugget=1.0).
                    Default is None.

                hyperparameters : dict
                    Dictionary of the hyperparameters.
                      - reg: how much regularization to use. Default None (no regularization).
                      - train_iters: number of training iterations. Default 300.

                verbose : boolean, optional
                    Whether or not to print parameters.
                    Default is True.

        Performs Gaussian process training and prediction.
        '''

        super().__init__()

        if isinstance(self.covariance, cf.CovarianceFunction):
            self.covariance = [self.covariance]

        if self.observation is None: self.observation = []

        # Supply defaults.
        default_hyperparameters = dict(reg=None, train_iters=300)
        if self.hyperparameters is None: self.hyperparameters = dict()
        self.hyperparameters = dict(default_hyperparameters, **self.hyperparameters)

        if self.locs is not None: self.locs = np.array(self.locs)
        if self.vals is not None: self.vals = np.array(self.vals)
        if self.cats is not None: self.cats = np.array(self.cats)

        # Collect paraameters.
        if self.parameters is None: self.parameters = {}
        vv = {v for c in self.covariance for v in c.gather_vars()}
        vv |= {v for o in self.observation for v in o.gather_vars()}

        self.parameter_space = ParameterSpace(check_parameters(vv, self.parameters))

    def fit(self, locs, vals, cats=None):

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

        # Train the GP.
        def gpm_fit(data, parameters, hyperparameters):
            optimizer = tf.keras.optimizers.Adam(learning_rate=0.01)

            j = 0 # Iteration count.
            for i in range(10):
                t0 = time.time()
                while j < (i + 1) * self.hyperparameters['train_iters'] / 10:
                    p, ll, reg = gp_train_step(optimizer, data, parameters, self.parameter_space,
                        hyperparameters, self.covariance, self.observation)
                    j += 1

                time_elapsed = time.time() - t0
                if self.verbose == True:
                    if self.report is None:
                        s = '[iter %4d, ll %.2f, reg %.2f, time %.1f] [%s]' % (
                            j, ll, reg, time_elapsed,
                            ' '.join('%s %4.2f' % (k, v) for k, v in p.items()))
                        print(s)
                    else:
                        self.report(dict(**p, iter=j, ll=ll, time=time_elapsed, reg=reg))

        up = self.parameter_space.get_underlying(self.parameters)

        gpm_fit(self.data, up, self.hyperparameters)

        new_parameters = self.parameter_space.get_surface(up, numpy=True)

        # Restore order if things were permuted.
        if cats is not None:
            revperm = np.argsort(perm)
            locs, vals, cats = locs[revperm], vals[revperm], cats[revperm]

        return replace(self, parameters = new_parameters, locs=locs, vals=vals, cats=cats)

    def mcmc(self, locs, vals, cats=None,
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
        # def f(*up_flat):
        #     return -tf.reduce_sum(tf.square(tf.stack(up_flat)))

        # Unnormalized log posterior distribution.
        def g(up):
            return gp_log_likelihood(
                self.data, up, self.parameter_space,
                self.covariance, self.observation)

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
                return_final_kernel_results=True)

            return samples, results, final_results
        
        def new_state_fn(scale, dtype):
          direction_dist = tfd.Normal(loc=dtype(0), scale=dtype(1))
          scale_dist = tfd.Exponential(rate=dtype(1/scale))
          pick_dist = tfd.Bernoulli(probs=0.5)

          def _fn(state_parts, seed):
            next_state_parts = []
            part_seeds = tfp.random.split_seed(
                seed, n=len(state_parts), salt='rwmcauchy')
            for sp, ps in zip(state_parts, part_seeds):
                pick = tf.cast(pick_dist.sample(sample_shape=sp.shape, seed=ps), tf.float32)
                direction = direction_dist.sample(sample_shape=sp.shape, seed=ps)
                scale = scale_dist.sample(seed=ps)
                next_state_parts.append(sp + pick * direction * scale)
            return next_state_parts
          return _fn

        def make_kernel_fn(target_log_prob_fn):
            return tfp.mcmc.RandomWalkMetropolis(
                target_log_prob_fn=target_log_prob_fn,
                new_state_fn=new_state_fn(scale=0.05, dtype=np.float32))

        inverse_temperatures = 0.5**tf.range(4, dtype=np.float32)

        kernel = tfp.mcmc.ReplicaExchangeMC(
            target_log_prob_fn=f,
            inverse_temperatures=inverse_temperatures,
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
                print('BURNIN' if is_burnin else 'SAMPLING')
            
            t0 = time.time()
            samples, results, final_results = run_chain(current_state, final_results, kernel, report_interval)

            if not is_burnin:
                acc_states.append(tf.nest.map_structure(lambda x: x.numpy(), samples))
                all_states = [np.concatenate(x, 0) for x in zip(*acc_states)]
                up = tf.nest.pack_sequence_as(initial_up, all_states)
                sp = self.parameter_space.get_surface(up, numpy=True) 

                # Reporting
                if self.verbose == True:
                    for q in [0.05, 0.5, 0.95]:
                        x = tf.nest.map_structure(lambda x: np.quantile(x, q), sp)
                        print(f'Quantile {q}:')
                        self.report(x)

            if self.verbose == True:
                accept_rates = results.post_swap_replica_results.is_accepted.numpy().mean(axis=0)
                print('[time {:.1f}] [accept rates {}]'.format(
                    time.time() - t0,
                    ' '.join([f'{x:.2f}' for x in accept_rates.tolist()])))
                print('-------')

            current_state = [s[-1] for s in samples]


        # new_parameters = self.parameter_space.get_surface(up, numpy=True) 

        return

    def generate(self, locs, cats=None):
        assert self.locs is None and self.vals is None, 'Conditional generation not yet supported'

        locs = np.array(locs)

        # Permute datapoints if cats is given.
        if cats is not None:
            cats = np.array(cats)
            perm = np.argsort(cats)
            locs, cats = locs[perm], cats[perm]

        up = self.parameter_space.get_underlying(self.parameters)

        p = self.parameter_space.get_surface(up)

        m, S = gp_covariance(
            self.covariance,
            self.observation,
            tf.constant(locs, dtype=tf.float32),
            None if cats is None else tf.constant(cats, dtype=tf.int32),
            p)

        vals = MVN(m, tf.linalg.cholesky(S)).sample().numpy()

        # Restore order if things were permuted.
        if cats is not None:
            revperm = np.argsort(perm)
            locs, vals, cats = locs[revperm], vals[revperm], cats[revperm]

        return replace(self, locs=locs, vals=vals, cats=cats)

    def predict(self, locs2, cats2=None):

        '''
        Parameters:
                locs2 : n-dim array
                    Locations to make predictions.

        Returns:
                u2_mean : array
                    GP mean.

                u2_var : array
                    GP variance.


        Performs GP predictions of the mean and variance.
        Has support for batch predictions for large data sets.

        '''

        if self.locs is None:
            self.locs = np.zeros([0, locs2.shape[0]], np.float32)

        if self.vals is None:
            self.vals = np.zeros([0], np.float32)

        assert self.locs.shape[-1] == locs2.shape[-1], 'Mismatch in location dimentions'
        if cats2 is not None:
            assert cats2.shape == locs2.shape[:-1], 'Mismatched shapes in cats and locs'

        x1, u1, cats1 = self.locs, self.vals, self.cats

        # Define inputs.
        self.batch_size = x1.shape[0] // 2

        def interpolate_gp(locs1, vals1, cats1, locs2, cats2, parameters, hyperparameters):

            N1 = len(locs1) # Number of measurements.

            locs = np.concatenate([locs1, locs2], axis=0)

            if cats1 is None:
                cats = None
            else:
                cats = np.concatenate([cats1, cats2], axis=0)

            # Permute datapoints if cats is given.
            if cats is not None:
                perm = np.argsort(cats)
                locs, cats = locs[perm], cats[perm]

            p = self.parameter_space.get_surface(parameters)

            m, A = gp_covariance(
                self.covariance,
                self.observation,
                tf.constant(locs, dtype=tf.float32),
                None if cats is None else tf.constant(cats, dtype=tf.int32),
                p)

            # Restore order if things were permuted.
            if cats is not None:
                revperm = np.argsort(perm)
                m = tf.gather(m, revperm)
                A = tf.gather(tf.gather(A, revperm), revperm, axis=-1)

            A11 = A[:N1, :N1]
            A12 = A[:N1, N1:]
            A21 = A[N1:, :N1]
            A22 = A[N1:, N1:]

            u2_mean = m[N1:] + tf.matmul(A21, tf.linalg.solve(A11, e(vals1, -1)))[:, 0]
            u2_var = tf.linalg.diag_part(A22) -  tf.reduce_sum(A12 * tf.linalg.solve(A11, A12), axis=0)

            return u2_mean, u2_var

        # Interpolate in batches.
        for_gp = []
        locs2r = locs2.reshape([-1, locs2.shape[-1]])
        if cats2 is not None:
            cats2r = cats2.ravel()
        else:
            cats2r = np.zeros_like(locs2r[..., 0], np.int32)

        for start in np.arange(0, len(locs2r), self.batch_size):
            stop = start + self.batch_size
            subset = locs2r[start:stop], cats2r[start:stop]
            for_gp.append(subset)

        up = self.parameter_space.get_underlying(self.parameters)

        u2_mean_s = []
        u2_var_s = []

        for locs_subset, cats_subset in for_gp:
            u2_mean, u2_var = interpolate_gp(x1, u1, cats1, locs_subset, cats_subset, up, self.hyperparameters)
            u2_mean = u2_mean.numpy()
            u2_var = u2_var.numpy()
            u2_mean_s.append(u2_mean)
            u2_var_s.append(u2_var)

        u2_mean = np.concatenate(u2_mean_s).reshape(locs2.shape[:-1])
        u2_var = np.concatenate(u2_var_s).reshape(locs2.shape[:-1])

        return u2_mean, u2_var
