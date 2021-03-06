#!/usr/bin/env python
#-*- coding:utf8 -*-
"""
Scripts for measuring dispersion curves with two-station method

Procedures:
===========
    1. mft (multiple filter technique)
    2. linear-phase FIR bandpass digital filter (Kaiser window)
    3. 3-spline interpolation to transform the cross-correlation amplitude
       image to a phase velocity image.
    4. image analysis technique
"""
from . import pserrors
from .global_var import logger
from .distaz import DistAz
from .pscrosscorr import FTAN
from .psconfig import (PERIOD_RESAMPLE, FTAN_ALPHA, EQWAVEFORM_DIR)

import math
import os

import obspy
from obspy import UTCDateTime
from obspy.geodetics.base import gps2dist_azimuth
from obspy.signal.cross_correlation import correlate
import numpy as np
from scipy.signal import convolve as sig_convolve
from scipy.signal import firwin
from scipy import interpolate
import matplotlib.pyplot as plt
from scipy.signal import argrelextrema



class Tscombine(object):
    """
    Class holds imformation of matched stations and earthquakes
    which contains:
    - pairs of stations
    - pairs of waveforms
    """

    def __init__(self, sta1, sta2, event):
        # import fundamental information
        self.event = event
        self.sta1 = sta1
        self.sta2 = sta2
        # import waveform
        self.get_merged_traces()
        # calculate dist
        self.dist = self.sta1.dist(self.sta2)
        self.id = '{0}.{1}-{2}.{3}-{4}'.format(self.sta1.network, self.sta1.name,
                                               self.sta2.network, self.sta2.name,
                                               self.event['origin'].strftime(
                                               "%Y%m%d%H%M%S"))
        # debug
        logger.debug("Dist -> {}".format(self.dist))
        logger.debug("Traces -> {}-{}".format(self.tr1.id, self.tr2.id))
        logger.debug("CombinationId -> {}".format(self.id))

    def __repr__(self):
        """
        e.g. <BL.10.NUPB <-> BL.00.NUPB <-> 2014050112013>
        """
        return '<Combination {0}.{1} <-> {2}.{3} <-> {4}>'.format(
            self.sta1.network, self.sta1.name, self.sta2.network,
            self.sta2.name, self.event['origin'].strftime("%Y%m%d%H%M%S"))

    def get_merged_traces(self, sacdir=EQWAVEFORM_DIR):
        """
        Attach traces to objects and check if they are correctly removing
        instrument response

        :type sacdir: str or path-like object
        :parm sacdir: contains SAC files trimmed from continuous waveform
        """
        subdir = self.event['origin'].strftime("%Y%m%d%H%M%S")

        prefix = self.event['origin'].strftime("%Y.%j.%H.%M.%S")
        filename1 = ".".join([prefix, "0000", self.sta1.network, self.sta1.name,
                              "*", self.sta1.channel, "M", "SAC"])
        filename2 = ".".join([prefix, "0000", self.sta2.network, self.sta2.name,
                              "*", self.sta2.channel, "M", "SAC"])
        filepath = os.path.join(sacdir, subdir, filename1)
        st1 = obspy.read(filepath, format="SAC")

        filepath = os.path.join(sacdir, subdir, filename1)
        st2 = obspy.read(filepath, format="SAC")

        tr1 = st1.merge()[0]
        tr2 = st2.merge()[0]

        if tr1.data.dtype != "float32":
            raise pserrors.TracesNotCorrected(
                "{} without response removal".format(tr1.id))
        if tr2.data.dtype != "float32":
            raise pserrors.TracesNotCorrected(
                "{} without response removal".format(tr2.id))
        self.tr1 = tr1
        self.tr2 = tr2
        # checkout sampling rate
        if self.tr1.stats.sampling_rate != self.tr2.stats.sampling_rate:
            raise pserrors.CannotImportData('sampling rates are different')

    def measure_dispersion(self, periods, alpha, shift_len):
        """
        estimate raw group velocity dispersion curves with FTAN and
        cross-correlation technique

        1. apply FTAN method on each traces and return amplitude time series of
           analytical signal
        2. estimate inter-station group velocity  of various periods via
           cross-correlation of analytical signal
        """
        logger.info("Processing {}".format(self.id))
        # initialization amplitude/phase matrix: each column = amplitude
        # function of time for a given Faussian filter centered around a period
        self.dispersion = np.zeros(len(periods))
        tr1 = self.tr1
        tr2 = self.tr2

        # measure dispersion curves of group velocity
        reftime1, group_arr1 = measure_group_arrival(tr1, periods, alpha, debug=True)
        reftime2, group_arr2 = measure_group_arrival(tr2, periods, alpha)
        if reftime1 != reftime2:
            logger.error("ReferenceTimeNotMatch")
            return None
        if not group_arr1 or not group_arr2:
            logger.error("Defeatly obtain arrival of group wave")
            return None
        reftime = reftime1

        # apply isolation in time domain
        isotr1 = movement_windows_construction(tr1, periods, group_arr1,
                                               reftime, debug=True)
        isotr2 = movement_windows_construction(tr2, periods, group_arr2,
                                               reftime)

        logger.debug("Isotr1.{} - Isotr2.{}".format(len(isotr1), len(isotr2)))

        # filter traces separately based on kaiser windowed FIR filter
        filttr1 = kaiser_windows_filter(tr1, isotr1, periods, debug=True)
        filttr2 = kaiser_windows_filter(tr2, isotr2, periods)

        # TODO:attach spectral SNR here
        # apply cross-correlation technique and transfer period-delay
        # cross-correlatione functioni matrix to period-velocity matrix
        veloscale, vmatrix = intersta_t_v_construct(tr1, tr2, filttr1, filttr2)
        self.veloscale = veloscale
        self.vmatrix = vmatrix

        # extrct the dispersion curves
        if not self.vmatrix:
            logger.debug("Rrror in period_velocity matrix construction")
            self.dispersion = None
            return

        self.extraction_of_phasevelo(periods)

    def extraction_of_phasevelo(self, periods):
        """
        Extract dispersion curves of this stations and event pair
        """
        # obtain reference point
        refpoint = self.plot_vmatrix(periods)
        if not refpoint:
            raise pserrors.CannotMeasureDispersion("Bad data")
        # extract dispersion phase velocity at various periods
        leftpoint, rightpoint = self.local_maximum_tracking(refpoint=refpoint,
                                                            periods=periods)
        while (rightpoint):
            _, rightpoint = self.local_maximum_tracking(refpoint=rightpoint,
                                                        periods=periods)
        while (leftpoint):
            _leftpoint, _ = self.local_maximum_tracking(refpoint=leftpoint,
                                                        periods=periods)
        if not self.dispersion:
            raise pserrors.CannotMeasureDispersion(
                            "Measure dispersion errorly")
        # recheck dispersion curve
        self.plot_vmatrix(periods, dispersion=True)

    # plot vmatrix and obtain search starting point
    def local_maximum_tracking(self, refpoint, periods):
        """
        Find out the local maximum
        """
        iperiod, ivelo = get_point_index(refpoint, self.veloscale, periods)
        # find out local maximum and minimum
        xcorr = self.vmatrix[iperiod, :]
        local_maximums = argrelextrema(xcorr, np.greater)[0]
        local_minimums = argrelextrema(xcorr, np.greater)[0]

        # combinae extremes
        extremums = np.concatenate([local_minimums, local_maximums])
        extremums_left = extremums[(extremums - ivelo) <= 0]
        extremums_right = extremums[(extremums - ivelo) >= 0]

        # calculate relative maximum and minimum location
        shift_length = min(len(extremums_left), len(extremums_right))
        for shift in np.arange(shift_length):
            # separate left extremums and right ones
            left_extremum = extremums_left[-(shift + 1)]
            right_extremum = extremums_right[shift]

           # constrain the maximum and minimum must across approximate
            # half-wave length
            shift_time = shift * 1.0 / self.tr1.sampling_rate
            if float(shift_time) / float(periods[iperiod]) > 0.4:
                phasevelo = max(xcorr[left_extremum], xcorr[right_extremum])
                break
            else:
                continue

        self.dispersion[iperiod] = phasevelo
        logger.debug("Calculate Dispersion Suc.!")

        leftpoint = rightpoint = ()
        if iperiod != 0:
            leftpoint = (iperiod - 1, ivelo)
        if iperiod != len(periods):
            rightpoint = (iperiod + 1, ivelo)
        return leftpoint, rightpoint

    def plot_vmatrix(self, periods, dispersion=None):
        """
        Use matplotlib to obtain value of mouse-clicked point
        """
        logger.info("If click point (period < 20), disgard this extraction!")

        if not self.dispersion:
            plt.imshow(self.vmatrix)
            refpoint = plt.ginput(1)[0]
            if refpoint[0] < 20:
                logger.info("Period < 20 -> disgard this extraction!")
                return None
            return refpoint
        else:
            plt.imshow(self.vmatrix)
            plt.plot(periods, self.dispersion)
            refpoint = plt.ginput(1)[0]
            if refpoint[0] < 20:
                logger.info("Period < 20 -> disgard this extraction!")
                self.dispersion = None

