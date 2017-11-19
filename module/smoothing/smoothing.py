#!/usr/bin/env python

# Postprocessing performs basic algorithms on redis data
#
# Postprocessing is part of the EEGsynth project (https://github.com/eegsynth/eegsynth)
#
# Copyright (C) 2017 EEGsynth project
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from numpy import log, log2, log10, exp, power, sqrt, mean, median, var, std
import ConfigParser # this is version 2.x specific, on version 3.x it is called "configparser" and has a different API
import argparse
import numpy as np
import os
import redis
import sys
import time

if hasattr(sys, 'frozen'):
    basis = sys.executable
elif sys.argv[0]!='':
    basis = sys.argv[0]
else:
    basis = './'
installed_folder = os.path.split(basis)[0]

# eegsynth/lib contains shared modules
sys.path.insert(0, os.path.join(installed_folder,'../../lib'))
import EEGsynth

# these function names can be used in the equation that gets parsed
from EEGsynth import compress, limit, rescale

parser = argparse.ArgumentParser()
parser.add_argument("-i", "--inifile", default=os.path.join(installed_folder, os.path.splitext(os.path.basename(__file__))[0] + '.ini'), help="optional name of the configuration file")
args = parser.parse_args()

config = ConfigParser.ConfigParser()
config.read(args.inifile)

# this determines how much debugging information gets printed
debug = config.getint('general','debug')

try:
    r = redis.StrictRedis(host=config.get('redis','hostname'), port=config.getint('redis','port'), db=0)
    response = r.client_list()
    if debug>0:
        print "Connected to redis server"
except redis.ConnectionError:
    print "Error: cannot connect to redis server"
    exit()

# see https://en.wikipedia.org/wiki/Median_absolute_deviation
def mad(arr, axis=None):
    if axis==1:
        val = np.apply_along_axis(mad, 1, arr)
    else:
        val = np.nanmedian(np.abs(arr - np.nanmedian(arr)))
    return val

prefix      = config.get('output','prefix')
inputlist   = config.get('input','channels').split(",")
stepsize    = config.getfloat('smoothing','stepsize')   # in seconds
window      = config.getfloat('smoothing','window')     # in seconds
numchannel  = len(inputlist)
numhistory  = int(round(window/stepsize))

# this will contain the full list of historic values
history = np.empty((numchannel, numhistory))
history[:] = np.NAN

# this will contain the statistics of the historic values
historic = {}

while True:
    # determine the start of the actual processing
    start = time.time()

    # shift data to next sample
    history[:,:-1] = history[:,1:]

    # update with current data
    for channel in range(numchannel):
        history[channel,numhistory-1] = r.get(inputlist[channel])

    # compute some statistics
    historic['mean']    = np.nanmean(history, axis=1)
    historic['std']     = np.nanstd(history, axis=1)
    historic['minimum'] = np.nanmean(history, axis=1)
    historic['maximum'] = np.nanmean(history, axis=1)
    historic['range']   = historic['maximum'] - historic['minimum']
    # use some robust estimators
    historic['median']  = np.nanmedian(history, axis=1)
    historic['mad']     = mad(history, axis=1)
    # for a normal distribution the 16th and 84th percentile correspond to the mean plus-minus one standard deviation
    historic['p16']     = np.percentile(history, 16, axis=1)
    historic['p84']     = np.percentile(history, 84, axis=1)

    if debug>1:
        print historic

    for operation in historic.keys():
        for channel in range(numchannel):
            key = prefix + "." + operation + "." + inputlist[channel]
            val = historic[operation][channel]
            r.set(key, val)

    elapsed = time.time()-start
    naptime = stepsize - elapsed
    if naptime>0:
        # this approximates the desired update speed
        time.sleep(naptime)