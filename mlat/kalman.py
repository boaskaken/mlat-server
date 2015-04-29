# -*- mode: python; indent-tabs-mode: nil -*-

import math
import numpy
import pykalman.unscented
import functools
import logging

import mlat.geodesy
import mlat.constants

glogger = logging.getLogger("kalman")


class KalmanState(object):
    """Kalman filter state for a single aircraft.

    Should be subclassed to provide implementations of
    set_initial_state(), transition_function(),
    transition_covariance().

    The state matrix is assumed to have position/velocity
    as the first 6 components.
    """

    def __init__(self, ac):
        self.ac = ac
        self._reset()

    def _reset(self):
        # the filter itself:
        self._mean = None
        self._cov = None
        self._acquiring = True
        self._outliers = 0
        self.last_update = None

        # does the filter have useful derived data?
        self.valid = False

        # most recent values derived from filter state
        self.position = None        # ECEF
        self.velocity = None        # ECEF
        self.position_error = None  # meters
        self.velocity_error = None  # m/s

        # .. some derived values in more useful reference frames
        self.position_llh = None    # LLH
        self.velocity_enu = None    # ENU
        self.heading = None         # degrees
        self.ground_speed = None    # m/s
        self.vertical_speed = None  # m/s

    def observation_function(self, state, *, positions):
        """Kalman filter observation function.

        Given state (position,...) and a list of N receiver positions,
        return N-1 pseudorange observations (each relative to the first receiver's
        pseudorange) and an altitude."""

        x, y, z = state[0:3]

        n = len(positions)
        obs = numpy.zeros(n)

        _, _, obs[0] = mlat.geodesy.ecef2llh((x, y, z))

        rx, ry, rz = positions[0]
        zero_range = ((rx - x)**2 + (ry - y)**2 + (rz - z)**2)**0.5

        for i in range(1, n):
            rx, ry, rz = positions[i]
            obs[i] = ((rx - x)**2 + (ry - y)**2 + (rz - z)**2)**0.5 - zero_range

        return obs

    def _update_derived(self):
        """Update derived values from self._mean and self._cov"""

        if self._mean is None or self._acquiring:
            self.valid = False
            return

        self.position = self._mean[0:3]
        self.velocity = self._mean[3:6]

        pe = numpy.trace(self._cov[0:3, 0:3])
        self.position_error = 0 if pe < 0 else math.sqrt(pe)
        ve = numpy.trace(self._cov[3:6, 3:6])
        self.velocity_error = 0 if ve < 0 else math.sqrt(ve)

        lat, lon, alt = self.position_llh = mlat.geodesy.ecef2llh(self.position)

        # rotate velocity into the local tangent plane
        lat_r = lat * mlat.constants.DTOR
        lon_r = lon * mlat.constants.DTOR
        C = numpy.array([[-math.sin(lon_r), math.cos(lon_r), 0],
                         [math.sin(-lat_r) * math.cos(lon_r), math.sin(-lat_r) * math.sin(lon_r), math.cos(-lat_r)],
                         [math.cos(-lat_r) * math.cos(lon_r), math.cos(-lat_r) * math.sin(lon_r), -math.sin(-lat_r)]])
        east, north, up = numpy.dot(C, self.velocity.T).T

        # extract speeds, headings
        self.heading = math.atan2(east, north) * 180.0 / math.pi
        if self.heading < 0:
            self.heading += 360
        self.ground_speed = math.sqrt(north**2 + east**2)
        self.vertical_speed = up

        self.valid = True

    def update(self, position_time, measurements, altitude, leastsquares_position, leastsquares_cov, distinct):
        """Update the filter given a new set of observations.

        position_time:         the time of these measurements, UTC seconds
        measurements:          a list of (receiver, timestamp, variance) tuples
        altitude:              reported altitude in meters
        leastsquares_position: the ECEF position computed by the least-squares solver
        leastsquares_cov:      the covariance of leastsquares_position
        distinct:              the number of distinct receivers (<= len(measurements))
        """

        if self.last_update is not None and (position_time - self.last_update > 60.0):
            glogger.info("{ac.icao:06X} tracking timed out".format(ac=self.ac))
            self._reset()

        if self._mean is None:
            # try to acquire an initial position
            if distinct >= 4:
                # accept this
                self.last_update = position_time
                self.set_initial_state(leastsquares_position, leastsquares_cov)
                return
            else:
                # nope.
                return

        if self._acquiring and distinct < 4:
            # don't trust 3 station results until we have converged
            return

        # update filter
        zero_pr = measurements[0][1] * mlat.constants.Cair
        positions = [measurements[0][0].position]

        n = len(measurements)
        obs = numpy.zeros(n)
        obs_var = numpy.zeros(n)

        obs[0] = altitude
        obs_var[0] = 50**2

        for i in range(1, n):
            receiver, timestamp, variance = measurements[i]
            positions.append(receiver.position)
            obs[i] = timestamp * mlat.constants.Cair - zero_pr
            obs_var[i] = (variance + measurements[0][2]) * mlat.constants.Cair**2

        obs_covar = numpy.diag(obs_var)

        dt = position_time - self.last_update

        try:
            trans_covar = self.transition_covariance(dt)
            transition_function = functools.partial(self.transition_function, dt=dt)
            observation_function = functools.partial(self.observation_function, positions=positions)

            #
            # This is extracted from pykalman's AdditiveUnscentedFilter.filter_update()
            # because we want to access the intermediate (prediction) result to decide
            # whether to accept this observation or not.
            #

            # make sigma points
            moments_state = pykalman.unscented.Moments(self._mean, self._cov)
            points_state = pykalman.unscented.moments2points(moments_state)

            # Predict.
            (_, moments_pred) = (
                pykalman.unscented.unscented_filter_predict(
                    transition_function=transition_function,
                    points_state=points_state,
                    sigma_transition=trans_covar
                )
            )
            points_pred = pykalman.unscented.moments2points(moments_pred)

            # Decide whether this is an outlier:
            # Get the predicted filter state mean and covariance
            # as an observation:
            (obs_points_pred, obs_moments_pred) = (
                pykalman.unscented.unscented_transform(
                    points_pred, observation_function,
                    sigma_noise=obs_covar
                )
            )

            # Find the Mahalanobis distance between the predicted observation
            # and our new observation, using the predicted observation's
            # covariance as our expected distribution.
            innovation = obs - obs_moments_pred.mean
            vi = numpy.linalg.inv(obs_moments_pred.covariance)
            mdsq = numpy.dot(numpy.dot(innovation.T, vi), innovation)

            # If the Mahalanobis distance is very large this observation is an outlier
            if mdsq > 400:  # 20 std devs
                glogger.info("{ac.icao:06X} skip innov={innovation} mdsq={mdsq}".format(
                    ac=self.ac,
                    innovation=innovation,
                    mdsq=mdsq))

                self._outliers += 1
                if self._outliers < 3 or (position_time - self.last_update) < 15.0:
                    # don't use this one
                    return
                glogger.info("{ac.icao:06X} reset due to outliers.".format(ac=self.ac))
                self._reset()
                return

            self._outliers = 0

            # correct filter state using the current observation
            (self._mean, self._cov) = (
                pykalman.unscented.unscented_filter_correct(
                    observation_function=observation_function,
                    moments_pred=moments_pred,
                    points_pred=points_pred,
                    observation=obs,
                    sigma_observation=obs_covar
                )
            )

            # converged enough to start reporting?
            if self._acquiring and numpy.trace(self._cov) < 9e6:
                glogger.info("{ac.icao:06X} acquired.".format(ac=self.ac))
                self._acquiring = False

            self.last_update = position_time
            self._update_derived()

        except Exception:
            glogger.exception("Kalman filter update failed. " +
                              "dt={dt} obs={obs} obs_covar={obs_covar} mean={mean} covar={covar}".format(
                                  dt=dt,
                                  obs=obs,
                                  obs_covar=obs_covar,
                                  mean=self._mean,
                                  covar=self._cov))
            self._reset()
            return

    def set_initial_state(self, leastsquares_position, leastsquares_cov):
        """Set the initial state of the filter from a least-squares result.

        Should set self._mean and self._cov.
        """

        raise NotImplementedError()

    def transition_function(self, state, *, dt):
        """Kalman filter transition function.

        Given the current state and a timestep, return the
        next predicted state."""

        raise NotImplementedError()

    def transition_covariance(self, dt):
        """Kalman filter transition covariance.

        Given a timestep, return the covariance of the
        process noise."""

        raise NotImplementedError()