def get_point_index(point, veloscale, periods):
    """
    Obtain point index
    """
    period, velo = point
    iperiod = (periods == period).argmax()
    ivelo = (veloscale == velo).argmax()
    return (iperiod, ivelo)

def get_point_coord(veloscale, periods, vmatrix, index=None, point=None):
    """
    Obtain point coord and value
    """
    if not point and index:
        iperiod, ivelo = index
        period = periods[iperiod]
        velo = veloscale[ivelo]
        value = vmatrix[iperiod, ivelo]
        coords = (period, velo, value)
    elif not index and point:
        index = get_point_index(point=point, veloscale=veloscale,
                                periods=periods)
        coords = get_point_coord(veloscale=veloscale, periods=periods,
                                 vmatrix=vmatrix, index=index)
    else:
        logger.error("No index or point")
        coords = ()
    return coords

def intersta_t_v_construct(tr1, tr2, filttr1, filttr2, periods, rmaxv=7, rminv=2,
                           deltav=0.01, shift_len=500):
    """
    Construct inter-station period-velocity matrix

    :type tr1: class:`obspy.Trace`
    :param tr1: first trace data corresponding to this station pair
    :type tr2: class:`obspy.Trace`
    :param tr2: second trace data corresponding to this station pair
    :type filttr1: `numpy.array`
    :param filttr1: isolated, normalized and filtered trace data
    :type filttr2: `numpy.array`
    :param filttr2: isolated, normalized and filtered trace data
    """
    if tr1.stats.sampling_rate != tr2.stats.sampling_rate:
        logger.error("Sampling rate of traces are different!")
        return None

    # calculate delay timescale
    npoints = shift_len * tr1.stats.samplinmg_rate
    timescale = shift_len * np.arange(-npoints, npoints + 1) / float(npoints)

    # calculate inter-station distance
    dist, _, _ = gps2dist_azimuth(tr1.stats.sac.stla, tr1.stats.sac.stlo,
                                  tr2.stats.sac.stla, tr2.stats.sca.stlo)
    dist /= 1000.0  # transfer meter into kilometer
    veloscale = dist / timescale

    # interested velocity scale
    intersveloscale = np.arange(rminv, rmaxv, deltav)

    vmatrix = np.zeros(shape=(len(periods), len(intersveloscale)))
    for iperiod, T0 in enumerate(periods):
        correlation = correlate(filttr1[iperiod, :], filttr2[iperiod, :],
                                shift=npoints)

        # transfer period-shiftlen matrix into period-velocity matrix
        maskarray = (veloscale < rmaxv) * (veloscale > rminv)
        splvector = correlation[maskarray]
        # normalize single cross-correlation functions
        splvector /= splvector.max()
        # interpolate cross-correlation function with cubic-spline method
        tck = interpolate.splrep(splvector, veloscale[maskarray], s=0)
        vmatrix[iperiod, :] = interpolate.splev(intersveloscale, tck, der=0)
    return intersveloscale, vmatrix


