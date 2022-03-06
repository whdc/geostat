import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial.distance import cdist, pdist
from scipy.stats import binned_statistic
import scipy.stats as stats
from scipy.optimize import curve_fit
import pandas as pd
import geopandas as gpd

# For krige tools only.
from shapely.geometry import Point
import pyproj

__all__ = ['Krige']

########################################################################
# Cutoff distance function using Pythagorean Theorem.
# Potentially need to find a better citation for this and a better automated method.
# Myers, D. E. (1991). On variogram estimation. The frontiers of 
# statistical scientific theory & industrial applications, 2, 261-266.

def cutoff_dist_func(x):
    
    '''
        Parameters:
                x : n-dim array
                    Locations of input data.
                    
        Returns:
                cutoff : float
                    The maximum lag distance to use in fitting the variogram.
                    Found using Pythagorean Theorem to roughly find one half the distance across the study area.
        
    '''
    
    a2 = np.square(x[:, 0].max() - x[:, 0].min())
    b2 = np.square(x[:, 1].max() - x[:, 1].min())

    cutoff = np.sqrt(a2 + b2) / 2

    return cutoff
    

#####################################################################   
# Variogram models.

def linear(D, *parameter_vals):
    slope, nugget = parameter_vals
    return slope * D + nugget

def gaussian(D, *parameter_vals):
    vrange, sill, nugget = parameter_vals
    return sill * (1. - np.exp(-np.square(D / (vrange)))) + nugget

def spherical(D, *parameter_vals):
    vrange, sill, nugget = parameter_vals
    return np.piecewise(D, [D <= vrange, D > vrange],
                    [lambda x: sill * ((3. * x) / (2. * vrange) - (x**3.) 
                                       / (2. * vrange**3.)) + nugget, sill + nugget])