class KalmanStateCV(KalmanState):
    """Kalman filter with a constant-velocity model."""

    accel_noise = 0.5   # m/s^2

    def set_initial_state(self, leastsquares_position, leastsquares_cov):
        """State is: (position, velocity)"""

        self._mean = numpy.array(list(leastsquares_position) + [0, 0, 0])
        self._cov = numpy.zeros((6, 6))
        self._cov[0:3, 0:3] = leastsquares_cov
        self._cov[3, 3] = self._cov[4, 4] = self._cov[5, 5] = 200**2

    def transition_function(self, state, *, dt):
        x, y, z, vx, vy, vz = state
        return numpy.array([x + vx*dt, y + vy*dt, z + vz*dt, vx, vy, vz])

    def transition_covariance(self, dt):
        trans_covar = numpy.zeros((6, 6))
        trans_covar[0, 0] = trans_covar[1, 1] = trans_covar[2, 2] = 0.25*dt**4
        trans_covar[3, 3] = trans_covar[4, 4] = trans_covar[5, 5] = dt**2
        trans_covar[0, 3] = trans_covar[3, 0] = 0.5*dt**3
        trans_covar[1, 4] = trans_covar[4, 1] = 0.5*dt**3
        trans_covar[2, 5] = trans_covar[5, 2] = 0.5*dt**3

        # we assume that accel_noise is white noise (uncorrelated) and so
        # scale by dt not dt**2 here
        return trans_covar * self.accel_noise**2 * dt