def movement_windows_construction(tr, periods, group_arr, reftime, halfwidth=3,
                                  debug=False):
    """
    Apply movement windows based on eauqtion described in Huajian Yao, 2004
    # movement window construction
    #             | 1               # tgi(Tc) - nTc < t < tgi(Tc) + nTc
    # w(t, Tc) =  | cos(pi * {abs(t-tgi(Tc))-nTc} / Tc)
    #             |                 # -Tc/2 < |t - tgi(Tc)|-nT<-Tc/2
    #             | 0               # else

    :type tr: class `obspy.Trace`
    :param tr: contains teleseismic waveform
    :type periods: numpy array
    :param periods: we measure shear wave velocity at these periods
    :type group_arr: list of `obspy.UTCDateTime`
    :parm group_arr: contain arrivals of group velocity at various period bands
    :type reftime: `obspy.UTCDateTime`
    :param reftime: reference time of SAC records, it should be origin time
    :type halfwidth: int
    :param halfwidth: width of movement windows
    """

    w = np.zeros(shape=(len(periods), len(tr.data)))
    isotr = np.zeros(shape=(len(periods), len(tr.data)))
    timescale = [tr.stats.starttime + x / tr.stats.sampling_rate
                 for x in range(tr.stats.npts)]
    timescale = np.array(timescale)
    for iperiod, T0 in enumerate(periods):
        #  mask array construction
        Tturna = group_arr[iperiod] - halfwidth * T0
        Tturnb = group_arr[iperiod] + halfwidth * T0
        Tjumpa = group_arr[iperiod] - halfwidth * T0 - T0 / 2
        Tjumpb = group_arr[iperiod] + halfwidth * T0 + T0 / 2
        w[iperiod, :][(timescale <= Tturnb)*(timescale >= Tturna)] = 1
        for index, time in enumerate(timescale):
            if (time < Tturna) and (time > Tjumpa):
                w[iperiod, index] = np.cos(np.pi *
                                           (np.abs(time - group_arr[iperiod])
                                            - halfwidth * T0) / T0
                                           )

            if (time < Tjumpb) and (time > Tturnb):
                w[iperiod, index] = np.cos(np.pi *
                                           (np.abs(time - group_arr[iperiod])
                                            - halfwidth * T0) / T0
                                           )
        isotr[iperiod, :] = tr.data * w[iperiod, :]
    if debug:
        period = 80
        logger.debug("period -> {}".format(period))
        plot_isolation(tr.data, isotr, w, timescale, periods, period)
    return isotr