class Krige:
    
    def __init__(self, 
                 x1, u1, 
                 bins,
                 variogram_func=None, 
                 variogram_params=None, 
                 cutoff_dist='auto',
                 featurization=None,
                 project=None,
                 epsg_proj='EPSG:3310',
                 show_plots=True, 
                 verbose=True,
                 ):
        
        '''
        Parameters:
                x1 : n-dim array
                    Locations of input data.
                    
                u1 : 1-d array
                    Values to be kriged.
                
                bins : int or None
                    The number of bins to use on the variogram cloud.
                    If None, variogram function is fit to the variogram cloud
                    and is not binned first.
                    
                variogram_func : str
                     Name of the variogram model to use in the kriging. 
                     Should be 'linear', 'gaussian', or 'spherical'.
                     Default is 'gaussian'.

                cutoff_dist : str or int, optional
                    The maximum lag distance to include in variogram modeling.
                    
                featurization : function, optional
                    Should be a function that takes x1 (n-dim array of input data) 
                    and returns the coordinates, i.e., x, y, x**2, y**2.
                    Example below.
                    Default is None.
                    
                project : str, opt
                    If not None, lon/lat coordinates will be projected. Can be 'meters'
                    or 'kilometers'.
                    Default is 'meters'.
                
                epsg_proj : str
                    The projected coordinate system to use. Ignored if project=False.
                    Default is 'EPSG:3310' (California Albers).

                show_plots : boolean, optional
                    Whether or not to show variogram plots.
                    Default is True.

                verbose : boolean, optional
                    Whether or not to print parameters.
                    Default is True.


        Performs experimental variogram calculation, bins data, and fits variogram model to estimate variogram parameters.       
                 
        Performs ordinary and universal kriging in up to 3 spatial dimensions.
        
        
        Trend model example:
        def featurization(x1):    
            return x1[:, 0], x1[:, 1], x1[:, 0]**2, x1[:, 1]**2
        
        
        '''
        
        # Save original x1 for stats function.
        self.x1_original = x1
        
        # Check and change the shape of u1 if needed (for pdist).
        if u1.ndim == 1:
            self.u1 = u1
        elif u1.shape[1] == 1:
            self.u1 = u1.reshape(-1)
        else:
            raise ValueError("Check dimensions of 'u1'.")
            
        
        if project == 'meters':
            self.project = 'meters'
            self.epsg_proj = epsg_proj
            
            # Setup the projection.
            p1 = pyproj.Proj(proj='latlong', datum='NAD83')
            p2 = pyproj.Proj(self.epsg_proj)

            self.transformer = pyproj.Transformer.from_proj(p1, p2)
            #transformer_inverse = pyproj.Transformer.from_proj(p2, p1)
            
            xm, ym = self.transformer.transform(x1[:, 0], x1[:, 1])
            
            # If 2d coords.
            if x1.shape[1] == 2:
                self.x1 = np.array([xm, ym]).T
            
            # If 3d coords.
            elif x1.shape[1] == 3:
                zm = x1[:, 2]
                self.x1 = np.array([xm, ym, zm]).T
            
        
        elif project == 'kilometers':
            self.project = 'kilometers'
            self.epsg_proj = epsg_proj
            
            # Setup the projection.
            p1 = pyproj.Proj(proj='latlong', datum='NAD83')
            p2 = pyproj.Proj(self.epsg_proj)

            self.transformer = pyproj.Transformer.from_proj(p1, p2)
            #transformer_inverse = pyproj.Transformer.from_proj(p2, p1)
            
            xm, ym = self.transformer.transform(x1[:, 0], x1[:, 1])
            
            # If 2d coords.
            if x1.shape[1] == 2:
                self.x1 = np.array([xm, ym]).T * 0.001
            
            # If 3d coords.
            elif x1.shape[1] == 3:
                print('Reminder: Input z-coordinate units should be METERS.')
                zm = x1[:, 2]
                self.x1 = np.array([xm, ym, zm]).T * 0.001
            
            
        elif project == None:
            self.project = None
            self.x1 = x1
            self.epsg_proj = epsg_proj
        
        else:
            raise ValueError("Project must be 'meters', 'kilometers', or 'None'.")

        


        if variogram_func == 'gaussian':
            self.variogram_func = gaussian
        elif variogram_func == 'spherical':
            self.variogram_func = spherical
        elif variogram_func == 'linear':
            self.variogram_func = linear
        else:  
            raise ValueError("Variogram function must be 'linear', 'gaussian', or 'spherical'.")


        if cutoff_dist == 'auto':
            self.cutoff_dist = cutoff_dist_func(self.x1)
        else:
            self.cutoff_dist = cutoff_dist


        self.verbose = verbose
        self.show_plots = show_plots
        self.bins = bins
        self.featurization = featurization

            
        # Lags and semivariance.
        dist = pdist(self.x1, metric='euclidean')
        gamma = 0.5 * pdist(self.u1.reshape(-1, 1), metric='sqeuclidean')


        if self.bins != None:  # Use bins.
            # Variogram cloud calculation.
            bin_means, bin_edges, binnumber = binned_statistic(dist, gamma, 
                                                               statistic='mean', 
                                                               bins=self.bins, 
                                                               range=[dist.min(), 
                                                                      self.cutoff_dist])
            bin_width = (bin_edges[1] - bin_edges[0])
            bin_centers = bin_edges[1:] - bin_width / 2

            # Bin counts calculation.
            bin_count, bin_count_edges, bin_count_number = binned_statistic(dist, gamma, 
                                                                            statistic='count', 
                                                                            bins=self.bins)
            bin_count_width = (bin_count_edges[1] - bin_count_edges[0])
            bin_count_centers = bin_count_edges[1:] - bin_count_width/2

        if self.show_plots == True:
            if self.bins == None:

                # Variogram cloud plot.
                plt.figure(dpi=100)
                plt.scatter(dist, gamma, ec='C0', fc='none', alpha=0.3)
                plt.ylabel('$\gamma(h)$')
                plt.xlabel('$h$')
                plt.grid(alpha=0.4)
                plt.show()

            if self.bins != None:
                
                # Variogram cloud plot.
                plt.figure(dpi=100)
                plt.scatter(dist, gamma, ec='C0', fc='none', alpha=0.3)
                plt.hlines(bin_means, bin_edges[:-1], bin_edges[1:], zorder=1, color='k')
                plt.scatter(bin_centers, bin_means, ec='k', lw=0.5)
                plt.ylabel('$\gamma(h)$')
                plt.xlabel('$h$')
                plt.grid(alpha=0.4)
                plt.show()

                # Bin counts plot.
                plt.figure(dpi=100)
                plt.hlines(bin_count, bin_count_edges[:-1], bin_count_edges[1:], zorder=1, color='k')
                plt.scatter(bin_count_centers, bin_count, ec='k', lw=0.5)
                plt.ylabel('Bin count')
                plt.xlabel('$h$')
                plt.grid(alpha=0.4)
                plt.show()

        
        
        ############ Fit the variogram model.
        
        if not variogram_params:  # Fit to the data.
            
            if self.bins == None:  # Fit the variogram cloud.
                
                if self.variogram_func == gaussian or self.variogram_func == spherical:

                    # Initial guess for the parameters.
                    p0 = [0.25 * np.max(dist),            # range
                          np.max(gamma) - np.min(gamma),  # sill
                          np.min(gamma) + 1e-6]           # nugget

                    # Bounds with constraints.
                    bounds = [(1e-6, 1e-6, 1e-6), (np.inf, np.inf, np.inf)]

                    # Apply the cutoff.
                    dist_cut = np.where(dist < self.cutoff_dist, dist, np.nan)
                    gamma_cut = np.where(dist < self.cutoff_dist, gamma, np.nan)
                    
                    # Remove nans for curve_fit.
                    dist_cut = dist_cut[~np.isnan(dist_cut)]
                    gamma_cut = gamma_cut[~np.isnan(gamma_cut)]
                    
                    popt, pcov = curve_fit(self.variogram_func, dist_cut, gamma_cut, p0=p0, bounds=bounds)

                    if self.show_plots == True:
                        
                        # Calculate 2d kde to help confirm cutoff.
                        xi = np.linspace(np.min(dist), np.max(dist), 60)
                        yi = np.linspace(np.min(gamma), np.max(gamma), 60)
                        xi, yi = np.meshgrid(xi, yi)
                        xyi = np.stack([xi.reshape(-1), yi.reshape(-1)], axis=1).T

                        kde = stats.gaussian_kde([dist, gamma])
                        z = kde.evaluate(xyi)
                        z = z.reshape(len(xi), len(yi))

                        plt.figure(dpi=100)
                        plt.pcolormesh(xi, yi, z, cmap=plt.cm.Blues, shading='auto')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()
                        
                        # With cutoff and variogram model.
                        xnew = np.linspace(np.min(dist_cut), self.cutoff_dist, 100)
                        plt.figure(dpi=100)
                        plt.scatter(dist_cut, gamma_cut, fc='none', ec='C1', lw=0.5, alpha=0.3)
                        plt.plot(xnew, self.variogram_func(xnew, *popt), color='k')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()
                        

                    vrange = popt[0]
                    sill = popt[1]
                    nugget = popt[2]


                    if self.verbose == True:
                        print('variogram model: {}'.format(variogram_func))
                        print('cutoff: {:.2f}'.format(self.cutoff_dist))
                        print('range: {:.5f}'.format(vrange))
                        print('sill: {:.5f}'.format(sill))
                        print('nugget: {:.5f}'.format(nugget))
                        print('full sill: {:.5f}'.format(sill + nugget))

                    self.parameter_vals = [vrange, sill, nugget]

                elif self.variogram_func == linear:

                    # Initial guess for the parameters.
                    p0 = [(np.max(dist) - np.min(dist)) / (np.max(gamma) - np.min(gamma)),  # slope
                          np.min(gamma) + 1e-6]                                             # nugget

                    # Bounds with constraints.
                    bounds = [(1e-6, 1e-6), (np.inf, np.inf)]

                    # Apply the cutoff.
                    dist_cut = np.where(dist < self.cutoff_dist, dist, np.nan)
                    gamma_cut = np.where(dist < self.cutoff_dist, gamma, np.nan)
                    
                    # Remove nans for curve_fit.
                    dist_cut = dist_cut[~np.isnan(dist_cut)]
                    gamma_cut = gamma_cut[~np.isnan(gamma_cut)]
                    
                    popt, pcov = curve_fit(self.variogram_func, dist_cut, gamma_cut, p0=p0, bounds=bounds)

                    if self.show_plots == True:

                        # Calculate 2d kde to help confirm cutoff.
                        xi = np.linspace(np.min(dist), np.max(dist), 60)
                        yi = np.linspace(np.min(gamma), np.max(gamma), 60)
                        xi, yi = np.meshgrid(xi, yi)
                        xyi = np.stack([xi.reshape(-1), yi.reshape(-1)], axis=1).T

                        kde = stats.gaussian_kde([dist, gamma])
                        z = kde.evaluate(xyi)
                        z = z.reshape(len(xi), len(yi))

                        plt.figure(dpi=100)
                        plt.pcolormesh(xi, yi, z, cmap=plt.cm.Blues, shading='auto')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()
                        
                        # With cutoff and variogram model.
                        xnew = np.linspace(np.min(dist_cut), self.cutoff_dist, 100)
                        plt.figure(dpi=100)
                        plt.scatter(dist_cut, gamma_cut, fc='none', ec='C1', lw=0.5, alpha=0.3)
                        plt.plot(xnew, self.variogram_func(xnew, *popt), color='k')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()
                        

                    slope = popt[0]
                    nugget = popt[1]

                    if self.verbose == True:
                        print('variogram model: {}'.format(variogram_func))
                        print('cutoff: {:.2f}'.format(self.cutoff_dist))
                        print('slope: {:.5f}'.format(slope))
                        print('nugget: {:.5f}'.format(nugget))


                    self.parameter_vals = [slope, nugget]

            
            
            
            elif self.bins != None:  # Fit the binned data.
                if self.variogram_func == gaussian or self.variogram_func == spherical:

                    # Initial guess for the parameters.
                    p0 = [0.25 * np.max(bin_centers),                 # range
                              np.max(bin_means) - np.min(bin_means),  # sill
                              np.min(bin_means)]                      # nugget

                    # Bounds with constraints.
                    bounds = [(1e-6, 1e-6, 1e-6), (np.inf, np.inf, np.inf)]

                    popt, pcov = curve_fit(self.variogram_func, bin_centers, bin_means, p0=p0, bounds=bounds)

                    if self.show_plots == True:

                        # Fit variogram plot.
                        plt.figure(dpi=100)
                        plt.scatter(bin_centers, bin_means, c='C1', ec='k', lw=0.5)
                        plt.plot(bin_centers, self.variogram_func(bin_centers, *popt), color='k')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()

                    vrange = popt[0]
                    sill = popt[1]
                    nugget = popt[2]


                    if self.verbose == True:
                        print('variogram model: {}'.format(variogram_func))
                        print('cutoff: {:.2f}'.format(self.cutoff_dist))
                        print('range: {:.5f}'.format(vrange))
                        print('sill: {:.5f}'.format(sill))
                        print('nugget: {:.5f}'.format(nugget))
                        print('full sill: {:.5f}'.format(sill + nugget))

                    self.parameter_vals = [vrange, sill, nugget]

                elif self.variogram_func == linear:

                    # Initial guess for the parameters.
                    p0 = [(np.max(bin_centers) - np.min(bin_centers)) / (np.max(bin_means) - np.min(bin_means)),  # slope
                          np.min(bin_means)]                                                                      # nugget

                    # Bounds with constraints.
                    bounds = [(1e-6, 1e-6), (np.inf, np.inf)]

                    popt, pcov = curve_fit(self.variogram_func, bin_centers, bin_means, p0=p0, bounds=bounds)

                    if self.show_plots == True:

                        # Fit variogram plot.
                        plt.figure(dpi=100)
                        plt.scatter(bin_centers, bin_means, c='C1', ec='k', lw=0.5)
                        plt.plot(bin_centers, self.variogram_func(bin_centers, *popt), color='k')
                        plt.ylabel('$\gamma(h)$')
                        plt.xlabel('$h$')
                        plt.grid(alpha=0.4)
                        plt.show()

                    slope = popt[0]
                    nugget = popt[1]

                    if self.verbose == True:
                        print('variogram model: {}'.format(variogram_func))
                        print('cutoff: {:.2f}'.format(self.cutoff_dist))
                        print('slope: {:.5f}'.format(slope))
                        print('nugget: {:.5f}'.format(nugget))


                    self.parameter_vals = [slope, nugget]


        else:  # Use the given variogram parameters.
            self.parameter_vals = variogram_params
            
            # Apply the cutoff.
            dist_cut = np.where(dist < self.cutoff_dist, dist, np.nan)
            gamma_cut = np.where(dist < self.cutoff_dist, gamma, np.nan)

            # Remove nans for curve_fit.
            dist_cut = dist_cut[~np.isnan(dist_cut)]
            gamma_cut = gamma_cut[~np.isnan(gamma_cut)]
            
            
            if self.show_plots == True:

                if self.bins == None:
                    
                    # Calculate 2d kde to help confirm cutoff.
                    xi = np.linspace(np.min(dist), np.max(dist), 60)
                    yi = np.linspace(np.min(gamma), np.max(gamma), 60)
                    xi, yi = np.meshgrid(xi, yi)
                    xyi = np.stack([xi.reshape(-1), yi.reshape(-1)], axis=1).T
                    
                    kde = stats.gaussian_kde([dist, gamma])
                    z = kde.evaluate(xyi)
                    z = z.reshape(len(xi), len(yi))

                    plt.figure(dpi=100)
                    plt.pcolormesh(xi, yi, z, cmap=plt.cm.Blues, shading='auto')
                    plt.ylabel('$\gamma(h)$')
                    plt.xlabel('$h$')
                    plt.grid(alpha=0.4)
                    plt.show()

                    # With cutoff and variogram model.
                    xnew = np.linspace(np.min(dist_cut), self.cutoff_dist, 100)
                    plt.figure(dpi=100)
                    plt.scatter(dist_cut, gamma_cut, fc='none', ec='C1', lw=0.5, alpha=0.3)
                    plt.plot(xnew, self.variogram_func(xnew, *self.parameter_vals), color='k')
                    plt.ylabel('$\gamma(h)$')
                    plt.xlabel('$h$')
                    plt.grid(alpha=0.4)
                    plt.show()
                    
                else:
                    
                    # Fit variogram plot.
                    plt.figure(dpi=100)
                    plt.scatter(bin_centers, bin_means, fc='C1', ec='k', lw=0.5)
                    plt.plot(bin_centers, self.variogram_func(bin_centers, *self.parameter_vals), color='k')
                    plt.ylabel('$\gamma(h)$')
                    plt.xlabel('$h$')
                    plt.grid(alpha=0.4)
                    plt.show()

            if self.variogram_func == gaussian or self.variogram_func == spherical:
                vrange = self.parameter_vals[0]
                sill = self.parameter_vals[1]
                nugget = self.parameter_vals[2]

                if self.verbose == True:
                    print('variogram model: {}'.format(variogram_func))
                    print('cutoff: {:.2f}'.format(self.cutoff_dist))
                    print('range: {:.5f}'.format(vrange))
                    print('sill: {:.5f}'.format(sill))
                    print('nugget: {:.5f}'.format(nugget))
                    print('full sill: {:.5f}'.format(sill + nugget))

            elif self.variogram_func == linear:
                slope = self.parameter_vals[0]
                nugget = self.parameter_vals[1]

                if self.verbose == True:
                    print('variogram model: {}'.format(variogram_func))
                    print('cutoff: {:.2f}'.format(self.cutoff_dist))
                    print('slope: {:.5f}'.format(slope))
                    print('nugget: {:.5f}'.format(nugget))
 
            
        
    
