#!/usr/bin/env python

# Outputaudio reads data from a FieldTrip buffer and writes it to an audio device
#
# This software is part of the EEGsynth project, see <https://github.com/eegsynth/eegsynth>.
#
# Copyright (C) 2018-2019 EEGsynth project
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

import configparser
import argparse
import numpy as np
import scipy.io
from scipy.io import wavfile
from copy import copy
import os
import redis
import sys
import time
import pyaudio
import threading

if hasattr(sys, 'frozen'):
    path = os.path.split(sys.executable)[0]
    file = os.path.split(sys.executable)[-1]
elif sys.argv[0] != '':
    path = os.path.split(sys.argv[0])[0]
    file = os.path.split(sys.argv[0])[-1]
else:
    path = os.path.abspath('')
    file = os.path.split(path)[-1] + '.py'

# eegsynth/lib contains shared modules
sys.path.insert(0, os.path.join(path, '../../lib'))
import EEGsynth
import FieldTrip

parser = argparse.ArgumentParser()
parser.add_argument("-i", "--inifile", default=os.path.join(path, os.path.splitext(file)[0] + '.ini'), help="optional name of the configuration file")
args = parser.parse_args()

config = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
config.read(args.inifile)

try:
    r = redis.StrictRedis(host=config.get('redis', 'hostname'), port=config.getint('redis', 'port'), db=0)
    response = r.client_list()
except redis.ConnectionError:
    raise RuntimeError("cannot connect to Redis server")

# combine the patching from the configuration file and Redis
patch = EEGsynth.patch(config, r)

# this can be used to show parameters that have changed
monitor = EEGsynth.monitor()

# get the options from the configuration file
debug           = patch.getint('general', 'debug')
device          = patch.getint('audio', 'device')
scaling_method  = patch.getstring('audio', 'scaling_method')
scaling         = patch.getfloat('audio', 'scaling')
scale_scaling   = patch.getfloat('scale', 'scaling', default=1)
offset_scaling  = patch.getfloat('offset', 'scaling', default=0)

p = pyaudio.PyAudio()

print('------------------------------------------------------------------')
info = p.get_host_api_info_by_index(0)
print(info)
print('------------------------------------------------------------------')
for i in range(info.get('deviceCount')):
    if p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels') > 0:
        print("Input  Device id ", i, " - ", p.get_device_info_by_host_api_device_index(0, i).get('name'))
    if p.get_device_info_by_host_api_device_index(0, i).get('maxOutputChannels') > 0:
        print("Output Device id ", i, " - ", p.get_device_info_by_host_api_device_index(0, i).get('name'))
print('------------------------------------------------------------------')
devinfo = p.get_device_info_by_index(device)
print("Selected device is", devinfo['name'])
print(devinfo)
print('------------------------------------------------------------------')

# this is to prevent concurrency problems
lock = threading.Lock()

input_channel, input_sample = list(zip(*config.items('input')))

# open first file to determine the format
rate, dat = wavfile.read(input_sample[0])

if len(dat.shape)<2:
    channels = 1
else:
    channels = dat.shape[1]

stack = np.zeros((0,channels), dtype=np.float32)

def callback(in_data, frame_count, time_info, status):
    global stack, channels

    with lock:
        if stack.shape[0] > frame_count:
            # select the samples for audio output and drop them from the stack
            dat   = stack[0:frame_count,:]
            stack = stack[frame_count:,:]
        else:
            dat = np.zeros((frame_count,channels), dtype=np.float32)

    try:
        # this is for Python 2
        buf = np.getbuffer(dat)
    except:
        # this is for Python 3
        buf = dat.tobytes()

    return buf, pyaudio.paContinue

class TriggerThread(threading.Thread):
    def __init__(self, redischannel, sample):
        threading.Thread.__init__(self)
        self.redischannel = redischannel
        self.sample = sample
        self.running = True
    def stop(self):
        self.running = False
    def run(self):
        global stack
        pubsub = r.pubsub()
        pubsub.subscribe('SAMPLER_UNBLOCK') # this message unblocks the Redis listen command
        pubsub.subscribe(self.redischannel) # this message triggers the event
        while self.running:
            for item in pubsub.listen():
                if not self.running or not item['type'] == 'message':
                    break
                if item['channel'].decode('utf-8') == self.redischannel:
                    # read the audio file
                    rate, dat = wavfile.read(self.sample)
                    print("playing %s for up to %d ms" % (self.sample, 1000*dat.shape[0]/rate))

                    # scale 8, 16 and 32 bit PCM to float, with values between -1.0 and +1.0
                    if dat.dtype == np.uint8:
                        dat = (dat.astype(np.float32) - 127.) / 255.
                    elif dat.dtype == np.int16:
                        dat = dat.astype(np.float32) / 32767.
                    elif dat.dtype == np.int32:
                        dat = dat.astype(np.float32) / 2147483647.

                    # apply the user-specified scaling
                    if scaling_method == 'multiply':
                        dat *= scaling
                    elif scaling_method == 'divide':
                        dat /= scaling
                    elif scaling_method == 'db':
                        dat *= np.power(10., scaling/20.)

                    if np.min(dat)<-1 or np.max(dat)>1:
                        print('WARNING: signal exceeds [-1,+1] range, the audio will clip')

                    with lock:
                        # replace the current playback stack
                        stack = np.atleast_2d(dat).transpose()


# create the background threads that deal with the triggers
trigger = []
for channel, sample in zip(input_channel, input_sample):
    print(channel, sample)
    trigger.append(TriggerThread(channel, sample))

for thread in trigger:
    thread.start()

# open audio stream
stream = p.open(format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                output=True,
                output_device_index=device,
                stream_callback=callback)

# start the output stream
stream.start_stream()

try:
    while True:
        monitor.loop()
        time.sleep(patch.getfloat('general','delay'))

        # update the scaling factor
        scaling = patch.getfloat('audio', 'scaling', default=1)
        scaling = EEGsynth.rescale(scaling, slope=scale_scaling, offset=offset_scaling)
        monitor.update("scaling", scaling)

except (SystemExit, KeyboardInterrupt):
    stream.stop_stream()
    stream.close()
    p.terminate()
    for thread in trigger:
        thread.stop()
    r.publish('SAMPLER_UNBLOCK', 1)
    for thread in trigger:
        thread.join()