def plot_isolation(raw, isotr, w, timescale, periods, period):
    """
    Plot comparation of raw data and isolated data

    :type raw: numpy matrix
    :param raw: raw trace data without isolation
    :type iso: numpy array
    :param iso: isolated trace data without isolation
    :type w: numpy matrix
    :param w: time window for isolation
    :type timescale: numpy array of obspy UTCDateTime class
    :param timescale: indicate time scale
    :type period: float or int
    :param period: period for visualization
    """
    for iperiod, T0 in enumerate(periods):
        if T0 == period:
            iso = isotr[iperiod, :]
            weight = w[iperiod, :]


    relativetime = timescale - timescale[0]
    f, ax1 = plt.subplots()
    ax1.plot(relativetime, raw, 'b-', linewidth=2, label="Raw")
    ax1.plot(relativetime, iso, 'r-', linewidth=2, label="Isolated")
    ax1.set_xlabel("Time [s]")
    ax1.set_ylabel("Amp")
    ax1.legend()
    # plot weight window
    ax2 = ax1.twinx()
    ax2.plot(relativetime, weight, linewidth=2, label="weight")
    f.tight_layout()
    plt.show()


def kaiser_windows_filter(tr, isotr, periods, window=('kaiser', 9.0),
                          deltaT=PERIOD_RESAMPLE, debug=False):
    """
    Apply FIR filter with kaiser window

    :type tr: `obspy.trace`
    :param tr: trace contains raw waveform
    :type isotr: numpy matrix
    :param isotr: matrix contains isolated waveform
    :type periods: numpy array
    :param periods: contains various periods
    :type window: tuple
    :param window: window for setting kaiser filter
    :type deltaT: int or float
    :param deltaT: time interval
    """
    conv_result = np.zeros(shape=(len(periods), len(tr.data)))

    nyq = tr.stats.sampling_rate / 2.0
    ntaps = 2 ** math.ceil(math.log(tr.stats.sac.npts, 2))
    if debug:
        print(nyq, ntaps)
    for iperiod, T0 in enumerate(periods):
        lowcut = 1.0 / (T0 + 0.2 * deltaT / 2.0)
        highcut = 1.0 / (T0 - 0.2 * deltaT / 2.0)
        width = (highcut - lowcut) / 2.0
        window = firwin(ntaps, [lowcut, highcut], width=width, window=window,
                        nyq=nyq, pass_zero=False)
        conv_result[iperiod, :] = sig_convolve(isotr[iperiod, :], window,
                                               mode='same')
    logger.info("Kaiser window filter applied Suc.!")
    return conv_result