####################################################

    def predict(self, x2_pred):
        
        '''
        Parameters:
                x2 : n-dim array
                    Locations to make kriging predictions.
        
        Returns:
                u2_mean : float
                    Kriging mean.
                    
                u2_var : float
                    Kriging variance.
        
        
        Performs ordinary or universal kriging using the estimated variogram parameters.
        
        '''
        
        
        # Check for 2d or 3d data.        
        # 2d
        if self.x1.shape[1] == 2:
            
            if self.project == 'meters':
                xm, ym = self.transformer.transform(x2_pred[:, 0], x2_pred[:, 1])
                self.x2 = np.array([xm, ym]).T

            elif self.project == 'kilometers':
                xm, ym = self.transformer.transform(x2_pred[:, 0], x2_pred[:, 1])
                self.x2 = np.array([xm, ym]).T * 0.001
                
            elif self.project == None:
                self.x2 = x2_pred
                
        # 3d    
        elif self.x1.shape[1] == 3:
            
            if self.project == 'meters':
                print('Reminder: Ensure depth coordinate units are meters.')
                xm, ym = self.transformer.transform(x2_pred[:, 0], x2_pred[:, 1])
                zm = x2_pred[:, 2]
                self.x2 = np.array([xm, ym, zm]).T

            elif self.project == 'kilometers':
                print('Reminder: Ensure depth coordinate units are kilometers.')
                xm, ym = self.transformer.transform(x2_pred[:, 0], x2_pred[:, 1])
                xkm = xm * 0.001
                ykm = ym * 0.001
                zkm = x2_pred[:, 2]
                self.x2 = np.array([xkm, ykm, zkm]).T 

            elif self.project == None:
                self.x2 = x2_pred                
            
        
        
        n1 = len(self.x1)
        n2 = len(self.x2)
        
        # Universal krige.
        if self.featurization:            
            # Ax = b with a trend.
            
            # Build A
            D1 = cdist(self.x1, self.x1)

            drift_data = np.array(list(self.featurization(self.x1)))

            An = n1 + 1 + drift_data.shape[0]

            A = np.zeros((An, An))
            A[:n1, :n1] = -self.variogram_func(D1, *self.parameter_vals)
            np.fill_diagonal(A, 0.)
            A[n1, :n1] = 1.
            A[:n1, n1] = 1.
            A[n1, n1] = 0.

            # Add in the trend for A.
            for i in range(drift_data.shape[0]):
                A[n1+i+1, :n1] = drift_data[i]
                A[:n1, n1+i+1] = drift_data[i]

            # Build b.
            D2 = cdist(self.x2, self.x1)
            b = np.zeros((D2.shape[0], D2.shape[1] + 1 + drift_data.shape[0]))
            b[:n2, :n1] = -self.variogram_func(D2, *self.parameter_vals)
            b = b.T
            b[n1, :] = 1.

            # Add the trend for b.
            drift_pred = np.array(list(self.featurization(self.x2)))

            for i in range(drift_pred.shape[0]):
                b[n1+1+i, :] = drift_pred[i]

            # Solve.
            x = np.linalg.solve(A, b)

            u2_mean = np.tensordot(self.u1, x[:n1], axes=1)
            u2_var = np.sum(x.T * -b.T, axis=1)

            return u2_mean, u2_var

        
        # Ordinary krige.
        else:
            # Ax = b.

            # Build A.
            D1 = cdist(self.x1, self.x1)
            A = np.zeros((n1+1, n1+1))
            A[:n1, :n1] = -self.variogram_func(D1, *self.parameter_vals)
            np.fill_diagonal(A, 0.)
            A[n1, :] = 1.
            A[:, n1] = 1.
            A[n1, n1] = 0.

            # Build b.
            D2 = cdist(self.x2, self.x1)
            b = np.zeros((D2.shape[0], D2.shape[1]+1))
            b[:n2, :n1] = -self.variogram_func(D2, *self.parameter_vals)
            b = b.T
            b[n1, :] = 1.

            # Solve.
            x = np.linalg.solve(A, b)

            u2_mean = np.tensordot(self.u1, x[:n1], axes=1)
            u2_var = np.sum(x.T * -b.T, axis=1)

            return u2_mean, u2_var
        
        
        
