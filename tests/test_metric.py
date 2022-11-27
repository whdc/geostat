import numpy as np
from geostat import GP, NormalizingFeaturizer
import geostat.covfunc as cf
import geostat.metric as gm


def test_scaled_euclidean():
    # Create 100 random locations in a square centered on the origin.
    locs1 = np.random.uniform(-1., 1., [1000, 3])

    # Initialize featurizer of location for trends.
    def trend_terms(x, y, z): return z, z*z
    featurizer = NormalizingFeaturizer(trend_terms, locs1)
    metric = gm.Euclidean(scale=[1., 1., 'zscale'])
    covariance = \
        cf.Trend(featurizer) + \
        cf.GammaExponential(metric=metric) + \
        cf.Delta(axes=[0, 1]) + \
        cf.Noise()

    # Generating GP.
    gp1 = GP(
        covariance = covariance,
        parameters = dict(alpha=1., zscale=5., range=0.5, sill=1., gamma=1., dsill=1., nugget=1.),
        verbose=True)

    # Generate data.
    vals1 = gp1.generate(locs1).vals

    # Fit GP.
    gp2 = GP(
        covariance = covariance,
        parameters = dict(alpha=2., zscale=1., range=1.0, sill=0.5, gamma=0.5, dsill=0.5, nugget=0.5),
        hyperparameters = dict(reg=0, train_iters=500),
        verbose=True).fit(locs1, vals1)

    # Interpolate using GP.
    N = 10
    xx, yy, zz = np.meshgrid(np.linspace(-1, 1, N), np.linspace(-1, 1, N), np.linspace(-1, 1, N))
    locs2 = np.stack([xx, yy, zz], axis=-1).reshape([-1, 3])

    mean, var = gp2.predict(locs2)
    mean2, var2 = gp2.predict(locs2)

    assert np.all(mean == mean2)
    assert np.all(var == var2)