def measure_group_arrival(tr, periods, alpha=FTAN_ALPHA, debug=False):
    """
    estimate dispersion curve of group velocity and isolate the stations
    """
    # estimate the group velocity
    dt = 1.0 / tr.stats.sampling_rate
    amp, phi = FTAN(tr.data, dt, periods, alpha)
    starttime = tr.stats.starttime
    reftime = UTCDateTime(year=tr.stats.sac.nzyear, julday=tr.stats.sac.nzjday,
                          hour=tr.stats.sac.nzhour, minute=tr.stats.sac.nzmin,
                          second=tr.stats.sac.nzsec,
                          microsecond=(tr.stats.sac.nzmsec*1000))
    # estimate the group velocity dispersion curve with cross-correlation
    group_arrival = [starttime+amp[iperiod,:].argmax()*dt
                     for iperiod in range(len(periods))]
    # debug
    if debug:
        period = 80
        plot_analytical_amp(amp, periods, period, starttime, dt)
    logger.debug("reftime -> {}".format(reftime))
    return reftime, group_arrival

def plot_analytical_amp(amp, periods, period, starttime, dt):
    """
    Plot amplitude of analytical signal

    :type amp: numpy matrix
    :param amp: amlitude matrix contains analytical signal at various periods
    :type periods: numpy array
    :param periods: periods contained in amp
    :type period: float or int
    :prama period: period for visualization
    :type starttime: class `obspy.UTCDateTime`
    :param starttime: start time
    :type dt: float
    :param dt: time interval
    """
    for iperiod, T0 in enumerate(periods):
        if period == T0:
            signal = amp[iperiod, :]
    timescale = np.array([starttime+x*dt for x in range(len(signal))])
    f, ax = plt.subplots()
    ax.plot(timescale, signal)
    ax.plot(starttime+signal.argmax()*dt, 0, 'P')
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Amp")
    ax.set_title("Envelope at {} s".format(period))
    logger.debug("Arrival -> {}".format(signal.argmax()))
    plt.show()

def common_line_judgement(event, station_pair, minangle=2.0):
    """
    Select matched station pair and event

    :type event dict
    :parm event contain info of event ['origin', 'latitude', 'longitude', 'depth'
                                       'magnitude']
    :type station_pair tuple
    :parm event contain two class Station `.psstation.Station`
    :type minangle float
    :parm minangle the minimum angle between two station-event lines
    """
    sta1, sta2 = station_pair
    # calculate azimuth and back-azimuth
    intersta = DistAz(sta1.coord[1], sta1.coord[0],
                      sta2.coord[1], sta2.coord[0])
    sta2event = DistAz(sta1.coord[1], sta1.coord[0],
                       event["latitude"], event["longitude"])
    # sta1-sta2-event
    if np.abs(intersta.baz - sta2event.baz) < minangle:
        logger.debug("Commonline -> %s.%s-%s.%s-%s", sta1.network, sta1.name,
                     sta2.network, sta2.name, event['origin'])
        return (sta1, sta2, event)
    elif np.abs(np.abs(intersta.baz - sta2event.baz) - 180) < minangle:
        logger.debug("Commonline -> %s.%s-%s.%s-%s", sta2.network, sta2.name,
                     sta1.network, sta2.name, event['origin'])
        return (sta2, sta1, event)
    else:
        return None