#########################################################################
    
    def convex_hull_grid(self, 
                         spacing, 
                         lon, 
                         lat, 
                         z=None
                        ):

        '''
        This function replaces manual workflows in gis using
        the minimum bounding geometry tool to make a custom
        extent/bounds on a spatial dataset. It also adds on
        a depth series (3d) and projects if desired.


        Parameters:
                spacing : int
                    The spacing of the grid locations produced. The bigger
                    the number, the closer the spacing and the denser
                    the dataset created.

                lon : The longitude coordinate of the input (x1) data to
                    be encompassed. 

                lat : The latitude coordinate of the input (x1) data to
                    be encompassed. 

                z : array, opt
                    The depths to make a depth series at each xy coordinate.
                    Example:  z = np.arange(-100, -5, 10) would make a depth
                    series at each xy coordinate from -100 to -5 by 10.
                    Default is None.
                    
        Returns:
                x2 :  pandas dataframe
                    Locations to make kriging predictions.


        '''

        # Make df then geodf of input data.
        df = pd.DataFrame()
        df['lon'] = lon
        df['lat'] = lat
        df['geometry'] = df.apply(lambda row: Point(row.lon, row.lat), axis=1)
        df_shp  = gpd.GeoDataFrame(df).set_crs('EPSG:4269')

        # Make square grid.
        loni = np.linspace(np.min(lon), np.max(lon), spacing)
        lati = np.linspace(np.min(lat), np.max(lat), spacing)
        gridlon, gridlat = np.meshgrid(loni, lati)

        # Grid df then geodf.
        df_grid = pd.DataFrame()
        df_grid['gridlon'] = gridlon.flatten()
        df_grid['gridlat'] = gridlat.flatten()
        df_grid['geometry'] = df_grid.apply(lambda row: Point(row.gridlon, row.gridlat), axis=1) 
        df_grid  = gpd.GeoDataFrame(df_grid).set_crs('EPSG:4269')

        # Clip.
        hull = df_shp.unary_union.convex_hull
        clipped = gpd.clip(df_grid, hull)

        # Lon/lat.
        x2_array = np.array([clipped.gridlon.to_numpy(), clipped.gridlat.to_numpy()]).T


        # If no z then make df and return.
        if z is None:
            if self.project == 'meters' or self.project == 'kilometers':
                x2 = pd.DataFrame()
                x2['lon'] = x2_array[:, 0]
                x2['lat'] = x2_array[:, 1]
                x2['xm'], x2['ym'] = self.transformer.transform(x2_array[:, 0], x2_array[:, 1])
                x2['xkm'] = x2['xm'] * 0.001
                x2['ykm'] = x2['ym'] * 0.001
                return x2

            elif self.project == None:
                x2 = pd.DataFrame()
                x2['lon'] = x2_array[:, 0]
                x2['lat'] = x2_array[:, 1]
                return x2

        # Make the depth series at each xy coord.
        else:
            points = []

            for xi, yi in zip(x2_array[:, 0], x2_array[:, 1]):
                for zi in z:
                    d = {'lon':[xi], 'lat':[yi], 'z':[zi]}
                    point = pd.DataFrame(d)            
                    points.append(point)

            x2 = pd.concat(points)

            if self.project == 'meters' or self.project == 'kilometers':
                x2['xm'], x2['ym'] = self.transformer.transform(x2['lon'].to_numpy(), x2['lat'].to_numpy())
                x2['xkm'] = x2['xm'] * 0.001
                x2['ykm'] = x2['ym'] * 0.001
                x2 = x2.rename(columns={'z':'zm'})
                x2['zkm'] = x2['zm'] * 0.001

            return x2


    #########################################################
    # Access the projected coords of the input data.
    def get_projected(self):
        return self.x1[:, 0], self.x1[:, 1]
    
    #########################################################   
    # Provide some stats on the model.
    def stats(self):         

        u2_mean_for_u1, u2_var_for_u1 = self.predict(self.x1_original)
        
        self.residuals = self.u1 - u2_mean_for_u1     
        
        self.res_mean = np.mean(self.residuals)
        self.res_std = np.std(self.residuals)
        self.res_skew = stats.skew(self.residuals)
        self.res_kurt = stats.kurtosis(self.residuals)
            
        if self.show_plots == True:

            plt.figure()
            plt.hist(self.u1, label='Input values')
            plt.ylabel('Count')
            plt.xlabel('Input values')
            plt.show()

            plt.figure()
            plt.hist(self.residuals, label='Residuals')
            plt.ylabel('Count')
            plt.xlabel('Residuals')
            plt.show()

        if self.verbose == True:
            print('Residual mean: {:.3e}'.format(self.res_mean))
            print('Residual standard deviation: {:.3f}'.format(self.res_std))
            print('Residual skewness: {:.3f}'.format(self.res_skew))
            print('Residual kurtosis: {:.3f}'.format(self.res_kurt))

    def get_residuals(self):
        return self.residuals

    def get_residual_moments(self):
        return dict(mean=self.res_mean, 
                    std=self.res_std, 
                    skew=self.res_skew, 
                    kurt=self.res_kurt)