class KalmanStateCA(KalmanState):
    """Kalman filter with a constant-acceleration model."""

    accel_noise = 0.05   # m/s^2

    def set_initial_state(self, leastsquares_position, leastsquares_cov):
        """State is: (position, velocity, acceleration)"""

        self._mean = numpy.array(list(leastsquares_position) + [0, 0, 0, 0, 0, 0])
        self._cov = numpy.zeros((9, 9))
        self._cov[0:3, 0:3] = leastsquares_cov
        self._cov[3, 3] = self._cov[4, 4] = self._cov[5, 5] = 200**2
        self._cov[6, 6] = self._cov[7, 7] = self._cov[8, 8] = 1

    def transition_function(self, state, *, dt):
        x, y, z, vx, vy, vz, ax, ay, az = state
        return numpy.array([x + vx*dt + 0.5*ax*dt**2,
                            y + vy*dt + 0.5*ay*dt**2,
                            z + vz*dt + 0.5*az*dt**2,
                            vx + ax*dt,
                            vy + ay*dt,
                            vz + az*dt,
                            ax,
                            ay,
                            az])

    def transition_covariance(self, dt):
        trans_covar = numpy.zeros((9, 9))
        trans_covar[0, 0] = trans_covar[1, 1] = trans_covar[2, 2] = 0.25*dt**4
        trans_covar[3, 3] = trans_covar[4, 4] = trans_covar[5, 5] = dt**2
        trans_covar[6, 6] = trans_covar[7, 7] = trans_covar[8, 8] = 1.0

        trans_covar[0, 3] = trans_covar[3, 0] = 0.5*dt**3
        trans_covar[1, 4] = trans_covar[4, 1] = 0.5*dt**3
        trans_covar[2, 5] = trans_covar[5, 2] = 0.5*dt**3

        trans_covar[0, 6] = trans_covar[6, 0] = 0.5*dt**2
        trans_covar[1, 7] = trans_covar[7, 1] = 0.5*dt**2
        trans_covar[2, 8] = trans_covar[8, 2] = 0.5*dt**2

        trans_covar[3, 6] = trans_covar[6, 3] = dt
        trans_covar[4, 7] = trans_covar[7, 4] = dt
        trans_covar[5, 8] = trans_covar[8, 5] = dt

        # we assume that accel_noise is white noise (uncorrelated) and so
        # scale by dt not dt**2 here
        return trans_covar * self.accel_noise**2 * dt